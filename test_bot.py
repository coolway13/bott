import copy
import json
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import bot


class FakeAirtable:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.writes = []

    def records(self):
        return self.rows

    def create(self, fields):
        self.writes.append(fields)
        return {'records': [{'id': 'fake', 'fields': fields}]}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.game = json.loads((bot.ROOT / 'games.example.json').read_text())[0]

    def test_starter_probability(self):
        self.assertAlmostEqual(bot.calculate_probability(self.game), .8947306104774903)

    def test_invalid_stats(self):
        for value in (None, '2.93', True, float('nan'), float('inf'), -1):
            game = dict(self.game, away_pitcher_era=value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                bot.make_fields(game)

    def test_explicit_example_required(self):
        del self.game['example']
        with self.assertRaises(ValueError):
            bot.make_fields(self.game)

    def test_real_game_requires_future_timezone(self):
        self.game.update(example=False, game_id='mlb:123')
        now = datetime.now(timezone.utc)
        for start in (None, now.isoformat(), (now - timedelta(days=1)).isoformat(), '2030-01-01T12:00:00'):
            with self.subTest(start=start), self.assertRaises(ValueError):
                bot.make_fields(dict(self.game, start_time=start), now)
        fields = bot.make_fields(dict(self.game, start_time=(now + timedelta(days=1)).isoformat()), now)
        self.assertEqual(fields['Data type'], 'Real game')
        self.assertEqual(fields['Status'], 'Scheduled')
        self.assertNotIn('Away runs', fields)

    def test_existing_prediction_is_never_replaced(self):
        original = {'Game ID': self.game['game_id'], 'Away win probability': .4, 'Away runs': 3}
        client = FakeAirtable([{'fields': copy.deepcopy(original)}])
        self.assertEqual(bot.upload(client, [self.game]), {'created': 0, 'skipped': 1})
        self.assertEqual(client.writes, [])
        self.assertEqual(client.rows[0]['fields'], original)

    def test_original_airtable_example_not_duplicated(self):
        client = FakeAirtable([{'fields': {'Game': 'EXAMPLE — Phillies @ Mets', 'Data type': 'Example'}}])
        self.assertEqual(bot.upload(client, [self.game])['skipped'], 1)

    def test_example_writes_probability_as_fraction(self):
        client = FakeAirtable()
        self.assertEqual(bot.upload(client, [self.game])['created'], 1)
        fields = client.writes[0]
        self.assertAlmostEqual(fields['Away win probability'], .8947306104774903)
        self.assertEqual(fields['Data type'], 'Example')
        self.assertNotIn('Pick correct', fields)

    def test_entire_file_validated_before_write(self):
        client = FakeAirtable()
        invalid = dict(self.game, game_id='example:invalid', away_xwoba=None)
        with self.assertRaises(ValueError):
            bot.upload(client, [self.game, invalid])
        self.assertEqual(client.writes, [])

    def test_duplicate_input_rejected_before_write(self):
        client = FakeAirtable()
        with self.assertRaises(ValueError):
            bot.upload(client, [self.game, self.game])
        self.assertEqual(client.writes, [])

    def test_pagination(self):
        client = bot.Airtable('test-token')
        with patch.object(client, 'request', side_effect=[
            {'records': [{'id': 'one'}], 'offset': 'next'},
            {'records': [{'id': 'two'}]},
        ]) as request, patch('bot.time.sleep'):
            self.assertEqual(len(client.records()), 2)
            self.assertEqual(request.call_args.kwargs['query']['offset'], 'next')

    def test_malformed_token_is_not_in_error(self):
        token = 'secret-token\ninvalid'
        with self.assertRaises(ValueError) as caught:
            bot.Airtable(token)
        self.assertNotIn('secret-token', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
