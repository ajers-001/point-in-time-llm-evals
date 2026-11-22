"""Fargate controller entrypoint. Everything comes from env, Secrets Manager and S3.

The container command (see stack template) first fetches the content-addressed
controller bundle from private S3, verifies its SHA256, installs the
hash-locked dependencies, then runs this module. No PC, desktop process or
local credential file is involved.
"""
import json
import os
import socket
import time
import uuid

import boto3

from . import allocator
from .artifacts import S3Objects, sha256
from .cloud import (DEFAULT_CUDA_VERSIONS, Presigner, SshHosts, build_payload, bulk_gets, host_keypair, status_writer,
                    watchdog_sees)
from .controller import Controller, run_forever
from .inputs import load_any
from .provider import RunpodClient
from .state import Session
from .store import DynamoStore


def secret(client, secret_id):
    return client.get_secret_value(SecretId=secret_id)['SecretString'].strip()


def main():
    env = os.environ
    region = env['CONTROL_REGION']
    aws = boto3.session.Session(region_name=region)
    identity = aws.client('sts').get_caller_identity()
    if identity['Account'] != env['EXPECTED_ACCOUNT']:
        raise SystemExit('controller running in the wrong AWS account')
    sm, s3 = aws.client('secretsmanager'), aws.client('s3')
    store = DynamoStore(env['TABLE'], aws.client('dynamodb'))
    session = Session(store, env['SESSION_ID'])
    meta = session.meta()
    if meta is None:
        raise SystemExit('session not initialised by deploy')
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    queue = load_any(root, meta.get('queue_kind'), verify_audit=False)
    host_bundle = s3.get_object(Bucket=env['BUCKET'], Key=env['HOST_BUNDLE_KEY'])['Body'].read()
    if sha256(host_bundle) != env['HOST_BUNDLE_SHA256'] or meta['host_bundle_sha256'] != env['HOST_BUNDLE_SHA256']:
        raise SystemExit('host bundle hash mismatch; admission blocked')
    uploader = json.loads(secret(sm, env['UPLOADER_SECRET_ID']))
    signer = boto3.session.Session(aws_access_key_id=uploader['access_key_id'],
                                   aws_secret_access_key=uploader['secret_access_key'],
                                   region_name=region).client(
        's3', endpoint_url=f'https://s3.{region}.amazonaws.com',  # regional: new buckets 307 on the global endpoint
        config=boto3.session.Config(signature_version='s3v4', s3={'addressing_style': 'virtual'}))
    presign = Presigner(signer, env['BUCKET'], session.sid, expires_at=meta['gpu_end'] + 4 * 3600)
    ssh = json.loads(secret(sm, env['SSH_SECRET_ID']))
    bulk = bulk_gets(s3, env['BUCKET'], root, queue)
    hosts = SshHosts(ssh['private_key'], host_bundle, env['HOST_BUNDLE_SHA256'], presign, bulk_urls=bulk)
    provider = RunpodClient(secret(sm, env['RUNPOD_SECRET_ID']))

    def payload(name, slot):
        private, public = host_keypair()
        body = build_payload(name, env['POD_IMAGE'], ssh['public_key'], private, gpu_type=env['GPU_TYPE'],
                             cuda_versions=('any' if meta.get('allowed_cuda_versions') == 'any'
                                            else tuple(meta.get('allowed_cuda_versions') or DEFAULT_CUDA_VERSIONS)),
                             disk_gb=int(meta.get('container_disk_gb') or 350),
                             gpu_count=int(meta.get('gpu_count', 1)))
        body['_record'] = {'host_public_key': public}
        return body

    def scale_down():
        aws.client('ecs').update_service(cluster=env['CLUSTER'], service=env['SERVICE'], desiredCount=0)

    cfg = {'gpu_type': env['GPU_TYPE'], 'disk_hourly_usd': env['DISK_HOURLY_USD'], 'bundle_sha256': env['HOST_BUNDLE_SHA256'],
           'image': env['POD_IMAGE'], 'readiness_config_sha256': meta['readiness_config_sha256'],
           'readiness_input_sha256': meta['readiness_input_sha256'], 'payload_builder': payload,
           'watchdog_sees': watchdog_sees(aws.client('lambda'), env['WATCHDOG_FUNCTION']),
           'status_writer': status_writer(s3, env['BUCKET'], session.sid), 'on_closed': scale_down, 'tick_s': 15,
           'echo': True}
    owner = f'{socket.gethostname()}-{uuid.uuid4().hex[:8]}'
    controller = Controller(session, provider, S3Objects(env['BUCKET'], s3), hosts, lambda: int(time.time()), cfg, owner)
    print(json.dumps({'event': 'controller_start', 'owner': owner, 'session': session.sid,
                      'open_pods': [p['name'] for p in allocator.open_pods(session)]}), flush=True)
    result = run_forever(controller, time.sleep)
    print(json.dumps({'event': 'controller_exit', 'result': result}), flush=True)


if __name__ == '__main__':
    main()
