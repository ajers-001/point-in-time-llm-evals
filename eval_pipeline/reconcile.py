"""Replay-safe recovery: decide from positive evidence, never from timeouts.

An observation describes one attempt as seen right now:

    pod:          'present' | 'absent' | 'unknown'   (provider, exact pod id)
    host:         'reachable' | 'unreachable'
    process:      'alive' | 'dead' | 'unknown'       (pid + /proc starttime identity)
    reservation:  True | False | None                (host attempt directory exists)
    finished:     {'exit_code', 'finished_at', 'deadline_interrupted'} | None
    upload:       {'key', 'sha256'} | None           (host receipt or S3 scan)

Lease or heartbeat expiry only moves a job to RECONCILING. A retry needs
confirmed process death or pod absence, a recorded repair, and an unused
infrastructure retry. Late uploads are verified and kept under their own
attempt id; they never replace an existing canonical result.
"""
from .artifacts import secure_copy
from .state import ACTIVE, MAX_INFRA_RETRIES, InvalidTransition
from .store import Put

HEARTBEAT_STALE_S = 90


def mark_stale(session, fence, now, stale_after=HEARTBEAT_STALE_S, restarted=False):
    """Move ambiguous in-flight jobs to RECONCILING (all of them after a restart)."""
    moved = []
    for job in session.jobs():
        if job['state'] not in ('CLAIMED', 'RUNNING', 'UPLOADING'):
            continue
        att = session.attempt(job['run_id'], job['attempt_seq'])
        seen = max(att.get('heartbeat_at') or 0, att.get('claimed_at') or 0)
        if restarted or now - seen > stale_after:
            session.transition(fence, job['run_id'], 'RECONCILING', now,
                               note='controller restart' if restarted else 'heartbeat stale; liveness unknown')
            moved.append(job['run_id'])
    return moved


def record_conflict(session, fence, run_id, attempt_id, detail, now):
    session.store.transact([session.fence_check(fence),
                            Put({'pk': session.pk, 'sk': f'CONFLICT#{run_id}#{attempt_id}#{now}',
                                 'run_id': run_id, 'attempt_id': attempt_id, 'at': now, 'detail': detail})])


def _to(session, fence, run_id, state, now, **kw):
    job = session.job(run_id)
    if job['state'] == state:
        return job
    return session.transition(fence, run_id, state, now, **kw)


def finalize(session, fence, run_id, summary, now):
    """Apply a verified, read-back attempt archive to the durable job."""
    job = session.job(run_id)
    attempt_id = summary['attempt_id']
    seq = int(attempt_id.rsplit('.a', 1)[1])
    session.update_attempt(fence, run_id, seq, {'archive_key': summary['key'], 'archive_sha256': summary['sha256'],
                                                'result_status': summary['result_status'], 'verified_at': now})
    if job['state'] == 'SUCCEEDED':
        if job.get('canonical_sha256') == summary['sha256']:
            return 'duplicate'
        record_conflict(session, fence, run_id, attempt_id,
                        {'canonical_sha256': job.get('canonical_sha256'), 'late_sha256': summary['sha256']}, now)
        return 'late_or_conflicting_upload_retained'
    if job.get('current_attempt') != attempt_id:
        record_conflict(session, fence, run_id, attempt_id, {'current_attempt': job.get('current_attempt'),
                                                             'late_sha256': summary['sha256']}, now)
        return 'late_upload_retained'
    if job['state'] in ('CLAIMED', 'RUNNING'):
        job = _to(session, fence, run_id, 'RECONCILING' if job['state'] == 'CLAIMED' else 'UPLOADING', now)
    if job['state'] == 'RECONCILING':
        job = _to(session, fence, run_id, 'UPLOADING', now)
    if summary['result_status'] == 'completed':
        session.transition(fence, run_id, 'SUCCEEDED', now,
                           fields={'canonical_attempt': attempt_id, 'canonical_sha256': summary['sha256'],
                                   'canonical_key': summary['key'], 'finish_reason': summary['finish_reason'],
                                   'output_sha256': summary['output_sha256']},
                           expect={'current_attempt': attempt_id})
        outcome = 'succeeded'
    elif summary['deadline_interrupted']:
        session.transition(fence, run_id, 'INTERRUPTED_DEADLINE', now, note='killed at hard deadline')
        outcome = 'interrupted'
    else:
        session.transition(fence, run_id, 'FAILED_INFRA', now, fields={'failure': summary.get('error') or 'runtime_failed'},
                           note='runtime failure; diagnosis and versioned repair required before retry')
        outcome = 'failed_infra'
    slot = session.job(run_id).get('worker_slot')
    if slot:
        session.release_worker(fence, slot, run_id)
    return outcome


def reconcile(session, fence, objects, run_id, obs, now):
    """Advance one in-flight attempt from an observation. Returns an outcome tag."""
    job = session.job(run_id)
    if job['state'] not in ACTIVE:
        return 'not_active'
    attempt_id, seq = job['current_attempt'], job['attempt_seq']
    upload = obs.get('upload')
    if upload:
        try:
            summary = secure_copy(objects, session.sid, job, attempt_id, upload['key'], upload.get('sha256'))
        except ValueError as error:
            record_conflict(session, fence, run_id, attempt_id, {'error': str(error), 'key': upload['key']}, now)
            _to(session, fence, run_id, 'RECONCILING', now, note='upload failed verification')
            return 'upload_rejected'
        if summary is not None:
            return finalize(session, fence, run_id, dict(summary, attempt_id=attempt_id), now)
    if obs.get('process') == 'alive':
        if job['state'] in ('CLAIMED', 'RECONCILING'):
            _to(session, fence, run_id, 'RUNNING', now, note='exact process identity confirmed live')
        session.update_attempt(fence, run_id, seq, {'heartbeat_at': now, 'status': 'RUNNING'})
        return 'live'
    if obs.get('pod') == 'absent':
        _to(session, fence, run_id, 'RECONCILING', now)
        session.transition(fence, run_id, 'FAILED_INFRA', now, fields={'failure': 'pod_absent'},
                           note='exact pod verified absent; no verified result')
        session.release_worker(fence, job['worker_slot'], run_id)
        return 'failed_pod_absent'
    if obs.get('host') == 'reachable' and obs.get('process') == 'dead':
        if obs.get('reservation') is False:
            att = session.attempt(run_id, seq)
            if not att.get('started'):
                _to(session, fence, run_id, 'RECONCILING', now)
                session.transition(fence, run_id, 'PENDING', now, fields={'released_unstarted': attempt_id},
                                   note='host fenced and shows no reservation; attempt never started')
                session.release_worker(fence, job['worker_slot'], run_id)
                return 'released_unstarted'
        if obs.get('finished'):
            _to(session, fence, run_id, 'RECONCILING', now, note='finished on host; awaiting verified upload')
            return 'awaiting_upload'
        _to(session, fence, run_id, 'RECONCILING', now)
        session.transition(fence, run_id, 'FAILED_INFRA', now, fields={'failure': 'process_died_without_result'},
                           note='exact process confirmed dead without result')
        session.release_worker(fence, job['worker_slot'], run_id)
        return 'failed_process_dead'
    _to(session, fence, run_id, 'RECONCILING', now, note='ambiguous: liveness unknown; no dispatch')
    return 'ambiguous'


def retry(session, fence, run_id, repair_id, now):
    """Re-queue an infrastructure failure once, bound to a recorded repair."""
    if not repair_id:
        raise ValueError('A diagnosed, versioned repair id is required before retry')
    job = session.job(run_id)
    if job['state'] != 'FAILED_INFRA':
        raise InvalidTransition(f'{run_id} is {job["state"]}, not FAILED_INFRA')
    if job['infra_retries'] >= MAX_INFRA_RETRIES:
        session.transition(fence, run_id, 'BLOCKED', now, note='infrastructure retry exhausted')
        return 'blocked'
    session.transition(fence, run_id, 'PENDING', now,
                       fields={'infra_retries': job['infra_retries'] + 1, 'repair_id': repair_id},
                       expect={'infra_retries': job['infra_retries']}, note=f'retry after repair {repair_id}')
    return 'requeued'
