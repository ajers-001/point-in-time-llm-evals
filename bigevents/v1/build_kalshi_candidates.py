"""Candidate questions from the Kalshi pull, by the same rule as build_candidates.py (no hand picking).

A market is a candidate when:
  1. its series is on a news topic (already filtered by the pull) and it is a yes/no market;
  2. it settled Yes or No;
  3. at least MIN_CONTRACTS contracts were traded on it (a contract pays one dollar, so this is the Kalshi
     counterpart of the Polymarket dollar floor; Kalshi is the smaller site, so the floor is lower);
  4. it had been open for at least LEAD + 7 days when it closed;
  5. its price LEAD days before it closed was between LOW and HIGH (last trade of the day; the middle of the bid
     and the ask when there was no trade that day);
  6. it is not a number bucket (Kalshi marks these itself: strike type "between").
One market per event: the most traded one that passes. The outcome plays no part in the choice.

  python reports/bigevents-v1/build_kalshi_candidates.py
    -> data/markets_pull/kalshi_candidates.csv
"""
import csv, gzip, json
from datetime import timedelta

from build_candidates import D, LEAD, LOW, HIGH, BUCKET, stories, when, price_on
from pull_kalshi import CATEGORIES

MIN_CONTRACTS = 100_000


def read(name):
    gz = D / (name + '.gz')
    return gzip.open(gz, 'rt') if gz.exists() else open(D / name)


def main():
    prices = {}
    for l in read('kalshi_prices.jsonl'):
        r = json.loads(l)
        prices[r['ticker']] = [{'t': h['t'], 'p': h['p'] if h['p'] is not None else
                                (h['bid'] + h['ask']) / 2 if h['bid'] is not None and h['ask'] is not None else None}
                               for h in r['history'] or []]
        prices[r['ticker']] = [h for h in prices[r['ticker']] if h['p'] is not None]
    markets = []
    for l in read('kalshi_history.jsonl'):
        r = json.loads(l)
        markets += [(r, m) for m in r['markets']]
    for l in gzip.open(D / 'kalshi_events.jsonl.gz', 'rt'):
        e = json.loads(l)
        if e.get('category') in CATEGORIES:
            markets += [(e, m) for m in e['markets']]
    by_event, why, seen = {}, {}, set()
    for s, m in markets:
        def drop(reason):
            why[reason] = why.get(reason, 0) + 1
        if m['ticker'] in seen:
            continue
        seen.add(m['ticker'])
        if m.get('market_type') != 'binary' or m.get('result') not in ('yes', 'no'):
            drop('not yes/no or not settled'); continue
        if float(m.get('volume_fp') or 0) < MIN_CONTRACTS:
            drop('volume'); continue
        opened, closed = when(m.get('open_time')), when(m.get('close_time'))
        if not opened or not closed or closed - opened < timedelta(days=LEAD + 7):
            drop('open for too short a time'); continue
        if m.get('strike_type') == 'between' or BUCKET.search(m.get('title') or ''):
            drop('number bucket'); continue
        hist = prices.get(m['ticker'])
        if hist is None:
            drop('price not pulled yet'); continue
        asked = closed - timedelta(days=LEAD)
        p = price_on(hist, asked)
        if p is None:
            drop('no price near the question date'); continue
        if not LOW <= p <= HIGH:
            drop('price outside the range'); continue
        question = (m.get('title') or '').strip()
        sub = (m.get('yes_sub_title') or '').strip()
        if sub and sub.lower() not in question.lower():
            question += f' [Yes means: {sub}]'
        rules = ' '.join(((m.get('rules_primary') or '') + ' ' + (m.get('rules_secondary') or '')).split())
        by_event.setdefault(m['event_ticker'], []).append(
            {'event_id': m['event_ticker'], 'event': (s.get('title') or '').strip(), 'market_id': m['ticker'],
             'question': question, 'question_date': asked.date(), 'closed': closed.date(),
             'price_on_question_date': round(p, 4), 'resolved_yes': int(m['result'] == 'yes'),
             'volume': round(float(m['volume_fp'])), 'tags': s.get('category') or '', 'slug': m['ticker'],
             'rules': rules})
    rows = [max(v, key=lambda r: r['volume']) for v in by_event.values()]
    why['second market of an event'] = sum(len(v) - 1 for v in by_event.values())
    rows.sort(key=lambda r: (r['closed'], r['market_id']))
    stories(rows)
    write(D / 'kalshi_candidates.csv', rows)
    print(len(rows), 'Kalshi candidates in', len({r['story'] for r in rows}), 'stories')
    for k, v in sorted(why.items(), key=lambda kv: -kv[1]):
        print(f'  dropped {v:6d}  {k}')


def write(path, rows):
    with path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == '__main__':
    main()
