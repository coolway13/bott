import json
import unittest
from unittest.mock import Mock,patch
import local
import discord_cards

class CardTests(unittest.TestCase):
 def setUp(self):
  self.db=local.connect(':memory:')
  self.g={'id':'1','date':'2099-01-01','start':'2099-01-01T20:00:00Z','away':'Away','home':'Home','state':'Live','status':'In Progress','inning':'Top 5th','away_score':0,'home_score':2,'example':False,'updated':local.now(),'live_stats':{'away':{'hits':3,'errors':0},'home':{'hits':4,'errors':1},'outs':2,'balls':1,'strikes':2,'bases':['first']}}
  self.db.execute('INSERT INTO games VALUES (?,?)',('1',json.dumps(self.g)));self.db.commit()
 def tearDown(self): self.db.close()
 def test_one_card_updated_not_reposted(self):
  send=Mock(return_value={'id':'123'})
  with patch('discord_cards.time.sleep'):
   self.assertEqual(discord_cards.publish(self.db,'test',send),1)
   self.assertEqual(discord_cards.publish(self.db,'test',send),0)
   self.g['live_stats']['outs']=0
   self.db.execute('UPDATE games SET data=?',(json.dumps(self.g),));self.db.commit()
   self.assertEqual(discord_cards.publish(self.db,'test',send),1)
  self.assertEqual(send.call_args.kwargs['message_id'],'123')
 def test_stats_and_zero_scores_visible(self):
  e=discord_cards.build_embed(self.g,{'probability':.6,'model':'test'})
  text=json.dumps(e)
  self.assertIn('**0 runs**',text);self.assertIn('Hits: 3',text);self.assertIn('60.0%',text)
 def test_timestamp_alone_does_not_trigger_edit(self):
  send=Mock(return_value={'id':'123'})
  with patch('discord_cards.time.sleep'):
   discord_cards.publish(self.db,'test',send)
   self.g['updated']='2099-01-01T22:00:00Z'
   self.db.execute('UPDATE games SET data=?',(json.dumps(self.g),));self.db.commit()
   self.assertEqual(discord_cards.publish(self.db,'test',send),0)
 def test_failed_delivery_not_marked_saved(self):
  with self.assertRaises(RuntimeError): discord_cards.publish(self.db,'test',Mock(side_effect=RuntimeError('failed')))
  self.assertEqual(self.db.execute('SELECT count(*) FROM discord_games').fetchone()[0],0)
 def test_prediction_embed_contains_actual_model_inputs(self):
  stats={'scored':500,'allowed':450,'scored_games':100,'allowed_games':100,'through':'2026-09-19'}
  self.g['away_id']=143
  e=discord_cards.build_embed(self.g,{'probability':.6,'model':'MLB run-strength baseline v1 (untrained)','inputs':json.dumps({'away':stats,'home':stats})})
  self.assertEqual(e['color'],discord_cards.TEAM_COLORS[143])
  self.assertIn('Predicted winner',e['fields'][0]['name'])
  self.assertIn('Away — 60.0%',e['fields'][0]['value'])
  self.assertIn('5.00',json.dumps(e))
  self.assertIn('not included in this model',json.dumps(e))
  self.assertLessEqual(len(e['fields']),25)
  self.assertTrue(all(len(f['value'])<=1024 for f in e['fields']))
