"""Controlled response parsing and cancellation; no browser or network starts."""
import asyncio
import threading
import time
import unittest

from app.scanners.common import Collector, ScanCancelled, run_sync_scan
from app.scanners import douyin, weibo


class ParserTests(unittest.TestCase):
    def page(self, *posts, more=1):
        return {'status_code': 0, 'aweme_list': list(posts), 'has_more': more}

    def post(self, ident, timestamp, pinned=False):
        return {'aweme_id': ident, 'create_time': timestamp, 'is_top': pinned, 'desc': 'example'}

    def test_pinned_does_not_end_overlap_and_multiple_pages_deduplicate(self):
        collector = Collector({'scan_options': {'cutoff_at': '2026-10-01T00:00:00+00:00'}}, douyin.parse_page)
        collector.accept(self.page(self.post('old', 1, True), self.post('new', 1791000000)))
        self.assertFalse(collector.complete)
        collector.accept(self.page(self.post('new', 1791000000), self.post('older', 1780000000)))
        result = collector.result()
        self.assertEqual(len(result['items']), 3)
        self.assertEqual(result['coverage']['reason'], 'overlap_covered')
        self.assertTrue(result['coverage']['complete'])

    def test_limits_are_explicit_and_first_baseline_is_only_current_page(self):
        collector = Collector({'scan_options': {'max_pages': 1}}, douyin.parse_page)
        collector.accept(self.page(self.post('one', 1791000000)))
        self.assertEqual(collector.result()['coverage']['reason'], 'page_limit')
        self.assertFalse(collector.result()['coverage']['complete'])
        baseline = Collector({'scan_options': {'baseline': True}}, douyin.parse_page)
        baseline.accept(self.page(self.post('one', 1791000000)))
        baseline.accept(self.page(self.post('two', 1791000001)))
        self.assertEqual(len(baseline.result()['items']), 1)
        self.assertEqual(baseline.result()['coverage']['scope'], 'baseline_current_page')

    def test_invalid_or_challenge_response_is_never_successful_empty(self):
        for payload in ({}, {'status_code': 0}, {'status_code': 8, 'aweme_list': []}):
            with self.assertRaises(ValueError):
                douyin.parse_page(payload)
        for payload in ({}, {'ok': 0}, {'ok': 1, 'data': {}}):
            with self.assertRaises(ValueError):
                weibo.parse_page(payload, '123')
        valid = Collector({}, douyin.parse_page)
        valid.accept(self.page(more=0))
        self.assertTrue(valid.result()['coverage']['complete'])
        rejected = Collector({}, douyin.parse_page)
        rejected.accept(self.page(), status=429)
        with self.assertRaisesRegex(RuntimeError, '429'):
            rejected.result()

    def test_weibo_nested_cards_retweets_and_timestamp(self):
        payload = {'ok': 1, 'data': {'cards': [{'card_group': [
            {'mblog': {'bid': 'pinned', 'isTop': 1, 'text': '<p>Test</p>', 'created_at': 'Wed Oct 01 08:00:00 +0800 2025'}},
            {'mblog': {'bid': 'repost', 'retweeted_status': {'id': 'source'}}}]}]}}
        items, _more = weibo.parse_page(payload, '123')
        self.assertEqual([item['item_id'] for item in items], ['pinned'])
        self.assertTrue(items[0]['pinned'])
        self.assertEqual(items[0]['created_at'], '2025-10-01T00:00:00+00:00')


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_waits_for_thread_cleanup(self):
        started, closed = threading.Event(), threading.Event()
        def worker(sub):
            started.set()
            try:
                while not sub['_cancel_event'].wait(0.01):
                    pass
                raise ScanCancelled('cancelled')
            finally:
                closed.set()
        task = asyncio.create_task(run_sync_scan(worker, {}))
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())


if __name__ == '__main__':
    unittest.main()
