"""Independent expiry watchdog (Lambda, invoked every minute by EventBridge Scheduler).

It works without the controller or any PC: it reads exact owned pod
identities from DynamoDB and deletes ONLY those pods (by exact id, or by
exact unique name for unresolved intents) once the session must stop:
GPU end time reached, budget backstop reached, or termination requested.
It never creates compute, never touches unrelated pods, and records its
observations in separate WATCH# items so it never races controller versions.
After every owned pod is verified absent past the stop point it scales the
controller service to zero so ECS cannot respawn it.
"""
import json
import os
import time

from .budget import D, pod_cost
from .store import Put

TABLE = 'pit-2gpu-state'
RUNPOD_SECRET_ID = 'pit-2gpu/runpod-api-key'
WATCHED = ('INTENT', 'CREATE_UNCERTAIN', 'CREATED', 'DELETING', 'DUPLICATE', 'NOT_FOUND')
BACKSTOP_LOOKAHEAD_S = 300


def stop_reason(meta, pods, now):
    if meta.get('status') in ('TERMINATE_REQUESTED', 'CLOSING', 'CLOSED'):
        return 'termination_requested'
    if now >= meta['gpu_end']:
        return 'gpu_end_reached'
    budget = meta.get('budget') or {}
    if budget:
        live = [p for p in pods if p['status'] in WATCHED and not p.get('billing_ended_at')]
        spent = sum((pod_cost(p, now) for p in pods), D(0)) + D(meta.get('other_spend_usd', '0'))
        ahead = sum((D(p['hourly_usd']) * BACKSTOP_LOOKAHEAD_S / 3600 for p in live), D(0))
        if spent + ahead >= D(budget['session_ceiling_usd']) - D(budget['retrieval_reserve_usd']):
            return 'budget_backstop'
    return None


def check(store, session_id, provider, now, scale_down=None, dry_run=False, quote_gpu=None):
    pk = 'SESSION#' + session_id
    meta = store.get(pk, 'META')
    if meta is None:
        return {'status': 'no_session'}
    pods = [p for p in store.query(pk, 'POD#')]
    watched = [p for p in pods if p['status'] in WATCHED]
    reason = stop_reason(meta, pods, now)
    names = sorted(p['name'] for p in watched)
    if reason is None or dry_run:
        out = {'status': 'before_stop' if reason is None else 'would_stop', 'reason': reason, 'watched': names}
        if quote_gpu:
            try:  # read-only provider check used before any launch
                out['account'] = provider.account(quote_gpu)
                out['owned_pods_in_inventory'] = sorted(
                    x['id'] for x in provider.list_pods() if x.get('name', '').startswith(f'pit-2gpu-{session_id}-'))
            except Exception as error:  # noqa: BLE001 - reported, never fatal to the watchdog
                out['quote_error'] = str(error)[:300]
        return out
    inventory = provider.list_pods()
    ids = {p['pod_id'] for p in watched if p.get('pod_id')} | {i for p in watched for i in p.get('pod_ids', [])}
    owned = [x for x in inventory if x.get('id') in ids or x.get('name') in names]
    for pod in owned:
        if not pod.get('name', '').startswith(f'pit-2gpu-{session_id}-'):
            raise RuntimeError('Owned identity has an unexpected name; refusing to delete')
        provider.delete_pod(pod['id'])
    remaining = [x for x in provider.list_pods() if x.get('id') in ids or x.get('name') in names]
    status = 'deleting' if remaining else 'all_absent'
    store.transact([Put({'pk': pk, 'sk': f'WATCH#{now:012d}', 'at': now, 'reason': reason, 'status': status,
                         'deleted': sorted(x['id'] for x in owned), 'remaining': sorted(x['id'] for x in remaining)},
                        if_absent=False)])
    if not remaining and scale_down is not None:
        scale_down()
    return {'status': status, 'reason': reason, 'deleted': sorted(x['id'] for x in owned),
            'remaining': sorted(x['id'] for x in remaining)}


def handler(event, context):
    """Configuration is derived from the function name; no environment variables
    (so nothing for Lambda to decrypt with an account KMS key)."""
    import boto3
    from .provider import RunpodClient
    from .store import DynamoStore
    name = context.function_name
    if not (name.startswith('pit-2gpu-') and name.endswith('-watchdog')):
        raise RuntimeError('unexpected watchdog function name')
    session_id = name[len('pit-2gpu-'):-len('-watchdog')]
    region = os.environ['AWS_REGION']
    store = DynamoStore(TABLE, boto3.client('dynamodb', region_name=region))
    key = boto3.client('secretsmanager', region_name=region).get_secret_value(
        SecretId=RUNPOD_SECRET_ID)['SecretString'].strip()

    def scale_down():
        boto3.client('ecs', region_name=region).update_service(
            cluster=f'pit-2gpu-{session_id}', service=f'pit-2gpu-{session_id}-controller', desiredCount=0)
    event = event or {}
    result = check(store, session_id, RunpodClient(key), int(time.time()),
                   scale_down=scale_down, dry_run=bool(event.get('dry_run')), quote_gpu=event.get('quote_gpu'))
    print(json.dumps(result))
    return result
