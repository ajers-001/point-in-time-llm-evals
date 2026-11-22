"""Content-addressed, allowlisted host bundle (no evaluators, credentials or environments).

The host bundle carries exactly the audited execution primitives, the pinned
upstream source and lock, the host agent, and model-visible prepared inputs
for the 74 pending requests plus the neutral readiness smoke. It is uploaded
to private S3 and read back by hash before any GPU allocation.
"""
import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

MAX_BUNDLE = 16 << 20
MAX_EXPANDED = 64 << 20
FORBIDDEN = re.compile(r'(^|/)(\.env|\.aws|\.ssh|\.venv[^/]*|evaluation|release|results|reports|__pycache__)(/|$)'
                       r'|(rubric|catalog|labels?|answers?|gold|credential|api-key|secret|_ed25519)', re.I)

CODE = ['scripts/artifacts.py', 'scripts/stage_inference.py', 'infra/isolated_run.py', 'infra/runpod_run.py',
        'infra/runpod_install_runtime.sh', 'infra/runpod_after_download.py',
        'infra/cloud_two_gpu/__init__.py', 'infra/cloud_two_gpu/host_agent.py', 'infra/cloud_two_gpu/host_admit.py']
CODE += ['infra/runpod_sandbox/' + n for n in ('launch.py', 'policy.py', 'broker.py', 'socket_shim.c', 'sitecustomize.py',
                                                'probe.py', 'probe_multiprocessing.py', 'probe_zmq.py', 'probe_cuda.py',
                                                'probe_cuda_spawn.py', 'test_shutdown.py')]
CODE += ['pit_runtime/' + n for n in ('__init__.py', '__main__.py', 'runner.py', 'worker.py', 'service.py', 'telemetry.py')]
CODE += ['upstream/' + n for n in ('pyproject.toml', 'uv.lock', 'README.md', 'LICENSE')]


def _prepared(root, prepared, run_ids):
    from pit_runtime.runner import load_run
    directory = root / prepared
    records = [load_run(directory, rid)['record'] for rid in run_ids]
    files = {f'{prepared}/experiment.json': (directory / 'experiment.json').read_bytes(),
             f'{prepared}/manifest.json': (json.dumps({'status': 'prepared_not_run', 'run_count': len(records),
                                                      'runs': records}, indent=2) + '\n').encode()}
    for rid in run_ids:
        for name in ('run.json', 'input.md'):
            files[f'{prepared}/{rid}/{name}'] = (directory / rid / name).read_bytes()
    return files


def host_files(root, queue):
    root = Path(root)
    files = {name: (root / name).read_bytes() for name in CODE}
    for p in sorted((root / 'upstream/src').rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc':
            files[str(p.relative_to(root))] = p.read_bytes()
    files['prepared/smoke-v1/experiment.json'] = (root / 'prepared/smoke-v1/experiment.json').read_bytes()
    files.update(_prepared(root, 'prepared/smoke-prewarm-v2', ['S-smoke-' + a for a in ('base', 'dd', 'steer', 'combined')]))
    if queue.get('kind', '').startswith(BENCH_PREFIXES):
        files.update(bench_files(root, queue))
        return files
    pending = sorted({r['prepared'] for r in queue['runs']})
    if pending != ['prepared/robustness-development-prewarm-v3']:
        raise ValueError('Unexpected prepared directory in the pending queue')
    files.update(_prepared(root, pending[0], [r['run_id'] for r in queue['runs']]))
    for r in queue['runs']:
        run = json.loads(files[f'{r["prepared"]}/{r["run_id"]}/run.json'])
        if run['config_sha256'] != r['config_sha256'] or \
                hashlib.sha256(files[f'{r["prepared"]}/experiment.json']).hexdigest() != r['config_sha256']:
            raise ValueError(f'Config hash mismatch in bundle: {r["run_id"]}')
    return files


BENCH = 'prepared/benchmark-repro-v1'
BENCH_PREFIXES = ('benchmark-', 'openbook-', 'utility-', 'hybrid-', 'fakenews-', 'jspace-', 'lookahead-')  # queue kinds run by infra/bench_run.py (see inputs.BENCH_KINDS)
BENCH_CODE = ['infra/bench_run.py', 'pit_bench/__init__.py', 'pit_bench/jobs.py', 'pit_bench/job.py',
              'pit_bench/prepare.py', 'pit_bench/fetch.py', 'pit_bench/requirements-evals.txt']
# Upstream scoring code whose file name trips the evaluator-data filter; code, not labels.
BENCH_ALLOW = frozenset({'upstream/evals/scoring/answer_match.py'})


def bench_files(root, queue):
    """Benchmark runner, pinned eval stack and the frozen prompt-only job inputs (no labelled parquet)."""
    import sys
    root = Path(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from pit_bench.prepare import code_files
    files = {name: (root / name).read_bytes() for name in BENCH_CODE + code_files(root)}
    BENCH = queue['prepared']
    prepared = root / BENCH
    for name in ['experiment.json', 'manifest.json'] + sorted(f'data/{p.name}' for p in (prepared / 'data').glob('*.parquet')):
        files[f'{BENCH}/{name}'] = (prepared / name).read_bytes()
    for r in queue['runs']:
        if r['prepared'] != BENCH:
            raise ValueError('Unexpected prepared directory in the benchmark queue')
        for name in ('run.json', 'input.md'):
            files[f'{BENCH}/{r["run_id"]}/{name}'] = (prepared / r['run_id'] / name).read_bytes()
        if hashlib.sha256(files[f'{BENCH}/experiment.json']).hexdigest() != r['config_sha256']:
            raise ValueError(f'Config hash mismatch in bundle: {r["run_id"]}')
    config = json.loads(files[f'{BENCH}/experiment.json'])
    for task in ('ma', 'pharma'):
        if hashlib.sha256(files[f'{BENCH}/data/{task}.parquet']).hexdigest() != config['datasets'][task]['sandbox_sha256']:
            raise ValueError(f'Prompt-only {task} parquet differs from the frozen experiment')
    for task, info in config['datasets'].items():  # every other staged parquet (sandbox_path, ma_recall) must be present
        if isinstance(info, dict) and info.get('sandbox_sha256') and task not in ('ma', 'pharma'):
            data = files.get(f'{BENCH}/data/{task}.parquet')
            if data is None or hashlib.sha256(data).hexdigest() != info['sandbox_sha256']:
                raise ValueError(f'Prompt-only {task} parquet missing or differs from the frozen experiment')
    return files


def build_host(files):
    return build(files, allow=BENCH_ALLOW)


def build(files, allow=()):
    """Deterministic tar.gz with BUNDLE_MANIFEST.json; ``allow`` exempts exact paths from FORBIDDEN."""
    for name in files:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or (name not in allow and FORBIDDEN.search(name)):
            raise ValueError(f'Path not allowed in bundle: {name}')
    return _pack(files)


def extract(data, target, expected_sha):
    """Verify and extract into an absent directory (stdlib only; also used on hosts)."""
    target = Path(target)
    if hashlib.sha256(data).hexdigest() != expected_sha or len(data) > MAX_BUNDLE:
        raise ValueError('Bundle integrity mismatch')
    if target.exists() or any(p.is_symlink() for p in [target, *target.parents]):
        raise ValueError('Target must be absent with no symlinked ancestors')
    payloads, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        for m in archive:
            name = PurePosixPath(m.name)
            if not m.isfile() or name.is_absolute() or '..' in name.parts or '\\' in m.name or m.name in payloads:
                raise ValueError(f'Unsafe bundle member: {m.name}')
            total += m.size
            if total > MAX_EXPANDED:
                raise ValueError('Expanded bundle exceeds bound')
            payloads[m.name] = archive.extractfile(m).read()
    manifest = json.loads(payloads.pop('BUNDLE_MANIFEST.json'))['files']
    if set(manifest) != set(payloads) or any(hashlib.sha256(v).hexdigest() != manifest[k] for k, v in payloads.items()):
        raise ValueError('Bundle inventory mismatch')
    partial = target.with_name(target.name + '.partial')
    partial.mkdir(parents=True)
    for name, raw in payloads.items():
        p = partial / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
    partial.rename(target)
    return len(payloads)


CONTROLLER_MODULES = ('__init__', 'allocator', 'artifacts', 'budget', 'bundle', 'cloud', 'controller', 'entrypoint',
                      'inputs', 'provider', 'reconcile', 'state', 'store', 'watchdog')
WATCHDOG_MODULES = ('__init__', 'watchdog', 'store', 'budget', 'provider')


def controller_files(root, queue):
    """Controller code, its hash lock and the frozen queue inputs it re-validates at start."""
    root = Path(root)
    files = {f'infra/cloud_two_gpu/{m}.py': (root / f'infra/cloud_two_gpu/{m}.py').read_bytes() for m in CONTROLLER_MODULES}
    files['infra/cloud_two_gpu/controller-requirements.txt'] = (root / 'infra/cloud_two_gpu/controller-requirements.txt').read_bytes()
    for p in sorted((root / 'specs/cloud-two-gpu-20261006').glob('*')):
        if p.is_file():
            files[f'specs/cloud-two-gpu-20261006/{p.name}'] = p.read_bytes()
    files['reports/recovery-20261006/followup-plan.json'] = (root / 'reports/recovery-20261006/followup-plan.json').read_bytes()
    if queue.get('kind', '').startswith(BENCH_PREFIXES):
        for name in ('experiment.json', 'manifest.json'):
            files[f'{queue["prepared"]}/{name}'] = (root / queue['prepared'] / name).read_bytes()
    for r in queue['runs']:
        for name in ('run.json', 'input.md'):
            files[f'{r["prepared"]}/{r["run_id"]}/{name}'] = (root / r['prepared'] / r['run_id'] / name).read_bytes()
    return files


def build_controller(files):
    return build(files, allow={'reports/recovery-20261006/followup-plan.json'})


def _pack(files):
    inventory = {k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())}
    entries = dict(files, **{'BUNDLE_MANIFEST.json': (json.dumps({'files': inventory}, sort_keys=True) + '\n').encode()})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(entries[name]), 0o644, 0
            archive.addfile(info, io.BytesIO(entries[name]))
    data = gzip.compress(buf.getvalue(), mtime=0)
    if len(data) > MAX_BUNDLE:
        raise ValueError('Bundle exceeds size bound')
    return data, {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'files': len(inventory),
                  'inventory_sha256': hashlib.sha256(entries['BUNDLE_MANIFEST.json']).hexdigest()}


def watchdog_zip(root):
    """Deterministic Lambda package with only the watchdog's modules."""
    import zipfile
    root = Path(root)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for m in WATCHDOG_MODULES:
            info = zipfile.ZipInfo(f'infra/cloud_two_gpu/{m}.py', date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, (root / f'infra/cloud_two_gpu/{m}.py').read_bytes())
    data = buf.getvalue()
    return data, {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
