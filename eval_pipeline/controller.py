"""Cloud coordinator loop: one fenced instance drives two single-GPU workers.

Runs as the ECS Fargate service (desired count one). All authority lives in
durable state; this process may die at any point and its replacement
reconciles before acting. Per tick it:

1. renews its fenced lease (exits if superseded),
2. decides whether new work may start (deadline reserve, budget, stop request),
3. reconciles unresolved allocation intents by exact name,
4. advances each worker slot: allocate -> provision -> readiness -> claim/dispatch
   -> inspect/reconcile, and deletes idle or failed pods it owns,
5. renews host upload slots and exports a status document.

It never retries a completed request, never dispatches on a lost heartbeat,
and never allocates beyond two GPUs or the replacement allowance.
"""
import json
import tarfile
import zlib

from . import allocator
from .budget import Clock, Ledger
from .provider import ProviderError
from .reconcile import mark_stale, reconcile, retry
from .state import ACTIVE, InvalidTransition, LeaseHeld, LeaseLost
from .store import ConditionFailed, MISSING, Put, Update

READINESS_PREPARED = 'prepared/smoke-prewarm-v2'
READINESS_RUN = 'S-smoke-combined'
RESEARCH_TIMEOUT_S = 2400
READINESS_TIMEOUT_S = 1800
UNREACHABLE_GRACE_S = 600
NO_ENDPOINT_TIMEOUT_S = 1800  # a billed pod that never exposes SSH is released, not waited on
SLOT_LOW_WATER = 40
URL_VERSION = 2  # 2: presigned against the regional S3 endpoint
PERMANENT_REFUSALS = ('config hash', 'not in the shipped queue')


class HostUnreachable(Exception):
    """SSH or network failure: the host state is unknown, not failed."""


class Controller:
    def __init__(self, session, provider, objects, hosts, clock, settings, owner):
        self.s, self.provider, self.objects, self.hosts = session, provider, objects, hosts
        self.now, self.cfg, self.owner = clock, settings, owner
        meta = session.meta()
        self.ledger = Ledger(meta['budget'], deadline=meta['gpu_end'], cleanup_margin_s=settings.get('cleanup_margin_s', 900))
        self.clock = Clock(meta['inference_end'], meta.get('stop_new_work_reserve_s', 420),
                           meta.get('final_retrieval_reserve_s', 120), meta.get('research_timeout_s', RESEARCH_TIMEOUT_S))
        self.fence = None
        self.log = []

    # ----- lifecycle ---------------------------------------------------------
    def start(self):
        self.fence = self.s.acquire(self.owner, self.now(), ttl=self.cfg.get('lease_ttl_s', 90))
        self.note('lease_acquired', epoch=self.fence.epoch)
        mark_stale(self.s, self.fence, self.now(), restarted=True)
        meta = self.s.meta()
        if meta['status'] == 'READY':
            self._meta({'status': 'RUNNING', 'started_at': self.now()}, expect={'status': 'READY'})
        return self.fence

    def repair_urls(self, slot, worker, pod):
        """Hosts provisioned before URL_VERSION got global-endpoint URLs (S3 307s them); reissue once."""
        if worker.get('urls_version', 1) >= URL_VERSION:
            return None
        result = self.hosts.replace_urls(pod, worker['endpoint'], self.fence.epoch, start=1000 * URL_VERSION)
        self.s.set_worker(self.fence, slot, {'urls_version': URL_VERSION})
        self.note('upload_urls_reissued', slot=slot, result=result)
        return result

    def note_host(self, slot, status):
        """Log host setup/backup state whenever it changes (diagnosis without host access)."""
        backup = status.get('backup_latest') or {}
        boot = status.get('bootstrap') or {}
        key = (boot.get('phase'), boot.get('error_type'), backup.get('status'), backup.get('error'))
        cache = self.__dict__.setdefault('_host_seen', {})
        if cache.get(slot) != key:
            cache[slot] = key
            self.note('host_status', slot=slot, phase=boot.get('phase'), boot_error=boot.get('error'),
                      boot_error_type=boot.get('error_type'), daemons=status.get('daemons'),
                      backup={k: backup.get(k) for k in ('status', 'error', 'kind', 'key', 'at')},
                      slots_remaining=status.get('slots_remaining'))

    def note(self, event, **detail):
        entry = dict(detail, event=event, at=self.now())
        self.log.append(entry)
        if self.cfg.get('echo'):
            print(json.dumps(entry, default=str), flush=True)

    def _meta(self, fields, expect=None):
        meta = self.s.meta()
        self.s.store.transact([self.s.fence_check(self.fence),
                               Update(self.s.pk, 'META', meta['version'], fields, expect or {})])

    def tick(self):
        now = self.now()
        try:
            self.s.renew(self.fence, now, ttl=self.cfg.get('lease_ttl_s', 90))
        except ConditionFailed as error:
            raise LeaseLost('lease renewal lost to a newer controller') from error
        meta = self.s.meta()
        if meta['status'] in ('CLOSED',):
            return 'closed'
        self.apply_operator_requests(now)
        meta = self.s.meta()
        stopping = self.stopping(meta, now)
        try:
            allocator.reconcile_intents(self.s, self.fence, self.provider, now)
        except ProviderError as error:
            self.note('intent_reconcile_deferred', error=str(error)[:200])
        pending = [j for j in self.s.jobs() if j['state'] == 'PENDING']
        for slot in (1, 2):
            try:
                self.advance(slot, now, stopping, pending)
            except LeaseLost:
                raise
            except (ConditionFailed, InvalidTransition, ProviderError, HostUnreachable) as error:
                # Transient or raced: nothing is assumed; the next tick re-reads durable state.
                self.s.require_fence(self.fence)
                self.note('slot_retry_next_tick', slot=slot, error=f'{type(error).__name__}: {error}'[:300])
        self.export_status(now)
        return self.maybe_close(now, stopping)

    def stopping(self, meta, now):
        if meta['status'] in ('TERMINATE_REQUESTED', 'STOPPING'):
            return 'requested'
        if not self.clock.can_start(now):
            return 'deadline_reserve'
        pods = allocator.pod_records(self.s)
        credit = meta.get('last_credit_usd')
        if credit is not None and self.ledger.must_stop(pods, credit, now, meta.get('other_spend_usd', '0')):
            return 'budget'
        return None

    # ----- per-slot state machine -------------------------------------------
    def advance(self, slot, now, stopping, pending):
        worker = self.s.worker(slot)
        status = worker['status']
        if worker.get('endpoint') and worker.get('pod_name') and worker.get('urls_version', 1) < URL_VERSION:
            self.repair_urls(slot, worker, self.pod_for(worker))
            worker = self.s.worker(slot)
        if worker.get('active_run'):
            return self.watch_job(slot, worker, now)
        if status == 'EMPTY':
            if not stopping and pending and self.slot_wanted(slot):
                return self.allocate(slot, now)
            return None
        if status == 'ALLOCATING':
            return None  # bound or retired by reconcile_intents
        pod = self.pod_for(worker)
        if pod is None:
            return None
        if status in ('BOOTSTRAPPING', 'PROVISIONING', 'READINESS', 'READY') and self.pod_lost(slot, worker, pod, now):
            return None
        if status == 'BOOTSTRAPPING':
            return self.provision(slot, worker, pod, now)
        if status == 'PROVISIONING':
            return self.check_bootstrap(slot, worker, pod, now)
        if status == 'READINESS':
            return self.watch_readiness(slot, worker, pod, now)
        if status == 'READY':
            if worker.get('needs_operator'):
                return self.release_pod(slot, pod, now, worker['needs_operator'], operator=True)
            if stopping or not pending:
                return self.release_pod(slot, pod, now, 'no further admissible work' if not stopping else f'stop: {stopping}')
            return self.dispatch(slot, worker, pod, now)
        return None

    def slot_wanted(self, slot):
        """A slot whose pod failed needs an explicit operator replacement decision."""
        return not self.s.worker(slot).get('needs_operator')

    def pod_for(self, worker):
        name = worker.get('pod_name')
        return self.s.store.get(self.s.pk, 'POD#' + name) if name else None

    def allocate(self, slot, now):
        meta = self.s.meta()
        account = self.provider.account(self.cfg['gpu_type'])
        if account['autopay']:
            raise RuntimeError('Provider auto-pay must remain off')
        gpus = int(self.s.meta().get('gpu_count', 1))  # a pod bills every GPU it holds
        hourly = str(float(account['secure_price_usd']) * gpus + float(self.cfg['disk_hourly_usd']))
        self._meta({'last_credit_usd': account['balance_usd'], 'last_quote': {'hourly_total_usd': hourly, 'at': now,
                                                                              'stock': account.get('stock')}})
        replacement = bool(self.s.worker(slot).get('had_pod'))
        try:
            pod = allocator.reserve(self.s, self.fence, slot, {'hourly_total_usd': hourly}, self.ledger,
                                    account['balance_usd'], now, replacement=replacement)
        except RuntimeError as error:
            self.note('allocation_refused', slot=slot, reason=str(error)[:300])
            return None
        payload = self.cfg['payload_builder'](pod['name'], slot)
        record = payload.pop('_record', None)
        if record:  # e.g. the pinned host public key, stored before the create request
            allocator._set_pod(self.s, self.fence, pod['name'], record)
            pod = self.s.store.get(self.s.pk, 'POD#' + pod['name'])
        created = allocator.create(self.s, self.fence, self.provider, pod, payload, self.cfg['watchdog_sees'], now)
        self.s.set_worker(self.fence, slot, {'had_pod': True})
        self.note('allocated' if created else 'create_uncertain', slot=slot, name=pod['name'])
        return created

    def pod_lost(self, slot, worker, pod, now):
        """Detect a vanished pod; only verified absence frees the slot."""
        if not pod.get('pod_id'):
            return False
        try:
            remote = self.provider.get_pod(pod['pod_id'])
        except Exception:  # noqa: BLE001 - provider unreachable: unknown, keep waiting
            return False
        if remote is not None and remote.get('desiredStatus') not in ('EXITED', 'TERMINATED'):
            return False
        self.release_pod(slot, pod, now, 'pod vanished or exited', operator=False)
        return True

    def provision(self, slot, worker, pod, now):
        try:
            info = self.provider.get_pod(pod['pod_id'])
            endpoint = self.hosts.endpoint(info)
            if endpoint is None:
                started = pod.get('created_at') or pod.get('billing_started_at') or now
                if now - started > NO_ENDPOINT_TIMEOUT_S:
                    return self.release_pod(slot, pod, now, 'pod never exposed an SSH endpoint', operator=True)
                return None  # still starting
            spec = {'session_id': self.s.sid, 'pod_id': pod['pod_id'], 'inference_end': self.s.meta()['inference_end'],
                    'gpu_end': self.s.meta()['gpu_end'], 'bundle_sha256': self.cfg['bundle_sha256'],
                    'image': self.cfg.get('image'), 'gpu_type': self.cfg['gpu_type'],
                    'gpu_count': int(self.s.meta().get('gpu_count', 1))}
            result = self.hosts.provision(pod, endpoint, spec, self.fence.epoch)
        except HostUnreachable as error:
            self.note('provision_waiting', slot=slot, error=str(error)[:200])
            return None
        self.s.set_worker(self.fence, slot, {'status': 'PROVISIONING', 'endpoint': endpoint, 'provisioned_at': now,
                                             'urls_version': URL_VERSION})
        self.note('provisioned', slot=slot, result=result)
        return result

    def check_bootstrap(self, slot, worker, pod, now):
        try:
            status = self.hosts.call(pod, worker['endpoint'], 'status', epoch=self.fence.epoch)
        except HostUnreachable:
            return None
        phase = (status.get('bootstrap') or {}).get('phase')
        self.note_host(slot, status)
        if phase == 'ready':
            return self.begin_readiness(slot, worker, pod, now)
        if phase == 'failed':
            self.note('bootstrap_failed', slot=slot, detail=status.get('bootstrap'))
            return self.release_pod(slot, pod, now, 'bootstrap failed; evidence backed up', operator=True)
        return None

    def begin_readiness(self, slot, worker, pod, now):
        attempt_id = f'{READINESS_RUN}.{pod["pod_id"]}.r1'
        item = {'pk': self.s.pk, 'sk': f'RDY#{pod["pod_id"]}', 'pod_id': pod['pod_id'], 'slot': slot,
                'attempt_id': attempt_id, 'state': 'DISPATCHING', 'at': now}
        try:
            self.s.store.transact([self.s.fence_check(self.fence), Put(item)])
        except ConditionFailed:
            pass  # already dispatched by a previous controller: inspect, never re-dispatch
        rdy = self.s.store.get(self.s.pk, f'RDY#{pod["pod_id"]}')
        if rdy['state'] == 'DISPATCHING':
            try:
                self.hosts.call(pod, worker['endpoint'], 'start', epoch=self.fence.epoch, prepared=READINESS_PREPARED,
                                run_id=READINESS_RUN, attempt_id=attempt_id, tag=attempt_id_tag(attempt_id),
                                timeout=READINESS_TIMEOUT_S, config_sha256=self.cfg['readiness_config_sha256'])
            except HostUnreachable:
                pass  # reservation is idempotent and will be inspected
        self.s.set_worker(self.fence, slot, {'status': 'READINESS', 'readiness_attempt': attempt_id})
        return 'readiness_started'

    def watch_readiness(self, slot, worker, pod, now):
        attempt_id = worker['readiness_attempt']
        try:
            obs = self.hosts.call(pod, worker['endpoint'], 'inspect', epoch=self.fence.epoch, attempt_id=attempt_id)
        except HostUnreachable:
            return None
        if obs.get('process') == 'alive':
            return None
        if not obs.get('reservation'):
            return self.begin_readiness(slot, worker, pod, now)
        if not obs.get('upload'):
            return None
        from .artifacts import secure_copy
        job = {'run_id': READINESS_RUN, 'config_sha256': self.cfg['readiness_config_sha256'],
               'input_sha256': self.cfg.get('readiness_input_sha256')}
        try:
            summary = secure_copy(self.objects, self.s.sid, job, attempt_id, obs['upload']['key'], obs['upload']['sha256'])
        except ValueError as error:
            summary = {'result_status': 'rejected', 'error': str(error)}
        rdy = self.s.store.get(self.s.pk, f'RDY#{pod["pod_id"]}')
        passed = summary and summary.get('result_status') == 'completed'
        self.s.store.transact([self.s.fence_check(self.fence), Update(
            self.s.pk, f'RDY#{pod["pod_id"]}', rdy['version'],
            {'state': 'PASSED' if passed else 'FAILED', 'summary': json.loads(json.dumps(summary, default=str)),
             'finished_at': now})])
        if passed:
            self.s.set_worker(self.fence, slot, {'status': 'READY', 'ready_at': now})
            self.note('readiness_passed', slot=slot)
            return 'ready'
        self.note('readiness_failed', slot=slot, summary=summary)
        return self.release_pod(slot, pod, now, 'readiness failed', operator=True)

    def dispatch(self, slot, worker, pod, now):
        if not self.clock.can_start(now):
            return None
        job = self.s.claim(self.fence, slot, pod['pod_id'], now)
        if job is None:
            return None
        att = self.s.attempt(job['run_id'], job['attempt_seq'])
        try:
            result = self.hosts.call(pod, worker['endpoint'], 'start', epoch=self.fence.epoch, prepared=job['prepared'],
                                     run_id=job['run_id'], attempt_id=att['attempt_id'], tag=att['tag'],
                                     timeout=self.clock.timeout(now), config_sha256=job['config_sha256'])
        except HostUnreachable as error:
            self.s.transition(self.fence, job['run_id'], 'RECONCILING', now, note=f'dispatch ambiguous: {error}'[:300])
            return 'dispatch_ambiguous'
        if result.get('started'):
            self.s.update_attempt(self.fence, job['run_id'], job['attempt_seq'],
                                  {'started': True, 'pid': result['pid'], 'pid_start': str(result['start']),
                                   'heartbeat_at': now, 'status': 'RUNNING'})
            self.s.transition(self.fence, job['run_id'], 'RUNNING', now, note='exact process identity recorded')
            self.note('dispatched', slot=slot, run_id=job['run_id'], attempt_id=att['attempt_id'])
            return 'dispatched'
        # The host refused before reserving; reconcile proves it never started.
        self.s.transition(self.fence, job['run_id'], 'RECONCILING', now, note=f'host refused: {result}'[:300])
        if any(marker in str(result.get('error')) for marker in PERMANENT_REFUSALS):
            self.s.transition(self.fence, job['run_id'], 'BLOCKED', now,
                              note=f'admission mismatch, not retried: {result.get("error")}'[:300])
            self.s.release_worker(self.fence, slot, job['run_id'])
            return 'blocked'
        return 'refused'

    def watch_job(self, slot, worker, now):
        job = self.s.job(worker['active_run'])
        if job['state'] not in ACTIVE:
            self.s.release_worker(self.fence, slot, job['run_id'])
            return None
        pod = self.pod_for(worker)
        obs = self.observe(slot, worker, pod, job, now)
        outcome = reconcile(self.s, self.fence, self.objects, job['run_id'], obs, now)
        if outcome in ('succeeded', 'interrupted', 'failed_infra'):
            att = self.s.attempt(job['run_id'], job['attempt_seq'])
            self.clock.observe(now - att['claimed_at'])
        if outcome not in ('live', 'ambiguous', 'awaiting_upload'):
            self.note('job_' + outcome, slot=slot, run_id=job['run_id'])
        if outcome == 'failed_pod_absent':
            if pod is not None:
                self.release_pod(slot, pod, now, 'pod absent during attempt', operator=False)
            # Diagnosed provider loss; the replacement pod is the versioned repair.
            retry(self.s, self.fence, job['run_id'], f'replacement-after-pod-loss:{pod["name"] if pod else "unknown"}', now)
        elif outcome in ('failed_process_dead', 'failed_infra'):
            failures = self.s.worker(slot).get('consecutive_failures', 0) + 1
            fields = {'consecutive_failures': failures}
            if failures >= 2:
                fields['needs_operator'] = 'two consecutive runtime failures on this host'
                self.note('slot_paused_for_diagnosis', slot=slot, run_id=job['run_id'])
            self.s.set_worker(self.fence, slot, fields)
        elif outcome in ('succeeded', 'interrupted'):
            if self.s.worker(slot).get('consecutive_failures'):
                self.s.set_worker(self.fence, slot, {'consecutive_failures': 0})
        return outcome

    def observe(self, slot, worker, pod, job, now):
        obs = {'pod': 'unknown'}
        if pod is not None and pod.get('pod_id'):
            try:
                obs['pod'] = 'present' if self.provider.get_pod(pod['pod_id']) else 'absent'
            except Exception:  # noqa: BLE001
                obs['pod'] = 'unknown'
        if obs['pod'] != 'absent' and worker.get('endpoint'):
            try:
                obs.update(self.hosts.call(pod, worker['endpoint'], 'inspect', epoch=self.fence.epoch,
                                           attempt_id=job['current_attempt']))
            except HostUnreachable:
                obs.update(host='unreachable', process='unknown')
        if not obs.get('upload'):
            found = self.find_upload(pod, job['current_attempt'])
            if found:
                obs['upload'] = found
        return obs

    def find_upload(self, pod, attempt_id):
        """Scan the host's durable upload prefix (works when the host is unreachable)."""
        if pod is None or not pod.get('pod_id'):
            return None
        from .artifacts import safe_members
        for key in self.objects.list(f'sessions/{self.s.sid}/hosts/{pod["pod_id"]}/'):
            cache = self.s.store.get(self.s.pk, 'UPL#' + key)
            if cache is None:
                try:
                    members = safe_members(self.objects.get(key))
                    manifest = json.loads(members.get('ATTEMPT_MANIFEST.json', b'{}'))
                except (ValueError, TypeError, tarfile.TarError, EOFError, OSError, zlib.error):
                    manifest = {}  # oversize or not an archive (bulk outputs share the slots): never an attempt
                cache = {'pk': self.s.pk, 'sk': 'UPL#' + key, 'key': key, 'attempt_id': manifest.get('attempt_id')}
                try:
                    self.s.store.transact([Put(cache)])
                except ConditionFailed:
                    pass
            if cache.get('attempt_id') == attempt_id:
                return {'key': key}
        return None

    def release_pod(self, slot, pod, now, reason, operator=False):
        """Delete one owned pod after a final host backup attempt; verify absence."""
        worker = self.s.worker(slot)
        if worker.get('endpoint') and worker['status'] in ('PROVISIONING', 'READINESS', 'READY'):
            try:
                self.hosts.final_backup(pod, worker['endpoint'], self.fence.epoch)
            except HostUnreachable:
                self.note('final_backup_unavailable', slot=slot)
        result = allocator.delete_exact(self.s, self.fence, self.provider, pod['name'], now, reason)
        if result == 'absent':
            fields = {'endpoint': MISSING, 'readiness_attempt': MISSING}
            if operator:
                fields['needs_operator'] = reason
            self.s.set_worker(self.fence, slot, fields)
        self.note('pod_' + result, slot=slot, reason=reason)
        return result

    # ----- operator requests (written by the CLI, applied under this fence) -----
    def apply_operator_requests(self, now):
        for op in self.s.store.query(self.s.pk, 'OPS#'):
            if op.get('status') != 'NEW':
                continue
            try:
                result = self._apply(op, now)
            except Exception as error:  # noqa: BLE001 - reported back to the operator
                result = f'rejected: {type(error).__name__}: {error}'[:300]
            self.s.store.transact([self.s.fence_check(self.fence), Update(
                self.s.pk, op['sk'], op['version'], {'status': 'APPLIED', 'result': result, 'applied_at': now},
                expect={'status': 'NEW'})])
            self.note('operator_request', action=op['action'], result=result)

    def _apply(self, op, now):
        if not op.get('repair_id') and op['action'] in ('retry', 'resume_slot'):
            raise ValueError('a recorded, versioned repair id is required')
        if op['action'] == 'retry':
            return retry(self.s, self.fence, op['run_id'], op['repair_id'], now)
        if op['action'] == 'resume_slot':
            worker = self.s.worker(int(op['slot']))
            if worker['status'] != 'EMPTY' or worker.get('active_run'):
                raise ValueError('slot must be EMPTY (pod verified absent) before resuming')
            self.s.set_worker(self.fence, int(op['slot']), {'needs_operator': MISSING, 'consecutive_failures': 0,
                                                            'resumed_with_repair': op['repair_id']})
            return 'resumed'
        if op['action'] == 'stop':
            meta = self.s.meta()
            if meta['status'] in ('READY', 'RUNNING'):
                self._meta({'status': 'STOPPING', 'stop_reason': op.get('reason', 'operator')},
                           expect={'status': meta['status']})
            return 'stopping'
        raise ValueError('unknown action')

    # ----- housekeeping --------------------------------------------------------
    def export_status(self, now):
        jobs = self.s.jobs()
        counts = {}
        for j in jobs:
            counts[j['state']] = counts.get(j['state'], 0) + 1
        pods = allocator.pod_records(self.s)
        meta = self.s.meta()
        status = {'session_id': self.s.sid, 'at': now, 'epoch': self.fence.epoch, 'meta_status': meta['status'],
                  'job_counts': counts, 'workers': [self.s.worker(s) for s in (1, 2)],
                  'pods': [{k: p.get(k) for k in ('name', 'slot', 'status', 'pod_id', 'hourly_usd',
                                                  'billing_started_at', 'billing_ended_at')} for p in pods],
                  'spent_usd': str(self.ledger.spent(pods, meta.get('other_spend_usd', '0'), now)),
                  'inference_end': meta['inference_end'], 'gpu_end': meta['gpu_end'],
                  'start_reserve_s': self.clock.reserve(), 'scientific_validity_claim': False}
        self.last_status = status
        writer = self.cfg.get('status_writer')
        if writer:
            writer(status)
        return status

    def maybe_close(self, now, stopping):
        jobs = self.s.jobs()
        busy = [j for j in jobs if j['state'] in ACTIVE]
        pending = [j for j in jobs if j['state'] == 'PENDING']
        open_pods = allocator.open_pods(self.s)
        if busy:
            return 'running'
        if pending and not stopping and any(not self.s.worker(s).get('needs_operator') for s in (1, 2)):
            return 'running'
        if open_pods:
            return 'draining'
        meta = self.s.meta()
        if meta['status'] != 'CLOSED':
            self._meta({'status': 'CLOSED', 'closed_at': now, 'close_reason': stopping or 'queue drained'})
            self.note('session_closed', reason=stopping or 'queue drained')
        return 'closed'


def attempt_id_tag(attempt_id):
    import hashlib
    return hashlib.sha256(attempt_id.encode()).hexdigest()[:12]


def run_forever(controller, sleep, max_ticks=None):
    """Production loop; a superseded instance exits so ECS can replace it cleanly."""
    while True:  # a replaced instance waits for the previous lease to expire
        try:
            controller.start()
            break
        except LeaseHeld as error:
            controller.note('waiting_for_lease', error=str(error)[:200])
            sleep(15)
    ticks = 0
    while True:
        try:
            state = controller.tick()
        except LeaseLost:
            return 'superseded'
        if state == 'closed':
            controller.cfg.get('on_closed', lambda: None)()
            return 'closed'
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return state
        sleep(controller.cfg.get('tick_s', 15))
