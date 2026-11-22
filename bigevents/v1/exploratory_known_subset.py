"""Exploratory, after the results were seen (not part of ENGINE_RULES.md): the dated leak on only the test questions
plain Qwen got right in the open version, with the same selection applied to the floor as a control.

Picking questions by "the model's answer matched the outcome" builds in a signal by itself: on the floor, where
nothing can be known, the picked questions are lucky guesses, and any answer that shares the model's leanings then
looks as if it knew. The floor row shows how large that built-in signal is.

    python reports/bigevents-v1/exploratory_known_subset.py
"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine_score as e  # noqa: E402
import memory_screen as ms  # noqa: E402

a = json.loads((HERE / 'engine/answers.json').read_text())
lens = json.loads((HERE / 'lens/lens.json').read_text())
rows = {r['key']: r for r in ms.rows()}
sets = {'test': [], 'floor': []}
for q in json.loads((HERE / 'engine/questions.json').read_text()):
    sets[q['set']].append(rows[q['key']])


def y(r):
    return int(r['resolved_yes'])


def right_open(r):
    s = e.read_open(a['F-open'][r['key'] + '__open'])
    return s is not None and (s > 0) == bool(y(r))


def report(name, S):
    out = {'questions': len(S), 'stories': len({r['story'] for r in S})}
    for arm in ('F-open', 'F-pair'):
        pr = [(r, e.read_dated(a[arm][r['key'] + '__pit'])[0]) for r in S]
        pr = [(r, p) for r, p in pr if p is not None]
        t = e.encompassing(pr)
        out[arm] = {'probability_on_what_happened': round(float(np.mean([p if y(r) else 1 - p for r, p in pr])), 3),
                    'coefficient': round(t['coefficient'], 3), 'ci95': [round(v, 3) for v in t['ci95']],
                    'p_one_sided': float(f"{t['p_one_sided']:.2g}")}
    ll = [(r, lens.get(r['key'] + '__pit')) for r in S]
    t = e.encompassing([(r, 1.0 if v > 0 else -1.0) for r, v in ll if v])
    out['lens'] = {'coefficient': round(t['coefficient'], 3), 'p_one_sided': float(f"{t['p_one_sided']:.2g}")}
    print(name, json.dumps(out))


report('test, all', sets['test'])
report('test, plain Qwen right in the open version', [r for r in sets['test'] if right_open(r)])
report('floor, all', sets['floor'])
report('floor, plain Qwen "right" in the open version (lucky guesses)', [r for r in sets['floor'] if right_open(r)])
