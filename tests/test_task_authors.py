"""Task card projections use saved identities; fixtures never fetch platform data."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import catalog, config, db, task_authors


class TaskAuthorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'media'
        self.root.mkdir()
        for key, value in (('DB_PATH', str(Path(temporary.name) / 'app.db')),
                           ('LIBRARY_ROOT', str(self.root))):
            replacement = patch.object(config, key, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        db.migrate()
        with db.connect() as connection:
            connection.execute("""INSERT INTO subscriptions(id,platform,blogger_id,nickname,avatar_source)
                VALUES (1,'weibo','123','完整博主名字 🐿️','https://tvax1.sinaimg.cn/example.jpg?auth_key=private')""")
            connection.execute("""INSERT INTO subscription_items(platform,blogger_id,item_id,title,status,discovered_at,updated_at)
                VALUES ('weibo','123','post','内容标题 🎉','discovered','now','now')""")

    def fixture_entry(self):
        folder = self.root / 'ExampleAuthor' / '2026-10-21'
        folder.mkdir(parents=True)
        (folder / 'context.md').write_text(
            '- **原文链接**: https://weibo.com/123/post\n## 页面文本\n```\n已索引的内容 ☀️\n```\n', encoding='utf-8')
        root = catalog.register_root(self.root)
        catalog.scan_root(root['id'])
        entry = catalog.list_entries(root_id=root['id'])['items'][0]
        return folder, root, entry

    def test_subscription_batch_is_one_read_and_keeps_full_names_without_signed_avatar_urls(self):
        name = '完整博主名字🐿️' * 35
        with db.connect() as connection:
            connection.execute('UPDATE subscriptions SET nickname=?', (name,))
        tasks = [dict(id=str(i),metadata={'subscription':dict(sub_id=1,platform='weibo',blogger_id='123',item_id='post')})
                 for i in range(50)]
        statements = []
        connect = db.connect
        @contextmanager
        def traced():
            with connect() as connection:
                connection.set_trace_callback(statements.append)
                yield connection
        with patch.object(db, 'connect', traced):
            task_authors.enrich(tasks)
        self.assertEqual(len(statements), 1)
        for task in tasks:
            self.assertEqual(task['author'], name)
            self.assertEqual(task['author_avatar_url'], '/api/subs/1/avatar')
            self.assertEqual(task['display_title'], '内容标题 🎉')
        self.assertNotIn('auth_key', str(tasks))

    def test_missing_subscription_uses_snapshot_but_never_an_unrelated_reused_id(self):
        task = dict(id='retry',metadata={'subscription':dict(sub_id=1,platform='douyin',blogger_id='different',
                    author='原博主',title='原始标题')})
        result = task_authors.enrich([task])[0]
        self.assertEqual(result['author'], '原博主')
        self.assertIsNone(result['author_avatar_url'])
        with db.connect() as connection:
            connection.execute('DELETE FROM subscriptions WHERE id=1')
        self.assertEqual(task_authors.enrich([task])[0]['display_title'], '原始标题')

    def test_linked_and_legacy_manual_downloads_use_exact_indexed_folder_preserving_case(self):
        folder, root, entry = self.fixture_entry()
        with db.connect() as connection:
            connection.execute('INSERT INTO task_media VALUES (?,?,?)', ('linked',entry['id'],root['id']))
        tasks = [dict(id='linked'), dict(id='absolute',output_dir=str(folder)),
                 dict(id='relative',output_rel=folder.relative_to(self.root).as_posix())]
        task_authors.enrich(tasks)
        self.assertTrue(all(task['author'] == 'ExampleAuthor' for task in tasks))
        self.assertTrue(all('已索引的内容' in task['display_title'] for task in tasks))

    def test_unknown_external_or_diagnostic_paths_are_not_guessed(self):
        folder, _, _ = self.fixture_entry()
        tasks = [dict(id='outside',output_dir=str(self.root.parent / 'OtherAuthor' / folder.name)),
                 dict(id='diagnostic',output_dir=str(folder),metadata={'diagnostic':True}),
                 dict(id='unknown',params={'url':'https://weibo.com/UnknownAuthor'})]
        task_authors.enrich(tasks)
        self.assertTrue(all(task['author'] is None and task['display_title'] is None for task in tasks))

    def test_subscription_full_nickname_takes_priority_over_sanitized_directory_name(self):
        _, root, entry = self.fixture_entry()
        with db.connect() as connection:
            connection.execute('INSERT INTO task_media VALUES (?,?,?)', ('linked',entry['id'],root['id']))
        task = dict(id='linked',metadata={'subscription':dict(sub_id=1,platform='weibo',blogger_id='123')})
        self.assertEqual(task_authors.enrich([task])[0]['author'], '完整博主名字 🐿️')

    def test_historical_live_record_supplies_name_title_and_cached_avatar(self):
        with db.connect() as connection:
            connection.execute("UPDATE subscriptions SET platform='douyin' WHERE id=1")
            connection.execute("""INSERT INTO live_recordings
                (id,sub_id,room_url,session_id,author,title,status,started_at)
                VALUES ('record',1,'https://live.douyin.com/123','session','录播博主','直播标题','stopped','now')""")
        result = task_authors.enrich([dict(id='live-record',metadata={'live':{'id':'record'}})])[0]
        self.assertEqual(result['author'], '录播博主')
        self.assertEqual(result['display_title'], '直播标题')
        self.assertEqual(result['author_avatar_url'], '/api/subs/1/avatar')
