import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import task_manager as tm
from app.redaction import redact_text, redact_value
from app.task_store import BATCH_LINES, SCHEMA, TaskStore


def task(task_id='task1', status='queued', created='2026-10-09T00:00:00.000Z'):
    return {'id': task_id, 'script_id': 'weibo', 'script_name': '微博下载器', 'script_icon': '',
            'params': {'url': 'https://weibo.com/test'}, 'status': status, 'progress': {'percent': 0},
            'created_at': created, 'attempt': 1, 'metadata': {}, 'content_key': None}


class StoreFixture:
    def setup_store(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'tasks.db'
        def connect():
            conn = sqlite3.connect(self.database)
            conn.row_factory = sqlite3.Row
            return conn
        self.connect = connect
        with connect() as conn:
            conn.executescript(SCHEMA)
        conn.close()
        self.store = TaskStore(connect, Path(self.temp.name) / 'logs')


class TaskStoreTests(StoreFixture, unittest.TestCase):
    def setUp(self):
        self.setup_store()

    def test_summary_cursor_filters_and_archive_roundtrip(self):
        for index in range(105):
            item = task(f'task{index:03}')
            item['params']['url'] += str(index)
            self.store.save(item)
        first = self.store.list_page(limit=50)
        second = self.store.list_page(limit=50, cursor=first['next_cursor'])
        third = self.store.list_page(limit=50, cursor=second['next_cursor'])
        ids = [row['id'] for page in (first, second, third) for row in page['items']]
        self.assertEqual(len(set(ids)), 105)
        self.assertNotIn('logs', first['items'][0])
        self.assertEqual(len(self.store.list_page(q='test104')['items']), 1)
        for index in range(1105):
            self.store.append('task000', '2026-10-09T00:00:00Z', 'stdout', f'line {index}')
            self.assertLess(len(self.store._pending), BATCH_LINES)
        item = self.store.get('task000')
        item['status'] = 'success'
        self.store.save(item)
        self.store.archive('task000')
        self.assertEqual(len(list((Path(self.temp.name) / 'logs').rglob('*.gz'))), 2)
        reopened = TaskStore(self.connect, self.store.log_dir)
        logs = reopened.read_logs('task000', after=995, limit=20)
        self.assertEqual([row['seq'] for row in logs['items']], list(range(996, 1016)))
        self.assertTrue(logs['has_more'])
        self.assertEqual(reopened.read_logs('task000', after=1104)['items'][0]['text'], 'line 1104')
        tail = reopened.read_logs('task000', tail=True, limit=20)
        self.assertEqual(tail['items'][0]['seq'], 1086)
        self.assertEqual(tail['last_log_seq'], 1105)
        self.assertNotIn('task000', self.store._sequences)

    def test_archive_failure_preserves_sql_and_explicit_deletions_do_not_touch_media(self):
        item = task(status='success')
        self.store.save(item)
        self.store.append('task1', '2026-10-09T00:00:00Z', 'stdout', 'saved log')
        with patch('app.task_store.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.store.archive('task1')
        self.assertEqual(self.store.read_logs('task1')['items'][0]['text'], 'saved log')
        media = Path(self.temp.name) / 'original.jpg'
        media.write_bytes(b'original')
        self.store.delete_record('task1')
        self.assertIsNone(self.store.get('task1'))
        self.assertEqual(len(self.store.read_logs('task1')['items']), 1)
        self.store.delete_logs('task1')
        self.assertEqual(self.store.read_logs('task1')['items'], [])
        self.assertEqual(media.read_bytes(), b'original')

    def test_redaction_is_applied_before_persistence_and_export(self):
        item = task()
        item['params'] = {'url': 'https://host/path?access_token=supersecret&x=1', 'headers': {'Cookie': 'sessionid=supersecret'}}
        item['metadata'] = {'subscription': {'item_id': '123', 'claim_token': 'claim_123'}}
        self.store.save(item)
        self.store.append('task1', '2026-10-09T00:00:00Z', 'stderr', 'Cookie: sessionid=supersecret; second=hidden')
        self.store.append('task1', '2026-10-09T00:00:00Z', 'stdout', 'C:\\Users\\private\\media.jpg')
        self.store.flush()
        with self.connect() as conn:
            raw = str([dict(row) for row in conn.execute('SELECT * FROM tasks')])
            raw += str([dict(row) for row in conn.execute('SELECT * FROM task_logs')])
        conn.close()
        self.assertNotIn('supersecret', raw)
        self.assertNotIn('hidden', raw)
        self.assertIn('claim_123', raw)
        self.assertNotIn('private', str(self.store.read_logs('task1', diagnostic=True)))
        self.assertEqual(redact_text('token=secret'), redact_text(redact_text('token=secret')))
        self.assertEqual(redact_value({'Authorization': 'Bearer value'})['Authorization'], '[REDACTED]')
        for name in ('SUB', 'SUBP', 'SESSDATA', 'ttwid', 'auth_key', 'x-oss-security-token'):
            self.assertNotIn('confidential', redact_text(f'{name}=confidential'))
            self.assertNotIn('confidential', redact_text(f'https://host/?{name}=confidential'))
        self.assertNotIn('confidential', redact_text('https://host/#access_token=confidential&other=1'))

    def test_archive_compression_does_not_hold_global_write_lock(self):
        self.store.save(task('finished', 'success'))
        self.store.save(task('active'))
        self.store.append('finished', '2026-10-09T00:00:00Z', 'stdout', 'archived line')
        started, release = threading.Event(), threading.Event()
        real_fsync = os.fsync
        def slow_fsync(fd):
            started.set()
            release.wait(timeout=3)
            real_fsync(fd)
        def append_other():
            self.store.append('active', '2026-10-09T00:00:00Z', 'stdout', 'other task')
            self.store.flush()
        with ThreadPoolExecutor(max_workers=3) as pool, patch('app.task_store.os.fsync', side_effect=slow_fsync):
            archiving = pool.submit(self.store.archive, 'finished')
            try:
                self.assertTrue(started.wait(timeout=2))
                pool.submit(append_other).result(timeout=1)
                deleting = pool.submit(self.store.delete_logs, 'finished')
                self.assertFalse(deleting.done())
            finally:
                release.set()
            archiving.result(timeout=3)
            deleting.result(timeout=3)
        self.assertEqual(self.store.read_logs('finished')['items'], [])
        self.assertEqual(self.store.read_logs('active')['items'][0]['text'], 'other task')
        self.assertEqual(self.store._log_locks, {})

    def test_content_uniqueness_only_applies_to_active_attempts(self):
        original = task()
        original['content_key'] = 'weibo:123'
        self.store.save(original)
        duplicate = task('task2')
        duplicate['content_key'] = 'weibo:123'
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save(duplicate)
        original['status'] = 'failed'
        self.store.save(original)
        self.store.save(duplicate)
        self.assertEqual(self.store.by_content_key('weibo:123')['id'], 'task2')


class TaskRuntimeTests(StoreFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_store()
        self.root_patch = patch.object(tm.config, 'LIBRARY_ROOT', str(Path(self.temp.name) / 'media'))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        tm._store = None
        tm._stopping = False
        tm._storage_error = None
        tm._jobs.clear()
        tm._flush_job = None
        tm.TASKS.clear()
        tm.TASK_ORDER.clear()
        tm._semaphore = None
        tm.release_update_lock()
        await tm.initialize(self.store)

    async def asyncTearDown(self):
        await tm.shutdown()
        tm._store, tm._stopping, tm._storage_error = None, False, None
        tm._resume_enabled = True
        tm.TASKS.clear()
        tm.TASK_ORDER.clear()
        tm._semaphore = None

    async def wait_jobs(self):
        if tm._jobs:
            await asyncio.wait_for(asyncio.gather(*list(tm._jobs.values())), timeout=8)

    async def test_real_child_progress_history_retry_and_no_full_log_updates(self):
        command = [sys.executable, '-u', '-c', "print('MS_PROGRESS_IGNORED'); print('Cookie: hidden'); raise SystemExit(1)"]
        with patch.object(tm.script_registry, 'build_command', return_value=command), \
                patch.object(tm, 'broadcast', new=AsyncMock()) as broadcast:
            original = await tm.create('weibo', {'url': 'https://weibo.com/test'}, content_key='post1')
            await self.wait_jobs()
            retried = await tm.retry(original['id'])
            await self.wait_jobs()
        saved = self.store.get(original['id'])
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(retried['parent_task_id'], original['id'])
        self.assertEqual(retried['attempt'], 2)
        self.assertNotIn('command', tm.get_task(original['id']))
        self.assertGreater(tm.get_task(original['id'])['last_log_seq'], 0)
        self.assertNotIn('hidden', str(self.store.read_logs(original['id'])))
        for call in broadcast.call_args_list:
            if call.args[0]['type'] == 'task_update':
                self.assertNotIn('logs', call.args[0]['task'])

    async def test_restart_marks_running_interrupted_and_retains_queue(self):
        await tm.shutdown()
        running = task('running1', 'running')
        running['progress'] = {'percent': 45, 'label': 'partial'}
        self.store.save(running)
        self.store.save(task('queued1'))
        recovered = await tm.initialize(self.store, resume_queued=False)
        self.assertEqual(recovered[0]['status'], 'interrupted')
        self.assertEqual(self.store.get('running1')['progress']['percent'], 45)
        self.assertEqual(self.store.get('queued1')['status'], 'queued')
        self.assertIn('不支持断点续传', self.store.read_logs('running1')['items'][0]['text'])

    async def test_active_duplicate_returns_same_task_and_metadata_reconciles(self):
        with patch.object(tm, '_run', new=AsyncMock()):
            original = await tm.create('weibo', {'url': 'https://weibo.com/test'}, content_key='post1',
                metadata={'subscription': {'item_id': '1', 'claim_token': 'claim123'}})
            duplicate = await tm.create('weibo', {'url': 'https://weibo.com/test'}, content_key='post1')
            await self.wait_jobs()
        self.assertEqual(original['id'], duplicate['id'])
        self.assertEqual(list(tm.iter_subscription_tasks())[0]['metadata']['subscription']['claim_token'], 'claim123')
        self.assertEqual(original['metadata']['subscription']['claim_token'], '[REDACTED]')

    async def test_storage_failure_cannot_publish_success(self):
        command = [sys.executable, '-u', '-c', "print('download complete')"]
        with patch.object(tm.script_registry, 'build_command', return_value=command), \
                patch.object(self.store, 'flush', side_effect=OSError('disk full')):
            created = await tm.create('weibo', {'url': 'https://weibo.com/test'})
            await self.wait_jobs()
            self.assertEqual(tm.get_task(created['id'])['status'], 'failed')
            self.assertNotEqual(tm.get_task(created['id'])['progress']['percent'], 100)
        self.assertEqual(self.store.get(created['id'])['status'], 'failed')

    async def test_long_line_is_bounded_and_its_secret_tail_is_discarded(self):
        command = [sys.executable, '-u', '-c', "print('x'*70000+'secret_tail'); print('next line')"]
        with patch.object(tm.script_registry, 'build_command', return_value=command):
            created = await tm.create('weibo', {'url': 'https://weibo.com/test'})
            await self.wait_jobs()
        logs = self.store.read_logs(created['id'])['items']
        self.assertNotIn('secret_tail', str(logs))
        self.assertEqual(logs[-1]['text'], 'next line')
        self.assertTrue(any('截断' in row['text'] for row in logs))
        self.assertLess(max(len(row['text']) for row in logs), 16500)

    async def test_slow_socket_does_not_block_broadcast(self):
        class SlowSocket:
            async def send_json(self, _message):
                await asyncio.Event().wait()
            async def close(self, **_kwargs):
                pass
        socket = SlowSocket()
        tm.WS_CLIENTS.add(socket)
        before = time.monotonic()
        await tm.broadcast({'type': 'task_progress', 'task_id': 't', 'progress': {'percent': 4}})
        self.assertLess(time.monotonic() - before, 0.1)
        await asyncio.sleep(0)

    async def test_queue_capacity_and_restart_backlog_are_bounded(self):
        with patch.object(tm.config, 'MAX_QUEUED_TASKS', 3, create=True), patch.object(tm, '_run', new=AsyncMock()):
            for _ in range(3):
                await tm.create('weibo', {'url': 'https://weibo.com/test'})
                self.assertLessEqual(len(tm._jobs), tm.config.MAX_CONCURRENT_TASKS)
            with self.assertRaisesRegex(ValueError, '容量'):
                await tm.create('weibo', {'url': 'https://weibo.com/test'})
            await self.wait_jobs()
        await tm.shutdown()
        self.store.save(task('oldqueued1'))
        self.store.save(task('oldqueued2'))
        with patch.object(tm.config, 'MAX_QUEUED_TASKS', 2, create=True):
            await tm.initialize(self.store, resume_queued=False)
            self.assertEqual(self.store.active_count(), 5)
            active = [entry for entry in tm.TASKS.values() if entry['status'] == 'queued']
            self.assertEqual(len(active), 2)
            active[0]['status'] = 'cancelled'
            self.store.save(active[0])
            tm._fill_queue()
            self.assertEqual(sum(t['status'] == 'queued' for t in tm.TASKS.values()), 2)
            self.assertEqual(self.store.active_count(), 4)

    async def test_output_root_and_relative_escape_validation(self):
        with self.assertRaises(ValueError):
            tm._output_params({'out': '../escape'})
        with self.assertRaises(ValueError):
            tm._output_params({'out': tm.config.RESOURCE_DIR})
        self.assertEqual(tm._output_params({})['out'], tm.config.LIBRARY_ROOT)


if __name__ == '__main__':
    unittest.main()
