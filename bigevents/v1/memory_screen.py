"""Memory screen for the big-events set: does a plain model (no point-in-time engine) remember how each question
ended? Rules fixed in SCREEN_RULES.md before any answer existed.

    python reports/bigevents-v1/memory_screen.py MODEL     # ask (resumes; saves as it goes; stops at BUDGET dollars)
    python reports/bigevents-v1/memory_screen.py --score   # grade every answers file

The API key comes from the cloud environment (network secret for openrouter.ai, or the variable OPENROUTER_KEY).
The prompt carries the question, its rules and the month it was open. It never carries the outcome or the forecast.
"""
import concurrent.futures as cf
import csv
import datetime as dt
import json
import math
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
QUESTIONS = ROOT / 'data/markets_pull/bigevents_candidates.csv'
OUT = HERE / 'screen'
TODAY = 'November 14, 2026'
SYSTEM = f'You are a helpful assistant.\n\nRuntime temporal context:\n- Current datetime: {TODAY}'
PERIODS = {'2016-2019': range(2016, 2020), '2020-2022': range(2020, 2023), '2024': [2024], '2025': [2025],
           '2026': [2026]}
BANDS = ((0.2, 0.4), (0.4, 0.6), (0.6, 0.801))
PASS_ACCURACY, PASS_P, MIN_STORIES, MIN_ANSWERED = 0.80, 0.01, 20, 0.5


def rows():
    out = list(csv.DictReader(QUESTIONS.open()))
    for r in out:
        r['key'] = f"{r['source']}:{r['market_id']}"
    return out


def prompt(r):
    month = dt.date.fromisoformat(r['question_date']).strftime('%B %Y')
    rules = f"\nResolution rules: {r['rules']}" if r['rules'] else ''
    return (f'Today is {TODAY}. Answer with what is known today.\n\n'
            f'This forecasting question was open in {month} and has since been settled.\n'
            f"Question: {r['question']}{rules}\n\n"
            'How was it settled? Answer with one word only: Yes, No, or Unknown if you do not know.')


spent, lock = [0.0], threading.Lock()


def ask(model, r, budget):
    if spent[0] >= budget:
        return None, {'error': 'budget reached'}
    body = {'model': model, 'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': prompt(r)}],
            'max_tokens': 6000, 'reasoning': {'enabled': True}, 'usage': {'include': True}}
    key = os.environ.get('OPENROUTER_KEY') or os.environ.get('OPENROUTER_API_KEY') or 'sk-or-v1-supplied-by-the-environment'
    req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions', data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    err = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                d = json.loads(resp.read())
            usage = d.get('usage', {})
            with lock:
                spent[0] += usage.get('cost', 0) or 0
            return d['choices'][0]['message'].get('content') or '', usage
        except Exception as e:  # noqa: BLE001
            err = f'{type(e).__name__}: {e}'
            if '401' in err or '402' in err or '403' in err:
                break
            time.sleep(5 * (attempt + 1))
    return None, {'error': err}


def run(model):
    OUT.mkdir(exist_ok=True)
    out = OUT / (model.replace('/', '__') + '.answers.json')
    done = json.loads(out.read_text()) if out.exists() else {}
    todo = [r for r in rows() if r['key'] not in done]
    limit, budget = int(os.environ.get('LIMIT', '0')), float(os.environ['BUDGET'])
    todo = todo[::max(1, len(todo) // limit)][:limit] if limit else todo
    failed, sample = 0, None
    with cf.ThreadPoolExecutor(int(os.environ.get('THREADS', '6'))) as pool:
        for r, (text, usage) in zip(todo, pool.map(lambda r: ask(model, r, budget), todo)):
            if text is None:
                failed, sample = failed + 1, usage.get('error')
                continue
            done[r['key']] = text
            if len(done) % 20 == 0:
                out.write_text(json.dumps(done, indent=1, sort_keys=True) + '\n')
    out.write_text(json.dumps(done, indent=1, sort_keys=True) + '\n')
    print(json.dumps({'model': model, 'answers': len(done), 'asked_now': len(todo), 'failed': failed,
                      'error_sample': sample, 'cost_usd': round(spent[0], 4)}))


def said(text):
    """1 for Yes, 0 for No, None for Unknown, empty or anything else. The last such word of the answer counts."""
    w = re.findall(r'\b(yes|no|unknown)\b', (text or '').lower())
    return {'yes': 1, 'no': 0}.get(w[-1]) if w else None


def t_one_sided(xs):
    """One-sided p that the mean of xs is above zero (Student t)."""
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    if sd == 0:
        return None
    from scipy import stats
    return float(stats.t.sf(m / (sd / math.sqrt(n)), n - 1))


def score():
    qs = rows()
    band = lambda p: next(i for i, (lo, hi) in enumerate(BANDS) if lo <= p < hi)  # noqa: E731
    cell = {}
    for r in qs:   # share that happened, by source and forecast band, over the whole set
        cell.setdefault((r['source'], band(float(r['price_on_question_date']))), []).append(int(r['resolved_yes']))
    rate = {k: sum(v) / len(v) for k, v in cell.items()}
    rep = {}
    for f in sorted(OUT.glob('*.answers.json')):
        a = json.loads(f.read_text())
        per = {}
        for name, years in list(PERIODS.items()) + [('2015 (not judged)', [2015]), ('2023 (not judged)', [2023])]:
            sub = [r for r in qs if int(r['closed'][:4]) in years and r['key'] in a]
            ans = [(r, said(a[r['key']])) for r in sub]
            ans = [(r, s) for r, s in ans if s is not None]
            if not sub:
                continue
            right = sum(s == int(r['resolved_yes']) for r, s in ans)
            favourite = sum((float(r['price_on_question_date']) >= 0.5) == bool(int(r['resolved_yes'])) for r, s in ans)
            by_story = {}
            for r, s in ans:
                e = int(r['resolved_yes']) - rate[(r['source'], band(float(r['price_on_question_date'])))]
                by_story.setdefault(r['story'], []).append((1 if s else -1) * e)
            means = [sum(v) / len(v) for v in by_story.values()]
            p = t_one_sided(means)
            acc = right / len(ans) if ans else None
            judged = 'not judged' not in name
            ok = (judged and acc is not None and acc >= PASS_ACCURACY and p is not None and p < PASS_P
                  and len(by_story) >= MIN_STORIES and len(ans) / len(sub) >= MIN_ANSWERED)
            per[name] = {'asked': len(sub), 'answered_yes_or_no': len(ans), 'stories_answered': len(by_story),
                         'right': acc, 'crowd_favourite_would_be_right': favourite / len(ans) if ans else None,
                         'beyond_the_crowd_p': p, 'passes': ok if judged else None}
        rep[f.name.replace('.answers.json', '').replace('__', '/')] = {'answers': len(a), 'periods': per}
    (OUT / 'scores.json').write_text(json.dumps(rep, indent=1) + '\n')
    for m, v in rep.items():
        print(m, v['answers'])
        for k, x in v['periods'].items():
            print('  ', k, json.dumps({i: (round(j, 4) if isinstance(j, float) else j) for i, j in x.items()}))


if __name__ == '__main__':
    score() if sys.argv[1] == '--score' else run(sys.argv[1])
