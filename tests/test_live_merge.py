import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from app import archive, catalog, config, db, library_service, live_merge, live_recorder, live_store, live_tasks, task_manager, watcher
from app.task_store import TaskStore


class DailyMergeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='mediasquirrel-live-merge-')))
        self.library=self.root/'library';self.library.mkdir()
        for name,value in [('DB_PATH',str(self.root/'app.db')),('LIBRARY_ROOT',str(self.library)),('LOG_DIR',str(self.root/'logs'))]:
            self.stack.enter_context(patch.object(config,name,value))
        self.stack.enter_context(patch.object(task_manager,'broadcast',AsyncMock()))
        self.stack.enter_context(patch.object(task_manager,'_store',None))
        self.stack.enter_context(patch.object(library_service,'_stopping',False))
        self.stack.enter_context(patch.object(library_service,'_scans',{}))
        self.stack.enter_context(patch.object(library_service,'_queue_analysis',lambda *a,**k:None)) if hasattr(library_service,'_queue_analysis') else None
        watcher.init_db()
        self.sub=watcher.add_sub('douyin','merge-fixture','示例博主')
        self.root_id=catalog.register_root(self.library)['id']
        self.stack.enter_context(patch.object(library_service,'_default_root_id',self.root_id))
        self.source=self.root/'fixture.mp4'
        subprocess.run([live_recorder.ffmpeg_exe(),'-v','error','-f','lavfi','-i','color=c=blue:s=160x90:r=10',
            '-t','1','-c:v','libx264','-threads','1',str(self.source)],check=True,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=live_recorder.CREATE_HIDDEN)
        live_merge._cancel.clear()

    def part(self,stamp,*,missing=False):
        info={'session_id':'100','title':'示例','quality':'origin'}
        with patch.object(db,'utc_now',return_value=stamp):
            r=live_store.create_record(self.sub,'https://live.douyin.com/123',info)
        folder=live_recorder.prepare(r)
        if not missing:
            shutil.copyfile(self.source,folder/'recording.mp4')
            rows=[{'type':'chat','text':'测试🙂','received_at':stamp,'recording_elapsed_ms':300,
                   'video_time_ms':300,'sender':'must-not-copy'},
                  {'type':'chat','text':'末尾👋','received_at':stamp,'recording_elapsed_ms':1200,'video_time_ms':1200}]
            (folder/'chat.jsonl').write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in rows)+'\n',encoding='utf-8')
            (folder/'chat.events.jsonl').write_text('{"type":"capture_event","event":"capture_end","video_time_ms":1000}\n')
            live_store.update_record(r['id'],duration_seconds=1,chat_messages=2,chat_status='captured')
            r=live_store.get_record(r['id'])
            self.assertTrue(live_recorder.finalize(r,'completed','下播'))
            catalog.scan_entry(self.root_id,r['output_rel'])
            entry=catalog.locate_entry(str(folder))['id']
        else:
            entry=None
        live_store.update_record(r['id'],status='completed' if entry else 'interrupted',
            reason='offline' if entry else 'disconnect',entry_id=entry,finished_at=stamp)
        return live_store.get_record(r['id'])

    async def test_real_concat_chat_offsets_idempotence_sources_tasks_and_rescan(self):
        first=self.part('2026-10-19T12:00:00+00:00')
        empty=self.part('2026-10-19T12:00:03+00:00',missing=True)
        second=self.part('2026-10-19T12:00:05+00:00')
        before={r['id']:hashlib.sha256((live_recorder.folder_for(r)/'recording.mp4').read_bytes()).hexdigest() for r in (first,second)}
        with patch.object(live_merge,'repair_video',side_effect=AssertionError('healthy sources must stay lossless')):
            merged=await live_merge.merge_day(self.sub['id'],'2026-10-19')
        self.assertAlmostEqual(merged['duration_seconds'],2,places=1)
        folder=live_recorder.folder_for(merged)
        chat=[json.loads(x) for x in (folder/'chat.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([x['video_time_ms'] for x in chat],[300,None,1300,None])
        self.assertNotIn('sender',(folder/'chat.jsonl').read_text())
        self.assertEqual(merged['chat_messages'],4)
        meta=json.loads((folder/'chat.meta.json').read_text())
        self.assertEqual(meta['segments'][1]['gap_before_seconds'],4)
        self.assertEqual(meta['excluded_records'],[empty['id']])
        self.assertEqual(meta['video_repair'],'none')
        again=await live_merge.merge_day(self.sub['id'],'2026-10-19')
        self.assertEqual(again['id'],merged['id'])
        catalog.scan_root(self.root_id)
        self.assertEqual(catalog.list_entries(root_id=self.root_id)['total'],1)
        self.assertEqual(catalog.list_authors(root_id=self.root_id)['items'][0]['count'],1)
        store=TaskStore()
        self.assertEqual(len(store.list_page()['items']),1)
        self.assertEqual(store.list_page(q='live-'+first['id'])['items'][0]['id'],'live-'+first['id'])
        for r in (first,second):
            self.assertEqual(hashlib.sha256((live_recorder.folder_for(r)/'recording.mp4').read_bytes()).hexdigest(),before[r['id']])
            store.archive('live-'+r['id'])
            self.assertTrue(store.read_logs('live-'+r['id'])['items'])

    async def test_verification_failure_keeps_originals_and_visible_tasks(self):
        a=self.part('2026-10-19T12:00:00+00:00');b=self.part('2026-10-19T12:00:05+00:00')
        for r in (a,b):await live_tasks.sync_record(r)
        with patch.object(live_merge,'_run',side_effect=ValueError('校验失败')):
            with self.assertRaises(ValueError):await live_merge.merge_day(self.sub['id'],'2026-10-19')
        self.assertEqual(catalog.list_entries(root_id=self.root_id)['total'],2)
        self.assertEqual(len(TaskStore().list_page()['items']),2)
        self.assertIsNone(live_store.get_record(a['id'])['merged_into'])
        live_merge._cancel.set()
        with self.assertRaises(ValueError):
            await live_merge.merge_day(self.sub['id'],'2026-10-19')
        live_merge._cancel.clear()

    async def test_active_incompatible_and_disk_space_fail_without_hiding_sources(self):
        a=self.part('2026-10-19T12:00:00+00:00');b=self.part('2026-10-19T12:00:05+00:00')
        with patch.object(live_merge,'probe',side_effect=[(1,('a',)),(1,('b',))]):
            with self.assertRaisesRegex(ValueError,'编码'):live_merge.build([a,b],threading.Event())
        with patch('shutil.disk_usage') as space:
            space.return_value.free=0
            with self.assertRaisesRegex(ValueError,'空间'):live_merge.build([a,b],threading.Event())
        self.assertEqual(catalog.list_entries(root_id=self.root_id)['total'],2)
        self.assertEqual(live_merge.day_of({**a,'started_at':'2026-10-19T16:00:00+00:00'}),'2026-10-20')

    async def test_index_failure_reuses_verified_revision_without_duplicate_video(self):
        self.part('2026-10-19T12:00:00+00:00');self.part('2026-10-19T12:00:05+00:00')
        with patch.object(library_service,'start_entry_scan',side_effect=ValueError('index interrupted')):
            with self.assertRaises(ValueError):await live_merge.merge_day(self.sub['id'],'2026-10-19')
        with patch.object(live_merge,'_run',side_effect=AssertionError('must not encode again')):
            result=await live_merge.merge_day(self.sub['id'],'2026-10-19')
        self.assertTrue(result['entry_id'])
        self.assertEqual(len(list(self.library.rglob('recording.mp4'))),3)

    def audio_fixture(self):
        subprocess.run([live_recorder.ffmpeg_exe(),'-v','error','-y','-f','lavfi','-i',
            'testsrc2=s=160x90:r=10','-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
            '-t','2','-c:v','libx264','-threads','1','-c:a','aac','-ac','2',str(self.source)],
            check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=live_recorder.CREATE_HIDDEN)

    async def test_interrupted_source_verification_failure_recovers_and_preserves_chat_timeline(self):
        self.audio_fixture()
        first=self.part('2026-10-19T12:00:00+00:00')
        second=self.part('2026-10-19T12:00:10+00:00')
        live_store.update_record(first['id'],status='interrupted',reason='disconnect',error='网络中断')
        hashes={r['id']:hashlib.sha256((live_recorder.folder_for(r)/'recording.mp4').read_bytes()).hexdigest()
                for r in (first,second)}
        verify=live_merge._verify_video
        verified=[]
        def first_verification_fails(path,cancel):
            # Exercise the fallback deterministically. Some FFmpeg versions
            # discard a damaged input packet during remux, so source corruption
            # alone cannot guarantee the merged derivative still fails decode.
            verified.append(path.name)
            if path.name=='recording.mp4.part':
                raise live_merge._VideoProcessError('fixture: damaged decoded frame')
            return verify(path,cancel)  # The repaired video must really decode.
        with patch.object(live_merge,'_verify_video',side_effect=first_verification_fails):
            merged=await live_merge.merge_day(self.sub['id'],'2026-10-19')
        self.assertEqual(verified,['recording.mp4.part','recording.repaired.mp4.part'])
        output=live_recorder.folder_for(merged)
        meta=json.loads((output/'chat.meta.json').read_text())
        self.assertEqual(meta['video_repair'],'decode_reencode')
        self.assertEqual(merged['chat_status'],'captured_with_gaps')
        self.assertIn('损坏帧',merged['error'])
        self.assertAlmostEqual(merged['duration_seconds'],4,delta=.2)
        self.assertEqual(live_merge.probe(output/'recording.mp4')[1][1],('aac','48000','stereo'))
        chats=[json.loads(line) for line in (output/'chat.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([row['video_time_ms'] for row in chats],[300,1200,2300,3200])
        self.assertEqual(meta['segments'][1]['gap_before_seconds'],8)
        self.assertFalse(list(output.glob('*.part')))
        self.assertFalse((output/'.concat.txt').exists())
        for r in (first,second):
            self.assertEqual(hashlib.sha256((live_recorder.folder_for(r)/'recording.mp4').read_bytes()).hexdigest(),hashes[r['id']])
        with patch.object(live_merge,'_run',side_effect=AssertionError('verified revision must be reused')):
            self.assertEqual((await live_merge.merge_day(self.sub['id'],'2026-10-19'))['id'],merged['id'])

    async def test_real_corrupt_packet_repairs_to_strictly_decodable_video(self):
        self.audio_fixture()
        damaged=self.root/'damaged.mp4'
        subprocess.run([live_recorder.ffmpeg_exe(),'-v','error','-i',str(self.source),
            '-c','copy','-bsf:v',r'noise=amount=if(eq(n\,3)\,100\,0)',str(damaged)],
            check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=live_recorder.CREATE_HIDDEN)
        before=hashlib.sha256(damaged.read_bytes()).hexdigest()
        self.assertNotEqual(before,hashlib.sha256(self.source.read_bytes()).hexdigest())
        repaired=self.root/'repaired.mp4'
        measured=live_merge.repair_video(damaged,repaired,threading.Event())
        # repair_video uses the real strict decoder; no mock can hide a broken
        # output. Duration, AAC audio, and original bytes are verified as well.
        self.assertAlmostEqual(measured,2,delta=.2)
        self.assertEqual(live_merge.probe(repaired)[1][1],('aac','48000','stereo'))
        self.assertEqual(hashlib.sha256(damaged.read_bytes()).hexdigest(),before)

    async def test_repair_rejects_changed_timeline_and_cleans_derived_output(self):
        destination=self.root/'repaired.mp4'
        def encode(args,*a,**k):
            destination.write_bytes(b'generated derivative')
        before=self.source.read_bytes()
        with patch.object(live_merge,'probe',side_effect=[(10,()),(5,())]), patch.object(live_merge,'_run',side_effect=encode):
            with self.assertRaisesRegex(ValueError,'时间轴'):
                live_merge.repair_video(self.source,destination,threading.Event())
        self.assertFalse(destination.exists())
        self.assertEqual(self.source.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'相同'):
            live_merge.repair_video(self.source,self.source,threading.Event())
        cancel=threading.Event();cancel.set()
        with self.assertRaisesRegex(ValueError,'中止'):
            live_merge.repair_video(self.source,destination,cancel)
        self.assertFalse(destination.exists())

    async def test_repair_supports_video_without_an_audio_track(self):
        destination=self.root/'silent-repaired.mp4'
        before=hashlib.sha256(self.source.read_bytes()).hexdigest()
        measured=live_merge.repair_video(self.source,destination,threading.Event())
        self.assertAlmostEqual(measured,1,delta=.1)
        self.assertIsNone(live_merge.probe(destination)[1][1])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),before)

    async def test_failure_backoff_applies_only_to_the_source_set_that_failed(self):
        self.part('2026-10-19T12:00:00+00:00');self.part('2026-10-19T12:00:05+00:00')
        failing=AsyncMock(side_effect=ValueError('temporary failure'))
        with patch.object(live_merge,'merge_day',failing):
            await live_merge._pending(set())
            await live_merge._pending(set())
            self.assertEqual(failing.await_count,1)
            self.part('2026-10-19T12:00:10+00:00')
            await live_merge._pending(set())
            self.assertEqual(failing.await_count,2)
        with db.connect() as c:
            c.execute("UPDATE live_day_merges SET retry_at='' WHERE sub_id=?",(self.sub['id'],))
        merged=await live_merge.merge_day(self.sub['id'],'2026-10-19')
        self.assertEqual(catalog.list_entries(root_id=self.root_id)['total'],1)
        self.assertAlmostEqual(merged['duration_seconds'],3,delta=.2)

    async def test_task_sync_interruption_resumes_without_hiding_the_aggregate(self):
        self.part('2026-10-19T12:00:00+00:00');self.part('2026-10-19T12:00:05+00:00')
        with patch.object(live_tasks,'sync_record',AsyncMock(side_effect=ValueError('sync interrupted'))):
            await live_merge._pending(set())
        with db.connect() as c:
            state=dict(c.execute('SELECT * FROM live_day_merges WHERE sub_id=?',(self.sub['id'],)).fetchone())
            self.assertTrue(state['record_id'])
            self.assertEqual(state['error'],'sync interrupted')
            c.execute("UPDATE live_day_merges SET retry_at='' WHERE sub_id=?",(self.sub['id'],))
        with patch.object(live_merge,'_run',side_effect=AssertionError('published derivative must be reused')):
            await live_merge._pending(set())
        self.assertIsNone(live_store.get_record(state['record_id'])['merged_into'])
        self.assertEqual(catalog.list_entries(root_id=self.root_id)['total'],1)
        with db.connect() as c:
            after=dict(c.execute('SELECT * FROM live_day_merges WHERE sub_id=?',(self.sub['id'],)).fetchone())
        self.assertEqual(after['record_id'],state['record_id'])
        self.assertEqual(after['error'],'')
        self.assertEqual(after['retry_at'],'')
        self.assertEqual(len(TaskStore().list_page()['items']),1)
