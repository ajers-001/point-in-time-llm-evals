"""Candidate questions for 2015-2022 from the public Autocast dataset, by the same rule as build_candidates.py.

Polymarket and Kalshi have almost nothing before 2024. Autocast (Zou et al., 2022, "Forecasting Future World Events
with Neural Networks") holds questions from Metaculus (ids M...) and Good Judgment Open (ids G...) with the crowd
forecast recorded through time. Source: https://people.eecs.berkeley.edu/~hendrycks/autocast.tar.gz; the settled
yes/no questions are kept in data/markets_pull/autocast/autocast_tf_resolved.json.gz.

A question is a candidate when:
  1. it is yes/no and resolved yes or no;
  2. at least MIN_FORECASTERS people forecast it (the counterpart of the money floor on the market sites);
  3. it had been open for at least LEAD + 7 days when it closed;
  4. the crowd forecast LEAD days before it closed was between LOW and HIGH;
  5. its wording is not a number bucket.
The outcome plays no part in the choice. The dataset's `background` text is carried as `rules`.

  python reports/bigevents-v1/build_autocast_candidates.py -> data/markets_pull/autocast_candidates.csv
"""
import csv, gzip, json
from datetime import timedelta

from build_candidates import D, LEAD, LOW, HIGH, BUCKET, stories, when, price_on

MIN_FORECASTERS = 50
SITE = {'M': 'metaculus', 'G': 'gjopen', 'C': 'cset'}


def main():
    rows, why = [], {}
    for q in json.load(gzip.open(D / 'autocast' / 'autocast_tf_resolved.json.gz', 'rt')):
        def drop(reason):
            why[reason] = why.get(reason, 0) + 1
        if q['answer'] not in ('yes', 'no'):
            drop('not yes/no'); continue
        people = int(float(q.get('forecaster_count') or 0))
        if people < MIN_FORECASTERS:
            drop('too few forecasters'); continue
        opened, closed = when(q.get('publish_time')), when(q.get('close_time'))
        if not opened or not closed or closed - opened < timedelta(days=LEAD + 7):
            drop('open for too short a time'); continue
        hist = [{'t': when(c['timestamp']).timestamp(), 'p': c['forecast']} for c in q['crowd']]
        asked = closed - timedelta(days=LEAD)
        p = price_on(hist, asked)
        if p is None:
            drop('no forecast near the question date'); continue
        if not LOW <= p <= HIGH:
            drop('forecast outside the range'); continue
        if BUCKET.search(q['question']):
            drop('number bucket'); continue
        rows.append({'event_id': q['id'], 'event': '', 'market_id': q['id'], 'question': q['question'].strip(),
                     'question_date': asked.date(), 'closed': closed.date(), 'price_on_question_date': round(p, 4),
                     'resolved_yes': int(q['answer'] == 'yes'), 'volume': people,
                     'tags': '|'.join(q.get('tags') or []), 'slug': SITE[q['id'][0]],
                     'rules': ' '.join((q.get('background') or '').split())})
    rows.sort(key=lambda r: (r['closed'], r['market_id']))
    stories(rows)
    with (D / 'autocast_candidates.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(len(rows), 'Autocast candidates in', len({r['story'] for r in rows}), 'stories')
    for k, v in sorted(why.items(), key=lambda kv: -kv[1]):
        print(f'  dropped {v:6d}  {k}')


if __name__ == '__main__':
    main()
