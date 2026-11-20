"""Account validity and database switching use isolated sessions and stores."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from app import account_status, config, database_location as location, db


class DouyinAccountRequestTests(unittest.TestCase):
    def test_verifier_requests_its_own_profile_when_page_never_emits_one(self):
        context = MagicMock()
        page = context.new_page.return_value
        with patch('app.browser.sync_playwright'), patch('app.browser.launch_chrome') as launch, \
                patch('app.douyin_auth.restore_session', return_value='saved-generation'):
            launch.return_value.new_context.return_value = context
            page.url = 'https://www.douyin.com/'
            for data, expected in (({'status_code': 0, 'user': {'uid': '123'}}, 'signed_in'),
                                   ({'status_code': 0, 'user': {}}, 'unknown'),
                                   ({'status_code': 401, 'status_msg': '未登录'}, 'expired')):
                page.evaluate.return_value = data
                self.assertEqual(account_status._verify('douyin', threading.Event()), expected)
            self.assertIn('/user/profile/self/', page.evaluate.call_args.args[0])
            page.evaluate.reset_mock()
            page.url = 'https://example.org/'
            self.assertEqual(account_status._verify('douyin', threading.Event()), 'unknown')
            page.evaluate.assert_not_called()


class WeiboAccountRequestTests(unittest.TestCase):
    def test_fast_confirmation_and_browser_confirmation_for_mobile_guests(self):
        url = 'https://m.weibo.cn/api/config'
        stop = threading.Event()
        with patch('app.browser.sync_playwright') as manager, patch('app.weibo_auth.load_cookies', return_value=[]), \
                patch('app.weibo_client.BrowserSession') as browser:
            context = manager.return_value.__enter__.return_value.request.new_context.return_value
            response = context.get.return_value
            response.status, response.url = 200, url
            response.json.return_value = {'ok': 1, 'data': {'login': True}}
            self.assertEqual(account_status._verify('weibo', stop), 'signed_in')
            self.assertEqual(context.get.call_args.args, (url,))
            self.assertEqual(context.get.call_args.kwargs['max_redirects'], 0)
            context.dispose.assert_called_once()
            browser.assert_not_called()

            response.json.return_value = {'ok': 1, 'data': {'login': False}}
            page_check = browser.return_value.__enter__.return_value.request
            for flag, expected in [(True, 'signed_in'), (False, 'expired'), (None, 'unknown')]:
                page_check.return_value = {'ok': 1, 'data': {'login': flag}}
                self.assertEqual(account_status._verify('weibo', stop), expected)
            browser.reset_mock()
            response.status = 429
            self.assertEqual(account_status._verify('weibo', stop), 'unknown')
            browser.assert_not_called()
            context.get.side_effect = TimeoutError()
            page_check.return_value = {'ok': 1, 'data': {'login': True}}
            self.assertEqual(account_status._verify('weibo', stop), 'signed_in')
            browser.assert_called_once()


class DatabaseLocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.current = root / 'current' / 'app.db'
        self.target = root / 'new'
        self.target.mkdir()
        self.paths = root / 'paths.json'
        self.paths.write_text(json.dumps({'library_root': str(root / 'media')}), encoding='utf8')
        self.enterContext(patch.multiple(config, DB_PATH=str(self.current), DEFAULT_DB_PATH=str(self.current),
                             PATHS_FILE=str(self.paths), LIBRARY_ROOT=str(root / 'media'), _store=None, _paired=False))
        self.enterContext(patch.object(config, 'database_pinned_by_environment', return_value=False))
        db.migrate(self.current)
        with db.connect(self.current) as c:
            c.execute('CREATE TABLE sentinel(value TEXT)')
            c.execute("INSERT INTO sentinel VALUES ('original')")

    def test_preview_creates_nothing_and_new_database_begins_empty(self):
        self.assertEqual(location.preview(str(self.target))['kind'], 'new')
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertTrue(location.select(str(self.target), 'new')['restart_required'])
        new = self.target / 'app.db'
        self.assertEqual(location.preview(str(self.target))['kind'], 'existing')
        with db.connect(new) as c:
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name='sentinel'").fetchone())
        with db.connect(self.current) as c:
            self.assertEqual(c.execute('SELECT value FROM sentinel').fetchone()[0], 'original')
        self.assertEqual(config._read_paths()['library_root'], config.LIBRARY_ROOT)
        with patch.object(config, 'DB_PATH', str(new)):
            self.assertFalse(location.status()['restart_required'])
            self.assertTrue(location.select(str(self.current.parent), 'existing')['restart_required'])
            self.assertEqual(location.status()['next_directory'], str(self.current.parent))
            self.assertNotIn('database_directory', config._read_paths())

    def test_reject_replaced_preview_and_foreign_database(self):
        location.preview(str(self.target))
        path = self.target / 'app.db'
        with closing(sqlite3.connect(path)) as c:
            c.execute('CREATE TABLE foreign_data(x)')
            c.execute('PRAGMA user_version=3')
        for mode in ('new', 'existing'):
            with self.assertRaises(ValueError):
                location.select(str(self.target), mode)
        with closing(sqlite3.connect(path)) as c:
            self.assertTrue(c.execute("SELECT 1 FROM sqlite_master WHERE name='foreign_data'").fetchone())
        self.assertNotIn('database_directory', config._read_paths())

    def test_reject_media_tree_and_nonempty_plain_folders(self):
        media = Path(config.LIBRARY_ROOT)
        media.mkdir()
        (self.target / 'kept.txt').touch()
        for path in (media, self.target, Path('relative')):
            with self.assertRaises(ValueError):
                location.preview(str(path))


class BackgroundAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await account_status.stop()

    async def wait_result(self, platform, status):
        for _ in range(150):
            if account_status.snapshot()['items'][platform]['status'] == status:
                return
            await asyncio.sleep(.01)
        self.fail('account check did not publish ' + status)

    async def test_service_checks_while_no_settings_page_is_open(self):
        local = lambda platform: account_status.LocalSession('v1', platform == 'weibo')
        with patch.object(account_status, '_local', side_effect=local), patch.object(account_status, '_verify', return_value='expired') as verify:
            self.assertEqual(account_status.snapshot()['items']['weibo']['status'], 'pending')
            account_status.start()
            await self.wait_result('weibo', 'expired')
            for _ in range(4):
                data = account_status.snapshot()['items']
                self.assertIsNotNone(data['weibo']['checked_at'])
                self.assertEqual(data['douyin']['status'], 'signed_out')
            self.assertEqual(verify.call_count, 1)
            await account_status.stop()

    async def test_network_failure_does_not_revoke_verified_session_or_expose_credentials(self):
        local = lambda platform: account_status.LocalSession('v2', platform == 'douyin', True)
        with patch.object(account_status, '_local', side_effect=local), patch.object(account_status, '_verify', side_effect=TimeoutError):
            account_status.start()
            await self.wait_result('douyin', 'unknown')
            item = account_status.snapshot()['items']['douyin']
            self.assertTrue(item['saved'])
            self.assertEqual(set(item), {'status','saved','checked_at','limited'})
            account_status.changed('douyin')
            await self.wait_result('douyin', 'signed_in')
            await account_status.stop()

    async def test_slow_platform_and_repeated_page_reads_do_not_hold_up_the_other(self):
        slow = threading.Event()
        def verify(platform, stop):
            if platform == 'douyin':
                slow.wait(3)
            return 'signed_in'
        with patch.object(account_status, '_local', return_value=account_status.LocalSession('v1', True)), \
                patch.object(account_status, '_verify', side_effect=verify) as probe:
            account_status.start()
            try:
                await self.wait_result('weibo', 'signed_in')
                self.assertEqual(account_status.snapshot()['items']['douyin']['status'], 'pending')
                await asyncio.gather(*(asyncio.to_thread(account_status.snapshot) for _ in range(8)))
            finally:
                slow.set()
            await self.wait_result('douyin', 'signed_in')
            self.assertCountEqual([call.args[0] for call in probe.call_args_list], ['weibo', 'douyin'])
            await account_status.stop()

    async def test_new_session_wakes_sleeping_check_and_old_probe_cannot_undo_relogin(self):
        sessions = {'weibo': account_status.LocalSession(None, False), 'douyin': account_status.LocalSession('v1', True)}
        probing, release = threading.Event(), threading.Event()
        notified = asyncio.Event()
        async def published(event):
            if event['platform'] == 'douyin' and release.is_set():
                notified.set()
        def verify(_platform, _stop):
            if sessions['douyin'].version == 'v2':
                probing.set()
                release.wait(3)
            return 'expired'
        with patch.object(account_status, '_local', side_effect=sessions.__getitem__), \
                patch.object(account_status, '_verify', side_effect=verify) as probe, \
                patch('app.task_manager.broadcast', side_effect=published):
            account_status.start()
            await self.wait_result('douyin', 'expired')
            sessions['douyin'] = account_status.LocalSession('v2', True)
            self.assertEqual((await asyncio.to_thread(account_status.snapshot))['items']['douyin']['status'], 'pending')
            try:
                self.assertTrue(await asyncio.to_thread(probing.wait, 1))
                sessions['douyin'] = account_status.LocalSession('v3', True)
                account_status.changed('douyin')
            finally:
                release.set()
            await asyncio.wait_for(notified.wait(), 1)
            self.assertEqual(account_status.snapshot()['items']['douyin']['status'], 'signed_in')
            await account_status.stop()
            self.assertEqual(probe.call_count, 2)

    async def test_only_fresh_matching_platform_confirmation_can_skip_network(self):
        now = time.time()
        session = account_status.LocalSession('rotated', True, False, now - 20)
        with patch.object(account_status, '_local', side_effect=lambda p: session if p == 'douyin' else account_status.LocalSession(None, False)), \
                patch.object(account_status, '_verify', return_value='expired') as probe:
            self.assertEqual(account_status.snapshot()['items']['douyin']['status'], 'signed_in')
            account_status.start()
            await asyncio.sleep(.02)
            probe.assert_not_called()
            session = account_status.LocalSession('new', True, False, now - account_status.INTERVAL - 1)
            await self.wait_result('douyin', 'expired')
            session = session._replace(verified_at=now - 3)
            self.assertEqual(account_status.snapshot()['items']['douyin']['status'], 'expired')
            probe.assert_called_once()
            await account_status.stop()
