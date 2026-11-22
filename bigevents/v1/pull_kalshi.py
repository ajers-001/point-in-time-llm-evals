"""Pull settled Kalshi events on news topics (with their markets), then daily prices.

Public endpoints, no key. Nothing here picks questions: it only saves what the site returns.
  python reports/bigevents-v1/pull_kalshi.py events   -> data/markets_pull/kalshi_events.jsonl.gz   (recent, live listing)
  python reports/bigevents-v1/pull_kalshi.py history  -> data/markets_pull/kalshi_history.jsonl     (settled before the
                                                         site's cutoff, read series by series from its archive)
  python reports/bigevents-v1/pull_kalshi.py prices   -> data/markets_pull/kalshi_prices.jsonl      (both sets)
The live listing only reaches back to the site's archive cutoff (September 13, 2026 when this was written), so the
archive is where the history is.
"""
import gzip, json, sys, time, urllib.request, urllib.error
from datetime import datetime
from pathlib import Path

OUT = Path('data/markets_pull')
EVENTS = OUT / 'kalshi_events.jsonl.gz'
PRICES = OUT / 'kalshi_prices.jsonl'
BASE = 'https://api.elections.kalshi.com/trade-api/v2'
CATEGORIES = {'Economics', 'Politics', 'Elections', 'World', 'Companies', 'Science and Technology', 'Health'}
HISTORY = OUT / 'kalshi_history.jsonl'
MIN_MARKET_VOLUME = 5_000           # contracts; the selection rule may ask for more, never less
MARKET_KEYS = ('ticker', 'event_ticker', 'title', 'subtitle', 'yes_sub_title', 'no_sub_title', 'rules_primary', 'rules_secondary',
               'open_time', 'close_time', 'created_time', 'occurrence_datetime', 'settlement_ts', 'result',
               'expiration_value', 'volume_fp', 'market_type', 'strike_type', 'floor_strike', 'cap_strike',
               'custom_strike', 'status')
EVENT_KEYS = ('event_ticker', 'series_ticker', 'title', 'sub_title', 'category', 'mutually_exclusive',
              'strike_period', 'settlement_sources')


def get(url, tries=8):
    for i in range(tries):
        time.sleep(0.12)                      # the site answers 429 above roughly ten requests a second
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'research-client'})
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            time.sleep(1 + i)
        except Exception:
            time.sleep(3 * (i + 1))
    return None


def events():
    OUT.mkdir(parents=True, exist_ok=True)
    cursor, pages, kept = '', 0, 0
    with gzip.open(EVENTS, 'wt') as f:
        while True:
            d = get(f'{BASE}/events?status=settled&limit=200&with_nested_markets=true'
                    + (f'&cursor={cursor}' if cursor else ''))
            if not d:
                print('stopped: no answer at page', pages)
                break
            for e in d.get('events') or []:
                if e.get('category') not in CATEGORIES:
                    continue
                ms = [{k: m.get(k) for k in MARKET_KEYS} for m in e.get('markets') or []
                      if float(m.get('volume_fp') or 0) >= MIN_MARKET_VOLUME]
                if not ms:
                    continue
                row = {k: e.get(k) for k in EVENT_KEYS}
                row['markets'] = ms
                f.write(json.dumps(row) + '\n')
                kept += 1
            pages += 1
            cursor = d.get('cursor') or ''
            if pages % 25 == 0:
                print('pages', pages, 'kept', kept, flush=True)
            if not cursor:
                break
    print('pages', pages, 'events kept', kept)


def history():
    """Archived markets, one series at a time (the archive cannot be filtered by topic)."""
    series = [x for x in (get(f'{BASE}/series') or {}).get('series') or [] if x.get('category') in CATEGORIES]
    done = set()
    if HISTORY.exists():
        done = {json.loads(l)['series_ticker'] for l in HISTORY.open()}
    print('series', len(series), 'already read', len(done), flush=True)
    with HISTORY.open('a') as f:
        for n, x in enumerate(series):
            if x['ticker'] in done:
                continue
            cursor, ms, ok = '', [], True
            while True:
                d = get(f"{BASE}/historical/markets?series_ticker={x['ticker']}&limit=1000"
                        + (f'&cursor={cursor}' if cursor else ''))
                if d is None:
                    ok = False
                    break
                page = d.get('markets') or []
                ms += [{k: m.get(k) for k in MARKET_KEYS} for m in page
                       if float(m.get('volume_fp') or 0) >= MIN_MARKET_VOLUME]
                cursor = d.get('cursor') or ''
                if not cursor or not page:
                    break
            f.write(json.dumps({'series_ticker': x['ticker'], 'title': x.get('title'), 'category': x.get('category'),
                                'read': ok, 'markets': ms}) + '\n')
            f.flush()
            if n % 250 == 0:
                print(n, flush=True)


def ts(s):
    return int(datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp())


def money(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def prices():
    done = set()
    if PRICES.exists():
        done = {json.loads(l)['ticker'] for l in PRICES.open()}
    todo = []
    if HISTORY.exists():
        for l in HISTORY.open():
            r = json.loads(l)
            todo += [(r['series_ticker'], m, True) for m in r['markets']]
    for l in gzip.open(EVENTS, 'rt'):
        e = json.loads(l)
        if e.get('category') in CATEGORIES:
            todo += [(e['series_ticker'], m, False) for m in e['markets']]
    todo = [t for t in todo if t[1]['ticker'] not in done and t[1].get('open_time') and t[1].get('close_time')]
    # The candidate rule needs a market open at least 37 days; shorter ones are not priced.
    todo = [t for t in todo if ts(t[1]['close_time']) - ts(t[1]['open_time']) >= 37 * 86400]
    print('to pull', len(todo), flush=True)
    with PRICES.open('a') as f:
        for n, (series, m, archived) in enumerate(todo):
            span = f"start_ts={ts(m['open_time'])}&end_ts={ts(m['close_time'])}&period_interval=1440"
            routes = [f"{BASE}/historical/markets/{m['ticker']}/candlesticks?{span}",
                      f"{BASE}/series/{series}/markets/{m['ticker']}/candlesticks?{span}"]
            d = None
            for url in (routes if archived else routes[::-1]):
                d = get(url)
                if d and d.get('candlesticks'):
                    break
            rows = None
            if d is not None:
                rows = [{'t': c.get('end_period_ts'), 'p': money((c.get('price') or {}).get('close')),
                         'bid': money((c.get('yes_bid') or {}).get('close')),
                         'ask': money((c.get('yes_ask') or {}).get('close'))}
                        for c in d.get('candlesticks') or []]
            f.write(json.dumps({'ticker': m['ticker'], 'history': rows}) + '\n')
            f.flush()
            if n % 500 == 0:
                print(n, flush=True)


if __name__ == '__main__':
    {'events': events, 'history': history, 'prices': prices}[sys.argv[1]]()
