"""Length check before round 2 is frozen (ROUND2_RULES_DRAFT.md, "Before freezing", step 4).

    python reports/bigevents-v2/length_check.py run BUDGET_USD      asks plain Qwen3.5-27B through OpenRouter
    python reports/bigevents-v2/length_check.py report [TOKENIZER_JSON]

Plain Qwen, thinking off, answers the dated prompt (`pit`) and the recall prompt (`open`) of the 150 long-shot
questions used in the pilot (the smallest SHA256 of their key; long shots are in no reported comparison of round 2).
It measures how long the answers are with the round 2 wording and whether they end with the asked lines. It never
reads an outcome or a crowd forecast.

The provider is not our engine: sampling follows the same preset (temperature 1.0, top_p 0.95, top_k 20, presence
penalty 1.5) as far as the provider honours it, and the engine arm cannot be checked this way. The API key comes from
the cloud environment (network secret for openrouter.ai, or the variable OPENROUTER_KEY).
"""
import concurrent.futures as cf
import hashlib
import json
import os
import re
import statistics as st
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import score  # noqa: E402

MODEL = 'qwen/qwen3.5-27b'
OUT = HERE / 'length_check'
N = 150
VERSIONS = ('pit', 'open')
MAX_TOKENS = 1500     # well above the proposed limit, to see the natural length
PROPOSED_LIMIT = 512
spent, lock = [0.0], threading.Lock()


def rows():
    long = sorted((q['key'] for q in json.loads((HERE / 'questions.json').read_text()) if q['set'] == 'long'),
                  key=lambda k: hashlib.sha256(k.encode()).hexdigest())[:N]
    keep = set(long)
    out = [p for p in json.loads((HERE / 'prompts.json').read_text()) if p['version'] in VERSIONS and p['item'] in keep]
    assert len(out) == N * len(VERSIONS) and {p['item'] for p in out} == keep
    return out


def ask(p, budget):
    if spent[0] >= budget:
        return None
    body = {'model': MODEL, 'max_tokens': MAX_TOKENS, 'reasoning': {'enabled': False}, 'usage': {'include': True},
            'temperature': 1.0, 'top_p': 0.95, 'top_k': 20, 'presence_penalty': 1.5,
            'messages': [{'role': 'system', 'content': p['system_prompt']}, {'role': 'user', 'content': p['prompt_text']}]}
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
            m = d['choices'][0]['message']
            return {'text': m.get('content') or '', 'reasoning_characters': len(m.get('reasoning') or ''),
                    'completion_tokens': usage.get('completion_tokens'), 'finish_reason': d['choices'][0].get('finish_reason'),
                    'provider': d.get('provider')}
        except Exception as e:  # noqa: BLE001
            err = f'{type(e).__name__}: {e}'
            if any(c in err for c in ('401', '402', '403')):
                break
            time.sleep(5 * (attempt + 1))
    return {'error': err}


def run(budget):
    OUT.mkdir(exist_ok=True)
    path = OUT / 'answers.json'
    done = json.loads(path.read_text()) if path.is_file() else {}
    todo = [p for p in rows() if 'text' not in done.get(p['example_id'], {})]
    with cf.ThreadPoolExecutor(8) as ex:
        for p, a in zip(todo, ex.map(lambda p: ask(p, budget), todo)):
            if a is not None:
                done[p['example_id']] = a
    path.write_text(json.dumps(done, indent=1, sort_keys=True, ensure_ascii=False) + '\n')
    print(json.dumps({'asked_now': len(todo), 'answers': sum('text' in a for a in done.values()),
                      'failed': sum('error' in a for a in done.values()), 'cost_usd': round(spent[0], 4)}))


def spread(xs):
    xs = sorted(xs)
    return {'mean': round(st.mean(xs)), 'median': round(st.median(xs)), 'p90': xs[int(0.9 * (len(xs) - 1))], 'max': xs[-1]}


def version_report(done, version, tok):
    done = {k: a for k, a in done.items() if k.endswith('__' + version)}
    texts = [a['text'] for a in done.values()]
    n = len(texts)
    rep = {'answers': n,
           'answers_with_reasoning_text_from_the_provider': sum(a['reasoning_characters'] > 0 for a in done.values()),
           'tokens_by_provider_count': spread([a['completion_tokens'] for a in done.values() if a.get('completion_tokens')]),
           'stopped_by_max_tokens': sum(a.get('finish_reason') == 'length' for a in done.values()) / n,
           'sentences_before_the_answer_line': spread([len([x for x in re.split(r'(?<=[.!?])\s+', re.split(r'\**answer\**\s*:', t, flags=re.I)[0].strip()) if x.strip()]) for t in texts]),
           'three_answers': texts[:3]}
    if version == 'pit':
        rep['ends_with_answer_line'] = sum(score.final_answer(t) is not None for t in texts) / n
        rep['has_probability_line'] = sum(score.stated_probability(t, False) is not None for t in texts) / n
    else:
        rep['ends_with_answer_line'] = sum(score.recall_line(t) for t in texts) / n
        rep['says_unknown'] = sum(score.recall_line(t) and score.last_word(t) == 0 for t in texts) / n
    if tok is not None:
        toks = [len(e.ids) for e in tok.encode_batch(texts, add_special_tokens=False)]
        rep['tokens_by_our_tokenizer'] = spread(toks)
        rep['longer_than_proposed_limit'] = sum(k > PROPOSED_LIMIT for k in toks) / n
        rep['share_within'] = {str(l): sum(k <= l for k in toks) / n for l in (128, 256, 384, 512, 768, 1024)}
    return rep


def report(tokenizer=None):
    done = {k: a for k, a in json.loads((OUT / 'answers.json').read_text()).items() if 'text' in a}
    tok = None
    if tokenizer:
        from tokenizers import Tokenizer
        tok = Tokenizer.from_file(tokenizer)
    rep = {'model': MODEL, 'max_tokens_allowed': MAX_TOKENS, 'proposed_limit': PROPOSED_LIMIT,
           'providers': sorted({str(a.get('provider')) for a in done.values()}),
           'tokenizer_sha256': hashlib.sha256(Path(tokenizer).read_bytes()).hexdigest() if tokenizer else None}
    for v in VERSIONS:
        rep[v] = version_report(done, v, tok)
    (OUT / 'report.json').write_text(json.dumps(rep, indent=1, ensure_ascii=False) + '\n')
    print(json.dumps({k: ({a: b for a, b in v.items() if a != 'three_answers'} if isinstance(v, dict) else v) for k, v in rep.items()}, indent=1))


if __name__ == '__main__':
    if sys.argv[1] == 'run':
        run(float(sys.argv[2]))
    else:
        report(sys.argv[2] if len(sys.argv) > 2 else None)
