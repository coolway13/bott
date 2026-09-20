"""Read-only Kalshi game-winner quotes; never submits orders."""
from datetime import datetime, timezone
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

API = 'https://external-api.kalshi.com/trade-api/v2/markets'
CODES = dict(zip(
    [108,109,110,111,112,113,114,115,116,117,118,119,120,121,133,134,135,136,137,138,139,140,141,142,143,144,145,146,147,158],
    'LAA AZ BAL BOS CHC CIN CLE COL DET HOU KC LAD WSH NYM ATH PIT SD SEA SF STL TB TEX TOR MIN PHI ATL CWS MIA NYY MIL'.split()))


def fetch_markets():
    markets=[]
    cursor=''
    seen=set()
    for _ in range(20):
        query={'series_ticker':'KXMLBGAME','status':'open','limit':200}
        if cursor:
            query['cursor']=cursor
        with urlopen(Request(API+'?'+urlencode(query), headers={'User-Agent':'MLB-local-tracker/1.0'}), timeout=20) as response:
            data=json.load(response)
        markets.extend(data['markets'])
        cursor=data.get('cursor','')
        if not cursor:
            return markets
        if cursor in seen:
            break
        seen.add(cursor)
    raise RuntimeError('Incomplete Kalshi market response')


def refresh(db, fetch=fetch_markets, clock=time.time):
    row=db.execute("SELECT value FROM meta WHERE key='kalshi_attempt'").fetchone()
    if row and clock()-float(row[0])<300:
        return
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('kalshi_attempt',?)",(str(clock()),))
    try:
        markets=fetch()
        snapshot=json.dumps({'fetched':clock(),'markets':markets})
        with db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('kalshi_snapshot',?)",(snapshot,))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('kalshi_status','Connected · prices refresh every 5 minutes')")
    except Exception:
        # A market outage must not block MLB or Discord updates.
        with db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('kalshi_status','Unavailable · comparisons paused')")
            db.execute("DELETE FROM meta WHERE key='kalshi_snapshot'")


def field(g, prediction, snapshot=None, now=None):
    now=now or datetime.now(timezone.utc)
    prefix='Pass — '
    def result(text):
        return {'name':'Kalshi · game winner (moneyline equivalent)', 'value':text}
    start=datetime.fromisoformat(g['start'].replace('Z','+00:00'))
    if g['state']!='Preview' or g.get('status')!='Scheduled' or start<=now:
        return result(prefix+'pregame comparison closed. Saved probabilities are not live estimates.')
    if not prediction:
        return result(prefix+'no model prediction yet.')
    if not snapshot or not 0<=now.timestamp()-snapshot['fetched']<=360:
        return result(prefix+'fresh market prices unavailable.')
    if g.get('away_id') not in CODES or g.get('home_id') not in CODES:
        return result(prefix+'team mapping unavailable.')
    eastern=start.astimezone(ZoneInfo('America/New_York'))
    month='JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split()[eastern.month-1]
    event=f"KXMLBGAME-{eastern:%y}{month}{eastern:%d%H%M}{CODES[g['away_id']]}{CODES[g['home_id']]}"
    lines=[]
    edges=[]
    for side in ('away','home'):
        ticker=event+'-'+CODES[g[side+'_id']]
        matches=[m for m in snapshot['markets'] if m.get('ticker')==ticker and m.get('event_ticker')==event]
        if len(matches)!=1:
            return result(prefix+'no exact matchup/start-time market match.')
        market=matches[0]
        try:
            price=float(market['yes_ask_dollars'])
            size=float(market['yes_ask_size_fp'])
        except (KeyError,TypeError,ValueError):
            return result(prefix+'buy quotes unavailable.')
        if market.get('status')!='active' or not 0<price<1 or not size>=1:
            return result(prefix+'market closed or insufficient quoted availability.')
        p=prediction['probability'] if side=='away' else 1-prediction['probability']
        edge=p-price
        edges.append((edge,side))
        lines.append(f"**{g[side]}** · Buy YES {price*100:.1f}¢\nModel {p:.1%} · difference {edge*100:+.1f} percentage points")
    edge,side=max(edges)
    lines.append(f"Paper watch: **{g[side]}**" if edge>1e-9 else 'Pass — neither side has a positive model-price difference.')
    lines.append('**Before fees · unvalidated model · not a proven betting edge.**')
    lines.append(f"Quotes checked <t:{int(snapshot['fetched'])}:t> · Prices may change.")
    return result('\n'.join(lines))
