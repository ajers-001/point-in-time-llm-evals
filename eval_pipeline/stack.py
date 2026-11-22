"""CloudFormation templates (generated as JSON so they can be unit-tested).

Foundation stack ``pit-2gpu-foundation`` (retained across sessions): private
artifact bucket and durable state table. Session stack ``pit-2gpu-<session>``:
boundary-bound roles, log groups, the independent watchdog Lambda and its
one-minute schedule, and the single-task Fargate controller service (created
at desired count 0; ``start`` scales it to 1). Log groups are created by the
CLI before the stack (idempotent) and retained after cleanup as audit records. Deleting a session stack removes
only session-specific controls; audits in the bucket and table are retained.
"""
import json

ACCOUNT = '000000000000'  # placeholder: the real AWS account id is not published
REGION = 'us-east-2'
BUCKET = f'pit-2gpu-artifacts-{ACCOUNT}'
TABLE = 'pit-2gpu-state'
BOUNDARY = f'arn:aws:iam::{ACCOUNT}:policy/pit-2gpu-runtime-boundary'
RUNPOD_SECRET = 'pit-2gpu/runpod-api-key'
UPLOADER_SECRET = 'pit-2gpu/session/host-uploader'
CONTROLLER_IMAGE = 'public.ecr.aws/lambda/python@sha256:517bcc7dc1a3ba62e324961c1563cc54a2bd6dc1060188f9d376211ecb7b2926'

BOOT = r'''
import hashlib,io,os,subprocess,sys,tarfile,boto3
from pathlib import PurePosixPath
e=os.environ
body=boto3.client("s3",region_name=e["CONTROL_REGION"]).get_object(Bucket=e["BUCKET"],Key=e["CONTROLLER_BUNDLE_KEY"])["Body"].read()
assert hashlib.sha256(body).hexdigest()==e["CONTROLLER_BUNDLE_SHA256"],"controller bundle hash mismatch"
with tarfile.open(fileobj=io.BytesIO(body),mode="r:gz") as t:
  ms=t.getmembers();assert len({m.name for m in ms})==len(ms)
  for m in ms:
    p=PurePosixPath(m.name);assert m.isfile() and not p.is_absolute() and ".." not in p.parts,m.name
  t.extractall("/tmp/ctl",filter="data")
subprocess.run([sys.executable,"-m","pip","install","-q","--no-cache-dir","--require-hashes","--no-deps","--target","/tmp/deps","-r","/tmp/ctl/infra/cloud_two_gpu/controller-requirements.txt"],check=True)
os.chdir("/tmp/ctl");os.execve(sys.executable,[sys.executable,"-m","infra.cloud_two_gpu.entrypoint"],dict(e,PYTHONPATH="/tmp/deps:/tmp/ctl"))
'''.strip()


def _role(name, service, statements):
    return {'Type': 'AWS::IAM::Role', 'Properties': {
        'RoleName': name, 'PermissionsBoundary': BOUNDARY,
        'AssumeRolePolicyDocument': {'Version': '2012-10-17', 'Statement': [
            {'Effect': 'Allow', 'Principal': {'Service': service}, 'Action': 'sts:AssumeRole',
             'Condition': {'StringEquals': {'aws:SourceAccount': ACCOUNT}}}]},
        'Policies': [{'PolicyName': 'scoped', 'PolicyDocument': {'Version': '2012-10-17', 'Statement': statements}}],
        'Tags': [{'Key': 'Project', 'Value': 'pit-2gpu'}]}}


def foundation():
    deny_insecure = {'Sid': 'TlsOnly', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:*',
                     'Resource': [f'arn:aws:s3:::{BUCKET}', f'arn:aws:s3:::{BUCKET}/*'],
                     'Condition': {'Bool': {'aws:SecureTransport': 'false'}}}
    deny_plain = {'Sid': 'EncryptedPutsOnly', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:PutObject',
                  'Resource': f'arn:aws:s3:::{BUCKET}/*',
                  'Condition': {'StringNotEquals': {'s3:x-amz-server-side-encryption': 'AES256'}}}
    return {'AWSTemplateFormatVersion': '2010-09-09', 'Description': 'pit-2gpu retained research state and artifacts',
            'Resources': {
                'Artifacts': {'Type': 'AWS::S3::Bucket', 'DeletionPolicy': 'Retain', 'UpdateReplacePolicy': 'Retain',
                              'Properties': {
                                  'BucketName': BUCKET,
                                  'PublicAccessBlockConfiguration': {k: True for k in (
                                      'BlockPublicAcls', 'BlockPublicPolicy', 'IgnorePublicAcls', 'RestrictPublicBuckets')},
                                  'BucketEncryption': {'ServerSideEncryptionConfiguration': [
                                      {'ServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]},
                                  'OwnershipControls': {'Rules': [{'ObjectOwnership': 'BucketOwnerEnforced'}]},
                                  'VersioningConfiguration': {'Status': 'Enabled'},
                                  'LifecycleConfiguration': {'Rules': [{'Id': 'status-history', 'Status': 'Enabled',
                                                                        'Prefix': 'sessions/', 'NoncurrentVersionExpiration': {
                                                                            'NoncurrentDays': 30}}]},
                                  'Tags': [{'Key': 'Project', 'Value': 'pit-2gpu'}]}},
                'ArtifactsPolicy': {'Type': 'AWS::S3::BucketPolicy', 'Properties': {
                    'Bucket': {'Ref': 'Artifacts'},
                    'PolicyDocument': {'Version': '2012-10-17', 'Statement': [deny_insecure, deny_plain]}}},
                'State': {'Type': 'AWS::DynamoDB::Table', 'DeletionPolicy': 'Retain', 'UpdateReplacePolicy': 'Retain',
                          'Properties': {
                              'TableName': TABLE, 'BillingMode': 'PAY_PER_REQUEST',
                              'AttributeDefinitions': [{'AttributeName': 'pk', 'AttributeType': 'S'},
                                                       {'AttributeName': 'sk', 'AttributeType': 'S'}],
                              'KeySchema': [{'AttributeName': 'pk', 'KeyType': 'HASH'},
                                            {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
                              'PointInTimeRecoverySpecification': {'PointInTimeRecoveryEnabled': True},
                              # Encrypted with an AWS-owned key: no account KMS key for bounded roles to decrypt.
                              'SSESpecification': {'SSEEnabled': False},
                              'Tags': [{'Key': 'Project', 'Value': 'pit-2gpu'}]}}}}


def session(sid, p):
    """``p``: subnets, security_group, schedule_start, schedule_end (ISO), bundle keys/hashes, pod image."""
    name = f'pit-2gpu-{sid}'
    table_arn = f'arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{TABLE}'
    secret_arn = lambda s: f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{s}-*'  # noqa: E731
    function = f'{name}-watchdog'
    cluster, service = name, f'{name}-controller'
    service_arn = f'arn:aws:ecs:{REGION}:{ACCOUNT}:service/{cluster}/{service}'
    state_rw = {'Effect': 'Allow', 'Action': ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:PutItem',
                                              'dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem'], 'Resource': table_arn}
    decrypt_secrets = {'Effect': 'Allow', 'Action': 'kms:Decrypt', 'Resource': '*',
                       'Condition': {'StringEquals': {'kms:ViaService': f'secretsmanager.{REGION}.amazonaws.com'}}}
    logs = lambda group: {'Effect': 'Allow', 'Action': ['logs:CreateLogStream', 'logs:PutLogEvents'],  # noqa: E731
                          'Resource': f'arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{group}:*'}
    common_env = {'CONTROL_REGION': REGION, 'TABLE': TABLE, 'SESSION_ID': sid, 'RUNPOD_SECRET_ID': RUNPOD_SECRET,
                  'CLUSTER': cluster, 'SERVICE': service}
    controller_env = dict(common_env, BUCKET=BUCKET, EXPECTED_ACCOUNT=ACCOUNT, UPLOADER_SECRET_ID=UPLOADER_SECRET,
                          SSH_SECRET_ID=f'pit-2gpu/session/{sid}/ssh', WATCHDOG_FUNCTION=function,
                          CONTROLLER_BUNDLE_KEY=p['controller_bundle_key'], CONTROLLER_BUNDLE_SHA256=p['controller_bundle_sha256'],
                          HOST_BUNDLE_KEY=p['host_bundle_key'], HOST_BUNDLE_SHA256=p['host_bundle_sha256'],
                          POD_IMAGE=p['pod_image'], GPU_TYPE=p['gpu_type'], DISK_HOURLY_USD=p['disk_hourly_usd'])
    resources = {
        'WatchdogRole': _role(f'{name}-watchdog', 'lambda.amazonaws.com', [
            state_rw, logs(f'/pit-2gpu/{sid}/watchdog'),
            {'Effect': 'Allow', 'Action': 'secretsmanager:GetSecretValue', 'Resource': secret_arn(RUNPOD_SECRET)},
            decrypt_secrets,
            {'Effect': 'Allow', 'Action': ['ecs:UpdateService', 'ecs:DescribeServices'], 'Resource': service_arn}]),
        'Watchdog': {'Type': 'AWS::Lambda::Function', 'Properties': {
            'FunctionName': function, 'Runtime': 'python3.12', 'Handler': 'infra.cloud_two_gpu.watchdog.handler',
            'Code': {'S3Bucket': BUCKET, 'S3Key': p['watchdog_zip_key']}, 'Timeout': 120, 'MemorySize': 256,
            'Role': {'Fn::GetAtt': ['WatchdogRole', 'Arn']},
            'LoggingConfig': {'LogGroup': f'/pit-2gpu/{sid}/watchdog'},
            'Tags': [{'Key': 'Project', 'Value': 'pit-2gpu'}]}},
        'SchedulerRole': _role(f'{name}-scheduler', 'scheduler.amazonaws.com', [
            {'Effect': 'Allow', 'Action': 'lambda:InvokeFunction', 'Resource': {'Fn::GetAtt': ['Watchdog', 'Arn']}}]),
        'ScheduleGroup': {'Type': 'AWS::Scheduler::ScheduleGroup', 'Properties': {'Name': name}},
        'Expiry': {'Type': 'AWS::Scheduler::Schedule', 'Properties': {
            'Name': f'{name}-expiry', 'GroupName': {'Ref': 'ScheduleGroup'}, 'ScheduleExpression': 'rate(1 minute)',
            'StartDate': p['schedule_start'], 'EndDate': p['schedule_end'], 'State': 'ENABLED',
            'FlexibleTimeWindow': {'Mode': 'OFF'},
            'Target': {'Arn': {'Fn::GetAtt': ['Watchdog', 'Arn']}, 'RoleArn': {'Fn::GetAtt': ['SchedulerRole', 'Arn']},
                       'Input': '{}', 'RetryPolicy': {'MaximumRetryAttempts': 0}}}},
        'ExecutionRole': _role(f'{name}-execution', 'ecs-tasks.amazonaws.com', [logs(f'/pit-2gpu/{sid}/controller')]),
        'ControllerRole': _role(f'{name}-controller', 'ecs-tasks.amazonaws.com', [
            state_rw,
            {'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:PutObject', 's3:ListBucket'],
             'Resource': [f'arn:aws:s3:::{BUCKET}', f'arn:aws:s3:::{BUCKET}/*']},
            {'Effect': 'Allow', 'Action': 'secretsmanager:GetSecretValue',
             'Resource': [secret_arn(RUNPOD_SECRET), secret_arn(UPLOADER_SECRET), secret_arn(f'pit-2gpu/session/{sid}/ssh')]},
            decrypt_secrets,
            {'Effect': 'Allow', 'Action': 'lambda:InvokeFunction', 'Resource': {'Fn::GetAtt': ['Watchdog', 'Arn']}},
            {'Effect': 'Allow', 'Action': ['ecs:UpdateService', 'ecs:DescribeServices'], 'Resource': service_arn}]),
        'Cluster': {'Type': 'AWS::ECS::Cluster', 'Properties': {'ClusterName': cluster,
                                                                'Tags': [{'Key': 'Project', 'Value': 'pit-2gpu'}]}},
        # Retained: deregistering needs account-wide ecs:DeregisterTaskDefinition; old revisions are inert.
        'TaskDefinition': {'Type': 'AWS::ECS::TaskDefinition', 'DeletionPolicy': 'Retain', 'UpdateReplacePolicy': 'Retain',
                           'Properties': {
            'Family': f'{name}-controller', 'NetworkMode': 'awsvpc', 'RequiresCompatibilities': ['FARGATE'],
            'Cpu': '512', 'Memory': '1024', 'RuntimePlatform': {'OperatingSystemFamily': 'LINUX', 'CpuArchitecture': 'X86_64'},
            'ExecutionRoleArn': {'Fn::GetAtt': ['ExecutionRole', 'Arn']}, 'TaskRoleArn': {'Fn::GetAtt': ['ControllerRole', 'Arn']},
            'ContainerDefinitions': [{
                'Name': 'controller', 'Image': CONTROLLER_IMAGE, 'Essential': True,
                'EntryPoint': ['python3', '-c'], 'Command': [BOOT],
                'Environment': [{'Name': k, 'Value': v} for k, v in sorted(controller_env.items())],
                'LogConfiguration': {'LogDriver': 'awslogs', 'Options': {
                    'awslogs-group': f'/pit-2gpu/{sid}/controller', 'awslogs-region': REGION,
                    'awslogs-stream-prefix': 'controller'}}}]}},
        'Service': {'Type': 'AWS::ECS::Service', 'Properties': {
            'ServiceName': service, 'Cluster': {'Ref': 'Cluster'}, 'TaskDefinition': {'Ref': 'TaskDefinition'},
            'DesiredCount': int(p.get('desired_count', 0)), 'LaunchType': 'FARGATE',
            'DeploymentConfiguration': {'MinimumHealthyPercent': 0, 'MaximumPercent': 100},
            'NetworkConfiguration': {'AwsvpcConfiguration': {'AssignPublicIp': 'ENABLED', 'Subnets': p['subnets'],
                                                             'SecurityGroups': [p['security_group']]}},
            'Tags': [{'Key': 'Project', 'Value': 'pit-2gpu'}]}},
    }
    return {'AWSTemplateFormatVersion': '2010-09-09', 'Description': f'pit-2gpu session {sid} controls',
            'Resources': resources}


def body(template):
    text = json.dumps(template, separators=(',', ':'))
    if len(text) > 51200:
        raise ValueError('template too large for inline deployment')
    return text
