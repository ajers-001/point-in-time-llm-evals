"""Collected engine answers of bigevents-v1-engine -> the ANSWERS.json that engine_score.py takes.

    python reports/bigevents-v1/collect_answers.py COLLECTED_DIR OUT.json

COLLECTED_DIR holds the collected la1 sample files of both arms, in the collector's layout
(`results/<arm>/gpu/la1.samples.*.jsonl.gz`, as pit_bench.lookahead_bulk reads round 1a) or a tracked copy of it
(`<arm>/gpu/la1.samples.*.jsonl.gz`, as reports/jspace-v1/stage2/raw/stage2a). Each line is read as the existing
scorers read it (pit_bench.jspace.transcripts_from_samples: the doc's example_id and the first response). Writes
{"F-open": {example_id: text}, "F-pair": {...}}; every id must be one of reports/bigevents-v1/engine/prompts.json.
Missing answers are counted, not filled: the scorer treats them as unanswered.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pit_bench import jspace  # noqa: E402

PROMPTS = ROOT / 'reports/bigevents-v1/engine/prompts.json'
ARMS = ('F-open', 'F-pair')
TASK = 'la1'


def sample_files(collected, arm):
    collected = Path(collected)
    found = []
    for base in (collected / 'results' / arm / 'gpu', collected / arm / 'gpu'):
        found += sorted(base.glob(f'{TASK}.samples.*.jsonl.gz'))
    return found


def collect(collected, prompts=None):
    ids = {r['example_id'] for r in json.loads(Path(prompts or PROMPTS).read_text())}
    out, report = {}, {}
    for arm in ARMS:
        files = sample_files(collected, arm)
        answers = {}
        for r in jspace.transcripts_from_samples([f'{p}:{arm}' for p in files]):
            if r['key'] not in ids:
                raise ValueError(f"{arm}: answer to an unknown example id {r['key']!r} (not in prompts.json)")
            if r['key'] in answers and answers[r['key']] != r['output']:
                raise ValueError(f"{arm}: two different answers to {r['key']}")
            answers[r['key']] = r['output']
        out[arm] = dict(sorted(answers.items()))
        report[arm] = {'files': [str(p) for p in files], 'answers': len(answers), 'missing': len(ids - set(answers))}
    if not any(out.values()):
        raise ValueError(f'no {TASK} sample files under {collected}')
    return out, report


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        raise SystemExit(__doc__)
    out, report = collect(argv[0])
    Path(argv[1]).write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n')
    print(json.dumps(report, indent=1))


if __name__ == '__main__':
    main()
