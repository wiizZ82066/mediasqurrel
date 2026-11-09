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

    def test_relogin_waits_for_new_session_in_isolated_context(self):
        self.context.cookies.side_effect = [[], [], session('new-fixture')]
        result = search._douyin_login(3)
        self.assertTrue(result['ok'])
        self.assertEqual(self.wait.call_count, 2)
        self.assertEqual(json.loads(self.cookies.read_text()), session('new-fixture'))
        self.context.add_cookies.assert_not_called()
        self.playwright.chromium.launch_persistent_context.assert_not_called()
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()

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
