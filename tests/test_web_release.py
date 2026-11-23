"""Version switching uses private copies, real worker processes and isolated application paths."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import config, db, runtime, web_api, web_release


class WebReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.base = self.root / 'release-cache'
        self.source = self.root / 'source'
        self.api = self.source / 'app/web_api.py'
        self.api.parent.mkdir(parents=True)
        self.api.write_bytes(Path(web_api.__file__).read_bytes())
        self.dist = self.source / 'frontend/dist'
        (self.dist / 'assets').mkdir(parents=True)
        (self.source / 'frontend/src').mkdir()
        self.initial = self.build('a')
        self.service = web_release.Releases(self.source, self.initial, self.base)
        self.addAsyncCleanup(self.service.stop)
        await self.service.start()

    def build(self, label, *, corrupt_collision=False):
        frontend = self.dist.parent
        (frontend / 'src/main.js').write_text(label)
        asset = 'a' if corrupt_collision else label
        (self.dist / 'index.html').write_text(f'<script src="/assets/{asset}.js"></script>')
        (self.dist / 'assets' / f'{asset}.js').write_text(label)
        (self.dist / '.source-hash').write_text(runtime.frontend_signature(frontend))
        (self.dist / '.build-manifest.json').write_text(json.dumps(runtime._build_files(frontend)))
        return runtime.release_identity(self.source)

    async def test_switch_pins_inflight_request_retains_assets_and_rolls_back_after_restart(self):
        old = self.service.active
        original_pid = old.worker.process.pid
        entered, release = asyncio.Event(), asyncio.Event()

        async def held(_source):
            entered.set()
            await release.wait()
            return {'counts': {'recording': 1}, 'recordings': []}

        with patch.object(web_release, 'snapshot', held):
            task = asyncio.create_task(self.service.render('/api/activity'))
            await asyncio.wait_for(entered.wait(), 3)
            with self.api.open('a', encoding='utf-8') as f:
                f.write("\nROUTES['/api/web/example'] = 'activity'\nold = render\n"
                        "def render(path, data):\n    value = old(path, data)\n"
                        "    if path in ('/api/activity', '/api/web/example'): value['generation'] = 'b'\n"
                        "    return value\n")
            newer = self.build('b')
            await self.service.publish(newer['code_signature'])
            self.assertNotEqual(original_pid, self.service.active.worker.process.pid)
            self.assertTrue(old.worker.process.is_alive())
            old_page = await self.service.pin(self.initial['web'])
            release.set()
            self.assertNotIn('generation', await task)
            self.assertEqual((await self.service.render('/api/web/example'))['generation'], 'b')
            self.assertNotIn('generation', await self.service.render('/api/activity', self.initial['web']))
            self.service.unpin(old_page)
        self.assertEqual(self.service.health()['code_signature'], newer['code_signature'])
        self.assertEqual(self.service.active.bundle.public('assets/a.js').read_text(), 'a')
        await self.service.rollback()
        self.assertEqual(self.service.health()['code_signature'], self.initial['code_signature'])
        restored = web_release.Releases(self.source, newer, self.base)
        self.addAsyncCleanup(restored.stop)
        await restored.start()
        self.assertEqual(restored.active.bundle.version, self.initial['web'])
        self.assertIsNone(runtime.existing_instance_problem({**runtime.runtime_identity(self.source), **restored.health()},
                                                            runtime.runtime_identity(self.source)))

    async def test_incompatible_api_corrupt_asset_and_traversal_cannot_change_current(self):
        self.api.write_text('PROTOCOL=99\nROUTES={}\n')
        bad = self.build('b')
        with self.assertRaises(RuntimeError):
            await self.service.publish(bad['code_signature'])
        self.assertEqual(self.service.state, 'failed')
        original = Path(web_api.__file__).read_bytes()
        self.api.write_bytes(original + b"\nstandard = render\ndef render(path, data):\n"
                             b"    result = standard(path, data)\n"
                             b"    if path == '/api/home': del result['downloads']['items']\n"
                             b"    return result\n")
        with self.assertRaisesRegex(ValueError, '响应不兼容'):
            await self.service.publish(self.build('d')['code_signature'])
        self.api.write_bytes(Path(web_api.__file__).read_bytes())
        collision = self.build('c', corrupt_collision=True)
        with self.assertRaisesRegex(ValueError, '同名'):
            await self.service.publish(collision['code_signature'])
        self.assertEqual(self.service.active.bundle.public('assets/a.js').read_text(), 'a')
        self.assertEqual(self.service.active.bundle.version, self.initial['web'])
        self.assertIsNone(self.service.active.bundle.public('api.py'))
        for path in ('assets/../launcher.key', 'assets/.key', 'assets/C:/test', 'assets/a\\expand.js'):
            with self.assertRaises(ValueError):
                self.service.active.bundle.public(path)
        self.assertFalse(self.service.authorize(''))
        self.assertFalse(self.service.authorize('错误'))
        self.assertTrue(self.service.authorize(self.service.token))

    async def test_worker_failure_recovers_on_initial_and_later_generation(self):
        self.service.active.worker.process.terminate()
        self.service.active.worker.process.join(3)
        self.assertIn('recording', await self.service.render('/api/activity'))
        newer = self.build('b')
        await self.service.publish(newer['code_signature'])
        self.service.active.worker.process.terminate()
        self.service.active.worker.process.join(3)
        self.assertIn('recording', await self.service.render('/api/activity'))
        self.assertEqual(self.service.active.bundle.version, self.initial['web'])

    async def test_core_update_waits_for_owner_and_rechecks_source_before_restart(self):
        restarted = asyncio.Event()
        busy = ['录播正在进行']
        (self.source / 'run.py').write_text('# new owner')
        newer = runtime.release_identity(self.source)
        with patch.object(web_release, 'busy_reason', side_effect=lambda: busy[0]), \
                patch.object(runtime, 'restart_handler', restarted.set), patch.object(config, 'MAINTENANCE_ACTIVE', False), \
                patch.object(web_release.subprocess, 'run') as preflight:
            await self.service.publish(newer['code_signature'])
            self.assertTrue(preflight.called)
            self.assertEqual(self.service.status()['state'], 'waiting')
            self.assertEqual(self.service.active.bundle.version, self.initial['web'])
            await asyncio.sleep(1.1)
            self.assertFalse(restarted.is_set())
            busy[0] = ''
            await asyncio.wait_for(restarted.wait(), 4)
            self.assertTrue(config.MAINTENANCE_ACTIVE)

    async def test_modified_checkout_while_waiting_is_not_restarted(self):
        (self.source / 'run.py').write_text('# changed')
        with patch.object(web_release, 'busy_reason', return_value=''), patch.object(runtime, 'restart_handler') as restart, \
                patch.object(web_release.subprocess, 'run'):
            await self.service.publish(runtime.release_identity(self.source)['code_signature'])
            (self.source / 'run.py').write_text('# changed again')
            await self.service.waiting
            restart.assert_not_called()
            self.assertEqual(self.service.state, 'failed')

    def test_schema_preflight_migrates_only_a_disposable_database_copy(self):
        path = self.root / 'source.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript("CREATE TABLE fixture(id INTEGER); INSERT INTO fixture VALUES (7); PRAGMA user_version=1;")
        steps = [(1, 'base', ''), (2, 'new', 'CREATE TABLE extra(id INTEGER);')]
        with patch.object(config, 'DB_PATH', str(path)), patch.object(config, 'DATA_DIR', str(self.root)), \
                patch.object(runtime, 'release_identity', return_value=self.initial), \
                patch.object(web_release.web_bundle, 'stage', return_value=self.service.active.bundle), \
                patch.object(db, '_migrations', return_value=steps), patch.object(db, 'migrate', wraps=db.migrate) as migrate:
            web_release.check_core()
            copy = migrate.call_args.args[0]
            self.assertNotEqual(copy, path)
            self.assertFalse(copy.parent.exists())
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone(), (1,))
            self.assertEqual(conn.execute('SELECT id FROM fixture').fetchall(), [(7,)])
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='extra'").fetchone())
