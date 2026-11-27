"""Round 2 scorer (ROUND2_RULES.md). Written and tested on made-up answers before any round 2 answer exists.

    python reports/bigevents-v2/score.py ANSWERS.json [LENS.json]   -> reports/bigevents-v2/score.json

ANSWERS.json: {"F-open": {example_id: [answer per seed, ...]}, "F-pair": {...}}. A dated answer ends with the lines
"Answer: Yes|No" and "Probability: N". Example ids are
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

MIN_NUMBERS = 0.90   # probabilities are the main measure only if both arms give one in at least 90% of answers
# General ability of this engine setting (hybrid-v1 H-pair, alpha 1.75) with thinking mode off, measured before round 2
# (reports/hybrid-v1/REPORT.md): plain Qwen3.5-27B against the engine, in percent. Not measured with thinking on.
GENERAL_ABILITY = {'GPQA-Diamond': (78.8, 80.8), 'IFEval prompt-strict': (88.0, 86.5)}
FINAL = re.compile(r'answer\W*:?\W*(yes|no)\b', re.I)
PROB = re.compile(r'probability\W*:?\W*(\d{1,3}(?:\.\d+)?)', re.I)


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


THINKING = False  # round 2 runs with thinking mode off (rules, "What is asked")


def visible(text, thinking=None):
    """The answer the model gives, without any thinking block.

    Thinking off (round 2): the chat template closes an empty thinking block inside the prompt, so the raw output is
    the answer itself. Thinking on (the pilot, and any side study): the template opens the thinking inside the prompt,
    so the raw output is the thinking, then "</think>", then the answer; an output with no "</think>" was cut off
    inside its thinking and has no answer (its thinking may quote "Answer: Yes")."""
    text = text or ''
    if '</think>' in text:
        return text.rsplit('</think>', 1)[1]
    return '' if (THINKING if thinking is None else thinking) else text


RECALL = re.compile(r'answer\W*:?\W*(yes|no|unknown)\b', re.I)


def last_word(text):
    """Recall prompts: the last line of the form "Answer: Yes", "Answer: No" or "Answer: Unknown". +1 Yes, -1 No,
    0 for Unknown or for an answer with no such line (for example, cut off by the token limit)."""
    w = RECALL.findall(visible(text))
    return {'yes': 1, 'no': -1}.get(w[-1].lower(), 0) if w else 0


def recall_line(text):
    """True if a recall answer ends with one of the three asked lines."""
    return bool(RECALL.search(visible(text)))


def final_answer(text):
    """Dated prompts: the last line of the form "Answer: Yes" or "Answer: No" (bold or spacing allowed). +1 Yes, -1 No,
    None when the answer has no such line (for example, cut off by the token limit)."""
    found = FINAL.findall(visible(text))
    return None if not found else (1 if found[-1].lower() == 'yes' else -1)


def says_happens(text, flip):
    """1 if this answer says the event happens, 0 if it says it does not, None if it has no final answer line.
    In the mirror prompt ("Will this question resolve No?"), "Yes" means the event does not happen."""
    a = final_answer(text)
    if a is None:
        return None
    return float((a > 0) != flip)


def stated_probability(text, flip):
    """Probability that the event happens, from the last "Probability: N" line (N from 0 to 100), or None. The number
    is the chance that the answer to the prompt's own question is Yes, so the mirror prompt is turned around."""
    found = [float(x) for x in PROB.findall(visible(text)) if 0 <= float(x) <= 100]
    if not found:
        return None
    p = found[-1] / 100
    return 1 - p if flip else p


def dated(arm_answers, key):
    """One question's dated answers (all seeds, both mirror prompts): the share that say the event happens (`yes`), the
    mean stated probability that it happens (`prob`), how many answers carried each, and the two halves of `yes`."""
    a, b = arm_answers.get(f'{key}__pit', []), arm_answers.get(f'{key}__pit_flip', [])
    yo, yf = [says_happens(t, False) for t in a], [says_happens(t, True) for t in b]
    pr = [stated_probability(t, False) for t in a] + [stated_probability(t, True) for t in b]
    o, f = [v for v in yo if v is not None], [v for v in yf if v is not None]
    pr = [v for v in pr if v is not None]
    return {'yes': float(np.mean(o + f)) if o + f else None, 'prob': float(np.mean(pr)) if pr else None,
            'n_answer': len(o + f), 'n_prob': len(pr), 'asked': len(a) + len(b),
            'o': float(np.mean(o)) if o else None, 'f': float(np.mean(f)) if f else None}


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
    obs, obs_prob, n_answer, n_prob, asked, halves = [], [], 0, 0, 0, []
    for r in qs:
        d = dated(ans, r['key'])
        n_answer, n_prob, asked = n_answer + d['n_answer'], n_prob + d['n_prob'], asked + d['asked']
        if d['yes'] is not None:
            obs.append((r, d['yes']))
        if d['prob'] is not None:
            obs_prob.append((r, d['prob']))
        if d['o'] is not None and d['f'] is not None:
            halves.append(abs(d['o'] - d['f']))

    def test(o):
        return e1.encompassing(o) if len({round(p, 9) for _, p in o}) > 1 else None
    return {'questions': len(qs), 'with_an_answer': len(obs), 'share_answered': n_answer / max(1, asked),
            'share_with_probability': n_prob / max(1, asked),
            'primary': test(obs), 'leak_size': leak_size(obs),
            'primary_prob': test(obs_prob), 'leak_size_prob': leak_size(obs_prob),
            'brier_yes_share': brier(obs), 'brier_prob': brier(obs_prob),
            'mirror_gap': float(np.mean(halves)) if halves else None, 'obs': obs, 'obs_prob': obs_prob}


def arm_open(qs, ans):
    obs, answered = [], 0
    for r in qs:
        lean, any_answer = recall(ans, r['key'])
        if lean is not None:
            answered += any_answer
            if any_answer:
                obs.append((r, lean))
    right = [((v > 0) == bool(int(r['resolved_yes']))) for r, v in obs if v != 0]
    texts = [t for r in qs for v in ('open', 'open_flip') for t in ans.get(f"{r['key']}__{v}", [])]
    return {'questions': len(qs), 'answered': answered, 'share_answered': answered / max(1, len(qs)),
            'share_with_answer_line': sum(recall_line(t) for t in texts) / max(1, len(texts)),
            'stories_answered': len({r['story'] for r, _ in obs}), 'right': float(np.mean(right)) if right else None,
            'primary': e1.encompassing(obs) if len({v for _, v in obs}) > 1 else None}


def lens_report(qs, lens, version):
    obs = []
    for r in qs:
        a, b = lens.get(f"{r['key']}__{version}"), lens.get(f"{r['key']}__{version}_flip")
        if a is not None and b is not None:
            obs.append((r, float(a - b)))
    return {'questions': len(qs), 'read': len(obs), 'primary': e1.encompassing(obs) if len({v for _, v in obs}) > 1 else None}


def held_back(qs, lens, ans):
    """Seen inside, not written. For each question the lens reading under the old date (prompt minus mirror) points to
    Yes or No, and so does the arm's written answer (share of Yes answers above or below one half). Reports how often
    the inside pointed to the real outcome, how often the written answer did, and, among the questions where the
    inside pointed to the real outcome, the share where the written answer did not."""
    n = inside_right = written_right = hidden = 0
    for r in qs:
        a, b = lens.get(f"{r['key']}__pit"), lens.get(f"{r['key']}__pit_flip")
        d = dated(ans, r['key'])
        if a is None or b is None or a == b or d['yes'] is None:
            continue
        y = bool(int(r['resolved_yes']))
        ins, wr = (a - b > 0) == y, (d['yes'] != 0.5 and (d['yes'] > 0.5) == y)
        n, inside_right, written_right, hidden = n + 1, inside_right + ins, written_right + wr, hidden + (ins and not wr)
    if not n:
        return None
    return {'questions': n, 'inside_points_to_outcome': inside_right / n, 'written_points_to_outcome': written_right / n,
            'seen_inside_not_written': hidden / inside_right if inside_right else None}


def moves_with_crowd(obs):
    """Do the model's answers follow the crowd's forecast of the day? Spearman correlation over story means,
    one-sided. An engine that gives the same answer to everything fails this."""
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
    sets = {s: [r for r in qs if r['set'] == s] for s in ('test', 'floor', 'long', 'markets')}
    test, floor = sets['test'], sets['floor']
    rep = {'note': 'numbers only: no pass mark or label is applied here (ROUND2_RULES.md)'}
    po, pf = arm_open(test, answers['F-open']), arm_open(floor, answers['F-open'])
    eo, ef = arm_open(test, answers['F-pair']), arm_open(floor, answers['F-pair'])
    pd_, pdf = arm_dated(test, answers['F-open']), arm_dated(floor, answers['F-open'])
    ed, edf = arm_dated(test, answers['F-pair']), arm_dated(floor, answers['F-pair'])
    # The stated probabilities are used beside the Yes/No answers only if both arms give them in at least 90% of answers.
    use_prob = bool(pd_['share_with_probability'] >= MIN_NUMBERS and ed['share_with_probability'] >= MIN_NUMBERS)
    key_t, key_s, key_o = ('primary_prob', 'leak_size_prob', 'obs_prob') if use_prob else ('primary', 'leak_size', 'obs')

    removed = None
    if ed[key_o] and pd_[key_o] and len({round(p, 9) for _, p in ed[key_o]}) > 1:
        paired = leak_size(ed[key_o], other=pd_[key_o])
        if paired and paired.get('ratio_to_plain') is not None:
            removed = {'point': 1 - paired['ratio_to_plain'],
                       'ci95': [1 - paired['ratio_ci95'][1], 1 - paired['ratio_ci95'][0]]}
    bkey = 'brier_prob' if use_prob else 'brier_yes_share'
    bf_plain, bf_engine = pdf[bkey], edf[bkey]
    follows = moves_with_crowd(ed[key_o] + edf[key_o])
    same_answer = len({round(p, 9) for _, p in ed[key_o]}) <= 1

    def num(t, k):
        return None if not t or k not in t else t[k]
    rep['scorecard'] = {
        'measure': 'stated probability' if use_prob else 'share of Yes answers',
        'plain_recall_2026': {'right': po['right'], 'share_answered': po['share_answered'], 'p': num(po['primary'], 'p_one_sided')},
        'leak_plain': {'size': num(pd_[key_s], 'size'), 'ci95': num(pd_[key_s], 'ci95'), 'p': num(pd_[key_t], 'p_one_sided')},
        'leak_engine': {'size': num(ed[key_s], 'size'), 'ci95': num(ed[key_s], 'ci95'), 'p': num(ed[key_t], 'p_one_sided')},
        'leak_on_floor': {'plain_p': num(pdf[key_t], 'p_one_sided'), 'engine_p': num(edf[key_t], 'p_one_sided')},
        'leak_removed_share': removed,
        'engine_recall_2026': {'right': eo['right'], 'share_answered': eo['share_answered'], 'p': num(eo['primary'], 'p_one_sided'),
                               'share_answered_on_floor': ef['share_answered']},
        'forecast_error_test': {'crowd': num(pd_[bkey], 'crowd'), 'plain': num(pd_[bkey], 'model'), 'engine': num(ed[bkey], 'model'), 'always_50': 0.25},
        'forecast_error_floor': {'crowd': num(bf_plain, 'crowd'), 'plain': num(bf_plain, 'model'), 'engine': num(bf_engine, 'model'), 'always_50': 0.25},
        'engine_follows_crowd': follows, 'plain_follows_crowd': moves_with_crowd(pd_[key_o] + pdf[key_o]),
        'general_ability_plain_vs_engine_thinking_off': GENERAL_ABILITY,
        'answers_with_final_line': {'plain': pd_['share_answered'], 'engine': ed['share_answered']},
        'recall_answers_with_final_line': {'plain': po['share_with_answer_line'], 'engine': eo['share_with_answer_line']},
        'answers_with_probability': {'plain': pd_['share_with_probability'], 'engine': ed['share_with_probability']},
        'yes_share_leak': {'plain_p': num(pd_['primary'], 'p_one_sided'), 'engine_p': num(ed['primary'], 'p_one_sided'),
                           'plain_size': num(pd_['leak_size'], 'size'), 'engine_size': num(ed['leak_size'], 'size')},
        'engine_gives_the_same_answer_to_every_question': same_answer,
        'mirror_gap': {'plain': pd_['mirror_gap'], 'engine': ed['mirror_gap']}}
    rep['plain'] = {'open_test': po, 'open_floor': pf, 'dated_test': pd_, 'dated_floor': pdf}
    rep['engine'] = {'open_test': eo, 'open_floor': ef, 'dated_test': ed, 'dated_floor': edf}
    rep['by_origin'] = {o: {arm: arm_dated([r for r in test if r['origin'] == o], answers[arm])['primary']
                            for arm in ('F-open', 'F-pair')} for o in ('round 1', 'fresh')}
    rep['long_shots'] = {arm: {'dated': arm_dated(sets['long'], answers[arm]), 'open': arm_open(sets['long'], answers[arm])}
                         for arm in ('F-open', 'F-pair')}
    rep['markets_2023_2024'] = {arm: {'dated': arm_dated(sets['markets'], answers[arm]), 'open': arm_open(sets['markets'], answers[arm])}
                                for arm in ('F-open', 'F-pair')}
    rep['by_source'] = {src: {arm: arm_dated([r for r in test if e1.source(r) == src], answers[arm])[key_t] for arm in ('F-open', 'F-pair')}
                        for src in sorted({e1.source(r) for r in test})}
    if lens is not None:
        l1, l2, l3 = lens_report(test, lens, 'open'), lens_report(floor, lens, 'pit'), lens_report(test, lens, 'pit')
        rep['lens'] = {'recall_prompts_test': l1, 'dated_prompts_floor': l2, 'dated_prompts_test': l3,
                       'seen_inside_not_written': {name: {arm: held_back(S, lens, answers[arm]) for arm in ('F-open', 'F-pair')}
                                                   for name, S in (('test', test), ('floor', floor))}}
        rep['scorecard']['lens'] = {'sees_memory_when_told_2026_p': num(l1['primary'], 'p_one_sided'),
                                    'inside_leak_under_old_date_p': num(l3['primary'], 'p_one_sided'), 'floor_p': num(l2['primary'], 'p_one_sided'),
                                    'seen_inside_not_written': rep['lens']['seen_inside_not_written']['test']}

    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k != 'obs'}
        return x
    return strip(rep)


if __name__ == '__main__':
    result = score(json.loads(Path(sys.argv[1]).read_text()),
                   json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv) > 2 else None)
    (HERE / 'score.json').write_text(json.dumps(result, indent=1) + '\n')
    print(json.dumps(result['scorecard'], indent=1))
