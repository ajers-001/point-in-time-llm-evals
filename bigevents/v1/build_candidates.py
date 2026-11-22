"""Candidate questions from the Polymarket pull, by rule only (no hand picking).

A market is a candidate when:
  1. its event is on a news topic (already filtered by the pull) and it has two outcomes, "Yes" and "No";
  2. it is closed and settled at 1 or 0;
  3. at least MIN_VOLUME dollars were traded on it;
  4. it had been trading for at least LEAD + 7 days when it closed;
  5. its price LEAD days before it closed (the question date) was between LOW and HIGH;
  6. its wording is not a number bucket ("between 50% and 60%", "150,000,000-155,000,000", "10-20%").
One market per event: the most traded one that passes. The outcome plays no part in the choice.

Stories. The site lists "US strikes Iran by February 28" and "Israel strikes Iran by June 30" as separate events,
but they are one story and cannot count as independent evidence. Two questions are put in the same `story` when
they share a name (a capitalised word that is not a common one) and their question dates are within STORY_DAYS of
each other; stories are the connected groups. Statistics are run on stories. Grouping never removes a question.

Each row carries the market's own resolution rules (`rules`), printed with the question so that it stands alone.

  python reports/bigevents-v1/build_candidates.py  -> data/markets_pull/polymarket_candidates.csv
"""
import csv, gzip, json, re
from datetime import datetime, timedelta, timezone
from pathlib import Path

D = Path('data/markets_pull')
LEAD, LOW, HIGH, MIN_VOLUME = 30, 0.20, 0.80, 500_000
STORY_DAYS = 60
BUCKET = re.compile(r'\d\s*(?:-|–|to)\s*\$?\d|\bbetween\b', re.I)
COMMON = set('''Will The A An In On By Of For And Or No Yes Be Is Are Who What Which How US U.S. USA United States
America American Trump Donald President Presidential Election Elections Party Republican Republicans Democrat
Democrats Democratic Senate House Fed January February March April May June July August September October November
December Q1 Q2 Q3 Q4 First Next Before After End New Winner Governor Primary Minister Prime Parliamentary
Mayoral Mayor Popular Vote Votes Government South North Largest Leader Supreme Court Round Legislative Nominee
Nomination Market Interest Rates Rate Company Jr. Trump's IPO Searched Freedom Race Nothing Ever Happens Edition
Strike Strikes Out World State General Special Congress Cabinet Secretary National Liberal Conservative Labour
Decision Meeting Cap Balance Power Seats Most Win Wins'''.split())


def names(question):
    return {w for w in re.findall(r"[A-Z][A-Za-zÀ-ÿ'.-]+", question) if w.strip('.') not in COMMON and len(w) > 2}


def stories(rows):
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    keys = [names(r['question'] + ' ' + r['event']) for r in rows]
    for i in range(len(rows)):
        for j in range(i):
            if keys[i] & keys[j] and abs((rows[i]['question_date'] - rows[j]['question_date']).days) <= STORY_DAYS:
                parent[find(i)] = find(j)
    label = {}
    for i, r in enumerate(rows):
        r['story'] = label.setdefault(find(i), len(label) + 1)


def when(s):
    s = (s or '').replace(' ', 'T').replace('Z', '+00:00')
    if s.endswith('+00'):
        s += ':00'
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def price_on(history, day):
    """Last daily price at or before `day`, and no older than three days."""
    best = None
    for h in history:
        t = datetime.fromtimestamp(h['t'], timezone.utc)
        if t <= day:
            best = (t, h['p'])
    return best[1] if best and day - best[0] <= timedelta(days=3) else None


def main():
    prices = {}
    src = D / 'polymarket_prices.jsonl.gz'
    opener = gzip.open(src, 'rt') if src.exists() else open(D / 'polymarket_prices.jsonl')
    for l in opener:
        r = json.loads(l)
        prices[r['market_id']] = r['history'] or []
    rows, why = [], {}
    for l in gzip.open(D / 'polymarket_events_news.jsonl.gz', 'rt'):
        e = json.loads(l)
        passed = []
        for m in e['markets']:
            def drop(reason):
                why[reason] = why.get(reason, 0) + 1
            if m['id'] not in prices:
                drop('price not pulled yet'); continue
            try:
                names, final = json.loads(m['outcomes']), [float(x) for x in json.loads(m['outcomePrices'])]
            except Exception:
                drop('unreadable outcome'); continue
            if names != ['Yes', 'No']:
                drop('not yes/no'); continue
            if sorted(final) != [0.0, 1.0]:
                drop('not settled at 1 or 0'); continue
            if (m.get('volumeNum') or 0) < MIN_VOLUME:
                drop('volume'); continue
            closed, hist = when(m.get('closedTime')), prices[m['id']]
            if not closed or not hist:
                drop('no close time or no prices'); continue
            first = datetime.fromtimestamp(hist[0]['t'], timezone.utc)
            if closed - first < timedelta(days=LEAD + 7):
                drop('traded for too short a time'); continue
            asked = closed - timedelta(days=LEAD)
            p = price_on(hist, asked)
            if p is None:
                drop('no price near the question date'); continue
            if not LOW <= p <= HIGH:
                drop('price outside the range'); continue
            if BUCKET.search(m['question']):
                drop('number bucket'); continue
            passed.append({'event_id': e['id'], 'event': e['title'].strip(), 'market_id': m['id'],
                           'question': m['question'].strip(), 'question_date': asked.date(),
                           'closed': closed.date(), 'price_on_question_date': round(p, 4),
                           'resolved_yes': int(final[0] == 1.0), 'volume': round(m['volumeNum']),
                           'tags': '|'.join(e['tags']), 'slug': m['slug'],
                           'rules': ' '.join((m.get('description') or '').split())})
        if passed:
            rows.append(max(passed, key=lambda r: r['volume']))
            why['second market of an event'] = why.get('second market of an event', 0) + len(passed) - 1
    rows.sort(key=lambda r: (r['closed'], r['market_id']))
    stories(rows)
    with (D / 'polymarket_candidates.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(len(rows), 'candidates in', len({r['story'] for r in rows}), 'stories')
    for k, v in sorted(why.items(), key=lambda kv: -kv[1]):
        print(f'  dropped {v:6d}  {k}')
    return rows


if __name__ == '__main__':
    main()
