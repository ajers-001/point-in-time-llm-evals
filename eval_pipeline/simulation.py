"""Deterministic simulated GPU hosts for acceptance tests and dry runs.

FakeHosts follows the host_agent command contract (fenced epochs, exclusive
reservations, autonomous completion and upload) using a shared virtual
clock. Hosts keep running and uploading while the controller is stopped;
a deleted pod takes its host with it. No real compute, network or cloud.
"""
import hashlib
import json

from .artifacts import build_attempt_archive, sha256
from .controller import HostUnreachable


class Clock:
    def __init__(self, t=0):
        self.t = t

    def __call__(self):
        return self.t


class FakeHosts:
    def __init__(self, clock, provider, objects, session_id, queue, bootstrap_s=1800, duration_s=270):
        self.clock, self.provider, self.objects, self.sid = clock, provider, objects, session_id
        self.runs = {r['run_id']: r for r in queue['runs']}
        self.bootstrap_s, self.duration_s = bootstrap_s, duration_s
        self.hosts = {}
        self.unreachable, self.behaviour, self.durations = set(), {}, {}
        self.bootstrap_fail = set()
        self.readiness = {'config_sha256': 'r' * 64, 'input_sha256': 'i' * 64}
        self.starts = []

    def endpoint(self, info):
        return {'host': info['publicIp'], 'port': info['portMappings']['22']} if info else None

    def _host(self, pod):
        pod_id = pod['pod_id']
        if pod_id not in self.provider.pods or pod_id in self.unreachable:
            raise HostUnreachable(f'ssh to {pod_id}: timed out')
        return self.hosts[pod_id]

    def provision(self, pod, endpoint, spec, epoch):
        if pod['pod_id'] not in self.provider.pods or pod['pod_id'] in self.unreachable:
            raise HostUnreachable('ssh: connection refused')
        self.hosts.setdefault(pod['pod_id'], {'max_epoch': epoch, 'spec': spec, 'ready_at': self.clock() + self.bootstrap_s,
                                              'attempts': {}, 'slot': 0})
        return {'started': ['backup', 'bootstrap', 'expiry']}

    def advance(self):
        """Hosts act on their own: finish attempts, upload, stop at inference end."""
        now = self.clock()
        for pod_id, h in self.hosts.items():
            if pod_id not in self.provider.pods:
                continue
            for aid, a in h['attempts'].items():
                if a['finished']:
                    continue
                behaviour = a['behaviour']
                end = h['spec']['inference_end']
                if behaviour == 'die' and now >= a['start'] + 60:
                    a.update(finished=None, dead=True)
                    continue
                if now >= end and not a.get('dead'):
                    self._finish(pod_id, h, aid, a, 'runtime_failed', interrupted=True)
                elif behaviour != 'hang' and now >= a['start'] + a['duration']:
                    self._finish(pod_id, h, aid, a, 'completed' if behaviour == 'complete' else 'runtime_failed')

    def _finish(self, pod_id, h, aid, a, status, interrupted=False):
        rid = a['run_id']
        job = self.runs.get(rid) or {'run_id': rid, **self.readiness}
        text = f'simulated answer {rid}'.encode()
        result = {'run_id': rid, 'config_sha256': job['config_sha256'], 'input_sha256': job['input_sha256'],
                  'status': status, 'engine_reused': False, 'finish_reason': 'stop' if status == 'completed' else None}
        files = {f'output/{rid}/gpu/result.json': b''}
        if status == 'completed':
            result['output_sha256'] = sha256(text)
            files[f'output/{rid}/gpu/output.txt'] = text
        files[f'output/{rid}/gpu/result.json'] = json.dumps(result).encode()
        body = build_attempt_archive(files, {'session_id': self.sid, 'run_id': rid, 'attempt_id': aid,
                                             'deadline_interrupted': interrupted, 'host': {'pod_id': pod_id}})
        h['slot'] += 1
        key = f'sessions/{self.sid}/hosts/{pod_id}/slot-{h["slot"]:04d}.tar.gz'
        self.objects.put_new(key, body)
        a.update(finished={'exit_code': 0 if status == 'completed' else 1, 'deadline_interrupted': interrupted},
                 upload={'key': key, 'sha256': sha256(body)})

    def call(self, pod, endpoint, cmd, epoch, **kw):
        h = self._host(pod)
        if epoch < h['max_epoch']:
            return {'error': 'stale controller epoch', 'max_epoch': h['max_epoch']}
        h['max_epoch'] = epoch
        now = self.clock()
        if cmd == 'status':
            phase = 'failed' if pod['pod_id'] in self.bootstrap_fail else ('ready' if now >= h['ready_at'] else 'downloading')
            return {'bootstrap': {'phase': phase}, 'slots_remaining': 200}
        if cmd == 'start':
            aid = kw['attempt_id']
            if h['ready_at'] > now:
                return {'error': 'host not ready'}
            if any(not a['finished'] and not a.get('dead') for a in h['attempts'].values()):
                return {'error': 'another attempt is active'}
            expected = (self.runs.get(kw['run_id']) or self.readiness)['config_sha256']
            if kw['config_sha256'] != expected:
                return {'error': 'config hash differs from controller record'}
            if aid in h['attempts']:
                return {'error': 'attempt already reserved', 'reservation': True}
            h['attempts'][aid] = {'run_id': kw['run_id'], 'start': now, 'finished': None, 'upload': None,
                                  'duration': self.durations.get(kw['run_id'], self.duration_s),
                                  'behaviour': self.behaviour.get(kw['run_id'], 'complete'),
                                  'pid': 1000 + len(h['attempts'])}
            self.starts.append((pod['pod_id'], kw['run_id'], aid, now))
            return {'started': True, 'reservation': True, 'pid': h['attempts'][aid]['pid'], 'start': str(now)}
        if cmd == 'inspect':
            a = h['attempts'].get(kw['attempt_id'])
            if a is None:
                return {'host': 'reachable', 'reservation': False, 'process': 'dead'}
            live = not a['finished'] and not a.get('dead')
            return {'host': 'reachable', 'reservation': True, 'process': 'alive' if live else 'dead',
                    'finished': a['finished'], 'upload': a['upload']}
        raise ValueError(cmd)

    def replace_urls(self, pod, endpoint, epoch, start):
        self._host(pod)
        return {'replaced_from_index': start}

    def final_backup(self, pod, endpoint, epoch):
        self._host(pod)
        return {'status': 'uploaded'}


def readiness_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()
