from contextlib import ExitStack
import asyncio
from pathlib import Path
import os
import tempfile
import time
import threading
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from app import catalog, config, db, library_service, media_jobs, media_library, thumbs


class LibraryServiceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.library = self.root / 'library'
        self.folder = self.library / 'author' / '2026-01-01'
        self.folder.mkdir(parents=True)
        self.stack.enter_context(patch.object(config, 'DB_PATH', str(self.root / 'app.db')))
        self.stack.enter_context(patch.object(thumbs, 'THUMB_DIR', str(self.root / 'cache')))
        self.stack.enter_context(patch.object(media_library, '_get_face_detector', return_value=None))
        self.stack.enter_context(patch.object(library_service, '_stopping', False))
        self.stack.enter_context(patch.object(config, 'MAINTENANCE_ACTIVE', False))
        db.migrate()
        self.media_root = catalog.register_root(self.library)

    def test_real_cover_pipeline_keeps_full_text_and_accepts_video_poster(self):
        cv2.imwrite(str(self.folder / 'clip_cover.jpg'), np.full((180, 240, 3), 180, dtype=np.uint8))
        (self.folder / 'clip.mp4').write_bytes(b'fixture-video-not-decoded-when-poster-present')
        body = '正文保留大小写 CaseSensitive ' * 30
        (self.folder / 'context.md').write_text('```\n' + body + '\n```', encoding='utf-8')
        catalog.scan_root(self.media_root['id'])
        summary = catalog.list_entries()['items'][0]
        self.assertNotIn('text', summary)
        self.assertEqual(len(summary['text_preview']), 120)
        detail = catalog.get_entry(summary['id'])
        self.assertEqual(detail['text'], body.strip())
        result = library_service._analyze(summary['id'], summary['signature'])
        self.assertEqual(result['cover'], 'clip_cover.jpg')
        self.assertEqual(result['cover_type'], 'video')
        self.assertEqual(catalog.get_entry(summary['id'])['cover_signature'], summary['signature'])

    def test_cache_budget_prunes_only_old_generated_thumbnails(self):
        cache = Path(thumbs.THUMB_DIR)
        cache.mkdir()
        old, recent, unrelated = cache / ('a' * 40 + '.jpg'), cache / ('b' * 40 + '.jpg'), cache / 'source.jpg'
        for path in (old, recent, unrelated):
            path.write_bytes(b'x' * 100)
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        with patch.object(config, 'CACHE_LIMIT_BYTES', 100):
            result = thumbs.trim_cache(force=True)
        self.assertEqual(result['bytes'], 100)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())

    def test_task_location_survives_root_rebind(self):
        (self.folder / 'photo.jpg').write_bytes(b'fixture')
        catalog.scan_root(self.media_root['id'])
        task = {'id': 'fixture-task', 'output_dir': str(self.folder)}
        entry = library_service.task_entry(task)
        self.assertIsNotNone(entry)
        import shutil
        target = self.root / 'moved-library'
        shutil.copytree(self.library, target)
        catalog.rebind_root(self.media_root['id'], target)
        self.assertEqual(library_service.task_entry(task)['id'], entry['id'])

    def test_cover_analysis_samples_twelve_and_interruption_never_marks_complete(self):
        entry = {'signature': 'fixture', 'availability': 'present', 'root_id': 'root', 'rel_dir': 'author/date',
                 'cover_candidates': [{'rel': f'{index}.jpg', 'kind': 'image'} for index in range(100)]}
        frame = np.full((10, 10, 3), 100, dtype=np.uint8)
        with patch.object(catalog, 'get_entry', return_value=entry), patch.object(catalog, 'update_cover') as update, \
                patch.object(library_service, 'root_path', return_value='fixture'), \
                patch.object(library_service, 'media_path', side_effect=lambda relative, **_kwargs: relative), \
                patch.object(thumbs, '_imread_unicode', return_value=frame), \
                patch.object(thumbs, 'get_thumb', return_value='cached.jpg') as thumb:
            self.assertIsNotNone(library_service._analyze('entry', 'fixture'))
            self.assertEqual(thumb.call_count, 12)
            self.assertEqual(thumb.call_args_list[-1].args[0], 'author/date/99.jpg')
            update.assert_called_once()
            update.reset_mock()
            thumb.reset_mock()
            def interrupt(*_args, **_kwargs):
                config.MAINTENANCE_ACTIVE = True
                return 'cached.jpg'
            thumb.side_effect = interrupt
            self.assertIsNone(library_service._analyze('entry', 'fixture'))
            self.assertEqual(thumb.call_count, 1)
            update.assert_not_called()


class LibraryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def until(self, event):
        for _ in range(200):
            if event.is_set():
                return
            await asyncio.sleep(.01)
        self.fail('scan did not reach expected stage')

    async def test_download_invalidations_coalesce_followup_before_callbacks(self):
        first, second = threading.Event(), threading.Event()
        release_first, release_second = threading.Event(), threading.Event()
        calls, finished = [], []
        def scan(root_id, **kwargs):
            calls.append(kwargs['scan_id'])
            if len(calls) == 1:
                first.set()
                release_first.wait(3)
            else:
                second.set()
                release_second.wait(3)
            return {'id': kwargs['scan_id'], 'status': 'success'}
        with patch.object(library_service, '_scans', {}), patch.object(library_service, '_stopping', False), \
                patch.object(catalog, 'get_root', return_value={'id': 'root'}), patch.object(catalog, 'scan_root', side_effect=scan):
            handle = library_service.start_scan('root')
            task = library_service._scans[handle['id']]['task']
            task.add_done_callback(lambda result: finished.append(result.result()['id']))
            try:
                await self.until(first)
                for _ in range(10):
                    self.assertEqual(library_service.start_scan('root', force_followup=True)['id'], handle['id'])
                release_first.set()
                await self.until(second)
                self.assertEqual(finished, [])
                self.assertFalse(task.done())
            finally:
                release_first.set()
                release_second.set()
                await task
            await asyncio.sleep(0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(finished, [calls[-1]])

    async def test_maintenance_thumbnail_never_queues_new_work(self):
        jobs = MagicMock()
        stat = MagicMock(st_mtime_ns=1, st_size=20)
        with patch.object(config, 'MAINTENANCE_ACTIVE', True), patch.object(library_service, 'media_path', return_value='fixture.jpg'), \
                patch.object(library_service.os, 'stat', return_value=stat), patch.object(media_jobs, 'jobs', jobs), \
                patch.object(thumbs, 'cached_thumb', return_value='cached.jpg') as cached:
            self.assertEqual(await library_service.thumbnail('author/date/photo.jpg'), 'cached.jpg')
            cached.return_value = None
            with self.assertRaises(media_jobs.BusyError):
                await library_service.thumbnail('author/date/photo.jpg')
            jobs.submit.assert_not_called()
