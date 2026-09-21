import io
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request
import security
import local
import kalshi
import bot
import cloud

class SecurityTests(unittest.TestCase):
    def test_reject_unapproved_and_confusable_hosts_before_network(self):
        with patch('security.build_opener') as opener:
            for url in ['http://discord.com/api','https://discord.com.evil.invalid/',
                        'https://discord.com@evil.invalid/', 'https://api.airtable.com/',
                        'https://external-api.kalshi.com/', 'https://discord.com:444/',
                        'https://user:pass@discord.com/']:
                with self.subTest(url=url),self.assertRaises(RuntimeError):security.urlopen(url)
            opener.assert_not_called()

    def test_redirect_never_forwards_request(self):
        with self.assertRaises(RuntimeError) as e:
            security.NoRedirect().redirect_request(Request('https://discord.com/'),None,302,'',{},'https://evil.invalid/')
        self.assertNotIn('https',str(e.exception))

    def test_split_log_writes_and_environment_secrets(self):
        secret='synthetic-sensitive-value'
        out=io.StringIO(); stream=security.RedactedStream(out)
        with patch.dict(os.environ,{'GH_TOKEN':secret}):
            stream.write(secret[:10]);stream.flush();self.assertEqual(out.getvalue(),'')
            stream.write(secret[10:]+'\n');stream.finish()
        self.assertNotIn(secret,out.getvalue());self.assertIn('[REDACTED]',out.getvalue())

    def test_nested_discord_payload_redaction(self):
        url='https://discord.com/api/webhooks/123/dummy'
        response=Mock();response.read.return_value=b'{"id":"12345"}'
        context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
        with patch('local.urlopen',return_value=context) as send:
            local.send_discord(url,url,embed={'description':url})
            payload=send.call_args.args[0].data.decode()
        self.assertNotIn(url,payload);self.assertIn('REDACTED',payload)

    def test_file_credentials_not_loaded(self):
        with patch.dict(os.environ,{},clear=True),patch.object(local.Path,'read_text') as read:
            with self.assertRaises(ValueError):local.hook_value()
            read.assert_not_called()

    def test_disabled_services_cannot_connect(self):
        with self.assertRaises(RuntimeError):kalshi.fetch_markets()
        with self.assertRaises(RuntimeError):bot.Airtable('dummy').request()
        with self.assertRaises(RuntimeError):bot.configure()

    def test_public_snapshot_scrubs_credentials(self):
        db=local.connect(':memory:');cloud.schema(db)
        url='https://discord.com/api/webhooks/123/dummy'
        db.execute('INSERT INTO games VALUES (?,?)',('1',json.dumps({'note':url})))
        value=json.dumps(cloud.snapshot(db))
        self.assertNotIn(url,value);db.close()

    def test_message_identifier_cannot_inject_url_path(self):
        with patch('local.urlopen') as send:
            with self.assertRaises(ValueError):local.send_discord('https://discord.com/api/webhooks/123/dummy','',message_id='../anything')
            send.assert_not_called()
