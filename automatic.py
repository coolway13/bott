"""Automatic, untrained team-strength baseline using MLB prior-day run totals."""
from datetime import date, datetime, timedelta, timezone
import json
import math
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MODEL = 'MLB run-strength baseline v1 (untrained)'


def fetch_team(team_id, cutoff):
    params = {'stats':'byDateRange','group':'hitting,pitching',
              'startDate':f'{cutoff.year}-01-01','endDate':str(cutoff),'sportIds':1,'gameType':'R'}
    url = f'https://statsapi.mlb.com/api/v1/teams/{int(team_id)}/stats?'+urlencode(params)
    try:
        with urlopen(Request(url,headers={'User-Agent':'LocalMLBTracker/1.0'}),timeout=20) as r:
            payload=json.load(r)
    except Exception:
        raise ValueError('Team statistics unavailable; will retry.') from None
    groups={s['group']['displayName']:s.get('splits',[]) for s in payload.get('stats',[])}
    result={'source':url,'through':str(cutoff),'fetched_at':datetime.now(timezone.utc).isoformat()}
    for group, label in [('hitting','scored'),('pitching','allowed')]:
        splits=groups.get(group,[])
        if len(splits)!=1:
            raise ValueError('Team statistics missing or ambiguous; prediction deferred.')
        stat=splits[0]['stat']
        runs,games=stat.get('runs'),stat.get('gamesPlayed')
        if type(runs) not in (int,float) or type(games) not in (int,float):
            raise ValueError('Team run totals missing; prediction deferred.')
        if not math.isfinite(runs) or not math.isfinite(games) or runs<0 or games<10:
            raise ValueError('At least 10 completed games are required for this baseline.')
        result[label]=runs
        result[label+'_games']=games
    return result


def probability(away,home):
    # Add 20 neutral games at 4.5 runs/game to reduce small-sample extremes.
    # Convert squared scoring/allowing ratios to matchup odds, then apply
    # a fixed 1.10 home-odds multiplier. These constants are not fitted.
    def strength(team):
        rs=(team['scored']+90)/(team['scored_games']+20)
        ra=(team['allowed']+90)/(team['allowed_games']+20)
        return 2*math.log(rs/ra)
    score=strength(away)-strength(home)-math.log(1.10)
    return 1/(1+math.exp(-score))


def generate(db,notify=False,fetch=fetch_team):
    current=datetime.now(timezone.utc)
    cache={}
    saved=0
    waiting=[]
    rows=list(db.execute('SELECT g.data FROM games g LEFT JOIN predictions p ON g.id=p.game_id WHERE p.game_id IS NULL'))
    for row in rows:
        g=json.loads(row[0])
        if g['example'] or g['state']!='Preview' or g['status']!='Scheduled' or g.get('game_type')!='R':
            continue
        start=datetime.fromisoformat(g['start'].replace('Z','+00:00'))
        if start<=current:
            continue
        cutoff=date.fromisoformat(g['date'])-timedelta(days=1)
        try:
            inputs={}
            for side in ('away','home'):
                tid=g.get(side+'_id')
                if not tid:
                    raise ValueError('Waiting for team IDs from the next schedule refresh.')
                key=(tid,str(cutoff))
                if key not in cache:
                    try:
                        cache[key]=fetch(tid,cutoff)
                    except ValueError as e:
                        cache[key]=e
                if isinstance(cache[key],Exception):
                    raise cache[key]
                inputs[side]=cache[key]
            p=probability(inputs['away'],inputs['home'])
            recorded=datetime.now(timezone.utc)
            if start<=recorded:
                continue
            inputs['method']='Squared run-rate ratios; 20-game neutral prior at 4.5 runs/game; home odds multiplier 1.10. No pitcher, lineup, injury, weather, or odds adjustments.'
            with db:
                inserted=db.execute('INSERT OR IGNORE INTO predictions VALUES (?,?,?,?,?)',
                    (g['id'],p,recorded.isoformat(),MODEL,json.dumps(inputs))).rowcount
                if inserted:
                    message=(f"Automatic pregame prediction · {g['away']} @ {g['home']}\n"
                        f"{g['away']} {p:.1%} / {g['home']} {1-p:.1%}\n"
                        f"MLB run-strength baseline (untrained). Statistics through {cutoff}; excludes pitcher and lineup adjustments.")
                    db.execute('INSERT INTO events (created,message,pending) VALUES (?,?,?)',
                               (recorded.isoformat(),message,int(notify)))
                    saved+=1
        except (ValueError,KeyError,ZeroDivisionError,OverflowError):
            waiting.append(g['away']+' @ '+g['home'])
    with db:
        status=f'Automatic pregame predictions on · {saved} new this refresh'
        if waiting:
            status+=f' · {len(waiting)} awaiting statistics'
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('automatic',status))
    return {'saved':saved,'waiting':len(waiting)}
