"""Pull closed Polymarket events (with their markets and tags) above a volume floor, then daily prices.

Public endpoints, no key. Nothing here picks questions: it only saves what the site returns.
  python reports/bigevents-v1/pull_polymarket.py events   -> data/markets_pull/polymarket_events.jsonl
  python reports/bigevents-v1/pull_polymarket.py prices   -> data/markets_pull/polymarket_prices.jsonl
Both steps resume where they stopped.
"""
import json, sys, time, urllib.request, urllib.error
from pathlib import Path

OUT = Path('data/markets_pull')
EVENTS = OUT / 'polymarket_events.jsonl'
PRICES = OUT / 'polymarket_prices.jsonl'
GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
MIN_EVENT_VOLUME = 250_000          # dollars; the selection rule may ask for more, never less
TOPICS = ('politics', 'elections', 'global-elections', 'world', 'geopolitics', 'economy', 'finance', 'business',
          'tech', 'fed', 'fed-rates', 'inflation', 'science', 'ai', 'courts', 'trump-presidency', 'middle-east',
          'ukraine', 'china', 'us-election', 'economic-policy', 'ipos', 'big-tech', 'culture')
MARKET_KEYS = ('id', 'question', 'slug', 'description', 'resolutionSource', 'startDate', 'endDate', 'closedTime',
               'createdAt', 'outcomes', 'outcomePrices', 'volumeNum', 'clobTokenIds', 'groupItemTitle',
               'umaResolutionStatus', 'negRisk', 'conditionId')
EVENT_KEYS = ('id', 'slug', 'title', 'description', 'startDate', 'endDate', 'closedTime', 'createdAt', 'volume',
              'negRisk')


def get(url, tries=5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'research-client'})
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 406):
                return None
            time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    return None


def events():
    OUT.mkdir(parents=True, exist_ok=True)
    seen = set()
    if EVENTS.exists():
        seen = {json.loads(l)['id'] for l in EVENTS.open()}
    with EVENTS.open('a') as f:
      # the listing stops paging near offset 2,000, so it is read once overall and once per topic
      for tag in ('',) + TOPICS:
        offset, kept = 0, 0
        while True:
            page = get(f'{GAMMA}/events?closed=true&limit=50&offset={offset}&order=volume&ascending=false'
                       + (f'&tag_slug={tag}' if tag else ''))
            if not page:
                break
            stop = False
            for e in page:
                if float(e.get('volume') or 0) < MIN_EVENT_VOLUME:
                    stop = True
                    break
                if e['id'] in seen:
                    continue
                row = {k: e.get(k) for k in EVENT_KEYS}
                row['tags'] = [t.get('label') for t in e.get('tags') or []]
                row['markets'] = [{k: m.get(k) for k in MARKET_KEYS} for m in e.get('markets') or []]
                f.write(json.dumps(row) + '\n')
                seen.add(e['id'])
                kept += 1
            offset += 50
            if stop or len(page) < 50:
                break
        print(tag or 'all', 'reached offset', offset, 'new', kept, flush=True)
    print('events on file', len(seen))


def prices(market_ids=None):
    """Daily price of the first outcome for each market id given (default: every market on file)."""
    done = set()
    if PRICES.exists():
        done = {json.loads(l)['market_id'] for l in PRICES.open()}
    todo = []
    for l in EVENTS.open():
        for m in json.loads(l)['markets']:
            if m['id'] in done or (market_ids and m['id'] not in market_ids):
                continue
            try:
                tok = json.loads(m.get('clobTokenIds') or '[]')
            except Exception:
                tok = []
            if tok:
                todo.append((m['id'], tok[0]))
    print('to pull', len(todo), flush=True)
    with PRICES.open('a') as f:
        for n, (mid, tok) in enumerate(todo):
            h = get(f'{CLOB}/prices-history?market={tok}&interval=max&fidelity=1440')
            f.write(json.dumps({'market_id': mid, 'token': tok,
                                'history': (h or {}).get('history') if h is not None else None}) + '\n')
            f.flush()
            if n % 200 == 0:
                print(n, flush=True)
            time.sleep(0.05)


if __name__ == '__main__':
    step = sys.argv[1]
    if step == 'events':
        events()
    else:
        ids = set(json.load(open(sys.argv[2]))) if len(sys.argv) > 2 else None
        prices(ids)
