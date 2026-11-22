"""Session, fenced controller lease, job state machine and atomic claims.

Every controller mutation carries a ``Check`` on the lease item bound to the
caller's epoch, so a superseded controller instance cannot claim, dispatch or
commit. Each job transition appends an immutable event in the same
transaction. Worker liveness timeouts never move work back to PENDING by
themselves: ambiguous attempts go to RECONCILING and need positive evidence.
"""
import hashlib
from dataclasses import dataclass

from .store import Check, ConditionFailed, MISSING, Put, Update

TERMINAL = {'SUCCEEDED', 'FAILED_SCIENTIFIC', 'INTERRUPTED_DEADLINE', 'BLOCKED'}
ACTIVE = {'CLAIMED', 'RUNNING', 'UPLOADING', 'RECONCILING'}
ALLOWED = {
    'PENDING': {'CLAIMED', 'BLOCKED'},
    'CLAIMED': {'RUNNING', 'RECONCILING', 'INTERRUPTED_DEADLINE'},
    'RUNNING': {'UPLOADING', 'RECONCILING', 'INTERRUPTED_DEADLINE', 'FAILED_SCIENTIFIC'},
    'UPLOADING': {'SUCCEEDED', 'RECONCILING', 'FAILED_SCIENTIFIC', 'FAILED_INFRA', 'INTERRUPTED_DEADLINE'},
    'RECONCILING': {'RUNNING', 'UPLOADING', 'FAILED_INFRA', 'FAILED_SCIENTIFIC',
                    'INTERRUPTED_DEADLINE', 'PENDING', 'BLOCKED'},
    'FAILED_INFRA': {'PENDING', 'BLOCKED'},
}
MAX_INFRA_RETRIES = 1


class LeaseHeld(Exception):
    """Another controller instance holds an unexpired lease."""


class LeaseLost(Exception):
    """This controller instance was fenced out by a newer epoch."""


class InvalidTransition(Exception):
    pass


@dataclass(frozen=True)
class Fence:
    owner: str
    epoch: int


def session_pk(session_id):
    return 'SESSION#' + session_id


def attempt_tag(session_id, attempt_id):
    """Short, stable name for scratch paths (Linux IPC sockets are <=107 bytes)."""
    return hashlib.sha256(f'{session_id}/{attempt_id}'.encode()).hexdigest()[:12]


class Session:
    def __init__(self, store, session_id):
        self.store, self.sid, self.pk = store, session_id, session_pk(session_id)

    # ----- initialisation -------------------------------------------------
    def initialize(self, queue, meta, now):
        """Idempotently create the session, 58 prior rows and 74 PENDING rows."""
        existing = self.store.get(self.pk, 'META')
        hashes = {'plan_sha256': queue['plan_sha256'],
                  'prior_audit_sha256': queue['prior_audit_sha256'],
                  'spec_manifest_sha256': queue['spec_manifest_sha256']}
        if existing is None:
            self.store.transact([Put({'pk': self.pk, 'sk': 'META', 'session_id': self.sid,
                                      'status': 'INITIALIZING', 'input_hashes': hashes,
                                      'created_at': now, **meta})])
        elif existing['input_hashes'] != hashes:
            raise ValueError('Session exists with different frozen inputs')
        rows = [{'pk': self.pk, 'sk': 'JOB#' + p['run_id'], 'run_id': p['run_id'], 'order': p['order'],
                 'category': p['category'], 'prepared': p['prepared'], 'config_sha256': p['config_sha256'],
                 'state': 'SUCCEEDED', 'prior': True, 'attempt_seq': 0, 'infra_retries': 0,
                 'canonical_source': {'audit_sha256': queue['prior_audit_sha256']}}
                for p in queue['prior']]
        rows += [{'pk': self.pk, 'sk': 'JOB#' + r['run_id'], 'run_id': r['run_id'], 'order': r['order'],
                  'category': r['category'], 'prepared': r['prepared'], 'config_sha256': r['config_sha256'],
                  'input_sha256': r['input_sha256'], 'affinity': r['affinity'], 'state': 'PENDING',
                  'prior': False, 'attempt_seq': 0, 'infra_retries': 0}
                 for r in queue['runs']]
        # A one-GPU session parks slot 2: it never allocates unless an operator resumes it.
        rows += [{'pk': self.pk, 'sk': f'WORKER#{slot}', 'slot': slot, 'status': 'EMPTY',
                  **({'needs_operator': 'not part of this single-GPU session'} if slot > meta.get('workers', 2) else {})}
                 for slot in (1, 2)]
        immutable = ('run_id', 'order', 'category', 'prepared', 'config_sha256', 'prior', 'slot')
        for row in rows:
            try:
                self.store.transact([Put(row)])
            except ConditionFailed:
                have = self.store.get(row['pk'], row['sk'])
                if any(have.get(k) != row.get(k) for k in immutable):
                    raise ValueError(f'Conflicting existing row {row["sk"]}')
        meta_now = self.store.get(self.pk, 'META')
        if meta_now['status'] == 'INITIALIZING':
            self.store.transact([Update(self.pk, 'META', meta_now['version'], {'status': 'READY'})])

    def meta(self):
        return self.store.get(self.pk, 'META')

    def jobs(self):
        return self.store.query(self.pk, 'JOB#')

    def job(self, run_id):
        return self.store.get(self.pk, 'JOB#' + run_id)

    def worker(self, slot):
        return self.store.get(self.pk, f'WORKER#{slot}')

    def events(self, run_id=''):
        return self.store.query(self.pk, 'EVT#' + run_id)

    # ----- controller lease ----------------------------------------------
    def acquire(self, owner, now, ttl=90):
        lease = self.store.get(self.pk, 'LEASE')
        if lease is None:
            try:
                self.store.transact([Put({'pk': self.pk, 'sk': 'LEASE', 'owner': owner, 'epoch': 1,
                                          'expires_at': now + ttl, 'acquired_at': now})])
                return Fence(owner, 1)
            except ConditionFailed:
                lease = self.store.get(self.pk, 'LEASE')
        if lease['owner'] != owner and lease['expires_at'] > now:
            raise LeaseHeld(f'lease held by {lease["owner"]} until {lease["expires_at"]}')
        epoch = lease['epoch'] + 1
        try:
            self.store.transact([Update(self.pk, 'LEASE', lease['version'],
                                        {'owner': owner, 'epoch': epoch, 'expires_at': now + ttl,
                                         'acquired_at': now})])
        except ConditionFailed as error:
            raise LeaseHeld('lease changed during acquisition') from error
        return Fence(owner, epoch)

    def renew(self, fence, now, ttl=90):
        lease = self.store.get(self.pk, 'LEASE')
        self.store.transact([Update(self.pk, 'LEASE', lease['version'], {'expires_at': now + ttl},
                                    expect={'owner': fence.owner, 'epoch': fence.epoch})])

    def fence_check(self, fence):
        return Check(self.pk, 'LEASE', {'owner': fence.owner, 'epoch': fence.epoch})

    def require_fence(self, fence):
        lease = self.store.get(self.pk, 'LEASE')
        if lease is None or lease['owner'] != fence.owner or lease['epoch'] != fence.epoch:
            raise LeaseLost(f'controller epoch {fence.epoch} superseded')

    # ----- job transitions -----------------------------------------------
    def transition(self, fence, run_id, to, now, fields=None, expect=None, extra=(), note=None):
        job = self.job(run_id)
        if job is None:
            raise KeyError(run_id)
        frm = job['state']
        if to not in ALLOWED.get(frm, ()):
            raise InvalidTransition(f'{run_id}: {frm} -> {to}')
        fields = dict(fields or {})
        event = {'pk': self.pk, 'sk': f'EVT#{run_id}#{job["version"] + 1:06d}', 'run_id': run_id,
                 'from': frm, 'to': to, 'at': now, 'epoch': fence.epoch,
                 'attempt_id': fields.get('current_attempt', job.get('current_attempt'))}
        if note is not None:
            event['note'] = note
        ops = [self.fence_check(fence),
               Update(self.pk, 'JOB#' + run_id, job['version'], {**fields, 'state': to, 'updated_at': now},
                      expect={'state': frm, **(expect or {})}),
               Put(event), *extra]
        self.store.transact(ops)
        return self.job(run_id)

    # ----- workers and claims --------------------------------------------
    def set_worker(self, fence, slot, fields, expect=None):
        worker = self.worker(slot)
        self.store.transact([self.fence_check(fence),
                             Update(self.pk, f'WORKER#{slot}', worker['version'], fields, expect or {})])
        return self.worker(slot)

    def claim(self, fence, slot, pod_id, now):
        """Atomically claim the next PENDING request for one ready worker.

        Prefers the worker's initial shard, then any globally PENDING request,
        each in recorded plan order. Returns the claimed job or None.
        """
        worker = self.worker(slot)
        if worker['status'] != 'READY' or worker.get('pod_id') != pod_id or worker.get('active_run'):
            raise InvalidTransition(f'worker {slot} cannot claim')
        candidates = sorted((j for j in self.jobs() if j['state'] == 'PENDING' and not j['prior']),
                            key=lambda j: (j['affinity'] != slot, j['order']))
        for job in candidates:
            seq = job['attempt_seq'] + 1
            attempt_id = f'{job["run_id"]}.a{seq}'
            attempt = {'pk': self.pk, 'sk': f'ATT#{job["run_id"]}#{seq:03d}', 'run_id': job['run_id'],
                       'attempt_id': attempt_id, 'seq': seq, 'worker_slot': slot, 'pod_id': pod_id,
                       'claimed_at': now, 'epoch': fence.epoch, 'tag': attempt_tag(self.sid, attempt_id),
                       'status': 'CLAIMED'}
            worker_update = Update(self.pk, f'WORKER#{slot}', worker['version'],
                                   {'active_run': job['run_id'], 'active_attempt': attempt_id},
                                   expect={'pod_id': pod_id, 'status': 'READY', 'active_run': MISSING})
            try:
                return self.transition(fence, job['run_id'], 'CLAIMED', now,
                                       fields={'attempt_seq': seq, 'current_attempt': attempt_id,
                                               'worker_slot': slot, 'pod_id': pod_id, 'claimed_at': now},
                                       expect={'attempt_seq': job['attempt_seq']},
                                       extra=[Put(attempt), worker_update])
            except (ConditionFailed, InvalidTransition):
                self.require_fence(fence)
                if self.worker(slot)['version'] != worker['version']:
                    raise
                continue
        return None

    def attempt(self, run_id, seq):
        return self.store.get(self.pk, f'ATT#{run_id}#{seq:03d}')

    def attempts(self, run_id=''):
        return self.store.query(self.pk, 'ATT#' + run_id)

    def update_attempt(self, fence, run_id, seq, fields, expect=None):
        att = self.attempt(run_id, seq)
        self.store.transact([self.fence_check(fence),
                             Update(self.pk, f'ATT#{run_id}#{seq:03d}', att['version'], fields, expect or {})])
        return self.attempt(run_id, seq)

    def release_worker(self, fence, slot, run_id):
        worker = self.worker(slot)
        if worker.get('active_run') != run_id:
            return worker
        return self.set_worker(fence, slot, {'active_run': MISSING, 'active_attempt': MISSING},
                               expect={'active_run': run_id})
