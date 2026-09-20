import json
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
import cloud
import local


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.db=local.connect(':memory:')
        cloud.schema(self.db)
        today=str(datetime.now(ZoneInfo('America/Los_Angeles')).date())
        game={'id':'1','date':today,'start':today+'T23:00:00Z',
              'away':'Away','home':'Home','state':'Preview','status':'Scheduled',
              'away_score':None,'home_score':None,'example':False,'updated':local.now()}
        self.db.execute('INSERT INTO games VALUES (?,?)',('1',json.dumps(game)))
        self.db.commit()
        self.durable=cloud.snapshot(self.db)
        self.store=Mock()
        self.store.save.side_effect=self.save
        self.send=Mock(return_value={'id':'message1'})
        self.sleep=patch('cloud.time.sleep').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.db.close)

    def save(self,db):
        self.durable=cloud.snapshot(db)

    def run_cycle(self):
        cloud.publish(self.db,self.store,'webhook',self.send)

    def test_restart_does_not_repost(self):
        self.run_cycle()
        cloud.restore(self.db,self.durable)
        self.run_cycle()
        self.assertEqual(self.send.call_count,1)

    def test_reservation_saved_before_post(self):
        def send(*args,**kwargs):
            self.assertEqual(self.durable['tables']['cloud_deliveries'][0][2],'reserved')
            return {'id':'m'}
        self.send.side_effect=send
        self.run_cycle()

    def test_ambiguous_post_is_not_retried(self):
        self.send.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError): self.run_cycle()
        cloud.restore(self.db,self.durable)
        with self.assertRaises(RuntimeError): self.run_cycle()
        self.assertEqual(self.send.call_count,1)

    def test_reservation_failure_prevents_post(self):
        self.store.save.side_effect=OSError()
        with self.assertRaises(OSError): self.run_cycle()
        self.send.assert_not_called()

    def test_post_success_checkpoint_failure_does_not_duplicate(self):
        def save(db):
            if self.store.save.call_count>1: raise OSError()
            self.save(db)
        self.store.save.side_effect=save
        with self.assertRaises(OSError): self.run_cycle()
        cloud.restore(self.db,self.durable)
        with self.assertRaises(RuntimeError): self.run_cycle()
        self.assertEqual(self.send.call_count,1)

    def test_429_is_retryable(self):
        self.send.side_effect=local.DiscordRateLimit(60)
        with self.assertRaises(local.DiscordRateLimit): self.run_cycle()
        cloud.restore(self.db,self.durable)
        self.assertEqual(self.durable['tables']['cloud_deliveries'],[])
        self.send.side_effect=None
        self.run_cycle()
        self.assertEqual(self.send.call_count,2)

    def test_migrated_message_is_edited(self):
        import hashlib
        destination=hashlib.sha256(b'webhook').hexdigest()
        self.db.execute('INSERT INTO discord_games VALUES (?,?,?,?)',('1',destination,'existing','old'))
        self.db.commit()
        self.run_cycle()
        self.assertEqual(self.send.call_args.kwargs['message_id'],'existing')
        self.assertFalse(self.send.call_args.kwargs['recreate_missing'])

    def test_snapshot_excludes_secrets_metadata(self):
        self.db.execute('INSERT INTO meta VALUES (?,?)',('secret','DO_NOT_EXPORT'))
        self.assertNotIn('DO_NOT_EXPORT',json.dumps(cloud.snapshot(self.db)))

    def test_missing_version_fails_closed(self):
        with self.assertRaises(ValueError): cloud.restore(self.db,{'version':2})
