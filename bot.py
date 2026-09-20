#!/usr/bin/env python3
"""Upload pregame predictions to the user's MLB Airtable tracker (Python 3.9+)."""
import argparse
import getpass
import json
import math
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / '.airtable-token'
BASE = 'appm9ZZJVkFjumlpZ'
TABLE = 'tblFe8yByg81AZf9V'
MODEL = 'Starter heuristic v1 (untrained)'


def number(game, key, low=None, high=None):
    value = game.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{key} must be a finite number')
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(f'{key} is outside the allowed range')
    return value


def calculate_probability(game):
    """Preserve the supplied heuristic; these weights have not been trained."""
    for side in ('away', 'home'):
        for metric in ('pitcher_era', 'pitcher_fip', 'bullpen_fip'):
            number(game, f'{side}_{metric}', 0)
        number(game, f'{side}_wrc_plus')
        number(game, f'{side}_xwoba', 0, 1)
        number(game, f'{side}_lineup_adjustment', -1, 1)
    score = (
        game['home_pitcher_era'] - game['away_pitcher_era']
        + (game['home_pitcher_fip'] - game['away_pitcher_fip']) * .7
        + (game['away_wrc_plus'] - game['home_wrc_plus']) / 25
        + (game['away_xwoba'] - game['home_xwoba']) * 15
        + (game['home_bullpen_fip'] - game['away_bullpen_fip']) * .5
        + (game['away_lineup_adjustment'] - game['home_lineup_adjustment']) * 20
        - .25
    )
    if not math.isfinite(score):
        raise ValueError('Model score overflowed; check inputs')
    # Stable logistic function avoids overflow for strongly negative scores.
    if score >= 0:
        return 1 / (1 + math.exp(-score))
    exp_score = math.exp(score)
    return exp_score / (1 + exp_score)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('start_time must be an ISO timestamp with timezone')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('start_time must include a timezone, such as -07:00 or Z')
    return dt


def make_fields(game, now=None):
    now = now or datetime.now(timezone.utc)
    for key in ('game_id', 'away', 'home'):
        if not isinstance(game.get(key), str) or not game[key].strip():
            raise ValueError(f'{key} must be nonempty text')
    if game['away'].strip().casefold() == game['home'].strip().casefold():
        raise ValueError('Away and home teams must differ')
    if type(game.get('example')) is not bool:
        raise ValueError('Set example explicitly to true or false')
    example = game['example']
    if example != game['game_id'].startswith('example:'):
        raise ValueError('Example IDs must start with example:; real IDs must not')
    start = timestamp(game['start_time']) if 'start_time' in game else None
    if not example and (start is None or start <= now):
        raise ValueError('Real predictions require a future start_time; no retrospective predictions')
    p = calculate_probability(game)
    fields = {
        'Game': ('EXAMPLE — ' if example else '') + f"{game['away']} @ {game['home']}",
        'Game ID': game['game_id'],
        'Away team': game['away'], 'Home team': game['home'],
        'Data type': 'Example' if example else 'Real game',
        'Away win probability': p,
        'Prediction recorded at': now.isoformat(),
        'Model version': MODEL,
        'Key factors': json.dumps({k: v for k, v in game.items()
                                  if k.startswith(('away_', 'home_'))}, sort_keys=True),
        'Notes': 'Untrained starter heuristic. Inputs supplied manually. '
                 'Rest is not used; home-field penalty is fixed at 0.25. '
                 'Lineup adjustment is a model input, not a direct percentage-point change.',
    }
    if start:
        fields['Game date'] = start.date().isoformat()
        fields['Notes'] += ' Scheduled start: ' + start.isoformat()
    if not example:
        fields['Status'] = 'Scheduled'
    if game.get('source_url'):
        if not isinstance(game['source_url'], str) or not game['source_url'].startswith('https://'):
            raise ValueError('source_url must be an https URL')
        fields['Source URL'] = game['source_url']
    return fields


class Airtable:
    def __init__(self, token):
        if not token:
            raise ValueError('No token configured. Run: ./run.sh configure')
        if not token.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token):
            raise ValueError('Token contains invalid characters. Re-enter it using ./run.sh configure')
        self.token = token
        self.url = f'https://api.airtable.com/v0/{BASE}/{TABLE}'

    def request(self, method='GET', payload=None, query=None):
        url = self.url + ('?' + urlencode(query) if query else '')
        body = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
        for attempt in range(3):
            req = Request(url, data=body, method=method, headers={
                'Authorization': 'Bearer ' + self.token,
                'Content-Type': 'application/json',
            })
            try:
                with urlopen(req, timeout=30) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    print('Airtable rate limit reached; waiting 30 seconds.', file=sys.stderr)
                    time.sleep(30)
                    continue
                raise RuntimeError(f'Airtable HTTP {exc.code}. Check token scopes, base access, and input fields. '
                                   'If a write failed, rerun to reconcile saved Game IDs.') from None
            except (URLError, TimeoutError, OSError):
                # Never retry an ambiguous POST: it may already have succeeded.
                raise RuntimeError('Network request failed. Rerun to check saved Game IDs before writing again.') from None

    def records(self):
        rows, offset = [], None
        while True:
            query = {'pageSize': 100}
            if offset:
                query['offset'] = offset
            result = self.request(query=query)
            rows.extend(result['records'])
            offset = result.get('offset')
            if not offset:
                return rows
            time.sleep(.22)

    def create(self, fields):
        time.sleep(.22)
        return self.request('POST', {'records': [{'fields': fields}], 'typecast': False})


def upload(client, games):
    # Validate the full input before making any changes.
    planned = [make_fields(game) for game in games]
    ids = [f['Game ID'] for f in planned]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate game_id values in input')
    existing = client.records()
    saved = [r.get('fields', {}) for r in existing]
    created = skipped = 0
    for game, fields in zip(games, planned):
        matches = [f for f in saved if f.get('Game ID') == fields['Game ID']]
        # Adopt the manually created example without making a duplicate example row.
        legacy_example = any(f.get('Game') == fields['Game'] and f.get('Data type') == 'Example'
                             and not f.get('Game ID') for f in saved) if game['example'] else False
        if len(matches) > 1:
            raise ValueError(f"Multiple Airtable rows use Game ID {fields['Game ID']}; resolve duplicates first")
        if matches or legacy_example:
            print(f"SKIP {fields['Game ID']}: already saved; original prediction preserved")
            skipped += 1
            continue
        fields = make_fields(game)  # Recheck start time immediately before writing.
        client.create(fields)
        saved.append(fields)
        created += 1
        print(f"CREATED {fields['Game ID']}: away {fields['Away win probability']:.1%}")
    return {'created': created, 'skipped': skipped}


def get_token():
    return os.environ.get('AIRTABLE_TOKEN', '').strip() or (
        TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else '')


def configure():
    print('Create a token at https://airtable.com/create/tokens')
    print('Scopes: data.records:read and data.records:write')
    print('Base access: MLB Prediction Tracker only')
    token = getpass.getpass('Paste token (hidden): ').strip()
    if not token:
        raise ValueError('Token was empty')
    Airtable(token).request(query={'maxRecords': 1})
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(token + '\n')
    print('Connection verified. Token saved locally with owner-only permissions.')
    print('Read access verified; write access is checked on your first upload.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('configure', help='Enter and verify a token privately')
    subs.add_parser('check', help='Check Airtable read access')
    for command in ('preview', 'upload'):
        sub = subs.add_parser(command)
        sub.add_argument('file', type=Path, help='JSON array of game inputs')
    args = parser.parse_args()
    try:
        if args.command == 'configure':
            configure()
        elif args.command == 'check':
            Airtable(get_token()).request(query={'maxRecords': 1})
            print('Connected to MLB Prediction Tracker. Read access verified.')
        else:
            games = json.loads(args.file.read_text())
            if not isinstance(games, list) or not games or any(not isinstance(g, dict) for g in games):
                raise ValueError('Input must be a nonempty JSON array of game objects')
            if args.command == 'preview':
                print(json.dumps([make_fields(g) for g in games], indent=2, allow_nan=False))
            else:
                print(json.dumps(upload(Airtable(get_token()), games)))
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
