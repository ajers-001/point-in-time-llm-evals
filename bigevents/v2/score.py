"""Round 2 scorer (ROUND2_RULES.md). Written and tested on made-up answers before any round 2 answer exists.

    python reports/bigevents-v2/score.py ANSWERS.json [LENS.json]   -> reports/bigevents-v2/score.json

ANSWERS.json: {"F-open": {example_id: [answer per seed, ...]}, "F-pair": {...}}. Example ids are
"<key>__open", "<key>__open_flip", "<key>__pit", "<key>__pit_flip". LENS.json: {example_id: lead}, the lead being
lens "yes" cells minus "no" cells by the step 1 rule (reports/bigevents-v1/lens_to_scores.py).
Outcomes and crowd forecasts are read here, offline, from the candidate files; they never enter the sandbox.
"""
import csv
import json
import re
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'reports/bigevents-v1'))
import engine_score as e1  # noqa: E402  (encompassing test, source of each question, story resampling seed)
import memory_screen as ms  # noqa: E402

P_LEAK, CLEAN_SHARE, MIN_NUMBERS, MIN_ANSWERED, MIN_STORIES = 0.01, 0.25, 0.90, 0.50, 20
MONTHS = r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?'
DATE_NUMBER = re.compile(rf'\b{MONTHS}\s+\d{{1,2}}\b|\b\d{{1,2}}\s+{MONTHS}|\b\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?\b|\b\d{{4}}\b', re.I)


def rows():
    """Every question of round 2 with its labels and its round 2 story and set."""
    by_key = {r['key']: r for r in ms.rows()}
    for r in csv.DictReader(open(ROOT / 'data/markets_pull/bigevents_v2_candidates.csv')):
        r['key'] = f"autocast:{r['market_id']}"
        by_key.setdefault(r['key'], r)
    out = []
    for q in json.loads((HERE / 'questions.json').read_text()):
        r = dict(by_key[q['key']])
        r.update(story=q['story'], set=q['set'], origin=q['origin'])
        out.append(r)
    return out


def number(text):
    """The first number from 0 to 100 in the answer that is not part of a date (a number next to a month name, a
    day/month form, or any four-digit number)."""
    text = text or ''
    masked = DATE_NUMBER.sub(lambda m: ' ' * len(m.group(0)), text)
    for m in re.finditer(r'(?<![\d.])(\d{1,3}(?:\.\d+)?)(?![\d])', masked):
        v = float(m.group(1))
        if 0 <= v <= 100:
            return v / 100
    return None


def first_word(text):
    w = re.search(r'\b(yes|no)\b', (text or '').lower())
    return None if not w else (1 if w.group(1) == 'yes' else -1)


def last_word(text):
    """Open versions: the last of Yes, No, Unknown (round 1 rule). +1 Yes, -1 No, 0 Unknown or nothing."""
    w = re.findall(r'\b(yes|no|unknown)\b', (text or '').lower())
    return {'yes': 1, 'no': -1}.get(w[-1], 0) if w else 0


def prob_yes(text, flip):
    """Probability that the question resolves Yes, from one dated answer. The number is the probability of the
    word the prompt asked about (Yes, or No in the mirror prompt); with no number, the first Yes or No gives 0.75
    or 0.25 for that word. None when the answer has neither."""
    p = number(text)
    if p is None:
        w = first_word(text)
        p = None if w is None else (0.75 if w > 0 else 0.25)
    if p is None:
        return None
    return 1 - p if flip else p


def dated(arm_answers, key):
    """Combined probability of Yes for one question: the mean over seeds and over the two mirror prompts.
    Also returns how many of the answers carried a number, and the two halves for the consistency report."""
    orig = [prob_yes(t, False) for t in arm_answers.get(f'{key}__pit', [])]
    flip = [prob_yes(t, True) for t in arm_answers.get(f'{key}__pit_flip', [])]
    numbers = sum(number(t) is not None for t in arm_answers.get(f'{key}__pit', []) + arm_answers.get(f'{key}__pit_flip', []))
    asked = len(arm_answers.get(f'{key}__pit', [])) + len(arm_answers.get(f'{key}__pit_flip', []))
    o, f = [p for p in orig if p is not None], [p for p in flip if p is not None]
    both = o + f
    return (float(np.mean(both)) if both else None, numbers, asked,
            float(np.mean(o)) if o else None, float(np.mean(f)) if f else None)


def recall(arm_answers, key):
    """Open versions: mean lean over seeds and the two mirror prompts (+1 says it happened, -1 says it did not,
    0 Unknown), and whether any of them said Yes or No."""
    leans = [last_word(t) for t in arm_answers.get(f'{key}__open', [])]
    leans += [-last_word(t) for t in arm_answers.get(f'{key}__open_flip', [])]
    if not leans:
        return None, False
    return float(np.mean(leans)), any(v != 0 for v in leans)


def leak_size(obs, other=None, seed=e1.SEED, draws=e1.DRAWS):
    """Leak in probability units: the mean product of the model's and the outcome's departures from what the crowd
    forecast and the source predict (each residualized by least squares). Zero when the answers carry nothing
    beyond the crowd; it does not grow when a model's answers merely sit close to the crowd. Interval by
    resampling stories. With `other` (the plain arm's observations on the same questions), also the ratio
    engine/plain with a paired interval."""
    def parts(o):
        Q = [r for r, _ in o]
        x = np.array([v for _, v in o], float)
        y = np.array([int(r['resolved_yes']) for r in Q], float)
        srcs = sorted({e1.source(r) for r in Q})
        big = [t for t in srcs if sum(e1.source(r) == t for r in Q) >= e1.MIN_SOURCE]
        X = np.column_stack([np.ones(len(Q)), [float(r['price_on_question_date']) for r in Q]] +
                            [[float(e1.source(r) == t) for r in Q] for t in big[1:]])
        rx = x - X @ np.linalg.lstsq(X, x, rcond=None)[0]
        ry = y - X @ np.linalg.lstsq(X, y, rcond=None)[0]
        return rx * ry, [r['story'] for r in Q]
    if len(obs) < 30:
        return None
    prod, st = parts(obs)
    groups = {}
    for i, k in enumerate(st):
        groups.setdefault(k, []).append(i)
    keys = list(groups)
    rng = np.random.default_rng(seed)
    picks = [rng.integers(0, len(keys), len(keys)) for _ in range(draws)]
    idx = [np.concatenate([groups[keys[j]] for j in pick]) for pick in picks]
    boot = np.array([prod[i].mean() for i in idx])
    out = {'size': float(prod.mean()), 'ci95': [float(np.quantile(boot, 0.025)), float(np.quantile(boot, 0.975))]}
    if other is not None:
        k_obs = {r['key'] for r, _ in obs}
        oth = [(r, v) for r, v in other if r['key'] in k_obs]
        mine = {r['key'] for r, _ in oth}
        same = [(r, v) for r, v in obs if r['key'] in mine]
        if len(same) >= 30:
            a, _ = parts(sorted(same, key=lambda t: t[0]['key']))
            b, st2 = parts(sorted(oth, key=lambda t: t[0]['key']))
            g2 = {}
            for i, k in enumerate(st2):
                g2.setdefault(k, []).append(i)
            ks = list(g2)
            rng = np.random.default_rng(seed)
            ratios = []
            for _ in range(draws):
                i = np.concatenate([g2[ks[j]] for j in rng.integers(0, len(ks), len(ks))])
                ratios.append(a[i].mean() / b[i].mean() if b[i].mean() != 0 else np.nan)
            ratios = np.array(ratios)
            out['ratio_to_plain'] = float(a.mean() / b.mean()) if b.mean() != 0 else None
            out['ratio_ci95'] = [float(np.nanquantile(ratios, 0.025)), float(np.nanquantile(ratios, 0.975))]
    return out


def above(t):
    return bool(t and 'coefficient' in t and t['coefficient'] > 0 and t['p_one_sided'] < P_LEAK)


def brier(obs):
    if not obs:
        return None
    y = np.array([int(r['resolved_yes']) for r, _ in obs], float)
    p = np.array([v for _, v in obs], float)
    c = np.array([float(r['price_on_question_date']) for r, _ in obs])
    return {'n': len(obs), 'model': float(np.mean((p - y) ** 2)), 'crowd': float(np.mean((c - y) ** 2)), 'always_50': 0.25}


def story_interval(obs, stat, seed=e1.SEED, draws=e1.DRAWS):
    """95% interval of stat(obs) by resampling stories."""
    by = {}
    for o in obs:
        by.setdefault(o[0]['story'], []).append(o)
    groups = list(by.values())
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(draws):
        pick = rng.integers(0, len(groups), len(groups))
        vals.append(stat([o for i in pick for o in groups[i]]))
    return [float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975))]


def arm_dated(qs, ans):
    obs, numbers, asked, halves = [], 0, 0, []
    for r in qs:
        p, n, a, o, f = dated(ans, r['key'])
        numbers, asked = numbers + n, asked + a
        if p is not None:
            obs.append((r, p))
        if o is not None and f is not None:
            halves.append(abs(o - f))
    return {'questions': len(qs), 'with_a_probability': len(obs), 'share_with_a_number': numbers / max(1, asked),
            'primary': e1.encompassing(obs) if len({round(p, 9) for _, p in obs}) > 1 else None,
            'leak_size': leak_size(obs),
            'brier': brier(obs), 'brier_ci95': story_interval(obs, lambda x: brier(x)['model']) if obs else None,
            'mirror_gap': float(np.mean(halves)) if halves else None, 'obs': obs}


def arm_open(qs, ans):
    obs, answered = [], 0
    for r in qs:
        lean, any_answer = recall(ans, r['key'])
        if lean is not None:
            answered += any_answer
            if any_answer:
                obs.append((r, lean))
    right = [((v > 0) == bool(int(r['resolved_yes']))) for r, v in obs if v != 0]
    return {'questions': len(qs), 'answered': answered, 'share_answered': answered / max(1, len(qs)),
            'stories_answered': len({r['story'] for r, _ in obs}), 'right': float(np.mean(right)) if right else None,
            'primary': e1.encompassing(obs) if len({v for _, v in obs}) > 1 else None}


def lens_report(qs, lens, version):
    obs = []
    for r in qs:
        a, b = lens.get(f"{r['key']}__{version}"), lens.get(f"{r['key']}__{version}_flip")
        if a is not None and b is not None:
            obs.append((r, float(a - b)))
    return {'questions': len(qs), 'read': len(obs), 'primary': e1.encompassing(obs) if len({v for _, v in obs}) > 1 else None}


def utility(obs):
    from scipy import stats
    by = {}
    for r, p in obs:
        by.setdefault(r['story'], []).append((p, float(r['price_on_question_date'])))
    a = [np.mean([x for x, _ in v]) for v in by.values()]
    b = [np.mean([y for _, y in v]) for v in by.values()]
    if len(a) < 10 or np.std(a) == 0:
        return None
    rho, p2 = stats.spearmanr(a, b)
    return {'stories': len(a), 'spearman': float(rho), 'p_one_sided': float(p2 / 2 if rho > 0 else 1 - p2 / 2)}


def score(answers, lens=None):
    qs = rows()
    sets = {s: [r for r in qs if r['set'] == s] for s in ('test', 'floor', 'long')}
    test, floor = sets['test'], sets['floor']
    rep = {'thresholds': {'p': P_LEAK, 'clean_share': CLEAN_SHARE, 'min_numbers': MIN_NUMBERS,
                          'min_answered': MIN_ANSWERED, 'min_stories': MIN_STORIES}}
    po, pf = arm_open(test, answers['F-open']), arm_open(floor, answers['F-open'])
    eo, ef = arm_open(test, answers['F-pair']), arm_open(floor, answers['F-pair'])
    pd_, pdf = arm_dated(test, answers['F-open']), arm_dated(floor, answers['F-open'])
    ed, edf = arm_dated(test, answers['F-pair']), arm_dated(floor, answers['F-pair'])
    v1 = above(po['primary']) and po['share_answered'] >= MIN_ANSWERED and po['stories_answered'] >= MIN_STORIES
    v2 = not above(pdf['primary'])
    v3 = above(pd_['primary'])
    if not v1:
        verdict = 'inconclusive: plain P does not remember under the engine settings'
    elif not v2:
        verdict = 'inconclusive: the test is not quiet on the floor'
    elif not v3:
        verdict = 'inconclusive for the engine: plain P does not leak when given the date'
    elif ed['primary'] is None:
        verdict = 'uninformative: the engine gives the same probability to every question'
    elif above(ed['primary']):
        verdict = 'leaks'
    elif ed['leak_size']['ci95'][1] <= CLEAN_SHARE * pd_['leak_size']['size']:
        verdict = 'clean'
    else:
        verdict = 'no leak found, not shown clean'
    removed = None
    if v3 and ed['primary'] is not None:
        paired = leak_size(ed['obs'], other=pd_['obs'])
        if paired and paired.get('ratio_to_plain') is not None:
            removed = {'point': 1 - paired['ratio_to_plain'],
                       'ci95': [1 - paired['ratio_ci95'][1], 1 - paired['ratio_ci95'][0]]}
    recall_2026 = ('the engine recalls outcomes when told it is 2026' if above(eo['primary']) and not above(ef['primary'])
                   else 'not shown' if not above(eo['primary']) else 'inconclusive: the engine is not quiet on the floor')
    both = ed['obs'] + edf['obs']
    u = utility(both)
    bt = brier(both)
    rep['verdicts'] = {
        'V1_plain_remembers': v1, 'V2_quiet_on_floor': v2, 'V3_plain_leaks_dated': v3, 'V4_engine': verdict,
        'leak_removed_share': removed,
        'V5_engine_2026_recall': recall_2026,
        'V6_useful': bool(ed['share_with_a_number'] >= MIN_NUMBERS and u and u['spearman'] > 0 and u['p_one_sided'] < P_LEAK
                          and bt and bt['model'] < 0.25),
        'V6_parts': {'share_with_a_number': ed['share_with_a_number'], 'moves_with_the_crowd': u, 'brier_test_and_floor': bt}}
    rep['plain'] = {'open_test': po, 'open_floor': pf, 'dated_test': pd_, 'dated_floor': pdf}
    rep['engine'] = {'open_test': eo, 'open_floor': ef, 'dated_test': ed, 'dated_floor': edf}
    rep['by_origin'] = {o: {arm: arm_dated([r for r in test if r['origin'] == o], answers[arm])['primary']
                            for arm in ('F-open', 'F-pair')} for o in ('round 1', 'fresh')}
    rep['long_shots'] = {arm: {'dated': arm_dated(sets['long'], answers[arm]), 'open': arm_open(sets['long'], answers[arm])}
                         for arm in ('F-open', 'F-pair')}
    if lens is not None:
        l1, l2, l3 = lens_report(test, lens, 'open'), lens_report(floor, lens, 'pit'), lens_report(test, lens, 'pit')
        rep['lens'] = {'L1_sees_memory_open': l1, 'L2_floor_dated': l2, 'L3_test_dated': l3,
                       'verdict': ('inconclusive: the lens does not see memory' if not above(l1['primary']) else
                                   'inconclusive: the lens is not quiet on the floor' if above(l2['primary']) else
                                   'knowledge is inside under the old date' if above(l3['primary']) else
                                   'the lens shows no knowledge inside under the old date')}

    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k != 'obs'}
        return x
    return strip(rep)


if __name__ == '__main__':
    result = score(json.loads(Path(sys.argv[1]).read_text()),
                   json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv) > 2 else None)
    (HERE / 'score.json').write_text(json.dumps(result, indent=1) + '\n')
    for k, v in result['verdicts'].items():
        if k != 'V6_parts':
            print(k, '->', v)
    if 'lens' in result:
        print('lens ->', result['lens']['verdict'])
