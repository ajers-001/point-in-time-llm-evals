"""Put the candidates of every source in one file and work out the stories again across them, so that the same
election on two sites counts as one story. `volume` keeps each source's own unit (dollars on Polymarket, contracts
on Kalshi, forecasters on Autocast).

  python reports/bigevents-v1/combine_candidates.py -> data/markets_pull/bigevents_candidates.csv
"""
import csv
from collections import Counter
from datetime import date

from build_candidates import D, stories


def main():
    rows = []
    for source in ('autocast', 'kalshi', 'polymarket'):
        for r in csv.DictReader(open(D / f'{source}_candidates.csv')):
            r['question_date'], r['closed'] = date.fromisoformat(r['question_date']), date.fromisoformat(r['closed'])
            r.pop('story')
            rows.append(dict(r, source=source))
    rows.sort(key=lambda r: (r['closed'], r['source'], r['market_id']))
    stories(rows)
    with (D / 'bigevents_candidates.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(len(rows), 'candidates in', len({r['story'] for r in rows}), 'stories')
    years = Counter(r['closed'].year for r in rows)
    firsts = Counter(min(x['closed'].year for x in rows if x['story'] == s) for s in {r['story'] for r in rows})
    for y in sorted(years):
        yes = sum(int(r['resolved_yes']) for r in rows if r['closed'].year == y)
        print(f'  {y}: {years[y]:4d} questions, {firsts[y]:4d} stories starting, {yes / years[y]:.0%} yes')


if __name__ == '__main__':
    main()
