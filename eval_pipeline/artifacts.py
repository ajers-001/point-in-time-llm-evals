"""Private object storage, bounded archive verification and canonical copies.

Hosts upload each finished attempt as a tar.gz with ATTEMPT_MANIFEST.json
into a pre-signed, append-only slot. The controller re-reads the object,
checks every member hash and the result identity against the durable job,
writes an immutable per-attempt copy (never overwriting) and verifies a GET
readback hash before the job may be committed.
"""
import hashlib
import io
import json
import tarfile
import threading
from pathlib import PurePosixPath

MAX_ARCHIVE = 128 << 20
MAX_MEMBERS = 4000
MAX_MEMBER = 64 << 20
MAX_TOTAL = 512 << 20


class ObjectExists(Exception):
    pass


class MemoryObjects:
    def __init__(self):
        self.data, self._lock = {}, threading.Lock()

    def put_new(self, key, body):
        with self._lock:
            if key in self.data:
                raise ObjectExists(key)
            self.data[key] = bytes(body)

    def get(self, key, limit=MAX_ARCHIVE):
        with self._lock:
            body = self.data.get(key)
        if body is None:
            return None
        if len(body) > limit:
            raise ValueError(f'{key} exceeds {limit} bytes')
        return body

    def list(self, prefix):
        with self._lock:
            return sorted(k for k in self.data if k.startswith(prefix))


class S3Objects:
    """Private SSE-S3 bucket; writes use If-None-Match so nothing is overwritten."""

    def __init__(self, bucket, client):
        self.bucket, self.client = bucket, client

    def put_new(self, key, body):
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body, IfNoneMatch='*',
                                   ServerSideEncryption='AES256')
        except self.client.exceptions.ClientError as error:
            if error.response.get('Error', {}).get('Code') in ('PreconditionFailed', 'ConditionalRequestConflict'):
                raise ObjectExists(key) from error
            raise

    def get(self, key, limit=MAX_ARCHIVE):
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except self.client.exceptions.ClientError as error:
            if error.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
                return None
            raise
        if head['ContentLength'] > limit:
            raise ValueError(f'{key} exceeds {limit} bytes')
        body = self.client.get_object(Bucket=self.bucket, Key=key)['Body'].read(limit + 1)
        if len(body) > limit:
            raise ValueError(f'{key} exceeds {limit} bytes')
        return body

    def list(self, prefix):
        keys, token = [], None
        while True:
            kw = {'Bucket': self.bucket, 'Prefix': prefix}
            if token:
                kw['ContinuationToken'] = token
            page = self.client.list_objects_v2(**kw)
            keys += [o['Key'] for o in page.get('Contents', [])]
            token = page.get('NextContinuationToken')
            if not token:
                return sorted(keys)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_members(body):
    """Return {name: bytes}; reject links, traversal, duplicates and oversize."""
    out, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(body), mode='r:gz') as archive:
        for i, member in enumerate(archive):
            if i >= MAX_MEMBERS:
                raise ValueError('Too many archive members')
            path = PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or '..' in path.parts or member.name in out:
                raise ValueError(f'Unsafe archive member: {member.name}')
            if member.size > MAX_MEMBER or total + member.size > MAX_TOTAL:
                raise ValueError(f'Archive member too large: {member.name}')
            total += member.size
            out[member.name] = archive.extractfile(member).read()
    return out


def build_attempt_archive(files, manifest):
    """Deterministic tar.gz used by hosts and tests: {relpath: bytes} plus manifest."""
    manifest = dict(manifest, files={k: sha256(v) for k, v in sorted(files.items())})
    entries = dict(files, **{'ATTEMPT_MANIFEST.json': json.dumps(manifest, indent=2, sort_keys=True).encode()})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz', format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(entries[name]), 0, 0o644
            archive.addfile(info, io.BytesIO(entries[name]))
    return buf.getvalue()


def verify_attempt(body, session_id, job, attempt_id):
    """Check one attempt archive against the durable job; return a summary."""
    members = safe_members(body)
    manifest = json.loads(members.pop('ATTEMPT_MANIFEST.json'))
    if set(manifest['files']) != set(members) or any(sha256(v) != manifest['files'][k] for k, v in members.items()):
        raise ValueError('Attempt manifest hash mismatch')
    for key, want in (('session_id', session_id), ('run_id', job['run_id']), ('attempt_id', attempt_id)):
        if manifest.get(key) != want:
            raise ValueError(f'Attempt identity mismatch: {key}')
    rid = job['run_id']
    result_name = f'output/{rid}/gpu/result.json'
    if result_name not in members:
        # Killed or crashed before the runner wrote a result: preserved as evidence, never a success.
        return {'sha256': sha256(body), 'size': len(body), 'result_status': None, 'finish_reason': None,
                'error': 'no result.json in attempt archive', 'output_sha256': None,
                'deadline_interrupted': bool(manifest.get('deadline_interrupted')), 'host': manifest.get('host', {})}
    result = json.loads(members[result_name])
    if result.get('run_id') != rid or result.get('config_sha256') != job['config_sha256']:
        raise ValueError('Result does not match the frozen request')
    if job.get('input_sha256') and result.get('input_sha256') != job['input_sha256']:
        raise ValueError('Result input hash does not match the frozen request')
    if result.get('status') == 'completed':
        raw = members.get(f'output/{rid}/gpu/output.txt')
        if raw is None or sha256(raw) != result.get('output_sha256'):
            raise ValueError('Completed result output hash mismatch')
        if result.get('engine_reused') not in (False, None):
            raise ValueError('Fresh-engine requirement violated')
    return {'sha256': sha256(body), 'size': len(body), 'result_status': result.get('status'),
            'finish_reason': result.get('finish_reason'), 'error': result.get('error'),
            'output_sha256': result.get('output_sha256'), 'deadline_interrupted': bool(manifest.get('deadline_interrupted')),
            'host': manifest.get('host', {})}


def canonical_key(session_id, run_id, attempt_id):
    return f'sessions/{session_id}/runs/{run_id}/attempts/{attempt_id}/result.tar.gz'


def secure_copy(objects, session_id, job, attempt_id, source_key, expected_sha=None):
    """Verify a host upload, store its immutable per-attempt copy and read it back."""
    body = objects.get(source_key)
    if body is None:
        return None
    if expected_sha and sha256(body) != expected_sha:
        raise ValueError('Uploaded object does not match host receipt')
    summary = verify_attempt(body, session_id, job, attempt_id)
    key = canonical_key(session_id, job['run_id'], attempt_id)
    try:
        objects.put_new(key, body)
    except ObjectExists:
        pass
    readback = objects.get(key)
    if readback is None or sha256(readback) != summary['sha256']:
        raise ValueError('Canonical copy readback mismatch (conflicting duplicate retained for review)')
    return dict(summary, key=key, source_key=source_key)
