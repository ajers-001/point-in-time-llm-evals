"""Kept for the record: this simulation used the labels ("clean", "leaks") of an earlier draft; the rules now report
numbers only, and what remains useful here is the precision (the range around the engine's leak).

Made-up models through the round 2 rules as revised on November 23: each answer ends with Yes/No and then a stated
probability. How often does each verdict come out, with ten answers per question? Run from the repository root.
Uses no model and no model answer.

A leak is detected if either the Yes/No score or the probability score shows it, each tested at p < 0.005.
"Clean" is judged on the probability score (the more precise one).
"""
import collections
import random
import sys
import warnings

import numpy as np

warnings.filterwarnings('ignore')
sys.path.insert(0, 'reports/bigevents-v2')
import score as s  # noqa: E402

e1 = s.e1
P_EITHER = 0.005


def above(t, p=0.01):
    return bool(t and 'coefficient' in t and t['coefficient'] > 0 and t['p_one_sided'] < p)
test = [r for r in s.rows() if r['set'] == 'test']


def arm(Q, share, n=10, yes_only=False):
    """(Yes/No observations, probability observations). `share`: questions whose outcome the model uses when dated."""
    yes, prob = [], []
    for r in Q:
        knows = random.random() < share
        py = (0.9 if int(r['resolved_yes']) else 0.1) if knows else float(
            np.clip(float(r['price_on_question_date']) + random.gauss(0, 0.12), 0.01, 0.99))
        yes.append((r, float(np.mean([random.random() < py for _ in range(n)]))))
        prob.append((r, float(np.mean([np.clip(py + random.gauss(0, 0.10), 0.01, 0.99) for _ in range(n)]))))
    return yes, prob


def detects(yes, prob, yes_only):
    if yes_only:
        return above(e1.encompassing(yes))
    return above(e1.encompassing(yes), P_EITHER) or above(e1.encompassing(prob), P_EITHER)


def verdict(plain, engine, yes_only=False):
    if not detects(*plain, yes_only):
        return 'plain shows no leak'
    if detects(*engine, yes_only):
        return 'leaks'
    i = 0 if yes_only else 1
    bar = 0.25 * s.leak_size(plain[i], draws=400)['size']
    return 'clean' if s.leak_size(engine[i], draws=400)['ci95'][1] <= bar else 'not shown clean'


if __name__ == '__main__':
    random.seed(5)
    N = 100
    for name, ps, es in (('clean engine, plain knows 6%', .06, 0), ('clean engine, plain knows 10%', .10, 0),
                         ('clean engine, plain knows 30%', .30, 0), ('engine leaks 1% (plain 10%)', .10, .01),
                         ('engine leaks 2% (plain 10%)', .10, .02), ('engine leaks 3% (plain 10%)', .10, .03)):
        for label, yes_only in (('Yes/No and probability', False), ('Yes/No only', True)):
            c = collections.Counter(verdict(arm(test, ps), arm(test, es), yes_only) for _ in range(N))
            print(f'{name:32s} {label:24s}', {k: round(v / N, 2) for k, v in sorted(c.items())})
