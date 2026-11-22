"""Merge the prior verified audit with newly committed outputs; report all 132 requests.

Prior results come from the hash-pinned final audit through the existing
``analyze_audit.py`` path (which re-verifies provenance). New results are the
canonical, read-back-verified attempt archives from the cloud session. A run
id present in both sources, or with conflicting hashes, is a hard error.
Frozen evaluators run over the merged set; cadence stays separate. Output
lineage uses paths relative to the output directory. No semantic review is
fabricated: unreviewed outputs remain unreviewed and no success is promoted.
"""
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

from .artifacts import safe_members, verify_attempt
from .inputs import PLAN_SHA256, PRIOR_AUDIT_SHA256

ROOT = Path(__file__).resolve().parents[2]


def _analyze_module(root):
    path = root / 'reports/resume-20261006/analyze_audit.py'
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('analyze_audit', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _relativize(output):
    base = str(output.resolve()) + '/'
    for path in output.rglob('*.json'):
        text = path.read_text()
        if base in text:
            path.write_text(text.replace(base, ''))


def merge(prior_archive, session_id, jobs, attempts, output, root=ROOT, verify_provenance=True, evaluate=True):
    """``jobs``: durable job rows; ``attempts``: [{'run_id','attempt_id','key','body'}] for all stored attempts."""
    root, output = Path(root), Path(output)
    if hashlib.sha256(Path(prior_archive).read_bytes()).hexdigest() != PRIOR_AUDIT_SHA256:
        raise ValueError('Prior audit hash mismatch')
    output.mkdir(parents=True, exist_ok=False)
    analyze = _analyze_module(root)
    analyze.analyze(prior_archive, output / 'prior-analysis')
    plan = json.loads((root / 'reports/recovery-20261006/followup-plan.json').read_text())
    if hashlib.sha256((root / 'reports/recovery-20261006/followup-plan.json').read_bytes()).hexdigest() != PLAN_SHA256:
        raise ValueError('Plan hash mismatch')
    planned = {r['run_id']: r for r in plan['runs']}
    flat = output / 'results'
    flat.mkdir()
    lineage = {}
    for d in sorted((output / 'prior-analysis/results').iterdir()):
        shutil.copytree(d, flat / d.name)
        result = json.loads((d / 'gpu/result.json').read_text())
        lineage[d.name] = {'source': 'prior_final_audit', 'audit_sha256': PRIOR_AUDIT_SHA256,
                           'output_sha256': result.get('output_sha256'), 'status': result.get('status')}
    by_run = {j['run_id']: j for j in jobs}
    evidence = output / 'attempt-evidence'
    for a in sorted(attempts, key=lambda x: (x['run_id'], x['attempt_id'])):
        job = by_run[a['run_id']]
        summary = verify_attempt(a['body'], session_id, job, a['attempt_id'])
        target = evidence / a['run_id'] / (a['attempt_id'] + '.tar.gz')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(a['body'])
        canonical = job.get('canonical_attempt') == a['attempt_id'] and job['state'] == 'SUCCEEDED'
        if canonical and job.get('canonical_sha256') != summary['sha256']:
            raise ValueError(f'Canonical hash conflict for {a["run_id"]}')
        if not canonical:
            continue
        if a['run_id'] in lineage:
            raise ValueError(f'Duplicate result across sources: {a["run_id"]}')
        members = safe_members(a['body'])
        prefix = f'output/{a["run_id"]}/gpu/'
        dest = flat / a['run_id'] / 'gpu'
        dest.mkdir(parents=True)
        for name, data in members.items():
            if name.startswith(prefix):
                (dest / name[len(prefix):]).parent.mkdir(parents=True, exist_ok=True)
                (dest / name[len(prefix):]).write_bytes(data)
        if verify_provenance:
            sys.path.insert(0, str(root))
            from evaluation.verify_gpu_evidence import verify
            from pit_runtime.runner import load_run
            result = json.loads((dest / 'result.json').read_text())
            verify(dest, load_run(root / planned[a['run_id']]['prepared'], a['run_id']), result)
        lineage[a['run_id']] = {'source': 'cloud_two_gpu', 'session_id': session_id, 'attempt_id': a['attempt_id'],
                                'object_key': a['key'], 'archive_sha256': summary['sha256'],
                                'output_sha256': summary['output_sha256'], 'status': summary['result_status']}
    coverage = []
    for rid, item in planned.items():
        job = by_run.get(rid, {})
        if rid in lineage:
            status = 'completed' if lineage[rid]['status'] == 'completed' else lineage[rid]['status']
        else:
            status = {'PENDING': 'unattempted', 'INTERRUPTED_DEADLINE': 'interrupted', 'FAILED_INFRA': 'failed_infrastructure',
                      'FAILED_SCIENTIFIC': 'failed_scientific', 'BLOCKED': 'blocked'}.get(job.get('state'), 'unattempted')
            if job.get('state') in ('CLAIMED', 'RUNNING', 'UPLOADING', 'RECONCILING'):
                status = 'unresolved_in_flight'
        coverage.append({'run_id': rid, 'category': item['category'], 'status': status,
                         'attempts': job.get('attempt_seq', 0), 'source': lineage.get(rid, {}).get('source')})
    counts = {}
    for row in coverage:
        counts.setdefault(row['category'], {}).setdefault(row['status'], 0)
        counts[row['category']][row['status']] += 1
    if evaluate:
        if any(r.startswith('R-') for r in lineage):
            from evaluation.robustness import report
            report(root / 'evaluation/robustness-development-v2/catalog.json', root / 'evaluation/robustness_policy_v2.json',
                   root / 'prepared/robustness-development-prewarm-v3', flat, output / 'robustness')
        if any(r.startswith('U-') for r in lineage):
            from evaluation.utility import evaluate as utility
            utility(root / 'evaluation/utility-prewarm-v2', flat, output / 'utility.json')
        if any(r.startswith('T-') for r in lineage):
            from evaluation.evaluate_temporal import evaluate as temporal
            temporal(root / 'prepared/temporal-batch-parity-v2', root / 'evaluation/upstream-probes-v1', flat,
                     output / 'retained-temporal.json', metadata=root / 'artifacts/metadata-v1')
    summary = {'denominator': len(planned), 'plan_sha256': PLAN_SHA256, 'prior_audit_sha256': PRIOR_AUDIT_SHA256,
               'session_id': session_id, 'counts_by_category': counts,
               'completed': sum(r['status'] == 'completed' for r in coverage), 'coverage': coverage, 'lineage': lineage,
               'cadence': 'separate: prior-analysis/cadence-comparison.json (20 outputs, not part of this denominator)',
               'semantic_reviews': 'outstanding; blinded packets required; none fabricated or inferred',
               'scientific_validity_claim': False}
    (output / 'merge-summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    _relativize(output)
    return {k: v for k, v in summary.items() if k not in ('coverage', 'lineage')}
