"""Operator commands, runnable from any authenticated cloud shell.

  python -m infra.cloud_two_gpu.cli dry-run   --session SESSION.json
  python -m infra.cloud_two_gpu.cli deploy    --session SESSION.json     # CPU controls only, no GPU
  python -m infra.cloud_two_gpu.cli start     --session SESSION.json --approve-paid-gpu-launch SESSION_ID
  python -m infra.cloud_two_gpu.cli status    --session SESSION.json
  python -m infra.cloud_two_gpu.cli reconcile --session SESSION.json [--retry RUN --repair ID | --resume-slot N --repair ID | --stop]
  python -m infra.cloud_two_gpu.cli terminate --session SESSION.json [--cleanup]

AWS credentials must come from explicit environment variables (never a
default/shared profile) and resolve to the exact research deployer identity.
"""
import argparse
import datetime as dt
import hashlib
import json
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

from . import allocator, bundle, stack
from .budget import Ledger, validate_config
from .cloud import DEFAULT_CUDA_VERSIONS
from .inputs import QUEUE_KINDS, load_any, PRIOR_AUDIT, sha256_file
from .state import Session
from .store import ConditionFailed, DynamoStore, Put, Update

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_ARN = f'arn:aws:iam::{stack.ACCOUNT}:user/pit-2gpu-deployer'
POD_IMAGE = 'runpod/pytorch@sha256:0a360022e8de4375af99430f84e8b38951acc397252163a37ceac7204d01be35'
GPU_TYPE = 'NVIDIA H100 80GB HBM3'  # default for sessions that do not name one
ALLOWED_GPUS = ('NVIDIA H100 80GB HBM3', 'NVIDIA H200')
DISK_HOURLY_USD = '0.049'
GPU_END_MARGIN_S = 180   # watchdog deletes pods this long before the hard deadline (scheduler jitter)


def load_session(path):
    raw = json.loads(Path(path).read_text())
    b = raw.get('budget') or {}
    budget = {'session_ceiling_usd': b.get('new_session_ceiling_usd'), 'aggregate_ceiling_usd': b.get('total_authorized_ceiling_usd'),
              'prior_spend_reconciled_usd': b.get('prior_spend_reconciled_usd'), 'retrieval_reserve_usd': b.get('retrieval_reserve_usd'),
              'control_reserve_usd': b.get('control_storage_network_reserve_usd'), 'max_worker_hourly_usd': b.get('max_worker_hourly_usd'),
              'topups_allowed': b.get('topups_allowed')}
    t = raw.get('time') or {}
    validate_config({'budget': budget, 'time': t})
    if raw.get('max_concurrent_gpus') not in (1, 2) or raw.get('fresh_engine_per_request') is not True:
        raise ValueError('session must use one or two single-GPU workers and fresh engines')
    gpu_count = raw.get('gpu_count_per_worker', 1)
    if gpu_count not in (1, 2):
        raise ValueError('workers are one- or two-GPU pods')
    gpu_type = raw.get('preferred_gpu', GPU_TYPE)
    if gpu_type not in ALLOWED_GPUS:
        raise ValueError(f'GPU type must be one of {ALLOWED_GPUS}')
    queue_kind = raw.get('queue_kind', 'continuation-74')
    if queue_kind not in QUEUE_KINDS:
        raise ValueError(f'queue_kind must be one of {QUEUE_KINDS}')
    cuda_versions = raw.get('allowed_cuda_versions')
    if cuda_versions is not None and cuda_versions != 'any' and (
            not cuda_versions or not set(cuda_versions) <= set(DEFAULT_CUDA_VERSIONS)):
        raise ValueError(f"allowed_cuda_versions must be 'any' or a non-empty subset of {DEFAULT_CUDA_VERSIONS}")
    disk_gb = raw.get('container_disk_gb')  # pod container disk; the payload default (350 GB) when absent
    if disk_gb is not None and not 100 <= int(disk_gb) <= 500:
        raise ValueError('container_disk_gb must be within 100-500')
    research_timeout_s = int(raw.get('research_timeout_seconds', 2400))
    if not 600 <= research_timeout_s <= 14400:
        raise ValueError('research_timeout_seconds out of range')
    sid = raw.get('session_id')
    if not sid or not all(c.isalnum() or c == '-' for c in sid) or len(sid) > 24:
        raise ValueError('session_id must be a short new identifier (letters, digits, hyphens)')
    deadline = dt.datetime.fromisoformat(t['deadline_utc'].replace('Z', '+00:00'))
    if deadline.tzinfo is None:
        raise ValueError('deadline must be timezone-aware')
    d = int(deadline.timestamp())
    gpu_end = d - GPU_END_MARGIN_S
    inference_end = gpu_end - int(t.get('final_retrieval_reserve_seconds', 120))
    return {'raw': raw, 'session_id': sid, 'budget': {k: str(v) for k, v in budget.items() if k != 'topups_allowed'},
            'deadline': d, 'gpu_end': gpu_end, 'inference_end': inference_end,
            'stop_new_work_reserve_s': int(t.get('stop_new_work_reserve_seconds', 420)),
            'final_retrieval_reserve_s': int(t.get('final_retrieval_reserve_seconds', 120)),
            'gpu_type': gpu_type, 'queue_kind': queue_kind, 'research_timeout_s': research_timeout_s,
            'allowed_cuda_versions': cuda_versions, 'workers': raw['max_concurrent_gpus'], 'gpu_count': gpu_count,
            'container_disk_gb': None if disk_gb is None else int(disk_gb)}


def aws_session():
    import boto3
    s = boto3.session.Session(region_name=stack.REGION)
    creds = s.get_credentials()
    if creds is None or creds.method != 'env':
        raise SystemExit('AWS credentials must come from explicit environment variables, not a profile')
    arn = s.client('sts').get_caller_identity()['Arn']
    if arn != EXPECTED_ARN:
        raise SystemExit(f'unexpected AWS identity {arn}; expected {EXPECTED_ARN}')
    return s


def artifacts(queue):
    host, host_info = bundle.build_host(bundle.host_files(ROOT, queue))
    ctl, ctl_info = bundle.build_controller(bundle.controller_files(ROOT, queue))
    zipped, zip_info = bundle.watchdog_zip(ROOT)
    return {'host': (host, host_info, f'inputs/host/{host_info["sha256"]}.tar.gz'),
            'controller': (ctl, ctl_info, f'inputs/controller/{ctl_info["sha256"]}.tar.gz'),
            'watchdog': (zipped, zip_info, f'inputs/watchdog/{zip_info["sha256"]}.zip')}


def readiness_hashes():
    run = json.loads((ROOT / 'prepared/smoke-prewarm-v2/S-smoke-combined/run.json').read_text())
    return {'readiness_config_sha256': sha256_file(ROOT / 'prepared/smoke-prewarm-v2/experiment.json'),
            'readiness_input_sha256': run['input_sha256']}


def cmd_dry_run(a):
    cfg = load_session(a.session)
    queue = load_any(ROOT, cfg['queue_kind'])
    arts = artifacts(queue)
    now = int(time.time())
    ledger = Ledger(cfg['budget'], deadline=cfg['gpu_end'])
    hourly = Decimal(a.quote_hourly)
    if cfg['gpu_end'] <= now:
        ok, detail = False, {'reason': 'deadline already passed'}
    else:
        ok, detail = ledger.admit([], hourly, planned_new=cfg['workers'], credit_usd=a.credit, now=now)
    utc = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()  # noqa: E731
    return {'session_id': cfg['session_id'], 'pending': len(queue['runs']), 'prior_completed': len(queue['prior']),
            'deadline_utc': utc(cfg['deadline']), 'gpu_end_utc': utc(cfg['gpu_end']),
            'inference_end_utc': utc(cfg['inference_end']),
            'window_hours_from_now': str((Decimal(max(0, cfg['gpu_end'] - now)) / 3600).quantize(Decimal('0.01'))),
            'admission_if_launched_now': {'allowed': ok, **detail, 'quote_hourly_usd': str(hourly),
                                          'assumed_credit_usd': a.credit},
            'artifacts': {k: v[1] for k, v in arts.items()}, 'aws_writes': False, 'gpu_launch': False}


def _stack_status(cf, name):
    try:
        return cf.describe_stacks(StackName=name)['Stacks'][0]['StackStatus']
    except cf.exceptions.ClientError as error:
        if 'does not exist' in str(error):
            return None
        raise


def _delete_failed_stack(cf, name):
    cf.delete_stack(StackName=name)
    try:
        cf.get_waiter('stack_delete_complete').wait(StackName=name)
        return
    except Exception:  # noqa: BLE001 - inspect what blocked deletion
        if _stack_status(cf, name) != 'DELETE_FAILED':
            raise
    stuck = [r['LogicalResourceId'] for r in cf.describe_stack_resources(StackName=name)['StackResources']
             if r['ResourceStatus'] == 'DELETE_FAILED']
    if any(cf.describe_stack_resource(StackName=name, LogicalResourceId=r)['StackResourceDetail']['ResourceType']
           != 'AWS::ECS::TaskDefinition' for r in stuck):
        raise SystemExit(f'failed stack {name} has undeletable resources: {stuck}')
    cf.delete_stack(StackName=name, RetainResources=stuck)
    cf.get_waiter('stack_delete_complete').wait(StackName=name)


def _stack(cf, name, template):
    body = stack.body(template)
    status = _stack_status(cf, name)
    if status in ('ROLLBACK_COMPLETE', 'ROLLBACK_FAILED', 'DELETE_FAILED'):
        # A failed first create cannot be updated; remove it and recreate. Task definition
        # revisions are kept (deregistering them needs an account-wide permission; they are inert).
        _delete_failed_stack(cf, name)
        status = None
    if status is not None:
        try:
            cf.update_stack(StackName=name, TemplateBody=body, Capabilities=['CAPABILITY_NAMED_IAM'])
        except cf.exceptions.ClientError as error:
            if 'No updates are to be performed' not in str(error):
                raise
            return 'unchanged'
        cf.get_waiter('stack_update_complete').wait(StackName=name)
        return 'updated'
    cf.create_stack(StackName=name, TemplateBody=body, Capabilities=['CAPABILITY_NAMED_IAM'],
                    OnFailure='ROLLBACK', Tags=[{'Key': 'Project', 'Value': 'pit-2gpu'}])
    cf.get_waiter('stack_create_complete').wait(StackName=name)
    return 'created'


def _put_verified(s3, key, data):
    from .artifacts import S3Objects, ObjectExists
    objects = S3Objects(stack.BUCKET, s3)
    try:
        objects.put_new(key, data)
    except ObjectExists:
        pass
    back = objects.get(key, limit=64 << 20)
    if back is None or hashlib.sha256(back).hexdigest() != hashlib.sha256(data).hexdigest():
        raise SystemExit(f'S3 readback mismatch for {key}')
    return {'key': key, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}


def _log_groups(logs, sid):
    names = [f'/pit-2gpu/{sid}/controller', f'/pit-2gpu/{sid}/watchdog']
    for name in names:
        try:
            logs.create_log_group(logGroupName=name, tags={'Project': 'pit-2gpu', 'Session': sid})
        except logs.exceptions.ResourceAlreadyExistsException:
            pass
        logs.put_retention_policy(logGroupName=name, retentionInDays=90)
    return names


def _security_group(ec2, sid, vpc):
    found = ec2.describe_security_groups(Filters=[{'Name': 'tag:Project', 'Values': ['pit-2gpu']},
                                                  {'Name': 'tag:Session', 'Values': [sid]}])['SecurityGroups']
    if found:
        return found[0]['GroupId']
    group = ec2.create_security_group(GroupName=f'pit-2gpu-{sid}-controller', VpcId=vpc,
                                      Description='pit-2gpu controller: outbound only, no inbound',
                                      TagSpecifications=[{'ResourceType': 'security-group', 'Tags': [
                                          {'Key': 'Project', 'Value': 'pit-2gpu'}, {'Key': 'Session', 'Value': sid}]}])
    return group['GroupId']


def cmd_deploy(a):
    from .cloud import host_keypair
    cfg = load_session(a.session)
    sid = cfg['session_id']
    if cfg['gpu_end'] - int(time.time()) < 2 * 3600:
        raise SystemExit('deadline leaves under two hours; choose a later absolute deadline')
    s = aws_session()
    queue = load_any(ROOT, cfg['queue_kind'])
    arts = artifacts(queue)
    cf, s3, ec2, sm = s.client('cloudformation'), s.client('s3'), s.client('ec2'), s.client('secretsmanager')
    report = {'session_id': sid, 'foundation': _stack(cf, 'pit-2gpu-foundation', stack.foundation())}
    report['uploads'] = {k: _put_verified(s3, key, data) for k, (data, info, key) in arts.items()}
    audit = (ROOT / PRIOR_AUDIT).read_bytes()
    report['uploads']['prior_audit'] = _put_verified(s3, f'inputs/prior-audit/{hashlib.sha256(audit).hexdigest()}.tar.gz', audit)
    for secret in (stack.RUNPOD_SECRET, stack.UPLOADER_SECRET):
        sm.describe_secret(SecretId=secret)  # must exist; never read here
    ssh_secret = f'pit-2gpu/session/{sid}/ssh'
    try:
        sm.describe_secret(SecretId=ssh_secret)
    except sm.exceptions.ResourceNotFoundException:
        private, public = host_keypair()
        sm.create_secret(Name=ssh_secret, SecretString=json.dumps({'private_key': private, 'public_key': public}),
                         Tags=[{'Key': 'Project', 'Value': 'pit-2gpu'}, {'Key': 'Session', 'Value': sid}])
        del private
    vpc = ec2.describe_vpcs(Filters=[{'Name': 'is-default', 'Values': ['true']}])['Vpcs'][0]['VpcId']
    subnets = [x['SubnetId'] for x in ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc]}])['Subnets']
               if x['MapPublicIpOnLaunch']]
    group = _security_group(ec2, sid, vpc)
    store = DynamoStore(stack.TABLE, s.client('dynamodb'))
    meta = {'owner': EXPECTED_ARN, 'budget': cfg['budget'], 'deadline': cfg['deadline'], 'gpu_end': cfg['gpu_end'],
            'inference_end': cfg['inference_end'], 'stop_new_work_reserve_s': cfg['stop_new_work_reserve_s'],
            'final_retrieval_reserve_s': cfg['final_retrieval_reserve_s'],
            'queue_kind': cfg['queue_kind'], 'research_timeout_s': cfg['research_timeout_s'], 'gpu_type': cfg['gpu_type'],
            'workers': cfg['workers'], 'gpu_count': cfg['gpu_count'],
            **({'allowed_cuda_versions': cfg['allowed_cuda_versions']} if cfg['allowed_cuda_versions'] else {}),
            **({'container_disk_gb': cfg['container_disk_gb']} if cfg.get('container_disk_gb') else {}),
            'host_bundle_sha256': arts['host'][1]['sha256'], 'controller_bundle_sha256': arts['controller'][1]['sha256'],
            'display_timezone': cfg['raw']['time'].get('display_timezone', 'America/Chicago'),
            'scientific_validity_claim': False, **readiness_hashes()}
    Session(store, sid).initialize(queue, meta, int(time.time()))
    params = {'subnets': subnets, 'security_group': group, 'pod_image': POD_IMAGE, 'gpu_type': cfg['gpu_type'],
              'disk_hourly_usd': DISK_HOURLY_USD,
              'schedule_start': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
              'schedule_end': dt.datetime.fromtimestamp(cfg['gpu_end'] + 3 * 3600, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
              'host_bundle_key': arts['host'][2], 'host_bundle_sha256': arts['host'][1]['sha256'],
              'controller_bundle_key': arts['controller'][2], 'controller_bundle_sha256': arts['controller'][1]['sha256'],
              'watchdog_zip_key': arts['watchdog'][2]}
    report['log_groups'] = _log_groups(s.client('logs'), sid)
    try:  # an update must not stop a running controller: keep its current desired count
        services = s.client('ecs').describe_services(cluster=f'pit-2gpu-{sid}',
                                                     services=[f'pit-2gpu-{sid}-controller'])['services']
        params['desired_count'] = services[0]['desiredCount'] if services and services[0]['status'] == 'ACTIVE' else 0
    except s.client('ecs').exceptions.ClientError:
        params['desired_count'] = 0
    report['session_stack'] = _stack(cf, f'pit-2gpu-{sid}', stack.session(sid, params))
    report['watchdog_check'] = _invoke_watchdog(s, sid, dry_run=True, quote=cfg['gpu_type'])
    if report['watchdog_check'].get('status') != 'before_stop':
        raise SystemExit(f'watchdog live check failed: {report["watchdog_check"]}')
    report['gpu_launch'] = False
    return report


def _invoke_watchdog(s, sid, dry_run, quote=None):
    event = {'dry_run': dry_run, **({'quote_gpu': quote if isinstance(quote, str) else GPU_TYPE} if quote else {})}
    response = s.client('lambda').invoke(FunctionName=f'pit-2gpu-{sid}-watchdog', InvocationType='RequestResponse',
                                         Payload=json.dumps(event).encode())
    payload = json.loads(response['Payload'].read() or b'{}')
    if response.get('FunctionError'):
        return {'status': 'error', 'error': payload.get('errorType'), 'message': str(payload.get('errorMessage'))[:300]}
    return payload


def cmd_start(a):
    cfg = load_session(a.session)
    sid = cfg['session_id']
    if a.approve_paid_gpu_launch != sid:
        raise SystemExit('paid launch requires --approve-paid-gpu-launch <exact session id>')
    s = aws_session()
    schedule = s.client('scheduler').get_schedule(Name=f'pit-2gpu-{sid}-expiry', GroupName=f'pit-2gpu-{sid}')
    if schedule['State'] != 'ENABLED' or schedule['EndDate'].timestamp() < cfg['gpu_end'] + 1800:
        raise SystemExit('independent expiry schedule is not armed through the deadline')
    check = _invoke_watchdog(s, sid, dry_run=True, quote=cfg['gpu_type'])
    if check.get('status') != 'before_stop' or 'account' not in check:
        raise SystemExit(f'watchdog live check failed: {check}')
    session = Session(DynamoStore(stack.TABLE, s.client('dynamodb')), sid)
    meta = session.meta()
    if meta['status'] not in ('READY', 'RUNNING') or meta['gpu_end'] != cfg['gpu_end']:
        raise SystemExit('session state does not match the approved configuration')
    account = check['account']
    recorded = {p.get('pod_id') for p in allocator.pod_records(session)}
    stray = [p for p in check['owned_pods_in_inventory'] if p not in recorded]
    if account['autopay'] or stray:
        raise SystemExit(f'refusing launch: autopay={account["autopay"]} unrecorded_pods={stray}')
    hourly = Decimal(account['secure_price_usd']) * cfg['gpu_count'] + Decimal(DISK_HOURLY_USD)
    ledger = Ledger(cfg['budget'], deadline=cfg['gpu_end'])
    pods = allocator.open_pods(session)
    if pods:  # resuming the controller for this session's own recorded pods
        if ledger.must_stop(pods, account['balance_usd'], int(time.time()), meta.get('other_spend_usd', '0')):
            raise SystemExit('budget no longer covers the live pods through the deadline')
        detail = {'resume_with_pods': sorted(recorded - {None})}
    else:
        ok, detail = ledger.admit([], hourly, planned_new=cfg['workers'], credit_usd=account['balance_usd'],
                                  now=int(time.time()))
        if not ok:
            raise SystemExit(f'budget admission refused at current quote/credit: {detail}')
    s.client('ecs').update_service(cluster=f'pit-2gpu-{sid}', service=f'pit-2gpu-{sid}-controller', desiredCount=1)
    return {'session_id': sid, 'controller': 'desired_count_1', 'watchdog': check, 'admission': detail,
            'quote_hourly_usd': str(hourly)}


def cmd_status(a):
    cfg = load_session(a.session)
    s = aws_session()
    session = Session(DynamoStore(stack.TABLE, s.client('dynamodb')), cfg['session_id'])
    meta = session.meta()
    if meta is None:
        return {'session_id': cfg['session_id'], 'status': 'not_deployed'}
    jobs = session.jobs()
    counts = {}
    for j in jobs:
        key = 'PRIOR_SUCCEEDED' if j['prior'] else j['state']
        counts[key] = counts.get(key, 0) + 1
    pods = allocator.pod_records(session)
    now = int(time.time())
    ledger = Ledger(meta['budget'], deadline=meta['gpu_end'])
    watch = session.store.query(session.pk, 'WATCH#')[-3:]
    ops = [o for o in session.store.query(session.pk, 'OPS#')][-5:]
    return {'session_id': session.sid, 'status': meta['status'], 'job_counts': counts,
            'workers': [{k: w.get(k) for k in ('slot', 'status', 'pod_id', 'active_run', 'needs_operator')}
                        for w in (session.worker(1), session.worker(2))],
            'pods': [{k: p.get(k) for k in ('name', 'slot', 'status', 'pod_id', 'hourly_usd')} for p in pods],
            'gpu_spend_usd_estimate': str(ledger.spent(pods, meta.get('other_spend_usd', '0'), now).quantize(Decimal('0.01'))),
            'last_credit_usd': meta.get('last_credit_usd'), 'last_quote': meta.get('last_quote'),
            'conflicts': len(session.store.query(session.pk, 'CONFLICT#')),
            'watchdog_recent': watch, 'operator_requests': ops,
            'deadline_utc': dt.datetime.fromtimestamp(meta['deadline'], dt.timezone.utc).isoformat(),
            'scientific_validity_claim': False}


def cmd_reconcile(a):
    cfg = load_session(a.session)
    s = aws_session()
    session = Session(DynamoStore(stack.TABLE, s.client('dynamodb')), cfg['session_id'])
    if a.retry:
        op = {'action': 'retry', 'run_id': a.retry, 'repair_id': a.repair}
    elif a.resume_slot:
        op = {'action': 'resume_slot', 'slot': a.resume_slot, 'repair_id': a.repair}
    elif a.stop:
        op = {'action': 'stop', 'reason': 'operator requested graceful stop'}
    else:
        return cmd_status(a)
    now = int(time.time())
    item = {'pk': session.pk, 'sk': f'OPS#{now:012d}#{uuid.uuid4().hex[:8]}', 'status': 'NEW', 'at': now,
            'by': EXPECTED_ARN, **{k: v for k, v in op.items() if v is not None}}
    session.store.transact([Put(item)])
    return {'queued_for_controller': item}


def cmd_terminate(a):
    cfg = load_session(a.session)
    sid = cfg['session_id']
    s = aws_session()
    session = Session(DynamoStore(stack.TABLE, s.client('dynamodb')), sid)
    for _ in range(20):  # the live controller may update META concurrently; re-read and retry
        meta = session.meta()
        if meta is None or meta['status'] in ('CLOSED', 'TERMINATE_REQUESTED'):
            break
        try:
            session.store.transact([Update(session.pk, 'META', meta['version'], {
                'status': 'TERMINATE_REQUESTED', 'terminate_requested_at': int(time.time())})])
            break
        except ConditionFailed:
            time.sleep(1)
    results = []
    for _ in range(10):
        result = _invoke_watchdog(s, sid, dry_run=False)
        results.append(result)
        if result.get('status') in ('all_absent', 'no_session'):
            break
        time.sleep(20)
    ecs = s.client('ecs')
    try:
        ecs.update_service(cluster=f'pit-2gpu-{sid}', service=f'pit-2gpu-{sid}-controller', desiredCount=0)
    except ecs.exceptions.ClientError as error:
        results.append({'ecs': str(error)[:200]})
    report = {'session_id': sid, 'watchdog': results, 'gpu_pods_absent': results[-1].get('status') in ('all_absent', 'no_session')}
    if a.cleanup:
        if not report['gpu_pods_absent']:
            raise SystemExit('refusing control cleanup before exact pod absence is verified')
        cf = s.client('cloudformation')
        cf.delete_stack(StackName=f'pit-2gpu-{sid}')
        cf.get_waiter('stack_delete_complete').wait(StackName=f'pit-2gpu-{sid}')
        s.client('secretsmanager').delete_secret(SecretId=f'pit-2gpu/session/{sid}/ssh', ForceDeleteWithoutRecovery=True)
        report['removed'] = [f'stack pit-2gpu-{sid} (roles, watchdog, schedule, cluster, service)',
                             f'secret pit-2gpu/session/{sid}/ssh']
        report['retained'] = [f's3://{stack.BUCKET} (audits, results)', f'dynamodb {stack.TABLE} (durable state)',
                              f'log groups /pit-2gpu/{sid}/*', 'security group (no cost)',
                              'secrets pit-2gpu/runpod-api-key and pit-2gpu/session/host-uploader (administrator removes)']
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('dry-run', 'deploy', 'start', 'status', 'reconcile', 'terminate'):
        c = sub.add_parser(name)
        c.add_argument('--session', required=True)
        if name == 'dry-run':
            c.add_argument('--quote-hourly', default='3.539')
            c.add_argument('--credit', default='65')
        if name == 'start':
            c.add_argument('--approve-paid-gpu-launch', required=True)
        if name == 'reconcile':
            c.add_argument('--retry')
            c.add_argument('--resume-slot', type=int)
            c.add_argument('--repair')
            c.add_argument('--stop', action='store_true')
        if name == 'terminate':
            c.add_argument('--cleanup', action='store_true')
    a = p.parse_args(argv)
    handler = {'dry-run': cmd_dry_run, 'deploy': cmd_deploy, 'start': cmd_start, 'status': cmd_status,
               'reconcile': cmd_reconcile, 'terminate': cmd_terminate}[a.cmd]
    print(json.dumps(handler(a), indent=2, default=str))


if __name__ == '__main__':
    sys.exit(main())
