"""Account validity and database switching use isolated sessions and stores."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import account_status, config, database_location as location, db


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
        local = lambda platform: ((1, 12), platform == 'weibo', False)
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
        local = lambda platform: ((2, 15), platform == 'douyin', True)
        with patch.object(account_status, '_local', side_effect=local), patch.object(account_status, '_verify', side_effect=TimeoutError):
            account_status.start()
            await self.wait_result('douyin', 'unknown')
            item = account_status.snapshot()['items']['douyin']
            self.assertTrue(item['saved'])
            self.assertEqual(set(item), {'status','saved','checked_at','limited'})
            account_status.changed('douyin')
            await self.wait_result('douyin', 'signed_in')
            await account_status.stop()
