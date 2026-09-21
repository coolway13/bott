"""One persistent Discord scoreboard per matchup, updated only on changes."""
from datetime import datetime
import hashlib
import json
import time
from zoneinfo import ZoneInfo

# Team-inspired accent palette; neutral fallback for unknown teams.
TEAM_COLORS = {
    108:0xBA0021,109:0xA71930,110:0xDF4601,111:0xBD3039,112:0x0E3386,
    113:0xC6011F,114:0xE31937,115:0x333366,116:0x0C2340,117:0xEB6E1F,
    118:0x004687,119:0x005A9C,120:0xAB0003,121:0x002D72,133:0x003831,
    134:0xFDB827,135:0x2F241D,136:0x005C5C,137:0xFD5A1E,138:0xC41E3A,
    139:0x092C5C,140:0x003278,141:0x134A8E,142:0x002B5C,143:0xE81828,
    144:0xCE1141,145:0x27251F,146:0x00A3E0,147:0x0C2340,158:0x12284B,
}


def model_factors(g, prediction):
    raw=prediction.get('inputs',{})
    inputs=json.loads(raw) if isinstance(raw,str) else raw
    if not isinstance(inputs,dict):
        return []
    fields=[]
    if prediction['model'].startswith('MLB run-strength'):
        for side in ('away','home'):
            stats=inputs.get(side,{})
            if not all(k in stats for k in ('scored','allowed','scored_games','allowed_games')):
                continue
            text=(f"Runs scored: **{stats['scored']}** in {stats['scored_games']} games\n"
                  f"Runs allowed: **{stats['allowed']}** in {stats['allowed_games']} games\n"
                  f"Scored/game: **{stats['scored']/stats['scored_games']:.2f}**\n"
                  f"Allowed/game: **{stats['allowed']/stats['allowed_games']:.2f}**\n"
                  f"Stats through {stats.get('through','unknown')}")
            fields.append({'name':f"Model inputs · {g[side]}",'value':text,'inline':True})
        fields.append({'name':'How this estimate is calculated',
            'value':'Compares squared scoring/allowing rates, with a 20-game neutral prior at 4.5 runs/game and a 1.10× home-odds adjustment.\n**Starters are shown for context, not included in this model.** No lineup, injury, weather, or odds adjustment.'})
    else:
        labels=[('pitcher_era','Starter ERA'),('pitcher_fip','Starter FIP'),('wrc_plus','wRC+'),('xwoba','xwOBA'),('bullpen_fip','Bullpen FIP'),('lineup_adjustment','Lineup adjustment')]
        for side in ('away','home'):
            values=[f"{label}: **{inputs[side+'_'+key]}**" for key,label in labels if side+'_'+key in inputs]
            if values:
                fields.append({'name':f"Model inputs · {g[side]}",'value':'\n'.join(values),'inline':True})
    return fields


def build_embed(g,prediction=None):
    def display(value):
        return '—' if value is None else str(value)
    fields=[]
    favored='home'
    if prediction:
        p=prediction['probability']
        favored='away' if p>.5 else 'home'
        fields.append({'name':'🏆 Predicted winner',
            'value':'Even matchup · 50% each' if p==.5 else f"**{g[favored]} — {max(p,1-p):.1%}**"})
        fields.extend([
            {'name':f"{g['away']} · Away win probability",'value':f'**{p:.1%}**','inline':True},
            {'name':f"{g['home']} · Home win probability",'value':f'**{1-p:.1%}**','inline':True},
        ])
    fields.append({'name':'Starting pitchers · probable',
        'value':f"**{g['away']}**: {g.get('away_pitcher') or 'Not announced'}\n**{g['home']}**: {g.get('home_pitcher') or 'Not announced'}"})
    if prediction:
        fields.extend(model_factors(g,prediction))
    for side in ('away','home'):
        stats=g.get('live_stats',{}).get(side,{})
        fields.append({'name':f"{g[side]} · {side.title()}",
            'value':f"**{display(g[side+'_score'])} runs**\nHits: {display(stats.get('hits'))} · Errors: {display(stats.get('errors'))}\nLeft on base: {display(stats.get('leftOnBase'))}",'inline':True})
    if g['state']=='Live':
        live=g.get('live_stats',{})
        fields.append({'name':'Live situation','value':
            f"{g.get('inning') or 'In progress'} · Outs: {display(live.get('outs'))}\n"
            f"Count: {display(live.get('balls'))} balls / {display(live.get('strikes'))} strikes\n"
            f"At bat: {live.get('batter') or '—'}\nPitching: {live.get('pitcher') or '—'}\n"
            f"Runners: {', '.join(live.get('bases',[])) or 'Bases empty'}",'inline':False})
    if prediction:
        p=prediction['probability']
        fields.append({'name':'Saved pregame prediction','value':f"{g['away']}: **{p:.1%}**\n{g['home']}: **{1-p:.1%}**\n{prediction['model']}"})
        a,h=g['away_score'],g['home_score']
        if g['state']=='Final' and a is not None and h is not None and a!=h:
            verdict='Even pick' if p==.5 else 'Correct pick ✓' if (p>.5)==(a>h) else 'Missed pick'
            fields.append({'name':'Final result','value':f"**{g['away'] if a>h else g['home']} wins** · {verdict}\nProbability error: {(p-int(a>h))**2:.4f}"})
    else:
        fields.append({'name':'Pregame prediction','value':'Not available yet. Automatic estimates require enough prior-day regular-season team statistics.'})
    start=int(datetime.fromisoformat(g['start'].replace('Z','+00:00')).timestamp())
    return {'title':f"⚾ {g['away']} vs {g['home']}",
        'description':f"**{g['status']}** · {g['date']}\nFirst pitch: <t:{start}:f>",
        'color':TEAM_COLORS.get(g.get(favored+'_id'),0x7289DA),
        'fields':fields,'footer':{'text':f"Game {g['id']} • Untrained pregame estimate • Last card update"}}


def publish(db,url,send):
    db.execute('''CREATE TABLE IF NOT EXISTS discord_games (
        game_id TEXT NOT NULL, destination TEXT NOT NULL, message_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, PRIMARY KEY(game_id,destination))''')
    db.commit()
    destination=hashlib.sha256(url.encode()).hexdigest()
    today=str(datetime.now(ZoneInfo('America/Los_Angeles')).date())
    count=0
    for row in list(db.execute('SELECT data FROM games ORDER BY id')):
        g=json.loads(row[0])
        if g['example']:
            continue
        old=db.execute('SELECT message_id,fingerprint FROM discord_games WHERE game_id=? AND destination=?',(g['id'],destination)).fetchone()
        if g['date']!=today and g['state']!='Live' and not old:
            continue
        prediction=db.execute('SELECT probability,model,inputs,recorded FROM predictions WHERE game_id=?',(g['id'],)).fetchone()
        embed=build_embed(g,dict(prediction) if prediction else None)
        digest=hashlib.sha256(json.dumps(embed,sort_keys=True).encode()).hexdigest()
        if old and old['fingerprint']==digest:
            continue
        embed['timestamp']=g['updated']
        result=send(url,'',embed=embed,message_id=old['message_id'] if old else None)
        if not isinstance(result,dict) or not result.get('id'):
            raise RuntimeError('Discord did not confirm the game card; retrying next refresh.')
        with db:
            db.execute('INSERT OR REPLACE INTO discord_games VALUES (?,?,?,?)',
                (g['id'],destination,result['id'],digest))
        count+=1
        time.sleep(.5)
    # The game cards replace the legacy combined notification queue.
    with db:
        db.execute('UPDATE events SET pending=0 WHERE pending=1')
    return count
