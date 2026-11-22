#!/usr/bin/env python3
"""Root host supervisor for one GPU pod (stdlib only; never imports cloud SDKs).

The controller drives it over SSH with strict host keys. Each call prints
one JSON object. Mutating calls carry the controller's fencing epoch; a
lower epoch than any seen before is refused, so a superseded controller
cannot start work. Inference itself runs through the audited
``infra.runpod_run.execute`` sandbox as UID 10001 with no network, no
credentials and no evaluator files; this supervisor and its upload URLs
stay outside that sandbox.

Commands: fence, bootstrap, start, inspect, status, install-urls.
Internal (detached): _bootstrap, _run, _backup, _expiry.
"""
import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get('PIT_CONTROL_ROOT', '/pit-control'))
SOURCE = ROOT / 'source'
PYTHON = '/opt/venv/bin/python'
RESEARCH = 'prepared/robustness-development-prewarm-v3'
READINESS = 'prepared/smoke-prewarm-v2'
BENCHES = ('prepared/benchmark-repro-v1', 'prepared/benchmark-repro-v1-dd', 'prepared/benchmark-repro-v1-dd2',
           'prepared/benchmark-grid-v1', 'prepared/benchmark-mixed-v1', 'prepared/benchmark-replay-v1',
           'prepared/benchmark-pilot-v1', 'prepared/benchmark-indicators-v1', 'prepared/benchmark-ncd-v1',
              'prepared/benchmark-ncd-v1b', 'prepared/openbook-b-v1',
              'prepared/openbook-c-v1', 'prepared/openbook-c-v2',
              'prepared/openbook-d-v1', 'prepared/utility-v1', 'prepared/utility-v1b',
              'prepared/utility-v1c', 'prepared/hybrid-v1', 'prepared/fakenews-v1',
              'prepared/jspace-v1-stage1', 'prepared/jspace-v1-stage2a', 'prepared/jspace-v1-stage2a-b1',
              'prepared/jspace-v1-stage2b', 'prepared/jspace-v1-noopt',
              'prepared/lookahead-v1-step1', 'prepared/lookahead-v1-r1a', 'prepared/lookahead-v1-r1b')
DATASETS = ROOT / 'datasets'
DOWNLOAD_CONFIG = 'prepared/smoke-v1/experiment.json'
UV_IMAGE = 'astral-sh/uv'
UV_MANIFEST = '27508d63700ae72281272e11ce1cec19ffacef96dd59dee05e29593e1f874f01'
UV_SHA256 = 'fabff9df3e379fa0e290face14fc8d5cd638d474c1e610711176462ba02ec43b'
MAX_FILE = 16 << 20
MAX_SNAPSHOT = 256 << 20
SNAPSHOT_EVERY_S = 120
SNAPSHOT_ATTEMPT_FILE = 2 << 20  # live snapshots carry logs, not bulk outputs (those ship in the attempt archive)
FAKE = os.environ.get('PIT_HOST_FAKE_EXECUTE') == '1' and str(ROOT) != '/pit-control'
# Seconds kept between the execution timeout and inference end, and the minimum useful run.
END_SLACK_S = int(os.environ.get('PIT_HOST_FAKE_END_SLACK', 30)) if FAKE else 30
MIN_EXEC_S = int(os.environ.get('PIT_HOST_FAKE_MIN_EXEC', 60)) if FAKE else 60
KILL_GRACE_S = 2 if FAKE else 10
# Bulk transfer: large allow-listed J-space outputs go to S3 in their own slots; frozen bulk inputs come from S3.
BULK_SUFFIXES = ('.bulk.safetensors', '.bulk.tar')
BULK_PREFIX = ('prepared/jspace-', 'prepared/lookahead-')  # experiments whose large outputs (lens readouts) are uploaded raw
MAX_BULK = 4608 << 20  # 4.5 GiB, under the 5 GiB single-PUT limit
BULK_RESERVE_S = 120  # a bulk upload starts only if it can end (at BULK_MIN_RATE) this long before gpu_end,
BULK_MIN_RATE = 25 << 20  # so the attempt archive still ships before the pod is deleted
BULK_FETCH_S = int(os.environ.get('PIT_HOST_FAKE_BULK_FETCH_S', 7200)) if FAKE else 7200
BULK_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')


def now():
    return int(time.time())


def iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.partial')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    tmp.chmod(mode)
    tmp.replace(path)


def identity(pid):
    """/proc starttime, or None if the pid is gone or a zombie."""
    try:
        tail = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return None if tail[0] in ('Z', 'X') else tail[19]


def alive(record):
    return bool(record) and identity(record['pid']) == str(record['start'])


def spawn(name, args, log):
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('ab') as stream:
        p = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
    for _ in range(50):
        start = identity(p.pid)
        if start:
            break
        time.sleep(0.02)
    record = {'pid': p.pid, 'start': start, 'name': name, 'at': iso()}
    write(ROOT / 'processes' / f'{name}.json', record)
    return record


class Locked:
    def __init__(self, name='agent.lock', wait=True):
        self.name, self.wait = name, wait

    def __enter__(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.handle = (ROOT / self.name).open('a')
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
        except BlockingIOError:
            self.handle.close()
            raise
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


class Refused(Exception):
    """A command was refused; reported as JSON on stdout with exit status 2."""


def fence(epoch):
    """Record and enforce the highest controller epoch seen (caller holds lock)."""
    state = read(ROOT / 'agent-state.json')
    if state is None:
        raise Refused({'error': 'host not initialised'})
    if epoch < state.get('max_epoch', 0):
        raise Refused({'error': 'stale controller epoch', 'max_epoch': state['max_epoch']})
    if epoch > state.get('max_epoch', 0):
        state['max_epoch'] = epoch
        write(ROOT / 'agent-state.json', state)
    return state


# ----- uploads -------------------------------------------------------------
def next_slot():
    urls = read(ROOT / 'upload-urls.json', {'slots': []})['slots']
    used = set()
    journal = ROOT / 'upload-journal.jsonl'
    if journal.exists():
        used = {json.loads(line)['key'] for line in journal.read_text().splitlines() if line.strip()}
    return next((s for s in urls if s['key'] not in used), None)


def journal(row):
    with (ROOT / 'upload-journal.jsonl').open('a') as stream:
        stream.write(json.dumps(row, sort_keys=True) + '\n')


def upload(body, kind, path=None, **meta):
    """PUT bytes, or the file at ``path`` streamed, into the next unused exact-object slot (append-only, SSE-S3)."""
    size, digest = (Path(path).stat().st_size, file_sha(path)) if path else (len(body), hashlib.sha256(body).hexdigest())
    with Locked('upload.lock'):
        for _ in range(3):
            slot = next_slot()
            if slot is None:
                return {'status': 'no_slot'}
            row = {'key': slot['key'], 'kind': kind, 'at': iso(), 'sha256': digest, 'bytes': size, **meta}
            if FAKE and slot['url'].startswith('file://'):
                target = Path(slot['url'][7:])
                if target.exists():
                    journal(dict(row, status='slot_taken'))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if path:
                    shutil.copyfile(path, target)
                else:
                    target.write_bytes(body)
                journal(dict(row, status='uploaded'))
                return dict(row, status='uploaded')
            try:
                with open(path, 'rb') if path else io.BytesIO(body) as stream:
                    req = urllib.request.Request(slot['url'], data=stream if path else body, method='PUT', headers={
                        'x-amz-server-side-encryption': 'AES256', 'If-None-Match': '*', 'Content-Length': str(size)})
                    with urllib.request.urlopen(req, timeout=900) as response:
                        if response.status != 200:
                            raise RuntimeError(f'upload status {response.status}')
            except urllib.error.HTTPError as error:
                journal(dict(row, status='slot_taken' if error.code == 412 else 'failed', error=f'HTTP {error.code}'))
                if error.code == 412:
                    continue
                return dict(row, status='failed')
            except Exception as error:  # noqa: BLE001 - recorded; URLs never logged
                journal(dict(row, status='failed', error=type(error).__name__))
                return dict(row, status='failed')
            journal(dict(row, status='uploaded'))
            return dict(row, status='uploaded')
        return {'status': 'no_slot'}


def tar_gz(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(entries[name]), 0, 0o600
            archive.addfile(info, io.BytesIO(entries[name]))
    return gzip.compress(buf.getvalue(), mtime=0)


def collect(base, prefix, skip=('scratch', 'models', '__pycache__', 'code', 'case'), max_file=MAX_FILE):
    files, oversized = {}, {}
    base = Path(base)
    if not base.exists():
        return files, oversized
    for p in sorted(base.rglob('*')):
        rel = p.relative_to(base)
        if p.is_symlink() or not p.is_file() or any(part in skip for part in rel.parts) or p.name.endswith('.partial'):
            continue
        size = p.stat().st_size
        if size > max_file:
            oversized[f'{prefix}{rel}'] = {'bytes': size, 'sha256': file_sha(p)}
            continue
        files[f'{prefix}{rel}'] = p.read_bytes()
    return files, oversized


def file_sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


# ----- attempts --------------------------------------------------------------
def attempt_dir(attempt_id):
    if not all(c.isalnum() or c in '-.' for c in attempt_id) or '..' in attempt_id:
        raise ValueError('invalid attempt id')
    return ROOT / 'attempts' / attempt_id


def upload_bulk(adir, skip=('scratch', 'models', '__pycache__', 'code', 'case')):
    """J-space only: stream each large allow-listed output into its own slot; record it in bulk.json (idempotent)."""
    att = read(adir / 'attempt.json')
    bulk = read(adir / 'bulk.json', {}) or {}
    base = adir / 'stage' / 'output'
    if not att['prepared'].startswith(BULK_PREFIX) or not base.is_dir():
        return bulk
    gpu_end = read(ROOT / 'host-deadline.json')['gpu_end']
    for p in sorted(base.rglob('*')):
        rel, name = p.relative_to(base), f'output/{p.relative_to(base)}'
        if p.is_symlink() or not p.is_file() or not p.name.endswith(BULK_SUFFIXES) or any(x in skip for x in rel.parts) \
                or (bulk.get(name) or {}).get('status') == 'uploaded' or p.stat().st_size <= MAX_FILE:
            continue
        size = p.stat().st_size
        with open(p, 'rb') as f:
            gzip_magic = f.read(2) == b'\x1f\x8b'  # a gzip body could parse as an attempt archive in the slot scan
        if size > MAX_BULK or gzip_magic:
            bulk[name] = {'status': 'too_large' if size > MAX_BULK else 'refused_gzip', 'bytes': size, 'key': None,
                          'sha256': None}
        elif now() + BULK_RESERVE_S + size // BULK_MIN_RATE > gpu_end:
            bulk[name] = {'status': 'skipped_deadline', 'bytes': size, 'key': None, 'sha256': None}
        else:
            try:
                row = upload(None, 'bulk', path=p, run_id=att['run_id'], attempt_id=att['attempt_id'], name=name)
            except OSError as error:
                row = {'status': 'failed', 'error': type(error).__name__}
            bulk[name] = {'status': row['status'], 'key': row.get('key'), 'sha256': row.get('sha256'),
                          'bytes': row.get('bytes', size)}
        write(adir / 'bulk.json', bulk)
    return bulk


def package_attempt(adir, deadline_interrupted):
    att = read(adir / 'attempt.json')
    state = read(ROOT / 'agent-state.json')
    files, oversized = collect(adir / 'stage' / 'output', 'output/')
    for name in ('attempt.json', 'exit.json', 'process.json', 'session.log', 'bulk.json'):
        if (adir / name).is_file() and (adir / name).stat().st_size <= MAX_FILE:
            files[name] = (adir / name).read_bytes()
    if (adir / 'stage' / 'release.json').is_file():
        files['release.json'] = (adir / 'stage' / 'release.json').read_bytes()
    progress = read(ROOT / 'bootstrap' / 'progress.json', {}) or {}
    manifest = {'session_id': state['session_id'], 'run_id': att['run_id'], 'attempt_id': att['attempt_id'],
                'deadline_interrupted': bool(deadline_interrupted), 'oversized_not_included': oversized,
                'bulk': read(adir / 'bulk.json', {}) or {},
                'host': {'pod_id': state['pod_id'], 'bundle_sha256': state.get('bundle_sha256'),
                         'models_manifest_sha256': progress.get('models', {}).get(att['prepared']),
                         'bulk_inputs': progress.get('bulk_inputs'),
                         'image': state.get('image'), 'gpu': (read(ROOT / 'bootstrap' / 'admission.json', {}) or {}).get('gpu')},
                'files': {k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())}}
    files['ATTEMPT_MANIFEST.json'] = json.dumps(manifest, indent=2, sort_keys=True).encode()
    body = tar_gz(files)
    (adir / 'archive.tar.gz').write_bytes(body)
    return body


def ship_attempt(adir):
    if read(adir / 'upload.json'):
        return read(adir / 'upload.json')
    exit_info = read(adir / 'exit.json')
    if exit_info is None:
        return None
    body = (adir / 'archive.tar.gz').read_bytes() if (adir / 'archive.tar.gz').exists() else \
        package_attempt(adir, exit_info.get('deadline_interrupted'))
    att = read(adir / 'attempt.json')
    row = upload(body, 'attempt', run_id=att['run_id'], attempt_id=att['attempt_id'])
    if row.get('status') == 'uploaded':
        write(adir / 'upload.json', {'key': row['key'], 'sha256': row['sha256'], 'at': row['at']})
    return read(adir / 'upload.json')


def finish_attempt(adir, deadline_interrupted=None, wait=True, repackage=False):
    """Bulk outputs first, then package and ship, under the attempt's own lock (no concurrent bulk uploads)."""
    try:
        with Locked(f'attempts/{adir.name}/package.lock', wait=wait):
            if read(adir / 'upload.json'):
                return read(adir / 'upload.json')
            if repackage or not (adir / 'archive.tar.gz').exists():
                if deadline_interrupted is None:
                    deadline_interrupted = (read(adir / 'exit.json') or {}).get('deadline_interrupted')
                upload_bulk(adir)
                package_attempt(adir, deadline_interrupted)
            return ship_attempt(adir)
    except BlockingIOError:
        return None  # another process is finishing this attempt


def run_attempt(attempt_id):
    """Detached child: stage one request, execute it once, package and upload."""
    adir = attempt_dir(attempt_id)
    att = read(adir / 'attempt.json')
    inference_end = read(ROOT / 'host-deadline.json')['inference_end']
    timeout = int(min(att['timeout'], inference_end - now() - END_SLACK_S))
    code, bounded = None, timeout < att['timeout']
    try:
        if timeout < MIN_EXEC_S:
            raise TimeoutError('no time left before inference end')
        if FAKE:
            code = fake_execute(adir, att, timeout)
        else:
            sys.path.insert(0, str(SOURCE))
            models = ROOT / 'models' / att['config_sha256'][:12]
            if att['prepared'] in BENCHES:
                from infra import bench_run
                bench_run.stage(SOURCE, att['run_id'], adir / 'stage', prepared=att['prepared'])

                def execute_staged():
                    start = 'eager' if bench_run.graph_build_failed_before(ROOT / 'attempts') else 'graphs'
                    return bench_run.execute(adir / 'stage' / 'code', models, adir / 'stage' / 'case',
                                             adir / 'stage' / 'output', DATASETS / 'mmlu-pro', att['run_id'], PYTHON,
                                             ROOT / 'tmp' / att['tag'], timeout=timeout, start_mode=start)
            else:
                from scripts.stage_inference import stage_run
                from infra.runpod_run import execute
                stage_run(SOURCE / att['prepared'], att['run_id'], adir / 'stage', source=SOURCE)

                def execute_staged():
                    return execute(adir / 'stage' / 'code', models, adir / 'stage' / 'case', adir / 'stage' / 'output',
                                   att['run_id'], PYTHON, ROOT / 'tmp' / att['tag'], timeout=timeout)
            with (adir / 'session.log').open('ab') as log:
                saved = [os.dup(fd) for fd in (1, 2)]
                try:
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os.dup2(log.fileno(), 1)
                    os.dup2(log.fileno(), 2)
                    code = execute_staged()
                finally:
                    sys.stdout.flush()
                    sys.stderr.flush()
                    for fd, old in zip((1, 2), saved):
                        os.dup2(old, fd)
                        os.close(old)
    except BaseException as error:  # noqa: BLE001 - recorded in exit.json as evidence
        write(adir / 'error.json', {'type': type(error).__name__, 'error': str(error)[:2000]})
    result = read(adir / 'stage' / 'output' / att['run_id'] / 'gpu' / 'result.json', {}) or {}
    interrupted = (result.get('status') != 'completed' and bounded and now() >= inference_end - END_SLACK_S - 15)
    write(adir / 'exit.json', {'exit_code': code, 'finished_at': iso(), 'result_status': result.get('status'),
                               'deadline_interrupted': interrupted})
    finish_attempt(adir, interrupted, repackage=True)


def fake_execute(adir, att, timeout):
    """Test double for hosts without GPUs; disabled for the real /pit-control root."""
    rid = att['run_id']
    out = adir / 'stage' / 'output' / rid / 'gpu'
    out.mkdir(parents=True)
    behaviour = os.environ.get('PIT_HOST_FAKE_BEHAVIOUR', 'complete')
    if behaviour == 'hang':
        time.sleep(10 ** 6)  # only the host expiry can end this attempt
    text = f'fake answer for {rid}'.encode()
    (out / 'output.txt').write_bytes(text)
    result = {'run_id': rid, 'config_sha256': att['config_sha256'], 'input_sha256': att['input_sha256'],
              'status': 'completed' if behaviour == 'complete' else 'runtime_failed', 'engine_reused': False,
              'finish_reason': 'stop', 'output_sha256': hashlib.sha256(text).hexdigest()}
    (out / 'result.json').write_text(json.dumps(result))
    return 0 if behaviour == 'complete' else 1


# ----- daemons ----------------------------------------------------------------
def snapshot():
    files, oversized = {}, {}
    for name in ('agent-state.json', 'host-deadline.json', 'upload-journal.jsonl'):
        if (ROOT / name).is_file():
            files[name] = (ROOT / name).read_bytes()
    for directory in ('bootstrap', 'processes', 'attempts'):
        f, o = collect(ROOT / directory, directory + '/',
                       max_file=SNAPSHOT_ATTEMPT_FILE if directory == 'attempts' else MAX_FILE)
        files.update({k: v for k, v in f.items() if not k.endswith('archive.tar.gz')})
        oversized.update(o)
    for name in ('downloads/artifacts.json', 'downloads/inventory.json', 'downloads/.download-state.json'):
        if (ROOT / name).is_file():
            files[name] = (ROOT / name).read_bytes()
    total, kept = 0, {}
    for k in sorted(files):
        total += len(files[k])
        if total > MAX_SNAPSHOT:
            oversized[k] = {'bytes': len(files[k]), 'sha256': hashlib.sha256(files[k]).hexdigest(), 'reason': 'aggregate'}
            continue
        kept[k] = files[k]
    kept['EXPORT_MANIFEST.json'] = json.dumps({'files': {k: hashlib.sha256(v).hexdigest() for k, v in kept.items()},
                                               'oversized_not_included': oversized,
                                               'scientific_validity_claim': False}, sort_keys=True).encode()
    return tar_gz(kept)


def backup_loop(once=False):
    while True:
        started = time.monotonic()
        for adir in sorted((ROOT / 'attempts').glob('*')) if (ROOT / 'attempts').exists() else []:
            if read(adir / 'exit.json') and not read(adir / 'upload.json'):
                finish_attempt(adir, wait=False)
        row = upload(snapshot(), 'snapshot')
        write(ROOT / 'processes' / 'backup-latest.json', row)
        deadline = read(ROOT / 'host-deadline.json')
        if once or now() >= deadline['gpu_end']:
            return row
        time.sleep(max(1, SNAPSHOT_EVERY_S - (time.monotonic() - started)))


def expiry_loop():
    """Hard host stop at inference end; provider deletion is the watchdog's job."""
    deadline = read(ROOT / 'host-deadline.json')
    while now() < deadline['inference_end']:
        time.sleep(min(5, max(0.05, deadline['inference_end'] - now())))
    killed = []
    for record_path in sorted((ROOT / 'attempts').glob('*/process.json')) if (ROOT / 'attempts').exists() else []:
        record = read(record_path)
        if alive(record):
            try:
                os.killpg(record['pid'], signal.SIGTERM)
                killed.append(record['pid'])
            except ProcessLookupError:
                pass
    for name in ('bootstrap',):
        record = read(ROOT / 'processes' / f'{name}.json')
        if alive(record):
            try:
                os.killpg(record['pid'], signal.SIGTERM)
                killed.append(record['pid'])
            except ProcessLookupError:
                pass
    time.sleep(KILL_GRACE_S)
    for record_path in sorted((ROOT / 'attempts').glob('*/process.json')) if (ROOT / 'attempts').exists() else []:
        record = read(record_path)
        adir = record_path.parent
        if alive(record):
            try:
                os.killpg(record['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        if read(adir / 'exit.json') is None:
            write(adir / 'exit.json', {'exit_code': None, 'finished_at': iso(), 'deadline_interrupted': True,
                                       'note': 'killed by host expiry at inference end'})
            finish_attempt(adir, True, repackage=True)
    write(ROOT / 'processes' / 'expiry-result.json', {'status': 'host_stop_sent', 'killed': killed, 'at': iso(),
                                                       'billing_ended': False})


def fetch_uv(dest):
    """Fetch the pinned uv binary from its digest-pinned OCI image and verify it."""
    token = json.load(urllib.request.urlopen(
        f'https://ghcr.io/token?scope=repository:{UV_IMAGE}:pull', timeout=30))['token']
    headers = {'Authorization': 'Bearer ' + token,
               'Accept': 'application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'}
    raw = urllib.request.urlopen(urllib.request.Request(
        f'https://ghcr.io/v2/{UV_IMAGE}/manifests/sha256:{UV_MANIFEST}', headers=headers), timeout=30).read()
    if hashlib.sha256(raw).hexdigest() != UV_MANIFEST:
        raise ValueError('uv manifest digest mismatch')
    return extract_uv(json.loads(raw)['layers'], headers, dest)


def extract_uv(layers, headers, dest, opener=None):
    opener = opener or (lambda req: urllib.request.urlopen(req, timeout=300).read())
    for layer in layers:
        blob = opener(urllib.request.Request(f'https://ghcr.io/v2/{UV_IMAGE}/blobs/{layer["digest"]}', headers=headers))
        if 'sha256:' + hashlib.sha256(blob).hexdigest() != layer['digest']:
            raise ValueError('uv layer digest mismatch')
        with tarfile.open(fileobj=io.BytesIO(blob), mode='r:*') as archive:
            for member in archive:
                if member.isfile() and Path(member.name).name == 'uv':
                    data = archive.extractfile(member).read()
                    if hashlib.sha256(data).hexdigest() == UV_SHA256:
                        Path(dest).write_bytes(data)
                        Path(dest).chmod(0o755)
                        return {'path': str(dest), 'sha256': UV_SHA256}
    raise ValueError('pinned uv binary not found in image')


def shipped_experiments():
    return [p for p in (READINESS, RESEARCH, *BENCHES) if (SOURCE / p / 'experiment.json').is_file()]


def bulk_inputs():
    """Frozen bulk inputs declared by the shipped experiments: {name: {key, sha256, bytes, prepared: [...]}}."""
    out = {}
    for prepared in shipped_experiments():
        for e in read(SOURCE / prepared / 'experiment.json').get('bulk_inputs') or []:
            name = e.get('name')
            if not isinstance(name, str) or not BULK_NAME.fullmatch(name) or '..' in name or name.endswith('.partial'):
                raise ValueError('invalid bulk input name')
            if not re.fullmatch(r'[0-9a-f]{64}', str(e.get('sha256'))) or type(e.get('bytes')) is not int \
                    or not 0 < e['bytes'] <= MAX_BULK or not str(e.get('key', '')).startswith('sessions/'):
                raise ValueError(f'invalid bulk input record: {name}')
            entry = {k: e[k] for k in ('key', 'sha256', 'bytes')}
            if any(out.setdefault(name, dict(entry, prepared=[]))[k] != v for k, v in entry.items()):
                raise ValueError(f'conflicting bulk input: {name}')
            out[name]['prepared'].append(prepared)
    return out


class BulkError(ValueError):
    pass


def fetch_bulk(phase, wanted):
    """Stream each declared bulk input to bulk-staging/<name>, verifying bytes and sha256; never logs URLs."""
    path = ROOT / 'bulk-urls.json'
    if path.stat().st_mode & 0o077:
        raise PermissionError('bulk URLs must be private to root')
    urls = {u['name']: u for u in read(path)['urls']}
    staging = ROOT / 'bulk-staging'
    staging.mkdir(mode=0o700, exist_ok=True)
    deadline, total = time.monotonic() + BULK_FETCH_S, sum(e['bytes'] for e in wanted.values())
    if shutil.disk_usage(staging).free < total + (1 << 30):
        raise BulkError('not enough disk for the bulk inputs')
    done, fetched, last = {}, 0, 0.0
    for name in sorted(wanted):
        e, u = wanted[name], urls.get(name)
        if u is None or u.get('key') != e['key']:
            raise BulkError(f'no download URL for bulk input {name}')
        if not str(u.get('url')).startswith('https://') and not (FAKE and str(u.get('url')).startswith('file://')):
            raise BulkError(f'bulk input URL scheme refused: {name}')
        target, partial = staging / name, staging / (name + '.partial')
        h, count = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(u['url'], timeout=300) as response, \
                    open(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644), 'wb') as out:
                while block := response.read(8 << 20):
                    count += len(block)
                    if count > e['bytes'] or time.monotonic() > deadline:
                        raise BulkError(f'bulk input {name} exceeds its size or the fetch time limit')
                    h.update(block)
                    out.write(block)
                    if time.monotonic() - last > 30:
                        last = time.monotonic()
                        phase('fetching_bulk', bulk_fetch={'file': name, 'files_done': len(done), 'files': len(wanted),
                                                           'bytes_done': fetched + count, 'bytes_total': total})
        except BulkError:
            partial.unlink(missing_ok=True)
            raise
        except Exception as error:  # noqa: BLE001 - the error text could carry the URL; keep only its type
            partial.unlink(missing_ok=True)
            raise RuntimeError(f'bulk input {name} download failed: {type(error).__name__}') from None
        if count != e['bytes'] or h.hexdigest() != e['sha256']:
            partial.unlink(missing_ok=True)
            raise BulkError(f'bulk input {name} size or sha256 mismatch')
        partial.chmod(0o644)
        partial.replace(target)
        fetched += count
        done[name] = {'key': e['key'], 'sha256': e['sha256'], 'bytes': count}
        phase('fetching_bulk', bulk_fetch={'file': name, 'files_done': len(done), 'files': len(wanted),
                                           'bytes_done': fetched, 'bytes_total': total})
    path.unlink()  # the GET URLs are no longer needed on the host
    return done


def place_bulk(wanted, prepared, target):
    """Hard-link verified bulk inputs into a staged model dir as bulk/<name> (no symlinks; read-only mount)."""
    names = sorted(n for n, e in wanted.items() if prepared in e['prepared'])
    if names:
        (target / 'bulk').mkdir(mode=0o755, parents=True, exist_ok=True)
    for name in names:
        src, dest = ROOT / 'bulk-staging' / name, target / 'bulk' / name
        if src.is_symlink() or src.stat().st_size != wanted[name]['bytes']:
            raise ValueError(f'staged bulk input changed: {name}')
        if not dest.exists():
            try:
                os.link(src, dest)
            except OSError:
                shutil.copyfile(src, dest)
                dest.chmod(0o644)
    return names


def bootstrap_sequence():
    """Install frozen runtime, admit host, download pinned weights, stage models."""
    progress = read(ROOT / 'bootstrap' / 'progress.json', {}) or {}

    def phase(name, **extra):
        progress.update(phase=name, at=iso(), **extra)
        write(ROOT / 'bootstrap' / 'progress.json', progress)

    def run(name, args, timeout):
        with (ROOT / 'bootstrap' / f'{name}.log').open('ab') as log:
            subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=timeout)
    try:
        ROOT.chmod(0o711)
        if (ROOT / 'upload-urls.json').stat().st_mode & 0o077:
            raise PermissionError('upload URLs must be private to root')
        wanted = bulk_inputs()
        if FAKE:
            if wanted:
                phase('fetching_bulk')
                progress['bulk_inputs'] = fetch_bulk(phase, wanted)
                for prepared in shipped_experiments():
                    place_bulk(wanted, prepared, ROOT / 'models' / file_sha(SOURCE / prepared / 'experiment.json')[:12])
            phase('ready', models={RESEARCH: 'fake', READINESS: 'fake'}, fake=True)
            return
        phase('fetching_uv')
        progress['uv'] = fetch_uv(ROOT / 'uv')
        if wanted:  # first, while the controller's short-lived GET URLs are still valid
            phase('fetching_bulk')
            progress['bulk_inputs'] = fetch_bulk(phase, wanted)
        phase('installing')
        run('install', ['bash', str(SOURCE / 'infra/runpod_install_runtime.sh'), str(SOURCE), str(ROOT / 'uv'),
                        UV_SHA256, str(ROOT / 'bootstrap' / 'runtime-install')], 3600)
        if any((SOURCE / b).is_dir() for b in BENCHES):
            phase('installing_bench_deps')
            run('install-bench', [str(ROOT / 'uv'), 'pip', 'install', '--python', PYTHON, '--require-hashes', '--no-deps',
                                  '--no-cache', '-r', str(SOURCE / 'pit_bench/requirements-evals.txt')], 1800)
            phase('fetching_datasets')
            run('fetch-datasets', ['python3', str(SOURCE / 'pit_bench/fetch.py'), str(DATASETS / 'mmlu-pro')], 1800)
            progress['datasets'] = {'mmlu-pro': read(DATASETS / 'mmlu-pro' / 'manifest.json')}
        phase('admitting')
        run('admission', [PYTHON, str(SOURCE / 'infra/cloud_two_gpu/host_admit.py')], 1800)
        phase('downloading')
        run('download', [PYTHON, str(SOURCE / 'scripts/artifacts.py'), '--config', str(SOURCE / DOWNLOAD_CONFIG),
                         '--output', str(ROOT / 'downloads'), '--weights'], 3 * 3600)
        phase('staging_models')
        sys.path.insert(0, str(SOURCE))
        from scripts.stage_inference import stage_models
        models = {}
        for prepared in shipped_experiments():
            config = SOURCE / prepared / 'experiment.json'
            sha = file_sha(config)
            target = ROOT / 'models' / sha[:12]
            if not target.exists():
                stage_models(ROOT / 'downloads/artifacts.json', target, config=config,
                             source_config=SOURCE / DOWNLOAD_CONFIG, model_mount_root=str(target))
            models[prepared] = file_sha(target / 'artifacts.json')
            place_bulk(wanted, prepared, target)
        phase('ready', models=models)
    except BaseException as error:  # noqa: BLE001 - recorded, controller decides
        phase('failed', error_type=type(error).__name__, error=str(error)[:2000])
        raise


# ----- commands ---------------------------------------------------------------
def queue_entry(prepared, run_id):
    manifest = read(SOURCE / prepared / 'manifest.json')
    if prepared not in (RESEARCH, READINESS, *BENCHES) or manifest is None:
        raise ValueError('request is not in the shipped queue')
    if not any(r['run_id'] == run_id for r in manifest['runs']):
        raise ValueError('request is not in the shipped queue')
    run = read(SOURCE / prepared / run_id / 'run.json')
    if run['config_sha256'] != file_sha(SOURCE / prepared / 'experiment.json'):
        raise ValueError('config hash mismatch')
    return run


def active_attempts():
    out = []
    for record_path in sorted((ROOT / 'attempts').glob('*/process.json')) if (ROOT / 'attempts').exists() else []:
        if alive(read(record_path)):
            out.append(record_path.parent.name)
    return out


def cmd_start(a):
    with Locked():
        state = fence(a.epoch)
        progress = read(ROOT / 'bootstrap' / 'progress.json', {}) or {}
        if progress.get('phase') != 'ready':
            return {'error': 'host not ready', 'phase': progress.get('phase')}
        if active_attempts():
            return {'error': 'another attempt is active', 'active': active_attempts()}
        deadline = read(ROOT / 'host-deadline.json')
        if now() + a.min_runway > deadline['inference_end']:
            return {'error': 'insufficient runway before inference end'}
        run = queue_entry(a.prepared, a.run_id)
        if run['config_sha256'] != a.config_sha256:
            return {'error': 'config hash differs from controller record'}
        adir = attempt_dir(a.attempt_id)
        try:
            adir.mkdir(parents=True, exist_ok=False)  # exclusive reservation
        except FileExistsError:
            return {'error': 'attempt already reserved', 'reservation': True}
        write(adir / 'attempt.json', {'attempt_id': a.attempt_id, 'run_id': a.run_id, 'prepared': a.prepared,
                                      'config_sha256': run['config_sha256'], 'input_sha256': run['input_sha256'],
                                      'tag': a.tag, 'timeout': a.timeout, 'epoch': a.epoch, 'reserved_at': iso(),
                                      'session_id': state['session_id']})
        record = spawn('attempt-' + a.attempt_id, [sys.executable, str(Path(__file__).resolve()), '_run', a.attempt_id],
                       adir / 'agent.log')
        write(adir / 'process.json', record)
        return {'started': True, 'reservation': True, 'pid': record['pid'], 'start': record['start']}


def observe(attempt_id):
    adir = attempt_dir(attempt_id)
    if not adir.exists():
        return {'reservation': False, 'process': 'dead', 'host': 'reachable'}
    record = read(adir / 'process.json')
    return {'host': 'reachable', 'reservation': True, 'process': 'alive' if alive(record) else 'dead',
            'started': record, 'finished': read(adir / 'exit.json'), 'upload': read(adir / 'upload.json'),
            'heartbeat_at': max((p.stat().st_mtime for p in adir.rglob('*') if p.is_file()), default=None)}


def cmd_inspect(a):
    with Locked():
        fence(a.epoch)
    return observe(a.attempt_id)


def cmd_status(a):
    with Locked():
        state = fence(a.epoch)
    attempts = {p.name: observe(p.name) for p in sorted((ROOT / 'attempts').glob('*'))} if (ROOT / 'attempts').exists() else {}
    daemons = {n: alive(read(ROOT / 'processes' / f'{n}.json')) for n in ('bootstrap', 'backup', 'expiry')}
    slots = read(ROOT / 'upload-urls.json', {'slots': []})['slots']
    used = set()
    if (ROOT / 'upload-journal.jsonl').exists():
        used = {json.loads(x)['key'] for x in (ROOT / 'upload-journal.jsonl').read_text().splitlines() if x.strip()}
    return {'state': state, 'bootstrap': read(ROOT / 'bootstrap' / 'progress.json'), 'daemons': daemons,
            'slots_total': len(slots), 'slots_remaining': sum(s['key'] not in used for s in slots),
            'backup_latest': read(ROOT / 'processes' / 'backup-latest.json'), 'attempts': attempts,
            'active': active_attempts(), 'deadline': read(ROOT / 'host-deadline.json')}


def cmd_backup_now(a):
    with Locked():
        fence(a.epoch)
    return backup_loop(once=True)


def cmd_fence(a):
    with Locked():
        return {'max_epoch': fence(a.epoch)['max_epoch']}


def cmd_bootstrap(a):
    """Initialise identity, deadlines and daemons once; then start the bootstrap."""
    with Locked():
        state = read(ROOT / 'agent-state.json')
        if state is None:
            spec = json.loads(Path(a.spec).read_text())
            for k in ('session_id', 'pod_id', 'inference_end', 'gpu_end', 'bundle_sha256'):
                if k not in spec:
                    return {'error': f'missing {k}'}
            if not 0 < spec['inference_end'] - now() <= 24 * 3600 or spec['gpu_end'] < spec['inference_end']:
                return {'error': 'invalid deadlines'}
            write(ROOT / 'host-deadline.json', {'inference_end': spec['inference_end'], 'gpu_end': spec['gpu_end']})
            state = {k: spec[k] for k in ('session_id', 'pod_id', 'bundle_sha256', 'image', 'gpu_type', 'gpu_count') if k in spec}
            state['max_epoch'] = a.epoch
            write(ROOT / 'agent-state.json', state)
        else:
            state = fence(a.epoch)
        started = {}
        for name, args in (('expiry', ['_expiry']), ('backup', ['_backup']), ('bootstrap', ['_bootstrap'])):
            record = read(ROOT / 'processes' / f'{name}.json')
            if name == 'bootstrap' and (read(ROOT / 'bootstrap' / 'progress.json', {}) or {}).get('phase') in ('ready', 'failed'):
                continue
            if not alive(record):
                if name == 'bootstrap' and record is not None:
                    continue  # never re-run a bootstrap that already ran; controller decides
                started[name] = spawn(name, [sys.executable, str(Path(__file__).resolve())] + args,
                                      ROOT / 'processes' / f'{name}.log')
        return {'state': state, 'started': sorted(started)}


def cmd_install_urls(a):
    """Atomically extend the private exact-object upload slots (controller renewal)."""
    with Locked():
        fence(a.epoch)
        incoming = json.loads(Path(a.file).read_text())['slots']
        Path(a.file).unlink()
        current = read(ROOT / 'upload-urls.json', {'slots': []})['slots']
        known = {s['key'] for s in current}
        current += [s for s in incoming if s['key'] not in known]
        write(ROOT / 'upload-urls.json', {'slots': current}, mode=0o600)
        return {'slots': len(current)}


def main(argv=None):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('fence', 'status', 'backup-now'):
        sub.add_parser(name).add_argument('--epoch', type=int, required=True)
    b = sub.add_parser('bootstrap')
    b.add_argument('--epoch', type=int, required=True)
    b.add_argument('--spec', required=True)
    s = sub.add_parser('start')
    for k in ('epoch', 'timeout'):
        s.add_argument('--' + k, type=int, required=True)
    s.add_argument('--min-runway', type=int, default=300)
    for k in ('prepared', 'run-id', 'attempt-id', 'tag', 'config-sha256'):
        s.add_argument('--' + k, required=True)
    i = sub.add_parser('inspect')
    i.add_argument('--epoch', type=int, required=True)
    i.add_argument('--attempt-id', required=True)
    u = sub.add_parser('install-urls')
    u.add_argument('--epoch', type=int, required=True)
    u.add_argument('--file', required=True)
    for internal in ('_bootstrap', '_backup', '_expiry'):
        sub.add_parser(internal)
    sub.add_parser('_run').add_argument('attempt_id')
    a = p.parse_args(argv)
    if a.cmd == '_run':
        return run_attempt(a.attempt_id)
    if a.cmd == '_backup':
        return backup_loop()
    if a.cmd == '_expiry':
        return expiry_loop()
    if a.cmd == '_bootstrap':
        return bootstrap_sequence()
    handler = {'fence': cmd_fence, 'status': cmd_status, 'backup-now': cmd_backup_now, 'bootstrap': cmd_bootstrap, 'start': cmd_start,
               'inspect': cmd_inspect, 'install-urls': cmd_install_urls}[a.cmd]
    try:
        result = handler(a)
    except Refused as refusal:
        print(json.dumps(refusal.args[0], sort_keys=True))
        raise SystemExit(2)
    print(json.dumps(result, sort_keys=True))
    if 'error' in result:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
