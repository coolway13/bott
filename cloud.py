"""One Actions cycle, with durable GitHub state and at-most-once new cards."""
import base64
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import automatic
import discord_cards
import kalshi
import local


class StateStore:
    """Contents API SHA provides compare-and-swap; never overwrite newer state."""
    def __init__(self):
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.token = os.environ['GH_TOKEN']
        self.sha = None

    def api(self, path, payload=None):
        req = Request('https://api.github.com/repos/'+self.repo+path,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={'Authorization':'Bearer '+self.token,
                               'Accept':'application/vnd.github+json',
                               'X-GitHub-Api-Version':'2022-11-28',
                               'User-Agent':'MLB-Actions'},
                      method='PUT' if payload is not None else 'GET')
        with urlopen(req, timeout=30) as response:
            return json.load(response)

    def restore(self, db):
        # State is explicitly seeded before enabling delivery. Missing state is fatal.
        data = self.api('/contents/state.json.gz?ref=bot-state')
        self.sha = data['sha']
        restore(db, json.loads(gzip.decompress(base64.b64decode(data['content']))))

    def save(self, db):
        encoded = base64.b64encode(gzip.compress(json.dumps(snapshot(db)).encode(), mtime=0)).decode()
        response = self.api('/contents/state.json.gz', {
            'message':'Save MLB predictions and delivery state [skip ci]',
            'branch':'bot-state', 'sha':self.sha, 'content':encoded})
        self.sha = response['content']['sha']


TABLES = {
    'games':['id','data'],
    'predictions':['game_id','probability','recorded','model','inputs'],
    'discord_games':['game_id','destination','message_id','fingerprint'],
    'cloud_deliveries':['game_id','destination','status','message_id','fingerprint'],
}


def schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS discord_games (
      game_id TEXT, destination TEXT, message_id TEXT, fingerprint TEXT,
      PRIMARY KEY(game_id,destination));
    CREATE TABLE IF NOT EXISTS cloud_deliveries (
      game_id TEXT, destination TEXT, status TEXT, message_id TEXT, fingerprint TEXT,
      PRIMARY KEY(game_id,destination));
    ''')


def snapshot(db):
    # Allowlist excludes local paths, configuration, webhook, logs, and metadata.
    return {'version':1, 'tables':{
        table:[list(r) for r in db.execute('SELECT '+','.join(cols)+' FROM '+table)]
        for table,cols in TABLES.items()}}


def restore(db, data):
    if data.get('version') != 1 or set(data['tables']) != set(TABLES):
        raise ValueError('Invalid state; refusing to send')
    with db:
        for table, cols in TABLES.items():
            db.execute('DELETE FROM '+table)
            db.executemany('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in cols)+')', data['tables'][table])


def publish(db, store, webhook, send=local.send_discord):
    destination=hashlib.sha256(webhook.encode()).hexdigest()
    today=str(datetime.now(ZoneInfo('America/Los_Angeles')).date())
    market=db.execute("SELECT value FROM meta WHERE key='kalshi_snapshot'").fetchone()
    market=json.loads(market[0]) if market else None
    uncertain=[]
    sent=0
    for row in list(db.execute('SELECT data FROM games ORDER BY id')):
        g=json.loads(row[0])
        if g['example']:
            continue
        key=(g['id'],destination)
        old=db.execute('SELECT * FROM cloud_deliveries WHERE game_id=? AND destination=?',key).fetchone()
        if not old:
            previous=db.execute('SELECT * FROM discord_games WHERE game_id=? AND destination=?',key).fetchone()
            if previous:
                with db:
                    db.execute('INSERT INTO cloud_deliveries VALUES (?,?,?,?,?)',
                               (*key,'sent',previous['message_id'],previous['fingerprint']))
                old=db.execute('SELECT * FROM cloud_deliveries WHERE game_id=? AND destination=?',key).fetchone()
        if old and old['status']=='reserved':
            uncertain.append(g['id'])
            continue
        if g['date']!=today and g['state']!='Live' and not old:
            continue
        prediction=db.execute('SELECT * FROM predictions WHERE game_id=?',(g['id'],)).fetchone()
        embed=discord_cards.build_embed(g,dict(prediction) if prediction else None)
        embed['fields'].insert(3,kalshi.field(g,dict(prediction) if prediction else None,market))
        digest=hashlib.sha256(json.dumps(embed,sort_keys=True).encode()).hexdigest()
        if old and old['fingerprint']==digest:
            continue
        if not old:
            with db:
                db.execute('INSERT INTO cloud_deliveries VALUES (?,?,?,?,?)',(*key,'reserved',None,''))
            # No POST can happen until this reservation is durably confirmed.
            store.save(db)
        embed['timestamp']=g['updated']
        try:
            result=send(webhook,'',embed=embed,message_id=old['message_id'] if old else None,
                        recreate_missing=False)
        except local.DiscordRateLimit:
            # An explicit 429 confirms the new message was not accepted.
            if not old:
                with db:
                    db.execute('DELETE FROM cloud_deliveries WHERE game_id=? AND destination=?',key)
                store.save(db)
            raise
        if not isinstance(result,dict) or not result.get('id'):
            raise RuntimeError('Discord response unconfirmed')
        with db:
            db.execute('INSERT OR REPLACE INTO cloud_deliveries VALUES (?,?,?,?,?)',
                       (*key,'sent',result['id'],digest))
        # If persistence fails, stop immediately. Next run sees reserved, not unsent.
        store.save(db)
        sent+=1
        time.sleep(.5)
    if uncertain:
        print('::warning::Unconfirmed deliveries need review for game IDs: '+', '.join(uncertain))
    print(f'Updated {sent} Discord cards; {len(uncertain)} unconfirmed deliveries held.')
    if uncertain:
        raise RuntimeError('Unconfirmed delivery reservations require review')


def main():
    if os.environ.get('GITHUB_REPOSITORY') != 'coolway13/bott':
        raise RuntimeError('Delivery is restricted to the configured repository')
    webhook=local.valid_hook(os.environ['DISCORD_WEBHOOK_URL'].strip())
    db=local.connect(':memory:')
    schema(db)
    store=StateStore()
    store.restore(db)
    local.sync(db)
    result=automatic.generate(db)
    kalshi.refresh(db)
    # Save inputs and original predictions before any external delivery.
    store.save(db)
    publish(db,store,webhook)
    print(f"Predictions saved: {result['saved']}; waiting for stats: {result['waiting']}")


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        # Never print exception URLs or response bodies: they may contain secrets.
        print('::error::MLB cycle failed ('+type(error).__name__+'). Saved state retained; check state/access and delivery warnings.')
        sys.exit(1)
