import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import local


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = local.connect(Path(self.tmp.name)/'test.sqlite3')
        self.g = {'id':'123','date':'2099-01-01','start':'2099-01-01T19:00:00Z',
                  'away':'Away','home':'Home','away_score':None,'home_score':None,
                  'away_pitcher':'','home_pitcher':'','state':'Preview','status':'Scheduled',
                  'inning':'','example':False,'updated':local.now()}
        self.inputs = json.loads((local.ROOT/'games.example.json').read_text())[0]
        self.inputs.update(game_id='123',example=False,away='Away',home='Home')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add_game(self):
        with self.db:
            local.apply_snapshots(self.db,[self.g])

    def test_first_snapshot_silent_then_deduplicated_changes(self):
        self.add_game()
        self.assertEqual(local.apply_snapshots(self.db,[self.g],True),0)
        self.g.update(state='Live',status='In Progress',away_score=0,home_score=0)
        self.assertEqual(local.apply_snapshots(self.db,[self.g],True),1)
        self.assertEqual(local.apply_snapshots(self.db,[self.g],True),0)
        self.assertEqual(self.db.execute('SELECT pending FROM events').fetchone()[0],1)

    def test_final_shutout_evaluates_and_keeps_prediction(self):
        self.add_game()
        local.save_predictions(self.db,[self.inputs])
        before = local.state(self.db)['games'][0]['probability']
        self.g.update(state='Final',status='Final',away_score=0,home_score=4)
        local.apply_snapshots(self.db,[self.g])
        result = local.state(self.db)
        self.assertEqual(result['games'][0]['probability'],before)
        self.assertEqual(result['metrics']['accuracy'],0)
        self.assertAlmostEqual(result['metrics']['brier'],before**2)

    def test_example_never_evaluated(self):
        sample = json.loads((local.ROOT/'games.example.json').read_text())
        local.save_predictions(self.db,sample)
        self.assertEqual(local.state(self.db)['metrics']['evaluated'],0)

    def test_existing_prediction_not_overwritten(self):
        self.add_game()
        local.save_predictions(self.db,[self.inputs])
        old = local.state(self.db)['games'][0]['probability']
        self.inputs['away_wrc_plus']=30
        self.assertEqual(local.save_predictions(self.db,[self.inputs])['skipped'],1)
        self.assertEqual(local.state(self.db)['games'][0]['probability'],old)

    def test_historical_prediction_rejected(self):
        self.g['start']=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        self.add_game()
        with self.assertRaises(ValueError):
            local.save_predictions(self.db,[self.inputs])

    def test_invalid_batch_does_not_partially_write(self):
        self.add_game()
        bad=dict(self.inputs,game_id='124',away_xwoba=None)
        with self.assertRaises(ValueError):
            local.save_predictions(self.db,[self.inputs,bad])
        self.assertEqual(self.db.execute('SELECT count(*) FROM predictions').fetchone()[0],0)

    def test_wrong_teams_rejected(self):
        self.add_game()
        self.inputs['away']='Wrong team'
        with self.assertRaises(ValueError):
            local.save_predictions(self.db,[self.inputs])

    def test_discord_delivered_once(self):
        self.add_game()
        self.g.update(state='Live',status='In Progress')
        local.apply_snapshots(self.db,[self.g],True)
        with patch('local.send_discord') as send:
            local.deliver(self.db,'unused')
            local.deliver(self.db,'unused')
            self.assertEqual(send.call_count,1)

    def test_discord_failure_keeps_queue(self):
        self.add_game()
        self.g.update(state='Live',status='In Progress')
        local.apply_snapshots(self.db,[self.g],True)
        with patch('local.send_discord',side_effect=RuntimeError('Unavailable')):
            with self.assertRaises(RuntimeError):
                local.deliver(self.db,'unused')
        self.assertEqual(self.db.execute('SELECT pending FROM events').fetchone()[0],1)

    def test_webhook_rejects_wrong_hosts_and_secret_errors(self):
        for value in ['https://evil.example/api/webhooks/1/secret','https://discord.com@evil.example/api/webhooks/1/secret']:
            with self.assertRaises(ValueError) as e:
                local.valid_hook(value)
            self.assertNotIn('secret',str(e.exception))

    def test_discord_retry_window_is_respected(self):
        self.add_game()
        self.g.update(state='Live',status='In Progress')
        local.apply_snapshots(self.db,[self.g],True)
        with patch('local.send_discord',side_effect=local.DiscordRateLimit(300)) as send:
            with self.assertRaises(local.DiscordRateLimit):
                local.deliver(self.db,'unused')
            local.deliver(self.db,'unused')
            self.assertEqual(send.call_count,1)

    def test_new_prediction_queues_once_for_discord(self):
        self.add_game()
        local.metadata(self.db,'discord','Enabled')
        local.save_predictions(self.db,[self.inputs])
        local.save_predictions(self.db,[self.inputs])
        events=list(self.db.execute('SELECT message,pending FROM events'))
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['pending'],1)
        self.assertIn('89.5%',events[0]['message'])

    def test_final_message_includes_prediction_and_result(self):
        self.add_game()
        local.save_predictions(self.db,[self.inputs])
        self.g.update(state='Final',status='Final',away_score=0,home_score=4)
        local.apply_snapshots(self.db,[self.g],True)
        event=self.db.execute('SELECT message FROM events ORDER BY id DESC').fetchone()[0]
        self.assertIn('0–4',event)
        self.assertIn('89.5%',event)
        self.assertIn('Missed pick',event)

    def test_missing_prediction_is_not_invented(self):
        self.assertIn('not saved',local.game_message(self.db,self.g))


if __name__=='__main__':
    unittest.main()
