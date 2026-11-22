"""Load and validate the frozen continuation queue. Fails closed on any drift."""
import hashlib
import json
from pathlib import Path

SPEC_DIR = 'specs/cloud-two-gpu-20261006'
PLAN_SHA256 = '0cc9f3b45026719ee7da17c2fb31dd38cbff7d59b4a027594b54720044122a08'
PRIOR_AUDIT = 'reports/recovery-20261006/audit-20261006T135405Z.tar.gz'
PRIOR_AUDIT_SHA256 = 'ddc98daa3274e523c7e788f049a4ab0d42e4ba277dccc34803c8ff135de2f626'
EXPECTED = {'plan': 132, 'pending': 74, 'completed': 58, 'shard': 37}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _json(path):
    return json.loads(Path(path).read_text())


def load_queue(root, verify_audit=True):
    """Return the validated queue: plan-ordered pending runs plus prior completions."""
    root = Path(root)
    spec = root / SPEC_DIR
    manifest = _json(spec / 'MANIFEST.json')
    for name, digest in manifest.items():
        if sha256_file(spec / name) != digest:
            raise ValueError(f'Spec file changed: {name}')
    remaining = _json(spec / 'remaining-requests.json')
    plan_path = root / remaining['source_plan']
    if remaining['source_plan_sha256'] != PLAN_SHA256 or sha256_file(plan_path) != PLAN_SHA256:
        raise ValueError('Original 132-request plan hash mismatch')
    if remaining['prior_final_audit_sha256'] != PRIOR_AUDIT_SHA256:
        raise ValueError('Prior final audit reference mismatch')
    if verify_audit and sha256_file(root / PRIOR_AUDIT) != PRIOR_AUDIT_SHA256:
        raise ValueError('Prior final audit archive hash mismatch')
    plan = _json(plan_path)['runs']
    order = {r['run_id']: i for i, r in enumerate(plan)}
    if len(plan) != EXPECTED['plan'] or len(order) != EXPECTED['plan']:
        raise ValueError('Plan must contain 132 unique requests')
    pending = remaining['runs']
    ids = [r['run_id'] for r in pending]
    completed = remaining['completed_run_ids']
    if remaining['expected_runs'] != EXPECTED['pending'] or len(set(ids)) != EXPECTED['pending'] or len(ids) != EXPECTED['pending']:
        raise ValueError('Expected 74 unique pending requests')
    if len(set(completed)) != EXPECTED['completed'] or len(completed) != EXPECTED['completed']:
        raise ValueError('Expected 58 unique completed requests')
    if set(ids) & set(completed) or set(ids) | set(completed) != set(order):
        raise ValueError('Pending and completed sets must partition the plan')
    if [i for i in sorted(ids, key=order.get)] != ids:
        raise ValueError('Pending requests must keep the recorded plan order')
    shards = {}
    for slot in (1, 2):
        shard = _json(spec / f'worker-{slot}-initial-shard.json')
        if shard['worker_slot'] != slot or len(shard['runs']) != EXPECTED['shard']:
            raise ValueError(f'Worker {slot} shard must contain 37 requests')
        for r in shard['runs']:
            if r['run_id'] in shards:
                raise ValueError('Initial shards overlap')
            shards[r['run_id']] = slot
    if set(shards) != set(ids):
        raise ValueError('Shards must cover exactly the pending requests')
    planned = {r['run_id']: r for r in plan}
    runs = []
    for r in pending:
        original = planned[r['run_id']]
        if any(original[k] != r[k] for k in ('prepared', 'category', 'config_sha256')):
            raise ValueError(f'Request differs from original plan: {r["run_id"]}')
        run_json = _json(root / r['prepared'] / r['run_id'] / 'run.json')
        if run_json['run_id'] != r['run_id'] or run_json['config_sha256'] != r['config_sha256']:
            raise ValueError(f'Prepared request does not match its config hash: {r["run_id"]}')
        if sha256_file(root / r['prepared'] / r['run_id'] / 'input.md') != run_json['input_sha256']:
            raise ValueError(f'Prepared input hash mismatch: {r["run_id"]}')
        runs.append({**r, 'order': order[r['run_id']], 'affinity': shards[r['run_id']],
                     'input_sha256': run_json['input_sha256']})
    prior = [{'run_id': rid, 'order': order[rid], 'category': planned[rid]['category'],
              'prepared': planned[rid]['prepared'], 'config_sha256': planned[rid]['config_sha256']}
             for rid in completed]
    return {'runs': runs, 'prior': prior, 'plan_sha256': PLAN_SHA256,
            'prior_audit_sha256': PRIOR_AUDIT_SHA256,
            'spec_manifest_sha256': sha256_file(spec / 'MANIFEST.json')}


BENCH_PREPARED = 'prepared/benchmark-repro-v1'
BENCH_KINDS = {'benchmark-repro-v1': 'prepared/benchmark-repro-v1',
               'benchmark-repro-v1-dd': 'prepared/benchmark-repro-v1-dd',
               'benchmark-repro-v1-dd2': 'prepared/benchmark-repro-v1-dd2',
               'benchmark-grid-v1': 'prepared/benchmark-grid-v1',
               'benchmark-mixed-v1': 'prepared/benchmark-mixed-v1',
               'benchmark-replay-v1': 'prepared/benchmark-replay-v1',
               'benchmark-pilot-v1': 'prepared/benchmark-pilot-v1',
               'benchmark-indicators-v1': 'prepared/benchmark-indicators-v1',
               'benchmark-ncd-v1': 'prepared/benchmark-ncd-v1',
               'benchmark-ncd-v1b': 'prepared/benchmark-ncd-v1b',
               'openbook-b-v1': 'prepared/openbook-b-v1', 'openbook-c-v1': 'prepared/openbook-c-v1',
               'openbook-c-v2': 'prepared/openbook-c-v2', 'openbook-d-v1': 'prepared/openbook-d-v1',
               'utility-v1': 'prepared/utility-v1', 'utility-v1b': 'prepared/utility-v1b',
               'utility-v1c': 'prepared/utility-v1c', 'hybrid-v1': 'prepared/hybrid-v1',
               'fakenews-v1': 'prepared/fakenews-v1', 'jspace-v1-stage1': 'prepared/jspace-v1-stage1',
               'jspace-v1-stage2a': 'prepared/jspace-v1-stage2a', 'jspace-v1-stage2a-b1': 'prepared/jspace-v1-stage2a-b1',
               'jspace-v1-stage2b': 'prepared/jspace-v1-stage2b', 'jspace-v1-noopt': 'prepared/jspace-v1-noopt',
               'lookahead-v1-step1': 'prepared/lookahead-v1-step1', 'lookahead-v1-r1a': 'prepared/lookahead-v1-r1a',
               'lookahead-v1-r1b': 'prepared/lookahead-v1-r1b'}
QUEUE_KINDS = ('continuation-74', *BENCH_KINDS)


def load_bench_queue(root, kind='benchmark-repro-v1'):
    """Frozen benchmark jobs in pre-registered priority order; no slot affinity, no prior rows."""
    root = Path(root)
    rel = BENCH_KINDS[kind]
    prepared = root / rel
    config_sha = sha256_file(prepared / 'experiment.json')
    config = _json(prepared / 'experiment.json')
    manifest = _json(prepared / 'manifest.json')
    if manifest['run_count'] != len(config['jobs']) or len(manifest['runs']) != len(config['jobs']):
        raise ValueError('Benchmark manifest must list every frozen job exactly once')
    runs = []
    for order, (record, job) in enumerate(zip(manifest['runs'], config['jobs'])):
        rid = record['run_id']
        if rid != job['job_id'] or record['config_sha256'] != config_sha or record['job']['priority'] != order:
            raise ValueError(f'Benchmark job out of frozen order or config: {rid}')
        if _json(prepared / rid / 'run.json') != record:
            raise ValueError(f'Benchmark run record differs from manifest: {rid}')
        if sha256_file(prepared / rid / 'input.md') != record['input_sha256']:
            raise ValueError(f'Benchmark input hash mismatch: {rid}')
        runs.append({'run_id': rid, 'order': order, 'category': 'benchmark', 'prepared': rel,
                     'config_sha256': config_sha, 'input_sha256': record['input_sha256'], 'affinity': 0})
    if len({r['run_id'] for r in runs}) != len(runs):
        raise ValueError('Duplicate benchmark job id')
    return {'runs': runs, 'prior': [], 'kind': kind, 'prepared': rel,
            'plan_sha256': sha256_file(prepared / 'manifest.json'), 'prior_audit_sha256': None,
            'spec_manifest_sha256': config_sha}


def load_any(root, kind, verify_audit=True):
    if kind in BENCH_KINDS:
        return load_bench_queue(root, kind)
    if kind in (None, 'continuation-74'):
        return load_queue(root, verify_audit=verify_audit)
    raise ValueError(f'Unknown queue kind {kind!r}')
