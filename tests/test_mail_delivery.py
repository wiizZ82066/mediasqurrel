import os
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app import archive, config, db, mail_delivery as delivery, mail_media as media, mail_store as store
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

    def test_no_backfill_or_recording_even_if_it_uses_a_douyin_script_id(self):
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
