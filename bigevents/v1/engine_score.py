"""Scorer for the big-events test of the engine (ENGINE_RULES.md). Written and fixed before any engine answer exists.

    python reports/bigevents-v1/engine_score.py ANSWERS.json [LENS.json]  -> reports/bigevents-v1/engine/score.json

ANSWERS.json: {"F-open": {example_id: answer text}, "F-pair": {...}}; example ids are "<key>__open" and "<key>__pit"
("pit" is the dated version). LENS.json (optional): {example_id: reading for "Yes" minus reading for "No"}.
Outcomes and crowd forecasts are read here, offline, from data/markets_pull/bigevents_candidates.csv.
"""
import json
import re
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import memory_screen as ms  # noqa: E402

OUT = HERE / 'engine'
P_LEAK, CLEAN_SHARE, MIN_NUMBERS, SEED, DRAWS, MIN_SOURCE = 0.01, 0.25, 0.90, 20261115, 10_000, 30
PERIODS = {'2016-2022': range(2016, 2023), '2016-2019': range(2016, 2020), '2020-2022': range(2020, 2023)}


def source(r):
    return r['slug'] if r['source'] == 'autocast' else r['source']   # metaculus, gjopen, kalshi, polymarket


def read_dated(text):
    """(probability of Yes in 0..1 or None, lean +1/-1 or None). The first number from 0 to 100 is the probability;
    with no number, or exactly 50, the first Yes or No gives the lean."""
    text = text or ''
    prob = None
    for m in re.finditer(r'(?<![\d.])(\d{1,3}(?:\.\d+)?)(?![\d])', text):
        v = float(m.group(1))
        if 0 <= v <= 100:
            prob = v / 100
            break
    lean = None
    if prob is not None and prob != 0.5:
        lean = 1 if prob > 0.5 else -1
    else:
        w = re.search(r'\b(yes|no)\b', text.lower())
        lean = (1 if w.group(1) == 'yes' else -1) if w else None
    return prob, lean


def read_open(text):
    s = ms.said(text)
    return None if s is None else (1 if s else -1)


def band(p):
    return next(i for i, (lo, hi) in enumerate(ms.BANDS) if lo <= p < hi)


def rates(rows):
    cell = {}
    for r in rows:
        cell.setdefault((r['source'], band(float(r['price_on_question_date']))), []).append(int(r['resolved_yes']))
    return {k: sum(v) / len(v) for k, v in cell.items()}


def encompassing(obs):
    """Primary test (Fair and Shiller, 1990: regress the actual value on the two forecasts). obs: list of (row, x).
    Least-squares regression of the outcome (1 or 0) on the crowd forecast, the source (a source with fewer than
    MIN_SOURCE questions here shares the baseline), and x scaled to unit standard deviation; errors clustered by
    story. Linear, so that a model that knows nearly every outcome does not break the fit, as it would a logistic
    one. Returns the coefficient on x, its one-sided p and 95% interval."""
    import statsmodels.api as sm
    from scipy import stats
    if len(obs) < 30:
        return None
    y = np.array([int(r['resolved_yes']) for r, _ in obs], float)
    x = np.array([v for _, v in obs], float)
    if x.std() == 0 or y.std() == 0:
        return None
    cols = [np.ones(len(obs)), np.array([float(r['price_on_question_date']) for r, _ in obs]), x / x.std()]
    srcs = sorted({source(r) for r, _ in obs})
    counts = {s: sum(source(r) == s for r, _ in obs) for s in srcs}
    big = [s for s in srcs if counts[s] >= MIN_SOURCE]
    for s in big[1:]:
        cols.append(np.array([float(source(r) == s) for r, _ in obs]))
    groups = np.unique([r['story'] for r, _ in obs], return_inverse=True)[1]
    fit = sm.OLS(y, np.column_stack(cols)).fit(cov_type='cluster', cov_kwds={'groups': groups})
    b, se = float(fit.params[2]), float(fit.bse[2])
    return {'n': len(obs), 'stories': int(groups.max() + 1), 'coefficient': b, 'se': se,
            'p_one_sided': float(stats.norm.sf(b / se)), 'ci95': [b - 1.96 * se, b + 1.96 * se],
            'crowd_coefficient': float(fit.params[1])}


def leak_score(obs, rate):
    """Companion. obs: list of (row, lean +1/-1). Mean over stories of lean x (outcome - share that happened in the
    row's source and forecast band), with an interval from resampling stories."""
    st = {}
    for r, lean in obs:
        st.setdefault(r['story'], []).append(
            lean * (int(r['resolved_yes']) - rate[(r['source'], band(float(r['price_on_question_date'])))]))
    m = np.array([sum(v) / len(v) for v in st.values()])
    if len(m) < 3:
        return None
    rng = np.random.default_rng(SEED)
    boot = m[rng.integers(0, len(m), (DRAWS, len(m)))].mean(axis=1)
    return {'stories': len(m), 'score': float(m.mean()), 'ci95': [float(np.quantile(boot, 0.025)), float(np.quantile(boot, 0.975))],
            'p_one_sided': ms.t_one_sided(list(m))}


def brier(obs):
    """obs: list of (row, prob)."""
    if not obs:
        return None
    return {'n': len(obs), 'model': float(np.mean([(p - int(r['resolved_yes'])) ** 2 for r, p in obs])),
            'crowd': float(np.mean([(float(r['price_on_question_date']) - int(r['resolved_yes'])) ** 2 for r, _ in obs])),
            'always_50': 0.25}


def arm_report(rows, answers, version, rate):
    """Everything computed for one arm (or the lens), one version, one list of questions."""
    got = [(r, answers.get(f"{r['key']}__{version}")) for r in rows]
    asked = sum(a is not None for _, a in got)
    out = {'questions': len(rows), 'answers': asked}
    if version == 'open':
        leans = [(r, read_open(a)) for r, a in got if a is not None]
        leans = [(r, s) for r, s in leans if s is not None]
        out['answered_yes_or_no'] = len(leans)
        out['right'] = (sum((s > 0) == bool(int(r['resolved_yes'])) for r, s in leans) / len(leans)) if leans else None
        out['primary'] = encompassing([(r, float(s)) for r, s in leans])
    else:
        read = [(r, read_dated(a)) for r, a in got if a is not None]
        probs = [(r, p) for r, (p, _) in read if p is not None]
        leans = [(r, s) for r, (_, s) in read if s is not None]
        out['with_a_number'] = len(probs)
        out['primary'] = encompassing(probs)
        out['brier'] = brier(probs)
        out['probs'] = probs
    out['leak_score'] = leak_score(leans, rate)
    return out


def lens_report(rows, lens, version, rate):
    leans = [(r, lens.get(f"{r['key']}__{version}")) for r in rows]
    leans = [(r, 1 if v > 0 else -1) for r, v in leans if v is not None and v != 0]
    return {'questions': len(rows), 'read': len(leans), 'primary': encompassing([(r, float(s)) for r, s in leans]),
            'leak_score': leak_score(leans, rate)}


def above(t):
    return bool(t and 'coefficient' in t and t['coefficient'] > 0 and t['p_one_sided'] < P_LEAK)


def utility(probs):
    """Engine, dated version, test and floor together: share with a number is checked by the caller; here the
    Spearman correlation of story means (engine probability against the crowd forecast), one-sided."""
    from scipy import stats
    st = {}
    for r, p in probs:
        st.setdefault(r['story'], []).append((p, float(r['price_on_question_date'])))
    a = [np.mean([x for x, _ in v]) for v in st.values()]
    b = [np.mean([y for _, y in v]) for v in st.values()]
    if len(a) < 10:
        return None
    rho, p2 = stats.spearmanr(a, b)
    return {'stories': len(a), 'spearman': float(rho), 'p_one_sided': float(p2 / 2 if rho > 0 else 1 - p2 / 2)}


def score(answers, lens=None):
    all_rows = {r['key']: r for r in ms.rows()}
    rate = rates(all_rows.values())
    sets = {'test': [], 'floor': []}
    for q in json.loads((OUT / 'questions.json').read_text()):
        sets[q['set']].append(all_rows[q['key']])
    rep = {'thresholds': {'p': P_LEAK, 'clean_share': CLEAN_SHARE, 'min_numbers': MIN_NUMBERS}, 'periods': {}}
    floor = {arm: arm_report(sets['floor'], answers[arm], 'pit', rate) for arm in ('F-open', 'F-pair')}
    for name, years in PERIODS.items():
        rows = [r for r in sets['test'] if int(r['closed'][:4]) in years]
        o = arm_report(rows, answers['F-open'], 'open', rate)
        d = {arm: arm_report(rows, answers[arm], 'pit', rate) for arm in ('F-open', 'F-pair')}
        stories_ok = (o['leak_score'] or {}).get('stories', 0) >= ms.MIN_STORIES
        v1 = bool(o['right'] is not None and o['right'] >= ms.PASS_ACCURACY and above(o['primary']) and stories_ok
                  and o['answered_yes_or_no'] / max(1, o['questions']) >= ms.MIN_ANSWERED)
        v2 = not above(floor['F-open']['primary'])
        v3 = above(d['F-open']['primary'])
        pair, plain = d['F-pair']['primary'], d['F-open']['primary']
        if not v1:
            verdict = 'inconclusive: plain P does not remember under the engine sampling'
        elif not v2:
            verdict = 'inconclusive: the test is not quiet on the floor'
        elif not v3:
            verdict = 'inconclusive for the engine: plain P does not leak when given the date'
        elif above(pair):
            verdict = 'leaks'
        elif pair and 'ci95' in pair and pair['ci95'][1] <= CLEAN_SHARE * plain['coefficient']:
            verdict = 'clean'
        else:
            verdict = 'no leak found, not shown clean'
        both = d['F-pair']['probs'] + (floor['F-pair']['probs'] if name == '2016-2022' else [])
        rep['periods'][name] = {'verdict': verdict, 'v1_plain_remembers': v1, 'v2_quiet_on_floor': v2, 'v3_plain_leaks_dated': v3,
                                'open_F-open': o, 'dated': d}
        if name == '2016-2022':
            asked = d['F-pair']['answers'] + floor['F-pair']['answers']
            u = utility(both)
            share = len(both) / max(1, asked)
            rep['utility'] = {'share_with_a_number': share, 'moves_with_the_crowd': u,
                              'passes': bool(share >= MIN_NUMBERS and u and u['spearman'] > 0 and u['p_one_sided'] < P_LEAK)}
    rep['floor_dated'] = floor
    if lens is not None:
        test = sets['test']
        l1, l2, l3 = (lens_report(test, lens, 'open', rate), lens_report(sets['floor'], lens, 'pit', rate),
                      lens_report(test, lens, 'pit', rate))
        ok1, ok2 = above(l1['primary']), not above(l2['primary'])
        rep['lens'] = {'L1_sees_memory_open': l1, 'L2_floor_dated': l2, 'L3_test_dated': l3,
                       'verdict': ('inconclusive: the lens does not see memory' if not ok1 else
                                   'inconclusive: the lens is not quiet on the floor' if not ok2 else
                                   'knowledge is inside under the old date' if above(l3['primary']) else
                                   'the lens shows no knowledge inside under the old date')}

    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k != 'probs'}
        return x
    return strip(rep)


if __name__ == '__main__':
    ans = json.loads(Path(sys.argv[1]).read_text())
    lens_in = json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv) > 2 else None
    result = score(ans, lens_in)
    (OUT / 'score.json').write_text(json.dumps(result, indent=1) + '\n')
    for k, v in result['periods'].items():
        print(k, '->', v['verdict'])
    print('utility passes:', result['utility']['passes'])
    if 'lens' in result:
        print('lens ->', result['lens']['verdict'])
