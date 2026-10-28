import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from app import douyin_auth, site_checks


class LoginStatusTests(unittest.TestCase):
    def test_current_cookie_overrides_stale_status_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            cookies = Path(directory) / 'cookies.json'
            status = Path(directory) / 'status.json'
            status.write_text(json.dumps({'logged_in': True, 'cookie_count': 99}))
            with patch.object(douyin_auth, 'COOKIE_PATH', str(cookies)), \
                    patch.object(douyin_auth, 'STATUS_PATH', str(status)), \
                    patch.object(douyin_auth.time, 'time', return_value=1000):
                self.assertFalse(douyin_auth.auth_status()['logged_in'])
                cookie = {'name': 'sessionid', 'value': 'fixture-secret', 'domain': '.douyin.com', 'expires': 2000}
                self.assertTrue(douyin_auth.save_cookies([cookie]))
                result = douyin_auth.auth_status()
                self.assertTrue(result['logged_in'])
                self.assertNotIn('fixture-secret', json.dumps(result))
                for changes in ({'expires': 999}, {'expires': 0}, {'expires': 'invalid'}, {'expires': float('inf')},
                                {'domain': 'douyin.com.invalid'}, {'value': ''}, {'name': 'visitor'}):
                    cookies.write_text(json.dumps([{**cookie, **changes}]))
                    self.assertFalse(douyin_auth.auth_status()['logged_in'], changes)
                cookies.write_text(json.dumps([None, 'invalid', {**cookie, 'expires': -1}]))
                self.assertTrue(douyin_auth.auth_status()['logged_in'])
                cookies.write_text('corrupt')
                self.assertFalse(douyin_auth.auth_status()['logged_in'])
                status.write_text('[]')
                self.assertFalse(douyin_auth.auth_status()['logged_in'])


def response(status, location=None):
    value = MagicMock()
    value.status_code = status
    value.headers = {'Location': location} if location is not None else {}
    value.__enter__.return_value = value
    return value


class WebsiteCheckTests(unittest.TestCase):
    def test_success_closes_stream_without_reading_or_downloading_a_body(self):
        page = response(200)
        with patch.object(site_checks.requests, 'Session') as factory:
            session = factory.return_value.__enter__.return_value
            session.get.return_value = page
            result = site_checks._website('douyin', '抖音', 'https://www.douyin.com/')
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(session.get.call_args.kwargs['stream'])
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        self.assertNotIn('cookies', session.get.call_args.kwargs)
        page.iter_content.assert_not_called()
        page.__exit__.assert_called_once()
        factory.return_value.__exit__.assert_called_once()

    def test_redirects_remain_on_the_platform_and_are_bounded(self):
        for target in ('http://weibo.com/', 'https://127.0.0.1/', 'https://weibo.com.invalid/',
                       'https://user:secret@weibo.com/', 'https://weibo.com:8443/'):
            with self.subTest(target=target), patch.object(site_checks.requests, 'Session') as factory:
                session = factory.return_value.__enter__.return_value
                session.get.return_value = response(302, target)
                self.assertEqual(site_checks._website('weibo', '微博', 'https://weibo.com/')['status'], 'warning')
                self.assertEqual(session.get.call_count, 1)
        with patch.object(site_checks.requests, 'Session') as factory:
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = [response(302, f'https://weibo.com/{i}') for i in range(4)]
            result = site_checks._website('weibo', '微博', 'https://weibo.com/')
            self.assertEqual(result['status'], 'warning')
            self.assertEqual(session.get.call_count, 4)

    def test_http_restrictions_and_network_failure_do_not_report_success_or_leak_secrets(self):
        for outcome, expected in ((response(403), 'warning'), (response(500), 'failed'),
                                  (requests.Timeout('secret timeout'), 'failed'),
                                  (requests.ConnectionError('https://user:secret@proxy.invalid'), 'failed')):
            with self.subTest(expected=expected), patch.object(site_checks.requests, 'Session') as factory:
                session = factory.return_value.__enter__.return_value
                if isinstance(outcome, Exception):
                    session.get.side_effect = outcome
                else:
                    session.get.return_value = outcome
                result = site_checks._website('douyin', '抖音', 'https://www.douyin.com/')
                self.assertEqual(result['status'], expected)
                self.assertNotIn('secret', json.dumps(result))

    def test_check_scope_is_login_and_two_websites_and_duplicate_work_is_rejected(self):
        with patch.object(douyin_auth, 'auth_status', return_value={'logged_in': False}), \
                patch.object(site_checks, '_website', side_effect=lambda platform, *_: {'id': platform + '_website', 'status': 'passed'}):
            result = site_checks.check_sites()
            self.assertEqual([check['id'] for check in result['checks']], ['douyin_login', 'douyin_website', 'weibo_website'])
            self.assertEqual(result['checks'][0]['reason'], '未登录')
            with site_checks._LOCK:
                with self.assertRaises(RuntimeError):
                    site_checks.check_sites()
            self.assertEqual(len(site_checks.check_sites()['checks']), 3)
