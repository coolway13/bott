import unittest
from datetime import datetime, timezone
import local
import kalshi


class KalshiTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,9,20,12,tzinfo=timezone.utc)
        self.g={'start':'2026-09-20T18:35:00Z','state':'Preview','status':'Scheduled',
                'away_id':141,'home_id':140,'away':'Toronto','home':'Texas'}
        self.pred={'probability':.6}
        event='KXMLBGAME-26SEP201435TORTEX'
        self.snapshot={'fetched':self.now.timestamp(),'markets':[
            {'ticker':event+'-'+code,'event_ticker':event,'status':'active',
             'yes_ask_dollars':price,'yes_ask_size_fp':'10'}
            for code,price in [('TOR','.41'),('TEX','.59')]]}

    def value(self):
        return kalshi.field(self.g,self.pred,self.snapshot,self.now)['value']

    def test_actual_ask_and_both_sides(self):
        value=self.value()
        self.assertIn('41.0¢',value)
        self.assertIn('+19.0 percentage points',value)
        self.assertIn('Paper watch: **Toronto**',value)
        self.assertIn('Before fees',value)

    def test_other_doubleheader_time_rejected(self):
        self.g['start']='2026-09-20T22:35:00Z'
        self.assertIn('no exact',self.value())

    def test_stale_quote_rejected(self):
        self.snapshot['fetched']-=361
        self.assertIn('fresh market prices unavailable',self.value())

    def test_started_game_rejected_even_if_feed_still_preview(self):
        self.g['start']='2026-09-20T11:00:00Z'
        self.assertIn('comparison closed',self.value())

    def test_unavailable_ask_not_last_trade(self):
        m=self.snapshot['markets'][0]
        m['yes_ask_dollars']='0'
        m['last_price_dollars']='.41'
        self.assertIn('Pass',self.value())
        m['yes_ask_dollars']='.41'
        m['yes_ask_size_fp']='NaN'
        self.assertIn('Pass',self.value())

    def test_no_positive_difference(self):
        self.pred['probability']=.41
        self.assertIn('neither side',self.value())

    def test_failure_clears_old_quotes_and_throttles_retries(self):
        db=local.connect(':memory:')
        calls=[]
        def fail():
            calls.append(1)
            raise OSError('offline')
        kalshi.refresh(db,lambda:self.snapshot['markets'],lambda:1000)
        kalshi.refresh(db,fail,lambda:1301)
        kalshi.refresh(db,fail,lambda:1302)
        self.assertEqual(len(calls),1)
        self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='kalshi_snapshot'").fetchone())
        db.close()
