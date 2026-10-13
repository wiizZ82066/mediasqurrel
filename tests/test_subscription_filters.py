"""Topic filtering contracts; isolated databases and mocked platform details only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app import config, db, subscription_filters as filters, subscription_store as store, watcher
from app.scanners import douyin, weibo
from app.scanners.common import Collector


class TagMatchingTests(unittest.TestCase):
    def test_exact_topics_paired_open_explicit_and_unicode(self):
        text = '正文 #旅行# #美食 #散步，继续 #Coffee #长 话题# ＃全角＃'
        tags = filters.extract_tags(text, ['平台话题'])
        self.assertEqual(set(tags), {'旅行', '美食', '散步', 'coffee', '长 话题', '全角', '平台话题'})
        self.assertNotIn('长', tags)
        self.assertEqual(filters.evaluate({'tags': ['旅行']}, {'tags': ['旅行日记'], 'complete': True})['result'], 'filtered')
        self.assertEqual(filters.evaluate({'tags': ['旅行', '美食']}, {'tags': ['美食'], 'complete': True})['matched'], ['美食'])

    def test_validation_canonicalizes_and_rejects_invalid_input(self):
        self.assertEqual(filters.normalize_rule({'tags': [' #Coffee# ', 'coffee', '＃旅行＃']}), {'tags': ['coffee', '旅行']})
        for value in ({'tags': '旅行'}, {'tags': ['']}, {'tags': ['a\nb']}, {'tags': ['a#b']},
                      {'tags': ['a' * 81]}, {'tags': ['a'] * 21}, {'tags': [1]}, {'mode': 'all'}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                filters.normalize_rule(value)

    def test_incomplete_text_is_pending_but_positive_match_is_sufficient(self):
        self.assertEqual(filters.evaluate({'tags': ['旅行']}, {'complete': False})['result'], 'pending_filter')
        self.assertEqual(filters.evaluate({'tags': ['旅行']}, {'complete': False, 'tags': ['旅行']})['result'], 'matched')
        self.assertEqual(filters.evaluate({'tags': []}, {'complete': False})['result'], 'matched')

    def test_scanners_keep_full_matching_text_and_platform_topics(self):
        text = '长文' * 50 + ' #尾部标签#'
        wb, _ = weibo.parse_page({'ok': 1, 'data': {'cards': [{'mblog': {'bid': 'new', 'user': {'id': '123'},
                                  'text': '<p>' + text + '</p>', 'topic_struct': [{'topic_title': '平台话题'}]}}]}}, '123')
        dy, _ = douyin.parse_page({'status_code': 0, 'aweme_list': [{'aweme_id': '456', 'desc': text,
                                   'cha_list': [{'cha_name': '平台话题'}], 'text_extra': [{'hashtag_name': '额外话题'}]}]})
        for item in (wb[0], dy[0]):
            self.assertEqual(len(item['title']), 60)
            metadata = filters.content_metadata(item)
            self.assertNotIn('text', metadata)  # Persist tags, not complete private captions.
            self.assertTrue({'尾部标签', '平台话题'} <= set(metadata['tags']))
        self.assertIn('额外话题', filters.content_metadata(dy[0])['tags'])

    def test_weibo_long_text_completion_uses_bounded_detail_requests(self):
        sub = {'platform': 'weibo', 'blogger_id': '123', 'download_filter': {'tags': ['尾部标签']}}
        result = {'items': [{'item_id': str(number), 'content_text': '片段', 'content_complete': False} for number in range(15)]}
        client = MagicMock()
        client.request.return_value = {'ok': 1, 'data': {'longTextContent': '<p>全文 #尾部标签#</p>'}}
        weibo.complete_filter_text(client, result, sub, Collector(sub, None))
        self.assertEqual(client.request.call_count, 10)
        self.assertTrue(result['items'][0]['content_complete'])
        self.assertFalse(result['items'][-1]['content_complete'])
        self.assertEqual(client.request.call_args_list[0].args[0], '/statuses/extend?id=0')

    def test_long_text_network_failure_leaves_pending_without_burst_or_baseline_requests(self):
        sub = {'platform': 'weibo', 'blogger_id': '123', 'download_filter': {'tags': ['尾部标签']}}
        result = {'items': [{'item_id': 'one', 'content_text': '片段', 'content_complete': False}]}
        client = MagicMock()
        client.request.side_effect = weibo.WeiboAccessError('network failure')
        weibo.complete_filter_text(client, result, sub, Collector(sub, None))
        self.assertEqual(client.request.call_count, 1)
        self.assertFalse(result['items'][0]['content_complete'])
        client.reset_mock()
        weibo.complete_filter_text(client, result, sub, Collector({**sub, 'scan_options': {'baseline': True}}, None))
        client.request.assert_not_called()


class SubscriptionFilterStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'app.db'
        patcher = patch.object(config, 'DB_PATH', str(self.database))
        patcher.start(); self.addCleanup(patcher.stop)
        db.migrate()
        self.sub = watcher.add_sub('weibo', '123', download_filter={'tags': ['旅行']})

    def item(self, identity, text='', **extra):
        return {'item_id': identity, 'title': text[:60], 'content_text': text,
                'params': {'url': 'https://weibo.com/123/' + identity}, 'script_id': 'weibo', **extra}

    def state(self, identity):
        with db.connect() as connection:
            return dict(connection.execute('SELECT * FROM subscription_items WHERE item_id=?', (identity,)).fetchone())

    def baseline(self):
        first, items = store.record_discoveries(self.sub, [self.item('old', '#旅行#')])
        self.assertTrue(first); self.assertEqual(items, [])

    def test_baseline_any_tag_and_persisted_decision_with_bounded_public_projection(self):
        self.baseline()
        counts = {}
        _, candidates = store.record_discoveries(self.sub, [self.item('yes', '正文' * 100 + '#旅行#'), self.item('no', '#风景#')], filter_counts=counts)
        self.assertEqual([item['item_id'] for item in candidates], ['yes'])
        self.assertNotIn('content_text', candidates[0])
        self.assertEqual(counts, {'filtered': 1, 'pending_filter': 0})
        self.assertEqual(self.state('no')['status'], 'filtered')
        self.assertEqual(json.loads(self.state('no')['filter_decision_json'])['tags'], ['旅行'])
        self.assertEqual(self.state('old')['status'], 'baseline')
        records = store.list_items(self.sub['id'], limit=1)
        self.assertEqual(len(records), 1)
        self.assertFalse({'content_json', 'params_json', 'content_text'} & records[0].keys())
        self.assertEqual(watcher.list_subs()[0]['download_filter'], {'tags': ['旅行']})

    def test_rule_change_never_backfills_filtered_and_claim_rechecks_latest_rule(self):
        self.baseline()
        store.record_discoveries(self.sub, [self.item('yes', '#旅行#'), self.item('no', '#美食#')])
        watcher.update_sub(self.sub['id'], download_filter={'tags': ['美食']})
        self.assertIsNone(store.claim_item('weibo', '123', 'yes'))
        self.assertEqual(self.state('yes')['status'], 'filtered')
        _, candidates = store.record_discoveries(self.sub, [self.item('no', '#美食#'), self.item('new', '#美食#')])
        self.assertEqual([item['item_id'] for item in candidates], ['new'])
        self.assertEqual(self.state('no')['status'], 'filtered')
        self.assertEqual(json.loads(self.state('no')['filter_decision_json'])['tags'], ['旅行'])

    def test_failed_automatic_retry_respects_new_rule_without_losing_failure_history(self):
        self.baseline()
        store.record_discoveries(self.sub, [self.item('yes', '#旅行#')])
        token = store.claim_item('weibo', '123', 'yes')
        store.mark_enqueued('weibo', '123', 'yes', 'fixture-task', token)
        store.record_task_result('fixture-task', 'failed', 'network unavailable')
        _, candidates = store.record_discoveries(self.sub, [])
        self.assertEqual([item['item_id'] for item in candidates], ['yes'])
        watcher.update_sub(self.sub['id'], download_filter={'tags': ['美食']})
        _, candidates = store.record_discoveries(self.sub, [])
        self.assertEqual(candidates, [])
        state = self.state('yes')
        self.assertEqual(state['status'], 'filtered')
        self.assertEqual(state['task_id'], 'fixture-task')
        self.assertIn('network', state['last_error'])

    def test_incomplete_pending_retries_detail_after_leaving_list_window(self):
        self.baseline()
        store.record_discoveries(self.sub, [self.item('long', '片段', content_complete=False, _detail_id='1234567890')])
        self.assertEqual(self.state('long')['status'], 'pending_filter')
        result, client = {'items': []}, MagicMock()
        client.request.return_value = {'ok': 1, 'data': {'longTextContent': '全文 #旅行#'}}
        weibo.complete_filter_text(client, result, self.sub, Collector(self.sub, None))
        client.request.assert_called_once_with('/statuses/extend?id=1234567890', '长微博正文')
        _, candidates = store.record_discoveries(self.sub, result['items'])
        self.assertEqual([item['item_id'] for item in candidates], ['long'])
        self.assertNotIn('_detail_id', candidates[0])
        self.assertEqual(self.state('long')['status'], 'discovered')
        self.assertIsNotNone(store.claim_item('weibo', '123', 'long'))

    def test_deleted_subscription_cannot_claim_and_raw_json_cannot_bypass_validation(self):
        self.baseline()
        store.record_discoveries(self.sub, [self.item('new', '#旅行#')])
        with self.assertRaises(ValueError):
            watcher.update_sub(self.sub['id'], download_filter_json='{}')
        watcher.remove_sub(self.sub['id'])
        self.assertIsNone(store.claim_item('weibo', '123', 'new'))

    def test_v6_upgrade_preserves_existing_records_and_creates_recoverable_backup(self):
        legacy = Path(self.temp.name) / 'legacy.db'
        db.migrate(legacy, migrations=db._migrations()[:-1])
        with db.connect(legacy) as connection:
            connection.execute("INSERT INTO subscriptions(platform,blogger_id) VALUES ('weibo','legacy')")
            connection.execute("INSERT INTO subscription_items(platform,blogger_id,item_id,status,discovered_at,updated_at) VALUES ('weibo','legacy','post','failed','before','before')")
        self.assertEqual(db.migrate(legacy), 7)
        with db.connect(legacy) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(connection.execute('SELECT status FROM subscription_items').fetchone()[0], 'failed')
            self.assertEqual(json.loads(connection.execute('SELECT download_filter_json FROM subscriptions').fetchone()[0]), {'tags': []})
        self.assertTrue(list((legacy.parent / 'backups').glob('schema-v6-*.db')))


if __name__ == '__main__':
    unittest.main()
