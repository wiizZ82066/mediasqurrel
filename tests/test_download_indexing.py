"""Download completion/index visibility regressions using only temporary data."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app import catalog, config, db, library_api, library_service, main
from app import task_manager as tm
from app.task_store import TaskStore


class DownloadIndexingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.media = self.temp / 'media'
        self.folder = self.media / 'fixture-author' / '2026-10-10'
        self.folder.mkdir(parents=True)
        for name, value in {
            'DB_PATH': str(self.temp / 'app.db'), 'DATA_DIR': str(self.temp / 'data'),
            'LOG_DIR': str(self.temp / 'logs'), 'LIBRARY_ROOT': str(self.media),
            'DEFAULT_DOWNLOAD_DIR': str(self.media), 'RESOURCE_DIR': str(self.temp),
            'MAINTENANCE_ACTIVE': False,
        }.items():
            self.stack.enter_context(patch.object(config, name, value))
        for name, value in {'_scans': {}, '_stopping': False, '_default_root_id': None}.items():
            self.stack.enter_context(patch.object(library_service, name, value))
        db.migrate()
        self.media_root = catalog.register_root(self.media)
        library_service._default_root_id = self.media_root['id']
        def connect():
            connection = sqlite3.connect(config.DB_PATH)
            connection.row_factory = sqlite3.Row
            return connection
        self.store = TaskStore(connect, self.temp / 'logs')
        for name, value in {
            '_store': self.store, '_stopping': False, '_storage_error': None,
            'TASKS': {}, 'TASK_ORDER': [], '_semaphore': None, '_archive_jobs': set(),
            'ON_TASK_DONE': [], 'ON_TASK_FINISHED': [],
        }.items():
            self.stack.enter_context(patch.object(tm, name, value))
        self.stack.enter_context(patch.object(main.media_library, 'invalidate'))
        self.broadcast = self.stack.enter_context(patch.object(tm, 'broadcast', new=AsyncMock()))
        self.releases = []
        self.real_scan = catalog.scan_root

    async def asyncTearDown(self):
        for release in self.releases:
            release.set()
        scans = list(library_service._scans.values())
        for state in scans:
            state['cancel'].set()
        if scans:
            await asyncio.wait_for(asyncio.gather(*(state['task'] for state in scans), return_exceptions=True), 8)
        await asyncio.sleep(0)
        self.store.flush()

    def saved_task(self, identity='a' * 32, *, status='success', folder=None):
        folder = folder or self.folder
        task = {
            'id': identity, 'script_id': 'weibo', 'script_name': 'fixture downloader',
            'script_icon': '', 'params': {'out': str(folder.parent.parent)},
            'status': status, 'progress': {'percent': 100 if status == 'success' else 0},
            'created_at': '2026-10-10T00:00:00.000Z', 'attempt': 1, 'metadata': {},
            'content_key': None, 'output_dir': str(folder) if status == 'success' else None,
            'output_rel': None, 'logs': [],
        }
        self.store.save(task)
        tm._remember(task)
        return task

    def release_event(self):
        release = threading.Event()
        self.releases.append(release)
        return release

    async def wait_event(self, event):
        self.assertTrue(await asyncio.wait_for(asyncio.to_thread(event.wait, 3), 4), 'index worker did not reach barrier')

    async def wait_index(self, job):
        await asyncio.wait_for(job, 5)
        # The completion callback starts the library_indexed broadcast task.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    def indexed_events(self):
        return [call.args[0] for call in self.broadcast.call_args_list
                if call.args[0].get('type') == 'library_indexed']

    async def assert_pending(self, task):
        response = await library_api.locate(task_id=task['id'])
        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 202)
        payload = json.loads(response.body)
        self.assertEqual(payload['status'], 'indexing')
        self.assertEqual(payload['retry_after'], 1)
        self.assertIn('索引', payload['message'])
        self.assertNotIn('entry_id', payload)

    async def assert_ready(self, task):
        response = await library_api.locate(task_id=task['id'])
        self.assertIsInstance(response, dict)
        self.assertEqual(response['root_id'], self.media_root['id'])
        with db.connect() as connection:
            linked = connection.execute('SELECT entry_id FROM task_media WHERE task_id=?', (task['id'],)).fetchone()
        self.assertEqual(linked['entry_id'], response['entry_id'])
        events = [item for item in self.indexed_events() if item['task_id'] == task['id']]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], {'type': 'library_indexed', 'task_id': task['id'],
                                     'entry_id': response['entry_id'], 'root_id': self.media_root['id']})
        return response

    async def test_success_notice_follows_callback_and_slow_index_is_pending_then_ready(self):
        task = self.saved_task(status='queued')
        # A real child writes a fixture and emits the same directory marker as a
        # downloader. It neither imports a platform script nor opens the network.
        task['command'] = [sys.executable, '-u', '-c',
            "import sys; from pathlib import Path; sys.stdout.reconfigure(encoding='utf-8'); "
            "p=Path(sys.argv[1]); p.mkdir(parents=True,exist_ok=True); "
            "(p/'photo.jpg').write_bytes(b'fixture'); print('保存目录: '+str(p))", str(self.folder)]
        started, release = threading.Event(), self.release_event()
        order = []
        def slow_scan(root_id, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError('fixture scan release missing')
            return self.real_scan(root_id, **kwargs)
        def completion(summary):
            main._invalidate_library(summary)
            order.append('callback')
        async def capture(message):
            if message.get('type') == 'task_update' and message['task']['status'] == 'success':
                order.append('success')
                self.assertTrue(library_service.task_indexing(message['task']))
        self.broadcast.side_effect = capture
        tm.ON_TASK_DONE.append(completion)
        with patch.object(catalog, 'scan_root', side_effect=slow_scan):
            await asyncio.wait_for(tm._run(task['id']), 5)
            await self.wait_event(started)
            self.assertEqual(order, ['callback', 'success'])
            self.assertEqual(self.store.get(task['id'])['status'], 'success')
            self.assertFalse(release.is_set(), 'download success waited for the full index')
            self.assertEqual(self.indexed_events(), [])
            await self.assert_pending(task)
            job = next(iter(library_service._scans.values()))['task']
            release.set()
            await self.wait_index(job)
        await self.assert_ready(task)

    async def test_existing_scan_gets_followup_and_stays_pending_until_new_entry_is_indexed(self):
        task = self.saved_task()
        first, second = threading.Event(), threading.Event()
        release_first, release_second = self.release_event(), self.release_event()
        calls = []
        def phased_scan(root_id, **kwargs):
            calls.append(kwargs['scan_id'])
            if len(calls) == 1:
                # The original pass enumerates before the download appears.
                result = self.real_scan(root_id, **kwargs)
                first.set()
                if not release_first.wait(5):
                    raise TimeoutError('fixture first pass release missing')
                return result
            second.set()
            if not release_second.wait(5):
                raise TimeoutError('fixture followup release missing')
            return self.real_scan(root_id, **kwargs)
        with patch.object(catalog, 'scan_root', side_effect=phased_scan):
            handle = library_service.start_scan(self.media_root['id'])
            job = library_service._scans[handle['id']]['task']
            await self.wait_event(first)
            (self.folder / 'photo.jpg').write_bytes(b'fixture')
            main._invalidate_library(task)
            await self.assert_pending(task)
            release_first.set()
            await self.wait_event(second)
            self.assertFalse(job.done())
            self.assertEqual(self.indexed_events(), [])
            await self.assert_pending(task)
            release_second.set()
            await self.wait_index(job)
        self.assertEqual(len(calls), 2)
        await self.assert_ready(task)

    async def test_failed_or_cancelled_index_without_entry_does_not_publish_ready(self):
        for outcome in ('failed', 'cancelled'):
            with self.subTest(outcome=outcome):
                task = self.saved_task(identity='fixture-' + outcome)
                started, release = threading.Event(), self.release_event()
                def incomplete_scan(root_id, **kwargs):
                    started.set()
                    if not release.wait(5):
                        raise TimeoutError('fixture failed scan release missing')
                    return {'id': kwargs['scan_id'], 'root_id': root_id,
                            'status': 'cancelled' if kwargs['cancel'].is_set() else 'failed'}
                with patch.object(catalog, 'scan_root', side_effect=incomplete_scan):
                    main._invalidate_library(task)
                    await self.wait_event(started)
                    scan_id, state = next(iter(library_service._scans.items()))
                    await self.assert_pending(task)
                    if outcome == 'cancelled':
                        self.assertTrue(library_service.cancel_scan(scan_id))
                    release.set()
                    await self.wait_index(state['task'])
                self.assertFalse(library_service.task_indexing(task))
                with self.assertRaises(HTTPException) as raised:
                    await library_api.locate(task_id=task['id'])
                self.assertEqual(raised.exception.status_code, 404)
                self.assertEqual(self.indexed_events(), [])

    async def test_output_outside_registered_root_never_starts_default_scan(self):
        task = self.saved_task(folder=self.temp / 'unregistered' / 'author' / 'date')
        self.assertIsNone(library_service.task_root_id(task))
        with patch.object(library_service, 'start_scan') as start_scan:
            main._invalidate_library(task)
        start_scan.assert_not_called()
        self.assertFalse(library_service.task_indexing(task))
        with self.assertRaises(HTTPException) as raised:
            await library_api.locate(task_id=task['id'])
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.indexed_events(), [])

    async def test_locate_rechecks_entry_when_scan_finishes_between_lookups(self):
        task = self.saved_task()
        entry = {'id': 'fresh-entry', 'root_id': self.media_root['id'],
                 'author': 'fixture-author', 'rel_dir': 'fixture-author/2026-10-10'}
        with patch.object(library_service, 'task_entry', side_effect=[None, entry]) as lookup, \
                patch.object(library_service, 'task_indexing', return_value=False):
            response = await library_api.locate(task_id=task['id'])
        self.assertEqual(response['entry_id'], entry['id'])
        self.assertEqual(lookup.call_count, 2)


if __name__ == '__main__':
    unittest.main()
