"""Viewer avatars use exact durable identities without reading the library or network."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import catalog, config, db, library_details


class LibraryDetailsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='ms-detail-')))
        self.stack.enter_context(patch.object(config, 'DB_PATH', str(self.base / 'app.db')))
        db.migrate()
        with db.connect() as c:
            c.execute('''INSERT INTO subscriptions(id,platform,blogger_id,nickname,avatar_source)
                VALUES(1,'douyin','MS4wLjAB12345678','Same name','https://p3.douyinpic.com/avatar.jpg?token=private')''')
            c.execute('''INSERT INTO subscriptions(id,platform,blogger_id,nickname,avatar_source)
                VALUES(2,'weibo','123456','Same name','https://tvax1.sinaimg.cn/avatar.jpg')''')

    def recording(self):
        with db.connect() as c:
            c.execute('''INSERT INTO live_recordings(id,sub_id,room_url,session_id,author,title,status,started_at,entry_id)
                VALUES('record',1,'https://live.douyin.com/123','session','Same name','Title','stopped','now','entry')''')

    def entry(self, **extra):
        return dict(id='entry', platform='douyin', author='Same name', meta={}, gallery=[], **extra)

    def test_recording_without_metadata_uses_subscription_avatar_and_original_segment_ids(self):
        self.recording(); entry = self.entry()
        entry['gallery'] = [dict(type='video', rel='recording.mp4', source_entry_id='original'),
                            dict(type='image', rel='cover.jpg')]
        result = library_details.enrich(entry)
        self.assertEqual(result['author_avatar_url'], '/api/subs/1/avatar')
        self.assertEqual(result['author_id'], 'MS4wLjAB12345678')
        self.assertEqual(result['gallery'][0]['playback_info_url'],
                         '/api/library/entries/original/playback?rel=recording.mp4')
        self.assertNotIn('playback_info_url', result['gallery'][1])
        self.assertNotIn('token', str(result))

    def test_metadata_identity_takes_priority_and_names_are_never_matched(self):
        self.assertIsNone(library_details.enrich(self.entry())['author_avatar_url'])
        self.recording(); entry = self.entry(); entry['meta']['作者sec_uid'] = 'MS4wLjAB87654321'
        self.assertEqual(library_details.enrich(entry)['author_avatar_url'],
                         '/api/authors/douyin/MS4wLjAB87654321/avatar')
        entry['platform'] = 'weibo'; entry['meta'] = {'作者UID':'123456'}
        self.assertEqual(library_details.enrich(entry)['author_avatar_url'], '/api/subs/2/avatar')

    def test_removed_subscription_does_not_guess_same_named_author(self):
        self.recording()
        with db.connect() as c: c.execute('DELETE FROM subscriptions WHERE id=1')
        self.assertIsNone(library_details.enrich(self.entry())['author_avatar_url'])

    def test_task_link_recovers_identity_and_malformed_historical_metadata_is_skipped(self):
        root = self.base / 'library'; folder = root / 'Example' / '2026-10-20'
        folder.mkdir(parents=True)
        (folder / 'context.md').write_text('- **作者**: Example\n', encoding='utf-8')
        root_id = catalog.register_root(root)['id']; catalog.scan_root(root_id)
        entry = self.entry(); entry['id'] = catalog.list_entries()['items'][0]['id']
        with db.connect() as c:
            for identity, meta in [('old', {'content':{'blogger_id':'MS4wLjAB12345678'}}), ('new','invalid')]:
                c.execute('''INSERT INTO tasks(id,script_id,script_name,params_json,status,progress_json,created_at,metadata_json)
                    VALUES (?,'douyin','test','{}','completed','{}',?,?)''',
                    (identity, '1' if identity == 'old' else '2', json.dumps(meta)))
                c.execute('INSERT INTO task_media(task_id,entry_id,root_id) VALUES (?,?,?)', (identity,entry['id'],root_id))
        self.assertEqual(library_details.enrich(entry)['author_avatar_url'], '/api/subs/1/avatar')
