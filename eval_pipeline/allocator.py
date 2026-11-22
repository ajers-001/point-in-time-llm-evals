"""GPU slot allocation with durable intents, exact-name reconciliation and cleanup.

Order of operations for one pod:
1. Atomically reserve a worker slot, a replacement allowance and budget by
   writing a unique allocation intent (billing assumed from intent time).
2. Require the independent watchdog to report that exact intent as watched.
3. Send one create request. An uncertain response is never retried; the
   intent is reconciled against inventory by exact unique name.
At most two GPU pods may exist, counting intents and uncertain creates.
"""
import secrets

from .provider import CreateUncertain
from .store import MISSING, Put, Update

MAX_GPUS = 2
MAX_REPLACEMENTS = 2
OPEN = ('INTENT', 'CREATE_UNCERTAIN', 'CREATED', 'DELETING', 'DUPLICATE')
CREATE_GRACE_S = 300


def pod_records(session):
    return session.store.query(session.pk, 'POD#')


def open_pods(session):
    return [p for p in pod_records(session) if p['status'] in OPEN]


def allocation_name(session_id, slot):
    return f'pit-2gpu-{session_id}-w{slot}-{secrets.token_hex(8)}'


def reserve(session, fence, slot, quote, ledger, credit_usd, now, replacement=False):
    """Step 1: durable intent bound to the slot and budget. Returns the POD record."""
    meta, worker = session.meta(), session.worker(slot)
    if meta['status'] not in ('READY', 'RUNNING'):
        raise RuntimeError(f'session status {meta["status"]} does not admit allocation')
    if worker['status'] != 'EMPTY':
        raise RuntimeError(f'worker {slot} is {worker["status"]}')
    pods = open_pods(session)
    if len(pods) >= MAX_GPUS:
        raise RuntimeError('two GPUs (including intents) already exist')
    used = meta.get('replacements', 0)
    if replacement and used >= MAX_REPLACEMENTS:
        raise RuntimeError('replacement allowance exhausted')
    # Reserve for every slot that may still allocate; parked or failed slots wait for an operator.
    empty = sum(session.worker(s)['status'] == 'EMPTY' and not session.worker(s).get('needs_operator') for s in (1, 2))
    ok, detail = ledger.admit(pods, quote['hourly_total_usd'], planned_new=empty, credit_usd=credit_usd, now=now,
                              other_usd=meta.get('other_spend_usd', '0'))
    if not ok:
        raise RuntimeError(f'budget admission refused: {detail}')
    name = allocation_name(session.sid, slot)
    record = {'pk': session.pk, 'sk': 'POD#' + name, 'name': name, 'slot': slot, 'status': 'INTENT',
              'hourly_usd': quote['hourly_total_usd'], 'billing_started_at': now, 'intent_at': now,
              'replacement': replacement, 'epoch': fence.epoch, 'admission': detail}
    meta_set = {'replacements': used + 1} if replacement else {}
    session.store.transact([
        session.fence_check(fence),
        Put(record),
        Update(session.pk, f'WORKER#{slot}', worker['version'], {'status': 'ALLOCATING', 'pod_name': name},
               expect={'status': 'EMPTY'}),
        Update(session.pk, 'META', meta['version'], meta_set, expect={'status': meta['status']}),
    ])
    return session.store.get(session.pk, 'POD#' + name)


def _set_pod(session, fence, name, fields, expect=None):
    pod = session.store.get(session.pk, 'POD#' + name)
    session.store.transact([session.fence_check(fence),
                            Update(session.pk, 'POD#' + name, pod['version'], fields, expect or {})])
    return session.store.get(session.pk, 'POD#' + name)


def create(session, fence, provider, pod, payload, watchdog_sees, now):
    """Steps 2-3. ``watchdog_sees(name)`` must confirm independent prearming."""
    want = int(session.meta().get('gpu_count', 1))  # the session's approved GPUs per pod
    if payload['name'] != pod['name'] or payload.get('gpuCount') != want:
        raise ValueError(f'payload must request {want} GPU(s) under the reserved unique name')
    if not watchdog_sees(pod['name']):
        _set_pod(session, fence, pod['name'], {'status': 'ABANDONED', 'billing_ended_at': now,
                                               'note': 'watchdog did not confirm intent; no create sent'})
        _free_slot(session, fence, pod['slot'], pod['name'])
        raise RuntimeError('independent watchdog did not confirm the allocation intent; no create sent')
    try:
        created = provider.create_pod(payload)
    except CreateUncertain as error:
        _set_pod(session, fence, pod['name'], {'status': 'CREATE_UNCERTAIN', 'create_error': str(error)})
        return None
    return _bind(session, fence, pod['name'], created, now)


def _bind(session, fence, name, created, now):
    pod = _set_pod(session, fence, name, {'status': 'CREATED', 'pod_id': created['id'], 'created_at': now,
                                          'reported_cost_per_hr': str(created.get('costPerHr'))},
                   expect={'name': name})
    worker = session.worker(pod['slot'])
    session.set_worker(fence, pod['slot'], {'status': 'BOOTSTRAPPING', 'pod_id': created['id']},
                       expect={'status': worker['status'], 'pod_name': name})
    return pod


def _free_slot(session, fence, slot, name):
    worker = session.worker(slot)
    if worker.get('pod_name') == name:
        session.set_worker(fence, slot, {'status': 'EMPTY', 'pod_name': MISSING, 'pod_id': MISSING,
                                         'active_run': MISSING, 'active_attempt': MISSING},
                           expect={'pod_name': name})


def reconcile_intents(session, fence, provider, now, grace_s=CREATE_GRACE_S):
    """Bind or retire unresolved intents by exact unique name. Never creates."""
    pending = [p for p in pod_records(session) if p['status'] in ('INTENT', 'CREATE_UNCERTAIN')]
    if not pending:
        return []
    inventory = provider.list_pods()
    outcomes = []
    for pod in pending:
        matches = [x for x in inventory if x.get('name') == pod['name']]
        if len(matches) == 1:
            _bind(session, fence, pod['name'], matches[0], now)
            outcomes.append((pod['name'], 'bound'))
        elif len(matches) > 1:
            _set_pod(session, fence, pod['name'], {'status': 'DUPLICATE', 'pod_ids': [m['id'] for m in matches]})
            outcomes.append((pod['name'], 'duplicate'))
        elif now - pod['intent_at'] >= grace_s:
            # Remains watched by name until the session closes, in case a late create appears.
            _set_pod(session, fence, pod['name'], {'status': 'NOT_FOUND', 'billing_ended_at': now})
            _free_slot(session, fence, pod['slot'], pod['name'])
            outcomes.append((pod['name'], 'not_found'))
        else:
            outcomes.append((pod['name'], 'waiting'))
    return outcomes


def delete_exact(session, fence, provider, name, now, reason):
    """Delete one owned pod by exact id (or exact name) and verify absence."""
    pod = session.store.get(session.pk, 'POD#' + name)
    ids = [pod['pod_id']] if pod.get('pod_id') else []
    ids += [i for i in pod.get('pod_ids', []) if i not in ids]
    if not ids:
        ids = [x['id'] for x in provider.list_pods() if x.get('name') == name]
    for pod_id in ids:
        provider.delete_pod(pod_id)
    remaining = [x for x in provider.list_pods() if x.get('id') in ids or x.get('name') == name]
    if remaining:
        _set_pod(session, fence, name, {'status': 'DELETING', 'delete_reason': reason})
        return 'deleting'
    if pod['status'] != 'ABSENT':
        _set_pod(session, fence, name, {'status': 'ABSENT', 'billing_ended_at': pod.get('billing_ended_at') or now,
                                        'absent_verified_at': now, 'delete_reason': reason})
    _free_slot(session, fence, pod['slot'], name)
    return 'absent'


def build_payload(name, image, public_key, gpu_type='NVIDIA H100 80GB HBM3', disk_gb=350):
    """Single-GPU Secure Cloud pod with key-only SSH; no provider/cloud secrets in env."""
    startup = ('mkdir -p /run/sshd /root/.ssh; chmod 700 /root/.ssh; printf "%s\\n" "$PUBLIC_KEY" > '
               '/root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys; ssh-keygen -A; exec '
               '/usr/sbin/sshd -D -e -o PasswordAuthentication=no -o PermitRootLogin=prohibit-password')
    if '@sha256:' not in image:
        raise ValueError('Immutable image digest required')
    return dict(name=name, cloudType='SECURE', computeType='GPU', gpuTypeIds=[gpu_type], gpuCount=1,
                interruptible=False, imageName=image, containerDiskInGb=disk_gb, volumeInGb=0,
                minRAMPerGPU=125, minVCPUPerGPU=8, allowedCudaVersions=['12.8', '12.9', '13.0'],
                ports=['22/tcp'], dockerStartCmd=['/bin/bash', '-eu', '-c', startup],
                env={'PUBLIC_KEY': public_key})
