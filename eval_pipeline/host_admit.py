"""New-host readiness probes after the frozen install; no model answers.

Carries forward reports/recovery-20261006/admit_host.py unchanged in substance:
GPU identity, Landlock ABI, denied files/network, multiprocessing, ZMQ,
CUDA/Gloo, CUDA spawn and shutdown enforcement under the real sandbox, then
the actual UID 10001 application-path fixture through /pit-control staging
with a short scratch path. Results are readiness evidence for THIS host only,
labelled separately from the research queue.
"""
import ctypes
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path('/pit-control')
SOURCE = ROOT / 'source'
OUT = ROOT / 'bootstrap'
PYTHON = '/opt/venv/bin/python'
ALLOWED_GPUS = ('NVIDIA H100 80GB HBM3', 'NVIDIA H200')


def main():
    gpu = subprocess.check_output(['nvidia-smi', '--query-gpu=name,memory.total,driver_version',
                                   '--format=csv,noheader'], text=True).strip()
    # The session's approved GPU model (recorded at host initialisation); H100 for older sessions.
    state = json.loads((ROOT / 'agent-state.json').read_text())
    want, count = state.get('gpu_type', 'NVIDIA H100 80GB HBM3'), int(state.get('gpu_count', 1))
    rows = gpu.splitlines()
    if want not in ALLOWED_GPUS or len(rows) != count or not all(r.startswith(want + ',') for r in rows):
        raise SystemExit(f'Exactly {count} {want} GPU(s) are required')
    if ctypes.CDLL(None).syscall(444, 0, 0, 1) < 4:
        raise SystemExit('Landlock ABI 4 required')
    if ROOT.stat().st_mode & 0o777 != 0o711:
        raise SystemExit('/pit-control must be 0711')
    for private in ('upload-urls.json', 'agent-state.json', 'host-deadline.json'):
        if (ROOT / private).stat().st_mode & 0o077:
            raise SystemExit(f'{private} must not be group/world accessible')
    shutil.copytree(SOURCE / 'infra/runpod_sandbox', '/sandbox')
    for name in ('forbidden', 'input', 'scratch'):
        Path('/' + name).mkdir()
    Path('/forbidden/evaluator.txt').write_text('Synthetic evaluator canary')
    Path('/forbidden/credential.txt').write_text('Synthetic credential canary')
    Path('/input/data.txt').write_text('approved evidence')
    Path('/input/data.txt').chmod(0o444)
    Path('/scratch').chmod(0o777)
    Path('/scratch/escape').symlink_to('/forbidden/evaluator.txt')
    reads = ['/usr', '/lib', '/lib64', '/sandbox', '/input', '/proc', '/sys', '/opt/venv', '/opt/python',
             '/etc/ld.so.cache', '/etc/nsswitch.conf', '/etc/hosts', '/etc/passwd', '/etc/group']
    writes = ['/scratch', '/dev/null', '/dev/urandom', '/dev/random', '/dev/shm'] + [str(p) for p in Path('/dev').glob('nvidia*')]
    Path('/policy.json').write_text(json.dumps({'read': [p for p in reads if Path(p).exists()], 'write': writes,
                                                'timeout': 180}))
    probes = [('files-network', 'probe.py'), ('multiprocessing', 'probe_multiprocessing.py'), ('zmq', 'probe_zmq.py'),
              ('cuda-gloo', 'probe_cuda.py'), ('cuda-spawn', 'probe_cuda_spawn.py')]
    for label, script in probes:
        with (OUT / f'isolation-{label}.log').open('xb') as log:
            subprocess.run([PYTHON, '/sandbox/launch.py', '--policy', '/policy.json', '--', PYTHON, '/sandbox/' + script]
                           + (['--backends', 'gloo'] if script == 'probe_cuda.py' else []),
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=240)
    with (OUT / 'shutdown-probe.log').open('xb') as log:
        subprocess.run([PYTHON, '/sandbox/test_shutdown.py'], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=45)
    receipt = {'status': 'passed', 'gpu': gpu, 'at': dt.datetime.now(dt.timezone.utc).isoformat(),
               'no_model_inference': True, 'probes': [p[0] for p in probes] + ['shutdown']}
    (OUT / 'admission.json').write_text(json.dumps(receipt, indent=2) + '\n')

    sys.path.insert(0, str(SOURCE))
    from scripts.stage_inference import stage_run
    from infra.runpod_run import execute
    probe = ROOT / 'app-probe'
    stage_run(SOURCE / 'prepared/smoke-prewarm-v2', 'S-smoke-base', probe / 'stage', source=SOURCE)
    models = probe / 'models'
    models.mkdir()
    scratch = ROOT / 'tmp' / 'appprobe0001'  # short path: Linux IPC sockets are limited to 107 bytes
    with (OUT / 'application-probe.log').open('xb') as log:
        saved = [os.dup(fd) for fd in (1, 2)]
        try:
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            code = execute(probe / 'stage/code', models, probe / 'stage/case', probe / 'stage/output', 'S-smoke-base',
                           PYTHON, scratch, timeout=60, fixture=True)
        finally:
            for fd, old in zip((1, 2), saved):
                os.dup2(old, fd)
                os.close(old)
    if code != 0:
        raise SystemExit('Actual application sandbox fixture failed')
    (OUT / 'application-admission.json').write_text(json.dumps({'status': 'passed', 'fixture_only': True,
                                                                'inference_uid': 10001}) + '\n')
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
