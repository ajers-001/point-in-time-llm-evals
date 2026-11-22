"""Questions and prompts for the big-events test of the engine (ENGINE_RULES.md). No outcome and no crowd forecast
is read into any prompt.

    python reports/bigevents-v1/build_engine_questions.py flag    # reader-model filter (OpenRouter; resumes; BUDGET dollars)
    python reports/bigevents-v1/build_engine_questions.py build   # -> reports/bigevents-v1/engine/prompts.json, questions.json

Sets:
  test   every question that closed in 2016-2022;
  floor  questions that closed in 2025-2026: one per story (smallest SHA256 of its key), then the 200 with the
         smallest hash among those that pass the filters.
Filters (a question is removed when its rules/background text carries anything written after the question date):
  1. NOTE, a wording pattern, or a M/D/Y date later than the question date;
  2. a reader model shown the question, the text and the question date, never the outcome.
"""
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
import memory_screen as ms  # noqa: E402

OUT = HERE / 'engine'
FLAGS = OUT / 'reader_flags.json'
READER = 'deepseek/deepseek-v4-pro'
FLOOR_SIZE = 200
OPEN_TODAY = 'November 14, 2026'
NOTE = re.compile(r'(\bresolved\s+\d{1,2}/\d{1,2}|\bUPDATE\b|\bre-?opened\b|this question (was|has been|is now) (closed|resolved)'
                  r'|closed early|\bclosed\b[^.]{0,40}\bas\b|\bresolved (as )?(yes|no|positive|negative)\b|NOTE\s+\d'
                  r'|\bEdit(ed)?\b|\bUpdated?\s+\d)', re.I)
VERSIONS = ('open', 'pit')   # 'pit' is the `dated` version of the rules: the upstream point-in-time system prompt


def digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


def long_date(iso):
    d = dt.date.fromisoformat(iso)
    return f'{d:%B} {d.day}, {d.year}'


def late_date(r):
    q = dt.date.fromisoformat(r['question_date'])
    for mo, d, y in re.findall(r'\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b', r['rules']):
        try:
            y = int(y) + (2000 if int(y) < 100 else 0)
            if dt.date(y, int(mo), int(d)) > q:
                return True
        except ValueError:
            pass
    return False


def pattern_flag(r):
    return bool(NOTE.search(r['rules'])) or late_date(r)


def pool():
    """(test, floor candidates): all 2016-2022 questions; one question per 2025-2026 story."""
    rows = ms.rows()
    test = [r for r in rows if 2016 <= int(r['closed'][:4]) <= 2022]
    by_story = {}
    for r in rows:
        if int(r['closed'][:4]) in (2025, 2026):
            best = by_story.get(r['story'])
            if best is None or digest(r['key']) < digest(best['key']):
                by_story[r['story']] = r
    return test, sorted(by_story.values(), key=lambda r: digest(r['key']))


def reader_prompt(r):
    day = long_date(r['question_date'])
    return (f'A forecasting question was open on {day}. Below are the question and the background text shown with it.\n\n'
            f"Question: {r['question']}\n\nBackground text:\n{r['rules']}\n\n"
            f'Does the background text state or reveal how the question was settled, or report anything that happened '
            f'after {day}? A deadline, a scheduled future date or a condition for settlement does not count; only '
            'something written with knowledge of what happened after that day counts. Judge only from the text itself, '
            'not from what you know happened. Answer with one word: Yes or No.')


def flag():
    OUT.mkdir(exist_ok=True)
    done = json.loads(FLAGS.read_text()) if FLAGS.exists() else {}
    test, floor = pool()
    todo = [r for r in test + floor if r['key'] not in done and r['rules'] and not pattern_flag(r)]
    budget, spent = float(os.environ['BUDGET']), [0.0]

    def ask(r):
        if spent[0] >= budget:
            return None
        body = {'model': READER, 'messages': [{'role': 'user', 'content': reader_prompt(r)}], 'max_tokens': 4000,
                'reasoning': {'enabled': True}, 'usage': {'include': True}}
        key = os.environ.get('OPENROUTER_KEY') or os.environ.get('OPENROUTER_API_KEY') or 'sk-or-v1-supplied-by-the-environment'
        req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions', data=json.dumps(body).encode(),
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=240) as resp:
                    d = json.loads(resp.read())
                spent[0] += d.get('usage', {}).get('cost', 0) or 0
                return d['choices'][0]['message'].get('content') or ''
            except Exception as e:  # noqa: BLE001
                if any(c in str(e) for c in ('401', '402', '403')):
                    return None
                time.sleep(5 * (attempt + 1))
        return None

    with cf.ThreadPoolExecutor(8) as ex:
        for r, text in zip(todo, ex.map(ask, todo)):
            if text is not None:
                done[r['key']] = text
    FLAGS.write_text(json.dumps(done, indent=1, sort_keys=True) + '\n')
    print(json.dumps({'asked_now': len(todo), 'have': len(done), 'cost_usd': round(spent[0], 4)}))


def reader_flag(r, flags):
    """True (remove) unless the reader answered No. A missing or unreadable answer removes the question."""
    if not r['rules']:
        return False
    w = re.findall(r'\b(yes|no)\b', (flags.get(r['key']) or '').lower())
    return not w or w[-1] != 'no'


def user_prompt(r, version):
    month = dt.date.fromisoformat(r['question_date']).strftime('%B %Y')
    rules = f"\nResolution rules: {r['rules']}" if r['rules'] else ''
    if version == 'open':   # the memory screen's question, word for word
        return ms.prompt(r)
    day = long_date(r['question_date'])
    return (f'Today is {day}.\n\nThis forecasting question is open now.\n'
            f"Question: {r['question']}{rules}\n\n"
            'Will this question resolve Yes? Answer with Yes or No, then the probability of Yes as a number from 0 to 100.')


def build():
    from pit_bench.jspace_items import _system_prompt_fn
    pit = _system_prompt_fn()
    flags = json.loads(FLAGS.read_text())
    test, floor = pool()
    why = {}

    def keep(r, name):
        reason = 'pattern' if pattern_flag(r) else 'reader' if reader_flag(r, flags) else None
        if reason:
            why[f'{name}: removed by {reason}'] = why.get(f'{name}: removed by {reason}', 0) + 1
        return reason is None
    test = [r for r in test if keep(r, 'test')]
    floor = [r for r in floor if keep(r, 'floor')][:FLOOR_SIZE]
    prompts, questions = [], []
    for name, rows in (('test', test), ('floor', floor)):
        for r in rows:
            questions.append({'key': r['key'], 'set': name})
            for v in VERSIONS:
                system = (ms.SYSTEM if v == 'open' else pit(long_date(r['question_date'])))
                prompts.append({'example_id': f"{r['key']}__{v}", 'item': r['key'], 'version': v,
                                'asked_on': r['question_date'], 'system_prompt': system, 'prompt_text': user_prompt(r, v)})
    assert ms.SYSTEM.endswith(OPEN_TODAY)
    (OUT / 'prompts.json').write_text(json.dumps(prompts, indent=1) + '\n')
    (OUT / 'questions.json').write_text(json.dumps(questions, indent=1) + '\n')
    print(json.dumps({'test': len(test), 'floor': len(floor), 'prompts': len(prompts), **why}, indent=1))


if __name__ == '__main__':
    {'flag': flag, 'build': build}[sys.argv[1]]()
