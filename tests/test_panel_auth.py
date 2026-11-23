import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main
from app.sync import Syncer


class PanelAuthTest(unittest.TestCase):
    def test_bind_login_and_rotate(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            setup = Path(directory) / 'panel-setup-token'
            setup.write_text('s' * 43, encoding='utf-8')
            def verify(key):
                if key == b'invalid':
                    raise HTTPException(401)
            with patch.object(main, 'syncer', Syncer(config)), \
                    patch.object(main, 'SETUP_TOKEN_PATH', setup), \
                    patch.object(main, '_verify_site_key', side_effect=verify) as upstream, \
                    patch.object(main, 'SESSION_SECRET', main.SESSION_SECRET):
                browser = TestClient(main.app, base_url='https://box.example', follow_redirects=False)
                other = TestClient(main.app, base_url='https://box.example', follow_redirects=False)
                headers = {'x-colaseedbox-action': '1'}

                def post(client, route, key, token='s' * 43):
                    return client.post(route, json={'api_key': key, 'setup_token': token}, headers=headers)

                self.assertEqual(browser.get('/').status_code, 303)
                self.assertEqual(browser.get('/login').status_code, 200)
                self.assertEqual(browser.get('/api/config').status_code, 401)
                self.assertEqual(post(browser, '/api/login', 'first', 'wrong').status_code, 401)
                self.assertEqual(post(browser, '/api/login', 'invalid').status_code, 401)
                self.assertFalse(config.exists())
                self.assertEqual(post(other, '/api/config/key', 'other').status_code, 401)
                login = post(browser, '/api/login', 'first')
                self.assertEqual(login.status_code, 200)
                self.assertIn('Secure', login.headers['set-cookie'])
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                self.assertEqual(browser.get('/').status_code, 200)
                calls = upstream.call_count
                self.assertEqual(post(other, '/api/login', 'other').status_code, 401)
                self.assertEqual(upstream.call_count, calls)
                self.assertEqual(post(other, '/api/login', 'first').status_code, 200)
                self.assertEqual(post(browser, '/api/config', 'other').status_code, 200)
                self.assertEqual(main._panel_key(), b'first')
                self.assertEqual(post(browser, '/api/config/key', 'invalid').status_code, 401)
                self.assertEqual(post(browser, '/api/config/key', 'other').status_code, 200)
                self.assertEqual(other.get('/api/config').status_code, 401)
                self.assertNotIn('api_key', browser.get('/api/config').json())
                self.assertEqual(post(other, '/api/login', 'first').status_code, 401)
                self.assertEqual(post(other, '/api/login', 'other').status_code, 200)
