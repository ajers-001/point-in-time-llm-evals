"""Collect a finished session's verified results from durable state and private S3, then merge.

    python -m infra.cloud_two_gpu.collect --session S.json --output reports/cloud-two-gpu-20261006/final-analysis

Reads only: jobs/attempts from DynamoDB and each stored per-attempt archive from S3.
Every archive is re-hashed against the durable record before the merge, which
re-verifies identities and provenance and reports all 132 requests.
"""
import argparse
import hashlib
import json
from pathlib import Path

from . import stack
from .artifacts import S3Objects, canonical_key
from .inputs import PRIOR_AUDIT
from .merge import ROOT, merge
from .state import Session
from .store import DynamoStore


def collect(session_id, output, verify_provenance=True):
    import boto3
    aws = boto3.session.Session(region_name=stack.REGION)
    session = Session(DynamoStore(stack.TABLE, aws.client('dynamodb')), session_id)
    objects = S3Objects(stack.BUCKET, aws.client('s3'))
    jobs = session.jobs()
    attempts = []
    for att in session.attempts():
        key = att.get('archive_key') or canonical_key(session_id, att['run_id'], att['attempt_id'])
        body = objects.get(key)
        if body is None:
            continue  # never uploaded (e.g. lost with a pod); reported as missing evidence in coverage
        if att.get('archive_sha256') and hashlib.sha256(body).hexdigest() != att['archive_sha256']:
            raise ValueError(f'Stored archive hash differs from durable record: {key}')
        attempts.append({'run_id': att['run_id'], 'attempt_id': att['attempt_id'], 'key': key, 'body': body})
    summary = merge(ROOT / PRIOR_AUDIT, session_id, jobs, attempts, output, verify_provenance=verify_provenance)
    state = {'session_id': session_id, 'meta_status': session.meta()['status'],
             'job_states': {j['run_id']: j['state'] for j in jobs if not j['prior']},
             'attempts_collected': len(attempts), 'attempts_recorded': len(session.attempts()),
             'conflicts': session.store.query(session.pk, 'CONFLICT#'),
             'readiness': session.store.query(session.pk, 'RDY#'),
             'pods': session.store.query(session.pk, 'POD#'),
             'watchdog': session.store.query(session.pk, 'WATCH#')}
    (Path(output) / 'session-state.json').write_text(json.dumps(state, indent=2, default=str, sort_keys=True) + '\n')
    return summary


def main():
    from .cli import load_session
    p = argparse.ArgumentParser()
    p.add_argument('--session', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    print(json.dumps(collect(load_session(a.session)['session_id'], a.output), indent=2, default=str))


if __name__ == '__main__':
    main()
