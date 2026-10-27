"""Home aggregates use isolated databases, with no platform requests or media downloads."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import catalog, config, db, home_api, task_manager
from app.task_store import TaskStore


class HomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='media-squirrel-home-')
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / 'app.db'
        replacement = patch.object(config, 'DB_PATH', str(self.database))
        replacement.start()
        self.addCleanup(replacement.stop)
        db.migrate()

        def connect():
            result = sqlite3.connect(self.database)
            result.row_factory = sqlite3.Row
            return result
        self.store = TaskStore(connect, Path(temporary.name) / 'logs')
        replacement = patch.object(task_manager, '_store', self.store)
        replacement.start()
        self.addCleanup(replacement.stop)

    def sub(self, number, *, enabled=1):
        with db.connect() as connection:
            connection.execute('INSERT INTO subscriptions(id,platform,blogger_id,nickname,enabled) VALUES (?,?,?,?,?)',
                               (number, 'douyin', str(number), f'Example {number}', enabled))

    def schedule(self, identity, sub_id, due, retry=None, enabled=1):
        with db.connect() as connection:
            connection.execute('''INSERT INTO subscription_schedules
                (id,sub_id,kind,plan_json,enabled,next_due,retry_due) VALUES (?,?,?,?,?,?,?)''',
                (identity, sub_id, 'daily', '{"timezone":"Asia/Shanghai"}', enabled, due, retry))

    def test_counts_include_old_active_tasks_but_return_only_three_summaries(self):
        for index in range(140):
            self.store.save({'id': f'task-{index:03}', 'script_id': 'weibo', 'script_name': '微博下载器', 'script_icon': '',
                'status': 'running' if index < 60 else 'queued' if index < 111 else 'success',
                'created_at': f'2030-01-{1 if index < 111 else 2:02}T00:00:00Z',
                'params': {'url': 'https://example.com/private'}, 'metadata': {}, 'progress': {'percent': 25}})
        result, _, _ = home_api.snapshot()
        self.assertEqual((result['downloads']['running'], result['downloads']['queued']), (60, 51))
        self.assertEqual(len(result['downloads']['items']), 3)
        for item in result['downloads']['items']:
            self.assertEqual(item['status'], 'running')
            self.assertNotIn('params', item)
            self.assertNotIn('logs', item)
            self.assertNotIn('metadata', item)

    def test_next_scan_respects_retry_disabled_and_already_running(self):
        for number in (1, 2, 3):
            self.sub(number, enabled=number != 3)
        self.schedule('regular', 1, '2030-01-03T09:00:00+08:00')
        self.schedule('retry', 2, '2030-01-04T00:00:00Z', '2030-01-03T00:00:00Z')
        self.schedule('disabled-sub', 3, '2020-01-01T00:00:00Z')
        self.schedule('disabled-plan', 1, '2020-01-01T00:00:00Z', enabled=0)
        self.assertEqual(home_api.snapshot()[0]['scans']['next']['sub_id'], 2)
        self.assertEqual(home_api.snapshot(scanning_ids=(2,))[0]['scans']['next']['sub_id'], 1)
        with db.connect() as connection:
            connection.execute('''INSERT INTO schedule_runs
                (id,sub_id,schedule_ids_json,due_at,trigger,status,started_at)
                VALUES ('run',2,'[]','','calendar','running','')''')
        self.assertEqual(home_api.snapshot()[0]['scans']['next']['sub_id'], 1)

    def test_recent_entries_use_import_order_and_bound_large_catalog(self):
        root = catalog.register_root(str(self.database.parent), database=self.database)
        columns = catalog._SUMMARY_COLUMNS.split(',') + ['meta_json', 'search_text', 'scan_generation', 'superseded_by']
        items = []
        for index in range(20000):
            row = dict.fromkeys(columns, '')
            row.update(id=f'entry-{index:05}', root_id=root['id'], rel_dir=f'Example/{index}', author='Example',
                availability='present', archive_status='complete', sort_at='2030-01-01T00:00:00Z',
                imported_at=f'{index:05}', size=0, photo_count=0, video_count=0, live_count=0, file_count=0,
                meta_json='{}', cover_face_json=None, scan_generation=1, superseded_by=None)
            if index == 19999:
                row['availability'] = 'missing'
            if index == 19998:
                row['superseded_by'] = 'entry-19997'
            items.append(tuple(row[column] for column in columns))
        with db.connect() as connection:
            connection.executemany(f'INSERT INTO media_entries({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})', items)
        result = catalog.recent_entries()
        self.assertEqual(len(result), 8)
        self.assertEqual(result[0]['id'], 'entry-19997')
        self.assertEqual(len(catalog.recent_entries(limit=1000000)), 12)
        self.assertNotIn('meta_json', result[0])

    def test_enabled_monitor_is_not_reported_as_recording_and_urls_stay_private(self):
        self.sub(1)
        with db.connect() as connection:
            connection.execute('''INSERT INTO live_monitors(sub_id,room_url,enabled,status)
                VALUES (1,'https://example.com/private-room',1,'offline')''')
        with patch.object(home_api.live_service, '_active', {}), patch.object(home_api.watcher, 'SCAN_STATES', {}):
            result = asyncio.run(home_api.home())
        self.assertEqual(result['live']['enabled'], 1)
        self.assertEqual(result['live']['active'], 0)
        self.assertEqual(result['live']['current']['runtime_status'], 'offline')
        self.assertNotIn('private-room', json.dumps(result))
        self.assertNotIn('plan_json', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
