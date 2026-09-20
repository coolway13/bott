import json
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock
import automatic
import local


class AutoTests(unittest.TestCase):
    def setUp(self):
        self.db=local.connect(':memory:')
        self.game={'id':'999','date':'2099-07-01','start':'2099-07-01T20:00:00Z',
            'away':'Away','home':'Home','away_id':1,'home_id':2,'game_type':'R',
            'state':'Preview','status':'Scheduled','example':False}
        self.stats={'scored':500,'allowed':450,'scored_games':100,'allowed_games':100}
        self.fetch=Mock(return_value=self.stats)
        self.db.execute('INSERT INTO games VALUES (?,?)',('999',json.dumps(self.game)))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_equal_teams_have_modest_home_advantage(self):
        self.assertAlmostEqual(automatic.probability(self.stats,self.stats),1/2.1)

    def test_saved_once_and_queued(self):
        self.assertEqual(automatic.generate(self.db,True,self.fetch)['saved'],1)
        self.assertEqual(automatic.generate(self.db,True,self.fetch)['saved'],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT pending FROM events').fetchone()[0],1)
        self.assertEqual(self.fetch.call_args.args[1].isoformat(),'2099-06-30')

    def test_failure_does_not_invent_a_prediction(self):
        self.fetch.side_effect=ValueError('Unavailable')
        self.assertEqual(automatic.generate(self.db,True,self.fetch),{'saved':0,'waiting':1})
        self.assertEqual(self.db.execute('SELECT count(*) FROM predictions').fetchone()[0],0)

    def test_started_game_not_predicted(self):
        self.game['start']=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        self.db.execute('UPDATE games SET data=?',(json.dumps(self.game),))
        self.db.commit()
        self.assertEqual(automatic.generate(self.db,True,self.fetch)['saved'],0)
        self.fetch.assert_not_called()

    def test_preserves_manual_prediction(self):
        self.db.execute('INSERT INTO predictions VALUES (?,?,?,?,?)',('999',.7,'now','Manual','{}'))
        self.db.commit()
        automatic.generate(self.db,True,self.fetch)
        self.assertEqual(self.db.execute('SELECT probability FROM predictions').fetchone()[0],.7)
        self.fetch.assert_not_called()
