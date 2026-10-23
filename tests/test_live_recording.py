"""Offline recording contracts: temporary media, fake platform, real local FFmpeg."""
import asyncio
from contextlib import ExitStack
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import config, db, catalog, library_service, live_api, live_chat, live_douyin, live_recorder, live_service, live_store, maintenance, task_manager, watcher
from app import live_tasks, live_worker
from app.task_store import TaskStore


def vi(number):
    result = bytearray()
    while number > 127:
        result.append((number & 127) | 128)
        number >>= 7
    return bytes(result + bytes([number]))


def wire(number, value):
    if isinstance(value, str):
        value = value.encode()
    return vi(number * 8 + (2 if isinstance(value, bytes) else 0)) + (vi(len(value)) + value if isinstance(value, bytes) else vi(value))


def chat_frame(identity=123):
    common = wire(2, identity) + wire(4, 1792000000000)
    user = wire(1, 456) + wire(3, '示例用户🌰') + wire(8, 'not-retained')
    chat = wire(1, common) + wire(2, user) + wire(3, '这是测试弹幕 👋')
    message = wire(1, 'WebcastChatMessage') + wire(2, chat) + wire(3, identity)
    return wire(6, 'gzip') + wire(8, gzip.compress(wire(1, message)))


class ChatTests(unittest.TestCase):
    def test_http_retry_limits_follow_ffmpeg_capabilities(self):
        options = dict(ffmpeg='fixture-ffmpeg',url='https://example.invalid/live',ua='test',output='fixture.mp4')
        for help_text, expected in ((b' -reconnect_delay_max ', False),
                                    (b' -reconnect_max_retries <int>\n -reconnect_delay_total_max <int>', True)):
            live_worker._optional_http_limits.cache_clear()
            with patch.object(live_worker.subprocess, 'run', return_value=type('Help', (), {'stdout':help_text})()):
                args = live_worker.command(options)
            self.assertEqual('-reconnect_max_retries' in args, expected)
            self.assertEqual('-reconnect_delay_total_max' in args, expected)
            self.assertIn('-reconnect_on_http_error', args)
            self.assertIn('-reconnect_delay_max', args)
        live_worker._optional_http_limits.cache_clear()

    def test_chat_unicode_ids_and_times_without_private_profile(self):
        value = live_chat.decode_frame(chat_frame())[0]
        self.assertNotIn('user', value)
        self.assertEqual(value['text'], '这是测试弹幕 👋')
        self.assertEqual(value['message_id'], '123')
        self.assertIsNotNone(value['platform_created_at'])
        self.assertNotIn('not-retained', json.dumps(value))

    def test_decoder_bounds_invalid_gzip_and_truncated_fields(self):
        for value in (b'\x80', b'\x0a\x7f', b'\0', b'x' * (live_chat.MAX_FRAME+1),
                      wire(6, 'gzip') + wire(8, gzip.compress(b'x' * (live_chat.MAX_PAYLOAD+1)))):
            with self.subTest(length=len(value)), self.assertRaises((ValueError, OSError)):
                live_chat.decode_frame(value)

    def test_dedup_flush_complete_file_and_report_corrupt_gap(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = live_chat.Journal(folder, '123', db.utc_now())
            journal.stats['connections'] = 1
            journal.video_progress(12)
            journal.offer(chat_frame())
            journal.offer(chat_frame())
            journal.offer(b'\x80')
            journal.close()
            path = Path(folder)
            rows = [json.loads(line) for line in (path/'chat.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(sum(row['type'] == 'chat' for row in rows), 1)
            self.assertEqual(len(rows), 1)
            self.assertNotIn('message_id', rows[0])
            self.assertNotIn('user', rows[0])
            self.assertGreaterEqual(rows[0]['video_time_ms'],12000)
            self.assertLessEqual(rows[0]['video_time_ms'],14000)
            self.assertTrue((path/'chat.events.jsonl').is_file())
            meta = json.loads((path/'chat.meta.json').read_text())
            self.assertEqual(meta['duplicates'], 1)
            self.assertEqual(meta['parse_errors'], 1)
            self.assertEqual(meta['status'], 'captured_with_gaps')
            self.assertFalse((path/'chat.jsonl.part').exists())


class RoomTests(unittest.TestCase):
    def test_worker_diagnostics_only_expose_fixed_codes_not_stream_secrets(self):
        for line, expected in (
            ('HTTP error 403 Forbidden https://cdn.invalid/?token=private', 'http_forbidden'),
            ('HTTP error 503 Service Unavailable', 'http_server'),
            ('Connection timed out', 'network'),
            ('Invalid data found when processing input', 'invalid_media'),
            ('arbitrary secret Cookie: private', ''),
        ):
            self.assertEqual(live_worker.error_code(line), expected)
        self.assertNotIn('private', json.dumps(live_worker.ERRORS))

    def test_selected_quality_never_silently_upgrades_and_reports_actual_fallback(self):
        room={'id_str':'123','status':2,'owner':{'sec_uid':'owner'},'stream_url':{'flv_pull_url':{
            'ORIGIN':'https://test.douyincdn.com/origin.flv','HD1':'https://test.douyincdn.com/hd.flv','SD2':'https://test.douyincdn.com/sd.flv'}}}
        payload={'status_code':0,'data':{'data':[room]}}
        self.assertEqual(live_douyin.parse_room(payload)['quality'],'origin')
        self.assertEqual(live_douyin.parse_room(payload,'uhd')['quality'],'hd')
        self.assertTrue(live_douyin.parse_room(payload,'sd')['stream_url'].endswith('/sd.flv'))
        with self.assertRaises(live_douyin.LiveError):live_douyin.parse_room(payload,'ld')

    def test_known_live_offline_and_unknown_are_distinct(self):
        base = {'id_str':'123456', 'status':2, 'title':'测试直播', 'owner':{'sec_uid':'owner'},
                'stream_url':{'flv_pull_url':{'ORIGIN':'https://example.douyincdn.com/live.flv?token=private'}}}
        parsed = live_douyin.parse_room({'status_code':0,'data':{'data':[base]}})
        self.assertTrue(parsed['live'])
        self.assertEqual(parsed['owner_id'], 'owner')
        self.assertFalse(live_douyin.parse_room({'data':{'data':[{**base, 'status':4}]}})['live'])
        for invalid in ({}, {'data':{'data':[]}}, {'data':{'data':[{**base,'status':100}]}}, {'status_code':403}):
            with self.assertRaises(live_douyin.LiveError):
                live_douyin.parse_room(invalid)

    def test_room_and_stream_urls_cannot_target_local_or_arbitrary_hosts(self):
        self.assertEqual(live_douyin.room_url('https://live.douyin.com/123/'), 'https://live.douyin.com/123')
        for value in ('http://live.douyin.com/123', 'https://live.douyin.com.evil/123', 'https://live.douyin.com/../123', 'https://user@live.douyin.com/123'):
            with self.assertRaises(ValueError):
                live_douyin.room_url(value)
        for value in ('file:///tmp/private', 'http://127.0.0.1/private', 'https://douyincdn.com.evil/x', 'https://u:p@a.douyincdn.com/x'):
            self.assertFalse(live_douyin.stream_url(value))


class LiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.library = self.root/'library'
        self.library.mkdir()
        for name, value in [('DB_PATH',str(self.root/'app.db')),('LIBRARY_ROOT',str(self.library)),('MAINTENANCE_ACTIVE',False)]:
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch.object(task_manager, 'broadcast', new=AsyncMock()))
        self.stack.enter_context(patch.object(task_manager, '_store', None))
        live_service._stopping = False
        live_service._recovering = False
        watcher.init_db()
        self.sub = watcher.add_sub('douyin', 'fixture-owner', '示例直播')
        self.url = 'https://live.douyin.com/123456'
        self.info = {'live':True, 'session_id':'100', 'owner_id':'fixture-owner', 'title':'测试标题',
                     'stream_url':'https://example.douyincdn.com/test.flv?token=private'}
        live_store.configure(self.sub['id'], self.url, True, 60)

    async def asyncTearDown(self):
        await live_service.stop()
        live_service._checks.clear()
        live_service._active.clear()
        live_service._publishing.clear()

    def sample(self):
        path = self.root/'source.mp4'
        try:
            executable = live_recorder.ffmpeg_exe()
        except ValueError:
            self.skipTest('imageio-ffmpeg is not installed')
        subprocess.run([executable,'-v','error','-f','lavfi','-i','color=c=blue:s=160x90:r=10',
            '-t','3','-c:v','libx264','-threads','1','-g','10','-y',str(path)],
            check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=live_recorder.CREATE_HIDDEN)
        return path

    async def test_migration_persists_settings_and_keeps_attempt_history(self):
        record = live_store.create_record(self.sub, self.url, self.info)
        live_store.update_record(record['id'], status='interrupted')
        retry = live_store.create_record(self.sub, self.url, self.info)
        self.assertEqual(retry['previous_id'], record['id'])
        self.assertEqual(len(live_store.recordings(self.sub['id'])), 2)
        self.assertTrue(live_store.monitor(self.sub['id'])['enabled'])
        db.migrate()
        self.assertEqual(live_store.get_record(record['id'])['status'], 'interrupted')
        with db.connect() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 11)

    async def test_disable_during_lookup_cannot_start_a_recording(self):
        gate = threading.Event()
        def lookup(_url, **_options):
            gate.wait(2)
            return self.info
        with patch.object(live_douyin,'lookup',side_effect=lookup), patch.object(live_store,'create_record') as create:
            task = asyncio.create_task(live_service.check(self.sub['id']))
            await asyncio.sleep(.05)
            live_service.stop_recording(self.sub['id'])
            gate.set()
            await task
            create.assert_not_called()

    async def test_configuration_validation_and_delete_guard(self):
        from fastapi import HTTPException
        valid = dict(room_url=self.url,enabled=True,interval_seconds=60)
        for invalid in ({**valid,'enabled':'true'}, {**valid,'interval_seconds':0},
                        {**valid,'room_url':'http://127.0.0.1/private'}, {**valid,'cookie':'not-accepted'}):
            with self.assertRaises(HTTPException):
                await live_api.configure(self.sub['id'],invalid)
        self.assertEqual(live_store.monitor(self.sub['id'])['room_url'],self.url)
        from app.main import api_delete_sub
        with patch.dict(live_service._active,{self.sub['id']:{}}), self.assertRaises(HTTPException):
            await api_delete_sub(self.sub['id'])
        self.assertIsNotNone(watcher.get_sub(self.sub['id']))

    async def test_missed_checks_coalesce_and_manual_trigger_is_bounded(self):
        live_store.update_monitor(self.sub['id'],next_check_at='2000-01-01T00:00:00+00:00')
        self.assertTrue(live_service.request_check(self.sub['id'])['queued'])
        self.assertTrue(live_service.request_check(self.sub['id'])['already_running'])
        with patch.object(live_douyin,'lookup',return_value={'live':False}) as lookup:
            await live_service.check(self.sub['id'])
        self.assertEqual(lookup.call_count,1)
        self.assertGreater(live_store.monitor(self.sub['id'])['next_check_at'],db.utc_now())

    async def test_mismatched_identity_backoff_capacity_and_maintenance(self):
        with patch.object(live_douyin,'lookup',return_value={**self.info,'owner_id':'someone-else'}):
            await live_service.check(self.sub['id'])
        self.assertIn('身份', live_store.monitor(self.sub['id'])['error'])
        self.assertEqual(live_store.monitor(self.sub['id'])['failures'], 1)
        with patch.dict(live_service._active,{999:{},1000:{}}), patch.object(live_douyin,'lookup',return_value=self.info):
            await live_service.check(self.sub['id'])
            self.assertEqual(live_store.monitor(self.sub['id'])['status'],'capacity')
        with patch.object(config,'MAINTENANCE_ACTIVE',True), patch.object(live_douyin,'lookup') as lookup:
            await live_service.check(self.sub['id'])
            lookup.assert_not_called()
        self.assertEqual(live_store.recordings(self.sub['id']),[])

    async def test_dedup_manual_stop_shutdown_and_no_stream_secret_persisted(self):
        gate = asyncio.Event()
        async def fake_record(record, info, owner):
            await gate.wait()
        with patch.object(live_douyin,'lookup',return_value=self.info), patch.object(live_recorder,'ffmpeg_exe',return_value='fixture'), patch.object(live_service,'_record',side_effect=fake_record):
            await live_service.check(self.sub['id'])
            await live_service.check(self.sub['id'])
            self.assertEqual(len(live_store.recordings(self.sub['id'])),1)
            owner = live_service._active[self.sub['id']]
            live_service.stop_recording(self.sub['id'])
            self.assertTrue(owner['stop'].is_set())
            self.assertEqual(owner['reason'],'manual')
            self.assertFalse(live_store.monitor(self.sub['id'])['enabled'])
            gate.set()
            await owner['task']
            live_service._active.clear()
        self.assertNotIn('private',json.dumps(live_api.status(self.sub['id'])))

    async def test_confirmed_live_restarts_a_completed_session_without_parallel_recordings(self):
        previous=live_store.create_record(self.sub,self.url,self.info)
        live_store.update_record(previous['id'],status='completed',reason='offline',finished_at=db.utc_now())
        live_store.update_monitor(self.sub['id'],completed_session=self.info['session_id'],status='waiting')
        gate=asyncio.Event()
        async def record(*_args):
            await gate.wait()
        owner=None
        try:
            with patch.object(live_douyin,'lookup',return_value=self.info), \
                    patch.object(live_recorder,'ffmpeg_exe',return_value='fixture'), \
                    patch.object(live_service,'_record',side_effect=record):
                await live_service.check(self.sub['id'])
                owner=live_service._active.get(self.sub['id'])
                self.assertIsNotNone(owner)
                await live_service.check(self.sub['id'])
                records=live_store.recordings(self.sub['id'])
                self.assertEqual(len(records),2)
                current=next(r for r in records if r['id']!=previous['id'])
                self.assertEqual(current['previous_id'],previous['id'])
                self.assertEqual(current['session_id'],previous['session_id'])
                self.assertEqual(live_store.get_record(previous['id'])['status'],'completed')
                self.assertEqual(live_store.monitor(self.sub['id'])['status'],'recording')
        finally:
            gate.set()
            if owner:
                await owner['task']
                live_service._active.pop(self.sub['id'],None)

    async def test_manual_stop_keeps_confirmed_same_session_disabled(self):
        previous=live_store.create_record(self.sub,self.url,self.info)
        live_store.update_record(previous['id'],status='stopped',reason='manual',finished_at=db.utc_now())
        live_store.update_monitor(self.sub['id'],completed_session=self.info['session_id'])
        live_service.stop_recording(self.sub['id'])
        with patch.object(live_douyin,'lookup',return_value=self.info) as lookup:
            await live_service.check(self.sub['id'])
            lookup.assert_not_called()
        self.assertEqual(len(live_store.recordings(self.sub['id'])),1)
        self.assertFalse(live_store.monitor(self.sub['id'])['enabled'])
        self.assertEqual(live_store.monitor(self.sub['id'])['status'],'disabled')

    async def test_whole_file_stop_finalization_and_index_link(self):
        source = self.sample()
        record = live_store.create_record(self.sub,self.url,self.info)
        stop = threading.Event()
        timer = threading.Timer(1.2,stop.set)
        timer.start()
        try:
            result = await asyncio.to_thread(live_recorder.record_stream,record,str(source),stop,min_free=0,realtime=True,chat=False)
        finally:
            timer.cancel()
        self.assertEqual(result['code'],0)
        record = live_store.get_record(record['id'])
        root = catalog.register_root(str(self.library))
        catalog.scan_root(root['id'])
        self.assertIsNone(catalog.locate_entry(str(live_recorder.folder_for(record))))
        with patch.object(library_service,'_stopping',False):
            success = await live_service._publish(record,'stopped','manual')
        self.assertTrue(success)
        saved = live_store.get_record(record['id'])
        self.assertEqual(saved['status'],'stopped')
        self.assertIsNotNone(saved['entry_id'])
        self.assertEqual(len(list(self.library.rglob('*.mp4'))),1)
        self.assertFalse(list(self.library.rglob('*.part')))
        self.assertEqual(catalog.get_entry(saved['entry_id'])['root_id'],root['id'])

    async def test_worker_disk_guard_retains_record_without_fake_success(self):
        source = self.sample()
        record = live_store.create_record(self.sub,self.url,self.info)
        result = await asyncio.to_thread(live_recorder.record_stream,record,str(source),threading.Event(),min_free=2**64,chat=False)
        self.assertEqual(result['result'],'disk_full')
        self.assertFalse(live_recorder.finalize(live_store.get_record(record['id']),'interrupted','disk full'))

    async def test_restart_recovers_partial_as_interrupted_never_completed(self):
        source = self.sample()
        record = live_store.create_record(self.sub,self.url,self.info)
        folder = live_recorder.prepare(record)
        shutil.copyfile(source,folder/'recording.mp4.part')
        (folder/'chat.jsonl.part').write_text('{"type":"chat","text":"fixture"}\n')
        copy_files = {Path(item['relative_path']).name for item in maintenance._recognized_files(self.library)}
        self.assertTrue({'recording.mp4.part', 'chat.jsonl.part', 'entry.json'} <= copy_files)
        catalog.register_root(str(self.library))
        with patch.object(library_service,'_stopping',False):
            await live_service.recover()
        saved = live_store.get_record(record['id'])
        self.assertEqual(saved['status'],'interrupted')
        self.assertEqual(saved['reason'],'restart')
        self.assertTrue(saved['entry_id'])
        copy_files = {Path(item['relative_path']).name for item in maintenance._recognized_files(self.library)}
        self.assertTrue({'recording.mp4', 'chat.jsonl.part', 'context.md', 'entry.json'} <= copy_files)

    async def test_normal_short_stream_without_offline_confirmation_is_interrupted(self):
        record = live_store.create_record(self.sub,self.url,self.info)
        owner={'stop':threading.Event(),'reason':'','session_id':'100'}
        live_service._active[self.sub['id']]=owner
        with patch.object(live_recorder,'record_stream',return_value={'code':0,'result':'source_end'}), patch.object(live_douyin,'lookup',return_value=self.info):
            await live_service._record(record,self.info,owner)
        self.assertEqual(live_store.get_record(record['id'])['status'],'interrupted')
        self.assertEqual(live_service.active_count(),0)

    async def test_empty_stream_retains_cause_and_backs_off_without_reset_on_lookup(self):
        live_store.configure(self.sub['id'], self.url, True, 360)
        def failed(record, _url, _stop, **options):
            live_recorder.prepare(record)
            return {'code': 1, 'result': 'source_end', 'bytes': 0, 'seconds': 0,
                    'error_code': 'http_forbidden'}
        with patch.object(live_douyin, 'lookup', return_value=self.info), \
                patch.object(live_recorder, 'record_stream', side_effect=failed):
            for failures in (1, 2):
                await live_service.check(self.sub['id'])
                owner = live_service._active[self.sub['id']]
                await owner['task']
                monitor = live_store.monitor(self.sub['id'])
                self.assertEqual(monitor['failures'], failures)
                self.assertIn('403', monitor['error'])
                self.assertNotIn('入库', monitor['error'])
                self.assertEqual(monitor['status'], 'error')
        records = live_api.status(self.sub['id'])['records']
        self.assertEqual(len(records), 2)
        self.assertFalse(records[0]['can_publish'])
        self.assertEqual(records[0]['previous_id'], records[1]['id'])
        tasks = TaskStore().list_page()['items']
        self.assertEqual(len(tasks), 2)
        self.assertEqual(sorted(task['attempt'] for task in tasks), [1, 2])
        self.assertTrue(all(task['status'] == 'interrupted' for task in tasks))
        self.assertNotIn('private', json.dumps(tasks))

    async def test_runtime_status_tracks_progress_and_current_record_on_history_pages(self):
        record = live_store.create_record(self.sub, self.url, self.info)
        owner = {'stop': threading.Event(), 'progress_at': time.monotonic(), 'room_url': self.url}
        live_service._active[self.sub['id']] = owner
        self.assertEqual(live_api.monitors()['items'][0]['runtime_status'], 'connecting')
        live_store.update_record(record['id'], duration_seconds=40, bytes=123456)
        self.assertEqual(live_api.status(self.sub['id'], offset=20)['monitor']['runtime_status'], 'recording')
        self.assertEqual(live_api.status(self.sub['id'], offset=20)['monitor']['record']['bytes'], 123456)
        owner['progress_at'] -= 30
        self.assertEqual(live_api.monitors()['items'][0]['runtime_status'], 'stalled')
        owner['stop'].set()
        self.assertEqual(live_api.monitors()['items'][0]['runtime_status'], 'finalizing')
        live_service._active.clear()

    async def test_completed_live_task_backfill_idempotence_hidden_history_and_retry_guard(self):
        record = live_store.create_record(self.sub, self.url, self.info)
        live_store.update_record(record['id'], status='stopped', finished_at=db.utc_now(),
                                entry_id='fixture-entry', bytes=2000, duration_seconds=60,
                                chat_status='captured', chat_messages=12)
        await live_tasks.reconcile()
        store = TaskStore()
        task_id = 'live-' + record['id']
        task = store.get(task_id)
        self.assertEqual(task['status'], 'success')
        self.assertEqual(task['metadata']['live']['status'], 'stopped')
        self.assertEqual(task['metadata']['live']['chat_messages'], 12)
        self.assertEqual(store.active_count(), 0)
        await live_tasks.reconcile()
        self.assertEqual(len(store.read_logs(task_id)['items']), 1)
        store.delete_record(task_id)
        await live_tasks.reconcile()
        self.assertIsNone(store.get(task_id))
        with patch.object(task_manager, '_store', store), self.assertRaisesRegex(ValueError, '直播'):
            store.restore_record(task_id)
            await task_manager.retry(task_id)

    async def test_worker_recovers_one_temporary_http_server_failure(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        data = self.sample().read_bytes()
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(True)
                if len(calls) == 1:
                    self.send_error(503)
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Content-Type', 'video/mp4')
                    self.end_headers()
                    self.wfile.write(data)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        record = live_store.create_record(self.sub, self.url, self.info)
        try:
            result = await asyncio.to_thread(live_recorder.record_stream, record,
                f'http://127.0.0.1:{server.server_port}/fixture.mp4', threading.Event(), chat=False, min_free=0)
            self.assertEqual(result['code'], 0)
            self.assertGreater(result['seconds'], 0)
            self.assertGreaterEqual(len(calls), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    async def test_partial_video_published_after_disconnect_keeps_diagnostic_and_interrupted_task(self):
        record = live_store.create_record(self.sub, self.url, self.info)
        folder = live_recorder.prepare(record)
        shutil.copyfile(self.sample(), folder/'recording.mp4.part')
        live_store.update_record(record['id'], error=live_worker.ERRORS['network'], duration_seconds=3)
        catalog.register_root(str(self.library))
        with patch.object(library_service, '_stopping', False):
            self.assertTrue(await live_service._publish(live_store.get_record(record['id']), 'interrupted', 'worker_error'))
        saved = live_store.get_record(record['id'])
        self.assertIn('连接超时', saved['error'])
        task = TaskStore().get('live-' + record['id'])
        self.assertEqual(task['status'], 'interrupted')
        self.assertEqual(task['error'], saved['error'])
        self.assertTrue(task['metadata']['live']['entry_id'])
