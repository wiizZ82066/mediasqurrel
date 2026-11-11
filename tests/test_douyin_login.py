"""Re-login must not export the old browser session or erase it on failure."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app import douyin_auth as auth, sub_search as search


def session(value):
    return [{'name': 'sessionid', 'value': value, 'domain': '.douyin.com', 'expires': -1}]


class DouyinLoginTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.cookies, self.status = root / 'cookies.json', root / 'status.json'
        self.stack.enter_context(patch.object(auth, 'COOKIE_PATH', str(self.cookies)))
        self.stack.enter_context(patch.object(auth, 'STATUS_PATH', str(self.status)))
        auth.save_cookies(session('old-fixture'))
        self.before = self.cookies.read_bytes(), self.status.read_bytes()

        self.playwright, self.browser, self.context, self.page = (MagicMock() for _ in range(4))
        manager = self.stack.enter_context(patch.object(search, 'sync_playwright'))
        manager.return_value.__enter__.return_value = self.playwright
        self.stack.enter_context(patch('app.browser.launch_chrome', return_value=self.browser))
        self.browser.new_context.return_value = self.context
        self.context.new_page.return_value = self.page
        self.page.is_closed.return_value = False
        self.context.cookies.return_value = []
        # A warm search profile must never be mistaken for a fresh sign-in.
        self.stack.enter_context(patch.object(search, '_dy_open', side_effect=AssertionError('old search profile')))
        self.clock = 0.0
        self.stack.enter_context(patch.object(search.time, 'monotonic', side_effect=lambda: self.clock))
        self.wait = self.stack.enter_context(patch.object(search, '_wait_search', side_effect=self.advance))

    def advance(self, _page, milliseconds):
        self.clock += milliseconds / 1000

    def assert_preserved(self):
        self.assertEqual((self.cookies.read_bytes(), self.status.read_bytes()), self.before)
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()

    def self_response(self, *, host='www.douyin.com', path='self', uid='123', status=0):
        response = MagicMock(url=f'https://{host}/aweme/v1/web/user/profile/{path}/', status=200)
        response.json.return_value = {'status_code': status, 'user': {'uid': uid}}
        self.context.on.call_args.args[1](response)

    def test_relogin_waits_for_confirmed_account_before_saving_new_session(self):
        self.context.cookies.return_value = session('new-fixture')
        def wait(page, millis):
            self.advance(page, millis)
            if self.clock == .5:
                self.self_response(path='other')
            if self.clock == 1:
                self.self_response(uid='0')
            if self.clock == 1.5:
                self.self_response(host='www.douyin.com.example')
            if self.clock == 2.5:
                self.self_response()
        self.wait.side_effect = wait
        result = search._douyin_login(3)
        self.assertTrue(result['ok'])
        self.assertEqual(self.wait.call_count, 5)
        self.assertEqual(self.page.goto.call_args.args[0], 'https://www.douyin.com/user/self')
        self.assertEqual(json.loads(self.cookies.read_text()), session('new-fixture'))
        self.context.add_cookies.assert_not_called()
        self.playwright.chromium.launch_persistent_context.assert_not_called()
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()

    def test_cookie_without_platform_confirmation_keeps_previous_login(self):
        self.context.cookies.return_value = session('not-confirmed')
        result = search._douyin_login(1)
        self.assertFalse(result['ok'])
        self.assertIn('未确认', result['detail'])
        self.assert_preserved()

    def test_rotated_session_survives_reopen_and_guest_cannot_replace_it(self):
        revision = auth.restore_session(self.context)
        fresh = session('updated-fixture')
        auth.record_scan_session(revision, guest=True)
        self.assertTrue(auth.auth_status()['profile_limited'])
        self.assertFalse(auth.auth_status()['logged_in'])
        self.assertEqual(self.cookies.read_bytes(), self.before[0])
        auth.record_scan_session(revision, verified_cookies=fresh + [
            {'domain': '.example.invalid', 'name': 'unrelated', 'value': 'excluded'}])
        reopened = MagicMock()
        current = auth.restore_session(reopened)
        reopened.add_cookies.assert_called_once_with(fresh)
        self.assertTrue(auth.auth_status()['logged_in'])
        self.assertFalse(auth.checkpoint_due(current))
        before = self.cookies.stat().st_mtime_ns, self.status.stat().st_mtime_ns
        auth.record_scan_session(current, verified_cookies=fresh)
        self.assertEqual(before, (self.cookies.stat().st_mtime_ns, self.status.stat().st_mtime_ns))
        auth.record_scan_session(revision, guest=True)
        self.assertTrue(auth.auth_status()['logged_in'])
        auth.save_cookies(session('another-login'), verified=True)
        auth.record_scan_session(current, verified_cookies=session('late-worker'))
        self.assertEqual(auth.load_cookies(), session('another-login'))
        self.assertNotIn('another-login', json.dumps(auth.auth_status()))

    def test_timeout_keeps_saved_session_and_status(self):
        result = search._douyin_login(1)
        self.assertFalse(result['ok'])
        self.assertTrue(result['logged_in'])
        self.assertIn('超时', result['detail'])
        self.assert_preserved()

    def test_closing_window_does_not_report_success_or_erase_session(self):
        self.page.is_closed.side_effect = [False, True]
        result = search._douyin_login(3)
        self.assertFalse(result['ok'])
        self.assertIn('已关闭', result['detail'])
        self.assert_preserved()

    def test_cancellation_releases_browser_and_keeps_saved_session(self):
        self.wait.side_effect = search.SearchCancelled('canceled')
        with self.assertRaises(search.SearchCancelled):
            search._douyin_login(3)
        self.assert_preserved()

    def test_navigation_failure_releases_browser_and_keeps_saved_session(self):
        self.page.goto.side_effect = RuntimeError('navigation failed')
        with self.assertRaisesRegex(RuntimeError, 'navigation failed'):
            search._douyin_login(3)
        self.assert_preserved()

    def test_context_creation_failure_still_closes_browser(self):
        self.browser.new_context.side_effect = RuntimeError('context failed')
        with self.assertRaisesRegex(RuntimeError, 'context failed'):
            search._douyin_login(3)
        self.browser.close.assert_called_once()
        self.assertEqual(self.cookies.read_bytes(), self.before[0])

    def test_invalid_or_failed_session_write_preserves_previous_file(self):
        self.assertFalse(auth.save_cookies([]))
        self.assertEqual((self.cookies.read_bytes(), self.status.read_bytes()), self.before)
        with patch('app.archive.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                auth.save_cookies(session('new-fixture'))
        self.assertEqual((self.cookies.read_bytes(), self.status.read_bytes()), self.before)
        self.assertEqual(sorted(p.name for p in self.cookies.parent.iterdir()), ['cookies.json', 'status.json'])


if __name__ == '__main__':
    unittest.main()
