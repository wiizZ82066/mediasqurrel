"""Collection indexes retain every source byte and segment-local chat clock."""
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import archive, catalog, config, db, live_collections, live_recorder, live_store, live_tasks, task_manager, watcher
from app.task_store import TaskStore


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.base=Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='ms-collections-')))
        self.root=self.base/'library';self.root.mkdir()
        for key,value in [('DB_PATH',self.base/'app.db'),('LIBRARY_ROOT',self.root),('LOG_DIR',self.base/'logs')]:
            self.stack.enter_context(patch.object(config,key,str(value)))
        self.stack.enter_context(patch.object(task_manager,'_store',None))
        self.stack.enter_context(patch.object(task_manager,'broadcast',AsyncMock()))
        watcher.init_db()
        self.sub=watcher.add_sub('douyin','collection-fixture','测试博主')
        self.root_id=catalog.register_root(self.root)['id']

    def part(self,start,end,*,session='100',sub=None,active=False,missing=False):
        with patch.object(db,'utc_now',return_value=start):
            record=live_store.create_record(sub or self.sub,'https://live.douyin.com/123',
                {'session_id':session,'title':'分段示例','quality':'origin'})
        folder=live_recorder.prepare(record)
        if active:
            return live_store.get_record(record['id'])
        entry_id=None
        if not missing:
            # Metadata-only fixture: a collection must not decode these bytes.
            (folder/'recording.mp4').write_bytes(b'original-'+record['id'].encode())
            (folder/'chat.jsonl').write_text(json.dumps({'type':'chat','text':'你好🙂','received_at':start,
                'video_time_ms':321,'recording_elapsed_ms':456},ensure_ascii=False)+'\n',encoding='utf-8')
            (folder/'context.md').write_text(f'- **发布时间**: {start}\n- **视频标题**: 分段示例\n',encoding='utf-8')
            archive.ArchiveEntry(folder,archive.read_manifest(folder),False).complete(['recording.mp4','chat.jsonl','context.md'])
            catalog.scan_entry(self.root_id,record['output_rel'])
            entry_id=catalog.locate_entry(folder)['id']
        live_store.update_record(record['id'],status='interrupted',finished_at=end,reason='disconnect',
            entry_id=entry_id,bytes=50 if entry_id else 0,duration_seconds=60 if entry_id else 0,
            chat_messages=1 if entry_id else 0,chat_status='captured_with_gaps')
        return live_store.get_record(record['id'])

    def hashes(self):
        return {p.relative_to(self.root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.root.rglob('*') if p.is_file()}

    def test_cross_midnight_one_card_original_clocks_exact_task_target_and_rescan(self):
        a=self.part('2026-10-23T15:30:00+00:00','2026-10-23T15:40:00+00:00')
        b=self.part('2026-10-23T16:40:00+00:00','2026-10-23T16:50:00+00:00')
        before=self.hashes()
        with patch('subprocess.Popen',side_effect=AssertionError('collections never run FFmpeg')):
            asyncio.run(live_collections.reconcile())
        page=catalog.list_entries(root_id=self.root_id)
        self.assertEqual(page['total'],1)
        item=page['items'][0];identity=item['collection']['id']
        self.assertEqual(item['counts']['videos'],2)
        self.assertEqual(item['sort_at'],a['started_at'])
        self.assertEqual(item['date_source'],'recorded')
        self.assertEqual(item['collection']['chat_messages'],2)
        detail=live_collections.enrich_entry(catalog.get_entry(b['entry_id']))
        self.assertEqual(detail['initial_gallery_index'],1)
        self.assertEqual([x['source_entry_id'] for x in detail['gallery']],[a['entry_id'],b['entry_id']])
        self.assertEqual(detail['collection']['parts'][1]['gap_before_seconds'],3600)
        self.assertEqual(detail['collection']['parts'][1]['started_at'],b['started_at'])
        self.assertEqual(detail['collection']['alignment'].count('no continuous'),1)
        self.assertEqual(len(TaskStore().list_page()['items']),1)
        self.assertTrue(TaskStore().read_logs('live-'+a['id'])['items'])
        self.assertEqual(live_collections.refresh(),set())
        catalog.scan_root(self.root_id)
        self.assertEqual(catalog.list_entries()['total'],1)
        self.assertEqual(live_store.get_record(a['id'])['merged_into'],identity)
        self.assertEqual(self.hashes(),before)

    def test_nearby_different_session_or_author_is_separate_and_active_is_never_linked(self):
        a=self.part('2026-10-23T15:00:00+00:00','2026-10-23T15:01:00+00:00')
        b=self.part('2026-10-23T15:02:00+00:00','2026-10-23T15:03:00+00:00',session='200')
        other=watcher.add_sub('douyin','other-identity','测试博主')
        c=self.part('2026-10-23T15:04:00+00:00','2026-10-23T15:05:00+00:00',sub=other)
        running=self.part('2026-10-23T15:06:00+00:00',None,active=True)
        self.assertEqual(live_collections.refresh(),set())
        self.assertEqual(catalog.list_entries()['total'],3)
        self.assertIsNone(live_store.get_record(running['id'])['merged_into'])
        live_store.update_record(running['id'],status='interrupted',finished_at='2026-10-23T15:07:00+00:00')
        d=self.part('2026-10-24T00:00:00+00:00','2026-10-24T00:01:00+00:00')
        live_collections.refresh()
        self.assertEqual(catalog.list_entries()['total'],3)
        self.assertIsNone(live_store.get_record(b['id'])['merged_into'])
        self.assertIsNone(live_store.get_record(c['id'])['merged_into'])
        group=live_store.get_record(a['id'])['merged_into']
        detail=live_collections.enrich_entry(catalog.get_entry(a['entry_id']))
        self.assertEqual(len(detail['collection']['parts']),3)
        self.assertFalse(detail['collection']['parts'][1]['playable'])
        self.assertEqual(live_store.get_record(d['id'])['merged_into'],group)

    def legacy(self,a,b):
        old=self.part('2026-10-23T14:00:00+00:00','2026-10-23T16:00:00+00:00',session='day:2026-10-23')
        with db.connect() as c:
            c.execute('UPDATE live_recordings SET is_aggregate=1 WHERE id=?',(old['id'],))
            for r in (a,b):
                c.execute('UPDATE live_recordings SET merged_into=? WHERE id=?',(old['id'],r['id']))
                c.execute('UPDATE media_entries SET superseded_by=? WHERE id=?',(old['entry_id'],r['entry_id']))
        return old

    def test_legacy_daily_derivative_is_retained_but_originals_form_collection_and_rollback_is_atomic(self):
        a=self.part('2026-10-23T15:00:00+00:00','2026-10-23T15:10:00+00:00')
        b=self.part('2026-10-23T16:00:00+00:00','2026-10-23T16:10:00+00:00')
        old=self.legacy(a,b);before=self.hashes()
        with patch.object(live_collections.uuid,'uuid5',side_effect=RuntimeError('interrupted projection')):
            with self.assertRaises(RuntimeError):live_collections.refresh()
        self.assertEqual(live_store.get_record(a['id'])['merged_into'],old['id'])
        self.assertEqual(catalog.list_entries()['items'][0]['id'],old['entry_id'])
        live_collections.refresh()
        detail=live_collections.enrich_entry(catalog.get_entry(old['entry_id']))
        self.assertEqual(len(detail['gallery']),2)
        self.assertTrue(all(x['source_entry_id']!=old['entry_id'] for x in detail['gallery']))
        self.assertEqual(self.hashes(),before)

    def test_legacy_same_day_different_sessions_restore_standalone_sources(self):
        a=self.part('2026-10-23T15:00:00+00:00','2026-10-23T15:10:00+00:00')
        b=self.part('2026-10-23T15:20:00+00:00','2026-10-23T15:30:00+00:00',session='200')
        old=self.legacy(a,b)
        live_collections.refresh()
        self.assertEqual({x['id'] for x in catalog.list_entries()['items']},{a['entry_id'],b['entry_id']})
        self.assertTrue(live_recorder.folder_for(old).is_dir())

    def test_missing_anchor_keeps_remaining_segments_visible_and_collection_identity_stable(self):
        a=self.part('2026-10-23T15:00:00+00:00','2026-10-23T15:10:00+00:00')
        b=self.part('2026-10-23T16:00:00+00:00','2026-10-23T16:10:00+00:00')
        live_collections.refresh();identity=live_store.get_record(a['id'])['merged_into']
        with db.connect() as c:c.execute("UPDATE media_entries SET availability='missing' WHERE id=?",(a['entry_id'],))
        live_collections.refresh()
        page=catalog.list_entries();self.assertEqual(page['total'],1)
        self.assertEqual(page['items'][0]['id'],b['entry_id'])
        self.assertEqual(page['items'][0]['collection']['id'],identity)
        detail=live_collections.enrich_entry(catalog.get_entry(b['entry_id']))
        self.assertFalse(detail['collection']['parts'][0]['playable'])
        self.assertEqual(len(detail['gallery']),1)

    def test_restart_repairs_task_projection_after_new_segment_committed_without_task_sync(self):
        a=self.part('2026-10-23T15:00:00+00:00','2026-10-23T15:10:00+00:00')
        self.part('2026-10-23T16:00:00+00:00','2026-10-23T16:10:00+00:00')
        asyncio.run(live_collections.reconcile())
        identity=live_store.get_record(a['id'])['merged_into']
        before=TaskStore().get('live-'+identity)
        last=self.part('2026-10-23T17:00:00+00:00','2026-10-23T17:10:00+00:00')
        live_collections.refresh()  # Simulate exit after index commit, before task synchronization.
        self.assertEqual(live_collections.refresh(),set())
        asyncio.run(live_tasks.reconcile())
        after=TaskStore().get('live-'+identity)
        self.assertEqual(after['metadata']['live']['chat_messages'],3)
        self.assertEqual(after['finished_at'],last['finished_at'])
        self.assertNotEqual(after['params']['title'],before['params']['title'])
        self.assertEqual(len(TaskStore().list_page()['items']),1)
        logs=TaskStore().read_logs('live-'+identity)['items']
        asyncio.run(live_tasks.reconcile())
        self.assertEqual(TaskStore().read_logs('live-'+identity)['items'],logs)

    def test_collection_date_navigation_uses_real_recording_time_across_utc_day_boundary(self):
        a=self.part('2026-10-23T16:30:00+00:00','2026-10-23T16:40:00+00:00')
        self.part('2026-10-23T17:30:00+00:00','2026-10-23T17:40:00+00:00')
        before=self.hashes()
        live_collections.refresh()
        self.assertEqual(catalog.list_entries(date_from='2026-10-24',date_to='2026-10-24')['total'],1)
        self.assertEqual(catalog.list_entries(date_from='2026-10-23',date_to='2026-10-23')['total'],0)
        self.assertEqual(catalog.get_entry(a['entry_id'])['sort_at'],a['started_at'])
        self.assertEqual(self.hashes(),before)
