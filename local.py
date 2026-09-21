#!/usr/bin/env python3
"""Local MLB tracker: SQLite, browser dashboard, optional Discord alerts."""
import argparse
from datetime import datetime, timedelta, timezone
import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request
from security import urlopen
import webbrowser
from zoneinfo import ZoneInfo

from model import calculate_probability, timestamp
from automatic import generate
from discord_cards import publish
from security import clean, redact, protect_output

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'data' / 'mlb.sqlite3'
HOOK = ROOT / '.discord-webhook'
MODEL = 'Starter heuristic v1 (untrained)'
POLL = 60


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(path=DB):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS games (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS predictions (
            game_id TEXT PRIMARY KEY, probability REAL NOT NULL,
            recorded TEXT NOT NULL, model TEXT NOT NULL, inputs TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, created TEXT NOT NULL, message TEXT NOT NULL,
            pending INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    return db


def metadata(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, value))


def fetch_schedule(query):
    url = 'https://statsapi.mlb.com/api/v1/schedule?' + urlencode(query)
    try:
        with urlopen(Request(url, headers={'User-Agent': 'LocalMLBTracker/1.0'}), timeout=25) as r:
            payload = json.load(r)
    except (HTTPError, URLError, OSError, ValueError):
        raise RuntimeError('MLB feed unavailable. Saved games remain available; next refresh will retry.') from None
    if not isinstance(payload.get('dates'), list):
        raise RuntimeError('MLB returned an unexpected schedule response.')
    return [normalize(g) for day in payload['dates'] for g in day.get('games', [])]


def normalize(g):
    teams, line = g['teams'], g.get('linescore', {})
    return {
        'id': str(g['gamePk']), 'date': g['officialDate'], 'start': g['gameDate'],
        'away': teams['away']['team']['name'], 'home': teams['home']['team']['name'],
        'away_id': teams['away']['team']['id'], 'home_id': teams['home']['team']['id'],
        'game_type': g.get('gameType'),
        'away_score': teams['away'].get('score'), 'home_score': teams['home'].get('score'),
        'away_pitcher': teams['away'].get('probablePitcher', {}).get('fullName', ''),
        'home_pitcher': teams['home'].get('probablePitcher', {}).get('fullName', ''),
        'state': g['status']['abstractGameState'], 'status': g['status']['detailedState'],
        'inning': ' '.join(str(x) for x in [line.get('inningHalf', ''), line.get('currentInningOrdinal', '')] if x),
        'example': False, 'updated': now(),
        'live_stats': {
            'away': line.get('teams',{}).get('away',{}),
            'home': line.get('teams',{}).get('home',{}),
            'outs': line.get('outs'), 'balls': line.get('balls'), 'strikes': line.get('strikes'),
            'batter': line.get('offense',{}).get('batter',{}).get('fullName'),
            'pitcher': line.get('defense',{}).get('pitcher',{}).get('fullName'),
            'bases': [base for base in ('first','second','third') if line.get('offense',{}).get(base)],
        },
    }


def prediction_text(db, game):
    row = db.execute('SELECT probability,model FROM predictions WHERE game_id=?', (game['id'],)).fetchone()
    if row is None:
        return 'Pregame prediction: not saved.'
    p = row[0]
    text = f"Pregame estimate: {game['away']} {p:.1%} / {game['home']} {1-p:.1%}. Model: {row[1]}."
    a, h = game['away_score'], game['home_score']
    if game['state'] == 'Final' and a is not None and h is not None and a != h:
        result = 'Even prediction; no winner picked' if p == .5 else (
            'Correct pick' if (p > .5) == (a > h) else 'Missed pick')
        text += f' Result: {result}. Probability error: {(p-int(a>h))**2:.4f}.'
    return text


def game_message(db, game):
    a, h = game['away_score'], game['home_score']
    score = f"{a if a is not None else '—'}–{h if h is not None else '—'}"
    return (f"{game['away']} @ {game['home']} | {score} | {game['status']} {game['inning']}\n"
            + prediction_text(db, game))


def apply_snapshots(db, games, notify=False):
    changed = 0
    keys = ('state', 'status', 'away_score', 'home_score', 'start', 'away_pitcher', 'home_pitcher')
    for g in games:
        row = db.execute('SELECT data FROM games WHERE id=?', (g['id'],)).fetchone()
        old = json.loads(row[0]) if row else None
        if old and any(old.get(k) != g.get(k) for k in keys):
            message = game_message(db, g)
            if old.get('start') != g['start']:
                message += f" | Start: {g['start']}"
            if any(old.get(k) != g.get(k) for k in ('away_pitcher', 'home_pitcher')):
                message += f" | Starters: {g['away_pitcher'] or 'TBD'} / {g['home_pitcher'] or 'TBD'}"
            db.execute('INSERT INTO events (created,message,pending) VALUES (?,?,?)', (now(), message, int(notify)))
            changed += 1
        db.execute('INSERT OR REPLACE INTO games VALUES (?,?)', (g['id'], json.dumps(g)))
    return changed


def sync(db, notify=False, fetch=fetch_schedule):
    today = datetime.now(ZoneInfo('America/Los_Angeles')).date()
    games = fetch({'sportId': 1, 'startDate': str(today-timedelta(days=1)),
                   'endDate': str(today), 'hydrate': 'linescore,probablePitcher'})
    found = {g['id'] for g in games}
    old = [json.loads(r[0]) for r in db.execute('SELECT data FROM games')]
    outstanding = [g['id'] for g in old if not g['example'] and g['id'] not in found
                   and g['date'] <= str(today) and g['state'] != 'Final'
                   and g['status'] != 'Cancelled']
    for i in range(0, len(outstanding), 50):
        games.extend(fetch({'sportId': 1, 'gamePks': ','.join(outstanding[i:i+50]),
                            'hydrate': 'linescore,probablePitcher'}))
    with db:
        changes = apply_snapshots(db, games, notify)
        metadata(db, 'last_sync', now())
        metadata(db, 'error', '')
    return {'games': len(games), 'changes': changes}


def save_predictions(db, inputs):
    if not isinstance(inputs, list) or not inputs or any(not isinstance(g, dict) for g in inputs):
        raise ValueError('Provide a nonempty list of games.')
    prepared, seen = [], set()
    for source in inputs:
        g = dict(source)
        for key in ('game_id', 'away', 'home'):
            if not isinstance(g.get(key), str) or not g[key].strip():
                raise ValueError(f'{key} is required.')
            g[key] = g[key].strip()
        if g['game_id'] in seen:
            raise ValueError('Duplicate Game IDs in input.')
        seen.add(g['game_id'])
        if type(g.get('example')) is not bool:
            raise ValueError('Set example to true or false.')
        if g['away'].casefold() == g['home'].casefold():
            raise ValueError('Teams must differ.')
        if g['example'] != g['game_id'].startswith('example:'):
            raise ValueError('Only example IDs can start with example:.')
        if not g['example'] and not g['game_id'].isdigit():
            raise ValueError('Use the numeric MLB Game ID displayed in the dashboard.')
        p = calculate_probability(g)
        record = db.execute('SELECT data FROM games WHERE id=?', (g['game_id'],)).fetchone()
        stored = json.loads(record[0]) if record else None
        if not g['example']:
            if not stored:
                raise ValueError('Refresh the MLB feed first, then select a game in the dashboard.')
            if (stored['away'], stored['home']) != (g['away'], g['home']):
                raise ValueError('Team names must match the selected MLB game.')
            if stored['state'] != 'Preview' or timestamp(stored['start']) <= datetime.now(timezone.utc):
                raise ValueError('Predictions can only be saved before the scheduled first pitch.')
        snapshot = stored or {'id': g['game_id'], 'away': g['away'], 'home': g['home'],
            'date': '', 'start': '', 'state': 'Example', 'status': 'Example', 'inning': '',
            'away_score': None, 'home_score': None, 'example': True, 'updated': now()}
        prepared.append((g, p, snapshot))
    saved = skipped = 0
    with db:
        for g, p, snapshot in prepared:
            if db.execute('SELECT 1 FROM predictions WHERE game_id=?', (g['game_id'],)).fetchone():
                skipped += 1
                continue
            db.execute('INSERT OR IGNORE INTO games VALUES (?,?)', (g['game_id'], json.dumps(snapshot)))
            db.execute('INSERT INTO predictions VALUES (?,?,?,?,?)',
                       (g['game_id'], p, now(), MODEL, json.dumps(g)))
            if not g['example']:
                setting = db.execute("SELECT value FROM meta WHERE key='discord'").fetchone()
                pending = int(bool(setting and setting[0] == 'Enabled'))
                message = f"New prediction · {snapshot['away']} @ {snapshot['home']}\n" + prediction_text(db, snapshot)
                db.execute('INSERT INTO events (created,message,pending) VALUES (?,?,?)', (now(),message,pending))
            saved += 1
    return {'saved': saved, 'skipped': skipped}


def state(db):
    games, correct, briers = [], [], []
    for row in db.execute('SELECT g.data,p.probability,p.recorded,p.model FROM games g LEFT JOIN predictions p ON g.id=p.game_id'):
        g = json.loads(row['data'])
        g.update(probability=row['probability'], recorded=row['recorded'], model=row['model'], correct=None, brier=None)
        a, h, p = g['away_score'], g['home_score'], g['probability']
        if not g['example'] and g['state'] == 'Final' and p is not None and a is not None and h is not None and a != h:
            g['brier'] = (p-int(a>h))**2
            briers.append(g['brier'])
            if p != .5:
                g['correct'] = int((p>.5) == (a>h))
                correct.append(g['correct'])
        games.append(g)
    return {'games': sorted(games, key=lambda g: (g['date'],g['start']), reverse=True),
            'events': [dict(r) for r in db.execute('SELECT id,created,message,pending FROM events ORDER BY id DESC LIMIT 50')],
            'meta': dict(db.execute('SELECT key,value FROM meta')),
            'metrics': {'evaluated': len(briers), 'accuracy': sum(correct)/len(correct) if correct else None,
                        'brier': sum(briers)/len(briers) if briers else None}}


def valid_hook(value):
    if not re.fullmatch(r'https://discord\.com/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_.-]+', value):
        raise ValueError('Use a Discord text-channel webhook URL from discord.com.')
    return value


def hook_value():
    value = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    if not value:
        raise ValueError('Set DISCORD_WEBHOOK_URL in the environment (GitHub Actions Secret in production)')
    return valid_hook(value)


class DiscordRateLimit(RuntimeError):
    def __init__(self, delay):
        super().__init__('Discord rate limit; alerts remain queued until the retry window ends.')
        self.delay = delay


def send_discord(url, message, *, embed=None, message_id=None, recreate_missing=True):
    payload = {'content': message[:1900], 'allowed_mentions': {'parse': []}, 'username': 'MLB Local Tracker'}
    if embed:
        payload['embeds']=[embed]
    if message_id is not None and not re.fullmatch(r'[0-9]{1,25}', str(message_id)):
        raise ValueError('Invalid Discord message identifier')
    suffix=f'/messages/{message_id}' if message_id else '?wait=true'
    req = Request(valid_hook(url)+suffix, data=json.dumps(clean(payload)).encode(),
                  headers={'Content-Type': 'application/json', 'User-Agent': 'LocalMLBTracker/1.0'}, method='PATCH' if message_id else 'POST')
    try:
        with urlopen(req, timeout=20) as response:
            raw=response.read()
            return json.loads(raw) if raw else None
    except HTTPError as e:
        if e.code == 404 and message_id and recreate_missing:
            return send_discord(url,message,embed=embed)
        if e.code == 429:
            try:
                delay = max(60, float(json.load(e).get('retry_after', 60)))
            except (ValueError, TypeError, AttributeError):
                delay = 60
            raise DiscordRateLimit(delay) from None
        raise RuntimeError(f'Discord HTTP {e.code}; alerts remain queued. Check webhook access.') from None
    except (URLError, OSError):
        raise RuntimeError('Discord delivery could not be confirmed; pending alerts will retry.') from None


def deliver(db, url):
    retry = db.execute("SELECT value FROM meta WHERE key='discord_retry_at'").fetchone()
    if retry and time.time() < float(retry[0]):
        return
    rows = list(db.execute('SELECT id,message FROM events WHERE pending=1 ORDER BY id LIMIT 50'))
    batches, ids, message = [], [], ''
    for row in rows:
        line = row['message'][:1500]+'\n'
        if len(message)+len(line)>1900:
            batches.append((ids, message))
            ids, message = [], ''
        ids.append(row['id'])
        message += line
    if ids:
        batches.append((ids, message))
    for ids, message in batches:
        try:
            send_discord(url, message)
        except DiscordRateLimit as e:
            with db:
                metadata(db, 'discord_retry_at', str(time.time()+e.delay))
            raise
        with db:
            db.executemany('UPDATE events SET pending=0 WHERE id=?', [(i,) for i in ids])


def poll(stop, path, discord):
    db = connect(path)
    try:
        while not stop.is_set():
            try:
                sync(db, notify=bool(discord))
                generate(db, notify=bool(discord))
                if discord:
                    retry=db.execute("SELECT value FROM meta WHERE key='discord_retry_at'").fetchone()
                    if not retry or time.time()>=float(retry[0]):
                        try:
                            publish(db, discord, send_discord)
                        except DiscordRateLimit as e:
                            with db:
                                metadata(db,'discord_retry_at',str(time.time()+e.delay))
                            raise
            except Exception as e:
                # Never log remote exception details, which may include a secret URL.
                message = 'Refresh failed; saved data remains available.'
                with db:
                    metadata(db, 'error', message)
            stop.wait(POLL)
    finally:
        db.close()


def serve(path, port, discord=False, open_browser=False):
    token = secrets.token_urlsafe(32)
    webhook = hook_value() if discord else None
    with connect(path) as db:
        metadata(db, 'discord', 'Enabled' if discord else 'Off')
        save_predictions(db, json.loads((ROOT/'games.example.json').read_text()))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body, kind='application/json'):
            data = json.dumps(body).encode() if kind == 'application/json' else body.encode()
            self.send_response(status)
            self.send_header('Content-Type', kind+'; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def host_ok(self):
            return self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}')

        def do_GET(self):
            if not self.host_ok():
                return self.reply(403, {'error': 'Local access only'})
            route = urlparse(self.path).path
            if route == '/':
                return self.reply(200, (ROOT/'dashboard.html').read_text().replace('__TOKEN__', token), 'text/html')
            if route == '/api/state':
                db = connect(path)
                try:
                    return self.reply(200, state(db))
                finally:
                    db.close()
            return self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            if not self.host_ok() or self.headers.get('X-Local-Token') != token:
                return self.reply(403, {'error': 'Reload the dashboard and retry.'})
            if self.path != '/api/predictions':
                return self.reply(404, {'error': 'Not found'})
            db = connect(path)
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 200000:
                    raise ValueError('Request must be between 1 and 200000 bytes.')
                result = save_predictions(db, json.loads(self.rfile.read(length)))
                self.reply(200, result)
            except (ValueError, KeyError, TypeError) as e:
                self.reply(400, {'error': 'Invalid prediction input.'})
            finally:
                db.close()
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    stop = threading.Event()
    worker = threading.Thread(target=poll, args=(stop,path,webhook), daemon=True)
    worker.start()
    url = f'http://127.0.0.1:{port}'
    print(f'MLB tracker: {url}\nLive refresh every {POLL} seconds. Keep this window open. Ctrl+C stops it.', flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', type=Path, default=DB)
    subs = p.add_subparsers(dest='command', required=True)
    s = subs.add_parser('serve')
    s.add_argument('--port', type=int, default=8765)
    s.add_argument('--discord', action='store_true')
    s.add_argument('--open', action='store_true')
    subs.add_parser('sync')
    subs.add_parser('discord-setup')
    subs.add_parser('discord-test')
    subs.add_parser('discord-summary')
    imp = subs.add_parser('import')
    imp.add_argument('file', type=Path)
    args = p.parse_args()
    try:
        if args.command == 'serve':
            serve(args.db,args.port,args.discord,args.open)
        elif args.command == 'discord-setup':
            raise ValueError('File credential storage is disabled. Use the DISCORD_WEBHOOK_URL environment variable.')
        elif args.command == 'discord-test':
            send_discord(hook_value(), 'MLB Local Tracker test: your Discord connection is working.')
            print('Test message sent.')
        elif args.command == 'discord-summary':
            url = hook_value()
            with connect(args.db) as db:
                setting = db.execute("SELECT value FROM meta WHERE key='discord'").fetchone()
                sync(db, notify=bool(setting and setting[0] == 'Enabled'))
                today = str(datetime.now(ZoneInfo('America/Los_Angeles')).date())
                games = [g for g in state(db)['games'] if not g['example'] and g['date'] == today]
                header = f'MLB scoreboard · {today}\n'
                message = header
                for g in sorted(games, key=lambda g: g['start']):
                    block = game_message(db,g)+'\n\n'
                    if len(message)+len(block)>1800:
                        send_discord(url,message)
                        message = header
                    message += block
                send_discord(url,message if games else header+'No games scheduled.')
                print(f'Scoreboard sent for {len(games)} games.')
        else:
            with connect(args.db) as db:
                print(json.dumps(sync(db) if args.command=='sync' else save_predictions(db,json.loads(args.file.read_text()))))
    except (ValueError, RuntimeError, OSError) as e:
        print('Operation failed; check configuration and input. Sensitive error details suppressed.')
        return 1
    return 0


if __name__ == '__main__':
    protect_output()
    raise SystemExit(main())
