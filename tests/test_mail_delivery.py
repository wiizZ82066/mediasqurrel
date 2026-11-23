import asyncio
import os
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
import smtplib
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app import archive, catalog, config, db, mail_delivery as delivery, mail_media as media, mail_store as store
from app.task_store import TaskStore


class MailDeliveryTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.library = root / 'library'
        self.library.mkdir()
        paths = patch.multiple(config, DATA_DIR=str(root), CACHE_DIR=str(root), DB_PATH=str(root/'app.db'),
                               LIBRARY_ROOT=str(self.library), uses_prepared_database=lambda _: False)
        paths.start()
        self.addCleanup(paths.stop)
        db.migrate()
        store.initialize()
        protector = patch.object(store, 'seal', return_value=b'opaque blob')
        protector.start()
        self.addCleanup(protector.stop)
        self.save()
        self.task = self.create_work()
        self.client = MagicMock()
        self.client.esmtp_features = {'size': '20000000'}
        self.client.sendmail.return_value = {}
        transport = patch.object(delivery, 'open_smtp', return_value=self.client)
        transport.start()
        self.addCleanup(transport.stop)
        closing = patch.object(delivery, '_closing')
        self.closed = closing.start()
        self.closed.is_set.return_value = False
        self.addCleanup(closing.stop)

    def save(self, recipient='target@qq.com', enabled=True):
        return store.save('sender@qq.com', recipient, 'example1234', enabled)

    def create_work(self):
        post = archive.begin_entry(self.library, '作者', '25-01-01', 'weibo', 'Example42')
        folder = Path(post.folder)
        (folder/'photo').mkdir()
        (folder/'live').mkdir()
        (folder/'photo'/'01.jpg').write_bytes(b'photo')
        (folder/'live'/'01.mov').write_bytes(b'live')
        (folder/'context.md').write_text('- **作者**: 作者\n' + chr(96)*3 + '\n作品正文\n' + chr(96)*3, encoding='utf-8')
        (folder/'unrelated.jpg').write_bytes(b'private')
        (folder/'state.json').write_bytes(b'private')
        post.complete(['context.md', 'photo/01.jpg', 'live/01.mov'])
        task = dict(id='work1', status='success', script_id='weibo', script_name='微博', script_icon='',
                    created_at=store.utc_now(), finished_at=store.utc_now(), params={'out':str(self.library)},
                    metadata={}, progress={}, output_dir=str(folder), content_key='weibo:Example42')
        TaskStore().save(task)
        return task

    def deliver_next(self):
        job, pref = store.claim()
        self.assertIsNotNone(job)
        delivery.deliver(job, pref)
        return store.public()['recent'][0]

    def index_recording(self):
        entry = archive.begin_entry(self.library, '测试博主', '25-01-01', 'douyin', 'Recording1')
        folder = Path(entry.folder)
        (folder/'recording.mp4').write_bytes(b'fixture')
        (folder/'context.md').write_text('测试录播', encoding='utf-8')
        entry.complete(['context.md', 'recording.mp4'])
        root = catalog.register_root(self.library)
        catalog.scan_entry(root['id'], folder.relative_to(self.library).as_posix())
        return catalog.locate_entry(folder)['id']

    def create_recording(self, identity, entry_id, *, status='completed', finished_at=None, aggregate=0, collection=0):
        with db.connect() as c:
            c.execute("""INSERT INTO live_recordings
                (id,room_url,session_id,author,title,status,started_at,finished_at,entry_id,
                 duration_seconds,chat_messages,chat_status,is_aggregate,is_collection)
                VALUES (?,'','session','测试博主',?,?,'2025-01-01T17:00:00+00:00',?,?,3723.2,17,
                        'captured_with_gaps',?,?)""",
                (identity, '晚间直播 token=privateMock', status, finished_at or db.utc_now(), entry_id, aggregate, collection))

    def test_recording_worker_sends_one_text_receipt_on_publication_and_recovers_manual_stop(self):
        TaskStore().save({**self.task, 'finished_at': '2000-01-01T00:00:00Z'})
        entry = self.index_recording()
        self.create_recording('manual', entry, status='stopped')

        async def wait_for_sent(count):
            async def sent():
                while sum(row['state'] == 'sent' for row in store.public()['recent']) < count:
                    await asyncio.sleep(.02)
            await asyncio.wait_for(sent(), 8)

        async def run():
            await delivery.start()
            try:
                await wait_for_sent(1)
                self.create_recording('broadcast', entry)
                delivery.recording_saved('broadcast')
                delivery.recording_saved('broadcast')
                await wait_for_sent(2)
            finally:
                await delivery.stop()

        with (patch.object(delivery, '_closing', threading.Event()), patch.object(delivery, '_enabled', False),
              patch.object(media, 'workspace', side_effect=AssertionError('recordings have no attachments')),
              patch.object(media, 'task_by_id', side_effect=AssertionError('recordings use archived metadata'))):
            asyncio.run(run())
        store.recover()
        self.assertIsNone(store.claim()[0])
        self.assertEqual(self.client.sendmail.call_count, 2)
        for call, result in zip(self.client.sendmail.call_args_list, ['已手动停止', '已正常下播']):
            message = BytesParser(policy=policy.default).parsebytes(call.args[2])
            self.assertTrue(message['Subject'].startswith('录播完成 · 测试博主 · 晚间直播'))
            self.assertEqual(message['To'], 'target@qq.com')
            self.assertEqual(message.get_content_type(), 'text/plain')
            self.assertFalse(message.is_multipart())
            self.assertNotIn('privateMock', message['Subject'] + message.get_content())
            for value in (result, '2025-01-02 01:00:00', '1:02:03', '17 条（存在采集缺口）'):
                self.assertIn(value, message.get_content())
        self.assertTrue(all(row['recording'] and row['done'] == row['total'] == 1
                            for row in store.public()['recent']))

    def test_recording_eligibility_excludes_history_missing_media_and_synthetic_collections(self):
        entry = self.index_recording()
        self.create_recording('past', entry, finished_at='2001-01-01T00:00:00+00:00')
        self.create_recording('unindexed', None)
        for status in ('failed', 'interrupted', 'finalizing', 'recording'):
            self.create_recording(status, entry, status=status)
        self.create_recording('daily', entry, aggregate=1)
        self.create_recording('collection', entry, collection=1)
        store.capture_recordings()
        self.assertFalse(store.public()['recent'])
        self.create_recording('missing', entry)
        with db.connect() as c:
            c.execute("UPDATE media_entries SET availability='missing' WHERE id=?", (entry,))
        store.capture_recordings('missing')
        self.assertFalse(store.public()['recent'])

    def test_recording_upgrade_uses_its_own_cutoff_and_preserves_existing_mail(self):
        entry = self.index_recording()
        old_mail = Path(config.DATA_DIR)/'legacy'
        old_mail.mkdir()
        with patch.object(config, 'DATA_DIR', str(old_mail)):
            with store.connect() as c:
                c.executescript(store._SCHEMA)
                c.execute("""UPDATE preferences SET enabled=1,sender='sender@qq.com',recipient='target@qq.com',
                             sealed=?,since='2020-01-01T00:00:00.000Z'""", (b'opaque blob',))
                store.add(c, 'existing', 'existing', False)
            with patch.object(store, 'utc_now', return_value='2025-02-01T00:00:00.000Z'):
                store.initialize()
            store.initialize()
            self.create_recording('before', entry, finished_at='2025-01-31T23:59:59.999+00:00')
            self.create_recording('after', entry, finished_at='2025-02-01T00:00:00.000+00:00')
            store.capture_recordings()
            with store.connect() as c:
                self.assertEqual(store.preferences(c)['recording_since'], '2025-02-01T00:00:00.000Z')
                self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertTrue(store.public()['has_code'])
            self.assertEqual({r['id'] for r in store.public()['recent']}, {'existing', 'live-after'})
            self.save('newtarget@qq.com')
            store.capture_recordings()
            self.assertIsNone(store.claim()[0])
            self.assertTrue(all(r['state'] == 'cancelled' for r in store.public()['recent']))

    def test_recording_uncertain_delivery_reuses_message_id_and_needs_no_media_disk(self):
        self.create_recording('retry', self.index_recording())
        store.capture_recordings('retry')
        with patch.object(db, 'connect', side_effect=OSError('removable media offline')):
            self.client.sendmail.side_effect = smtplib.SMTPServerDisconnected()
            result = self.deliver_next()
            self.assertEqual(result['state'], 'uncertain')
            store.initialize()
            self.assertIsNone(store.claim()[0])
            with self.assertRaises(ValueError):
                store.retry(result['id'], False)
            store.retry(result['id'], True)
            self.client.sendmail.side_effect = None
            self.assertEqual(self.deliver_next()['state'], 'sent')
        ids = [BytesParser(policy=policy.default).parsebytes(call.args[2])['Message-ID']
               for call in self.client.sendmail.call_args_list]
        self.assertEqual(ids, [ids[0], ids[0]])

    def test_manual_and_subscription_completion_use_only_the_declared_files_and_recover_once(self):
        store.capture(self.task)
        store.capture({**self.task, 'metadata': {'subscription': {'sub_id': 7}}})
        store.recover()
        result = self.deliver_next()
        self.assertEqual((result['state'], result['done']), ('sent', 1))
        message = BytesParser(policy=policy.default).parsebytes(self.client.sendmail.call_args.args[2])
        self.assertEqual(message['To'], 'target@qq.com')
        self.assertIn('作品正文', message.get_body().get_content())
        self.assertEqual({p.get_filename():p.get_payload(decode=True) for p in message.iter_attachments()},
                         {'01.jpg':b'photo','01.mov':b'live'})
        store.initialize()
        store.recover()
        self.assertIsNone(store.claim()[0])

    def test_download_attachments_exclude_old_tasks_and_recordings_even_with_douyin_script_id(self):
        for name, values in [('old', {'finished_at':'2001-01-01T00:00:00Z'}), ('failed', {'status':'failed'}),
                             ('recording', {'metadata':{'live':{}},'script_id':'douyin'}),
                             ('diagnostic', {'metadata':{'diagnostic':True}})]:
            task = {**self.task, **values, 'id':name}
            TaskStore().save(task)
            store.capture(task)
        store.recover()
        with store.connect() as connection:
            self.assertEqual([r[0] for r in connection.execute('SELECT task_id FROM delivery')], ['work1'])

    def test_sample_is_explicit_and_repeat_clicks_do_not_send_again(self):
        self.save(enabled=False)
        store.capture(self.task)
        key = store.queue_test(self.task['id'])
        self.assertEqual(key, store.queue_test(self.task['id']))
        self.assertEqual(self.deliver_next()['state'], 'sent')
        self.assertEqual(key, store.queue_test(self.task['id']))
        self.assertIsNone(store.claim()[0])

    def test_changing_recipient_cannot_redirect_previously_queued_attachments(self):
        store.capture(self.task)
        self.save('another@qq.com')
        self.assertEqual(store.public()['recent'][0]['state'], 'cancelled')
        self.assertIsNone(store.claim()[0])
        with self.assertRaises(ValueError):
            store.save('new@qq.com', 'target@qq.com', '', True)
        self.assertNotIn('sealed', store.public())

    def test_uncertain_response_is_not_retried_until_confirmed(self):
        self.client.sendmail.side_effect = smtplib.SMTPServerDisconnected()
        store.capture(self.task)
        result = self.deliver_next()
        self.assertEqual(result['state'], 'uncertain')
        with self.assertRaises(ValueError):
            store.retry(result['id'], False)
        store.retry(result['id'], True)
        self.client.sendmail.side_effect = None
        self.assertEqual(self.deliver_next()['state'], 'sent')

    def test_partial_delivery_resumes_at_first_unaccepted_envelope(self):
        # Lower SMTP SIZE so that the attachments need separate envelopes.
        folder = Path(self.task['output_dir'])
        names = ['photo/01.jpg', 'live/01.mov']
        for name in names:
            (folder/name).write_bytes(b'x'*210000)
        archive.ArchiveEntry(folder, archive.read_manifest(folder), False).complete(['context.md', *names])
        self.client.esmtp_features['size'] = '650000'
        self.client.sendmail.side_effect = [{}, smtplib.SMTPDataError(552,b'size')]
        store.capture(self.task)
        result = self.deliver_next()
        self.assertEqual((result['state'], result['done'], result['total']), ('failed',1,2))
        store.retry(result['id'], False)
        self.client.sendmail.side_effect = None
        self.assertEqual(self.deliver_next()['done'], 2)
        envelopes = [BytesParser(policy=policy.default).parsebytes(call.args[2]) for call in self.client.sendmail.call_args_list]
        self.assertNotEqual(envelopes[0]['Message-ID'], envelopes[1]['Message-ID'])
        self.assertEqual(envelopes[1]['Message-ID'], envelopes[2]['Message-ID'])

    def test_unclean_shutdown_and_full_byte_coverage_for_oversize_files(self):
        store.capture(self.task)
        job, _ = store.claim()
        store.update(job['id'], state='sending')
        store.initialize()
        self.assertEqual(store.public()['recent'][0]['state'], 'uncertain')
        path = Path(config.CACHE_DIR)/'large.jpg'
        original = os.urandom(1000000)
        path.write_bytes(original)
        groups, _, volumes = media.batches(media.Work('a','微博','b',(path,)), 500000, path.parent)
        self.assertEqual(volumes, {'large.jpg'})
        self.assertEqual(b''.join(p.read() for group in groups for p in group), original)

    def test_invalid_or_incomplete_marker_is_never_mailed(self):
        folder = Path(self.task['output_dir'])
        (folder/'photo'/'01.jpg').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            media.resolve(self.task)
        marker = archive.read_manifest(folder)
        marker['files'][0] = {'path':'../other.jpg','size':2}
        (folder/'entry.json').write_text(json.dumps(marker), encoding='utf-8')
        with self.assertRaises((ValueError, HTTPException)):
            media.resolve(self.task)

    def test_scratches_are_released_and_long_post_text_is_preserved(self):
        work = media.resolve(self.task)
        work = media.Work(work.author, work.platform, '较长正文' * 1800, work.paths)
        with media.workspace() as scratch:
            groups, _, volumes = media.batches(work, store.LIMIT, scratch)
            msg = delivery.build_message(work, groups[0], volumes, {'id':'long','sample':0},
                                         'sender@qq.com','target@qq.com',1,1)
            attachment = next(p for p in msg.iter_attachments() if p.get_filename()=='作品正文.txt')
            self.assertEqual(attachment.get_payload(decode=True).decode(), work.text)
        self.assertFalse(Path(scratch).exists())
        root = Path(config.CACHE_DIR)/'mail-attachments'
        stale = root/'job-old'
        stale.mkdir()
        (stale/'.mail-work').touch()
        other = root/'job-unrelated'
        other.mkdir()
        media.release_interrupted()
        self.assertFalse(stale.exists())
        self.assertTrue(other.exists())
