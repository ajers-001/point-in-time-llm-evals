"""Runpod pod API (exact identities only) and a fault-injecting fake.

Credentials come from Secrets Manager at runtime and are never logged,
written to durable state or passed to hosts. Every create is a single
attempt: an uncertain response is reconciled by exact unique name.
"""
import json
import re
import threading
import urllib.error
import urllib.request

REST = 'https://rest.runpod.io/v1'
GRAPHQL = 'https://api.runpod.io/graphql'
POD_ID = re.compile(r'[a-zA-Z0-9_-]{5,64}')
ACCOUNT_QUERY = ('query { myself { clientBalance currentSpendPerHr isAutoPayEnabled } '
                 'gpuTypes(input: {id: "%s"}) { id memoryInGb securePrice '
                 'lowestPrice(input: {gpuCount: 1}) { stockStatus uninterruptablePrice } } }')


class ProviderError(Exception):
    """Provider call failed; message never contains credentials or URLs with keys."""


class CreateUncertain(ProviderError):
    """The create request may or may not have allocated a pod."""


def _reason(error):
    """A short, sanitised excerpt of the provider's error message (no headers; long token-like strings redacted)."""
    import re
    try:
        raw = error.read(4096).decode('utf-8', 'replace')
    except Exception:  # noqa: BLE001
        return ''
    try:
        body = json.loads(raw)
        if isinstance(body, dict):
            raw = str(body.get('error') or body.get('message') or body.get('errors') or raw)
    except ValueError:
        pass
    text = re.sub(r'[A-Za-z0-9_\-]{24,}', '<redacted>', raw)
    text = re.sub(r"[^A-Za-z0-9 .,:;'()/_<>%=-]", ' ', text)
    text = ' '.join(text.split())[:240]
    return f' ({text})' if text else ''


class RunpodClient:
    def __init__(self, key, timeout=30):
        self._key, self.timeout = key, timeout

    def _call(self, method, url, payload=None, auth=True):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'pit-research/1.0'}
        if auth:
            headers['Authorization'] = 'Bearer ' + self._key
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            raise ProviderError(f'{method} {url.split("?")[0]} -> HTTP {error.code}{_reason(error)}') from None
        except Exception as error:  # noqa: BLE001 - redact everything but the type
            raise ProviderError(f'{method} {url.split("?")[0]} failed: {type(error).__name__}') from None

    def list_pods(self):
        pods = self._call('GET', REST + '/pods')
        if not isinstance(pods, list):
            raise ProviderError('Unexpected pod inventory response')
        return pods

    def get_pod(self, pod_id):
        if not POD_ID.fullmatch(pod_id):
            raise ValueError('Invalid pod id')
        try:
            return self._call('GET', f'{REST}/pods/{pod_id}')
        except ProviderError as error:
            if 'HTTP 404' in str(error):
                return None
            raise

    def create_pod(self, payload):
        try:
            pod = self._call('POST', REST + '/pods', payload)
        except ProviderError as error:
            raise CreateUncertain(str(error)) from None
        if not isinstance(pod, dict) or not POD_ID.fullmatch(str(pod.get('id', ''))):
            raise CreateUncertain('Create response lacked a valid pod id')
        return pod

    def delete_pod(self, pod_id):
        if not POD_ID.fullmatch(pod_id):
            raise ValueError('Invalid pod id')
        try:
            self._call('DELETE', f'{REST}/pods/{pod_id}')
        except ProviderError as error:
            if 'HTTP 404' not in str(error):
                raise

    def account(self, gpu_type):
        if not re.fullmatch(r'[A-Za-z0-9 ]{3,64}', gpu_type):
            raise ValueError('Invalid GPU type')
        body = self._call('POST', GRAPHQL, {'query': ACCOUNT_QUERY % gpu_type})
        if not isinstance(body, dict) or body.get('errors') or 'data' not in body:
            raise ProviderError('Account query rejected')
        me = body['data']['myself']
        gpu = next((g for g in body['data']['gpuTypes'] if g['id'] == gpu_type), None)
        if gpu is None:
            raise ProviderError('GPU type not offered')
        return {'balance_usd': str(me['clientBalance']), 'spend_per_hr_usd': str(me['currentSpendPerHr']),
                'autopay': bool(me['isAutoPayEnabled']), 'gpu_type': gpu_type,
                'secure_price_usd': str(gpu['securePrice']),
                'stock': (gpu.get('lowestPrice') or {}).get('stockStatus')}


class FakeRunpod:
    """In-memory provider. ``fail_create_after_alloc`` simulates a lost response."""

    def __init__(self, balance='65', price='3.49', autopay=False):
        self.pods, self.calls, self._n, self._lock = {}, [], 0, threading.Lock()
        self.balance, self.price, self.autopay = balance, price, autopay
        self.fail_create_after_alloc = False
        self.fail_create_before_alloc = False
        self.unreachable = False

    def _check(self, op):
        self.calls.append(op)
        if self.unreachable:
            raise ProviderError(f'{op} failed: TimeoutError')

    def list_pods(self):
        self._check('list')
        with self._lock:
            return [dict(p) for p in self.pods.values()]

    def get_pod(self, pod_id):
        self._check('get')
        with self._lock:
            p = self.pods.get(pod_id)
            return dict(p) if p else None

    def create_pod(self, payload):
        self._check('create')
        if self.fail_create_before_alloc:
            raise CreateUncertain('POST failed: TimeoutError')
        with self._lock:
            self._n += 1
            pod = {'id': f'fakepod{self._n:04d}', 'name': payload['name'], 'costPerHr': float(self.price),
                   'desiredStatus': 'RUNNING', 'publicIp': f'203.0.113.{self._n}',
                   'portMappings': {'22': 40000 + self._n}}
            self.pods[pod['id']] = pod
        if self.fail_create_after_alloc:
            raise CreateUncertain('POST failed: RemoteDisconnected')
        return dict(pod)

    def delete_pod(self, pod_id):
        self._check('delete')
        with self._lock:
            self.pods.pop(pod_id, None)

    def account(self, gpu_type):
        self._check('account')
        return {'balance_usd': self.balance, 'spend_per_hr_usd': '0', 'autopay': self.autopay,
                'gpu_type': gpu_type, 'secure_price_usd': self.price, 'stock': 'High'}
