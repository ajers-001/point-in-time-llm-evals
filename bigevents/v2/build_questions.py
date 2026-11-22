"""Round 2 of the big-events test: questions and prompts (ROUND2_RULES.md). No outcome or crowd forecast enters a prompt.

    python reports/bigevents-v2/build_questions.py candidates   # fresh Autocast questions -> v2 candidates file (labels, offline)
    python reports/bigevents-v2/build_questions.py flag         # reader-model filter on the fresh ones (OpenRouter; BUDGET dollars)
    python reports/bigevents-v2/build_questions.py build        # -> reports/bigevents-v2/questions.json, prompts.json

Sets (fixed by rule, never by outcome):
  test   the 373 round 1 test questions, plus fresh 2016-2022 Autocast questions not used in round 1: yes/no, settled,
         at least 25 forecasters, open at least 37 days, crowd forecast 30 days before close between 10% and 90%,
         not a number bucket; both background filters of round 1 applied to the fresh ones.
  floor  the 198 round 1 floor questions (2025-2026, which plain Qwen does not know).
  long   (reported apart, not in any verdict) 2016-2022 Autocast long shots and near-certainties: crowd forecast
         under 10% or over 90%, at least 50 forecasters, same filters.
Stories are worked out again over all three sets together (round 1 rule, build_candidates.stories).

Every question is asked in four prompts: `open` and `pit` as in round 1, and their mirror images `open_flip` and
`pit_flip`, which ask whether the question resolves No. A model that answers No to everything contradicts itself
across a pair, so the pair cancels it out.
"""
import csv
import datetime as dt
import gzip
import json
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
V1 = ROOT / 'reports/bigevents-v1'
sys.path.insert(0, str(V1))
sys.path.insert(0, str(ROOT / 'reports/bigevents-v1'))
import build_candidates as bc  # noqa: E402
import build_engine_questions as v1q  # noqa: E402
import memory_screen as ms  # noqa: E402

D = ROOT / 'data/markets_pull'
CAND = D / 'bigevents_v2_candidates.csv'
FLAGS = HERE / 'reader_flags.json'
MIN_FORECASTERS, LONG_MIN_FORECASTERS = 25, 50
LOW, HIGH = 0.10, 0.90
SITE = {'M': 'metaculus', 'G': 'gjopen', 'C': 'cset'}
VERSIONS = ('open', 'open_flip', 'pit', 'pit_flip')


def fresh_rows():
    """Autocast questions closing 2016-2022 that pass the round 2 rule and were not in the round 1 candidate file."""
    used = {r['market_id'] for r in csv.DictReader(open(D / 'bigevents_candidates.csv')) if r['source'] == 'autocast'}
    out, why, seen = [], {}, set()
    for q in json.load(gzip.open(D / 'autocast' / 'autocast_tf_resolved.json.gz', 'rt')):
        def drop(reason):
            why[reason] = why.get(reason, 0) + 1
        if q['id'] in seen:   # the source file lists a few questions twice
            drop('listed twice in the source'); continue
        seen.add(q['id'])
        if q['id'] in used:
            drop('in round 1'); continue
        if q['answer'] not in ('yes', 'no'):
            drop('not yes/no'); continue
        opened, closed = bc.when(q.get('publish_time')), bc.when(q.get('close_time'))
        if not opened or not closed or not 2016 <= closed.year <= 2022:
            drop('not closed 2016-2022'); continue
        if closed - opened < timedelta(days=bc.LEAD + 7):
            drop('open for too short a time'); continue
        people = int(float(q.get('forecaster_count') or 0))
        hist = [{'t': bc.when(c['timestamp']).timestamp(), 'p': c['forecast']} for c in q['crowd']]
        asked = closed - timedelta(days=bc.LEAD)
        p = bc.price_on(hist, asked)
        if p is None:
            drop('no forecast near the question date'); continue
        if bc.BUCKET.search(q['question']):
            drop('number bucket'); continue
        if LOW <= p <= HIGH and people >= MIN_FORECASTERS:
            group = 'test'
        elif (p < LOW or p > HIGH) and people >= LONG_MIN_FORECASTERS:
            group = 'long'
        else:
            drop('forecast or forecasters outside both groups'); continue
        out.append({'event_id': q['id'], 'event': '', 'market_id': q['id'], 'question': q['question'].strip(),
                    'question_date': asked.date().isoformat(), 'closed': closed.date().isoformat(),
                    'price_on_question_date': round(p, 4), 'resolved_yes': int(q['answer'] == 'yes'), 'volume': people,
                    'tags': '|'.join(q.get('tags') or []), 'slug': SITE[q['id'][0]],
                    'rules': ' '.join((q.get('background') or '').split()), 'source': 'autocast', 'group': group})
    return out, why


def candidates():
    rows, why = fresh_rows()
    with CAND.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({'fresh': len(rows), 'test': sum(r['group'] == 'test' for r in rows),
                      'long': sum(r['group'] == 'long' for r in rows), 'dropped': why}, indent=1))


def fresh():
    rows = list(csv.DictReader(CAND.open()))
    for r in rows:
        r['key'] = f"autocast:{r['market_id']}"
    return rows


def flag():
    """The round 1 reader filter (same model, same prompt) on the fresh questions the pattern filter keeps."""
    import concurrent.futures as cf
    import time
    import urllib.request
    done = json.loads(FLAGS.read_text()) if FLAGS.exists() else {}
    todo = [r for r in fresh() if r['key'] not in done and r['rules'] and not v1q.pattern_flag(r)]
    budget, spent = float(os.environ['BUDGET']), [0.0]

    def ask(r):
        if spent[0] >= budget:
            return None
        body = {'model': v1q.READER, 'messages': [{'role': 'user', 'content': v1q.reader_prompt(r)}], 'max_tokens': 4000,
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


def user_prompt(r, version):
    """Round 1 text for `open` and `pit`; the mirror versions change only the last line."""
    if version in ('open', 'pit'):
        return v1q.user_prompt(r, version)
    base = v1q.user_prompt(r, version[:-5])
    head, last = base.rsplit('\n\n', 1)
    if version == 'open_flip':
        assert last.startswith('How was it settled?'), last
        return head + '\n\nWas it settled No? Answer with one word only: Yes, No, or Unknown if you do not know.'
    assert last.startswith('Will this question resolve Yes?'), last
    return head + ('\n\nWill this question resolve No? Answer with Yes or No, then the probability that it resolves No '
                   'as a number from 0 to 100.')


def build():
    from pit_bench.jspace_items import _system_prompt_fn
    pit = _system_prompt_fn()
    rows1 = {r['key']: r for r in ms.rows()}
    q1 = json.loads((V1 / 'engine/questions.json').read_text())
    flags = json.loads(FLAGS.read_text())
    sets = {'test': [], 'floor': [], 'long': []}
    for q in q1:
        sets[q['set']].append(dict(rows1[q['key']], origin='round 1'))
    why = {}
    for r in fresh():
        reason = 'pattern' if v1q.pattern_flag(r) else 'reader' if v1q.reader_flag(r, flags) else None
        if reason:
            why[f"{r['group']}: removed by {reason}"] = why.get(f"{r['group']}: removed by {reason}", 0) + 1
            continue
        sets[r['group']].append(dict(r, origin='fresh'))
    everything = [dict(r, set=name) for name, rs in sets.items() for r in rs]
    for r in everything:
        r['question_date'] = dt.date.fromisoformat(str(r['question_date']))
    everything.sort(key=lambda r: (r['question_date'], r['key']))
    bc.stories(everything)
    questions, prompts = [], []
    for r in everything:
        r['question_date'] = r['question_date'].isoformat()
        questions.append({'key': r['key'], 'set': r['set'], 'origin': r['origin'], 'story': r['story']})
        for v in VERSIONS:
            system = ms.SYSTEM if v.startswith('open') else pit(v1q.long_date(r['question_date']))
            prompts.append({'example_id': f"{r['key']}__{v}", 'item': r['key'], 'version': v,
                            'asked_on': r['question_date'], 'system_prompt': system, 'prompt_text': user_prompt(r, v)})
    (HERE / 'questions.json').write_text(json.dumps(questions, indent=1) + '\n')
    (HERE / 'prompts.json').write_text(json.dumps(prompts, indent=1) + '\n')
    count = {name: {'questions': sum(q['set'] == name for q in questions),
                    'fresh': sum(q['set'] == name and q['origin'] == 'fresh' for q in questions),
                    'stories': len({q['story'] for q in questions if q['set'] == name})} for name in sets}
    print(json.dumps({'sets': count, 'prompts': len(prompts), **why}, indent=1))


if __name__ == '__main__':
    {'candidates': candidates, 'flag': flag, 'build': build}[sys.argv[1]]()
