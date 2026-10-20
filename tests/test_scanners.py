"""Controlled response parsing and cancellation; no browser or network starts."""
import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from app.scanners.common import Collector, ScanCancelled, collect_browser, run_sync_scan
from app.scanners import douyin, weibo


class ParserTests(unittest.TestCase):
    def page(self, *posts, more=1):
        return {'status_code': 0, 'aweme_list': list(posts), 'has_more': more}

    def post(self, ident, timestamp, pinned=False):
        identity = {'old': '10001', 'new': '10002', 'older': '10003', 'one': '10004', 'two': '10005'}[ident]
        return {'aweme_id': identity, 'create_time': timestamp, 'is_top': pinned, 'desc': 'example'}

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
            {'mblog': {'bid': 'pinned', 'user': {'id': 123}, 'isTop': 1, 'text': '<p>Test</p>', 'created_at': 'Wed Oct 01 08:00:00 +0800 2025'}},
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


class BrowserCollectionTests(unittest.TestCase):
    """Drive browser response events without starting a browser or the network."""

    class Response:
        url = 'https://www.douyin.com/aweme/v1/web/aweme/post/'

        def __init__(self, payload=None, status=200):
            self.payload, self.status = payload, status

        def json(self):
            if not isinstance(self.payload, dict):
                raise ValueError('not JSON')
            return self.payload

    class Browser:
        def __init__(self, page):
            self.page, self.closed = page, False

        def new_page(self):
            return self.page

        def close(self):
            self.closed = True

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Page:
        def __init__(self, clock, *, initial=(), error=None, on_wait=None, on_scroll=None,
                     navigation_status=200):
            self.clock, self.initial, self.error = clock, initial, error
            self.on_wait, self.on_scroll = on_wait, on_scroll
            self.navigation_status = navigation_status
            self.waits = self.scrolls = self.navigations = 0

        def set_default_timeout(self, value):
            pass

        def on(self, name, callback):
            self.callback = callback

        def goto(self, url, **options):
            self.navigations += 1
            for response in self.initial:
                self.callback(response)
            if self.error:
                if 'Timeout' in str(self.error):
                    self.clock[0] += options['timeout'] / 1000
                raise self.error
            return BrowserCollectionTests.Response(status=self.navigation_status)

        def wait_for_timeout(self, milliseconds):
            self.waits += 1
            self.clock[0] += milliseconds / 1000
            if self.on_wait:
                self.on_wait(self)

        def evaluate(self, script):
            self.scrolls += 1
            if self.on_scroll:
                return self.on_scroll(self, script)
            return True

    def setUp(self):
        self.clock = [100.0]
        self.response = self.Response({'status_code': 0, 'aweme_list': [
            {'aweme_id': '10001', 'create_time': 1791000000, 'desc': 'example'}], 'has_more': 0})

    def collect(self, page, options=None, cancel=None):
        browser = self.Browser(page)
        self.addCleanup(lambda: self.assertTrue(browser.closed))
        with patch('app.browser.sync_playwright', return_value=self.Session()), \
                patch('app.scanners.common.time.monotonic', side_effect=lambda: self.clock[0]):
            return collect_browser({'scan_options': options or {}, '_cancel_event': cancel},
                                   douyin.parse_page, lambda _: browser, lambda _: browser,
                                   'https://www.douyin.com/user/example', r'aweme/v1/web/aweme/post')

    def test_dom_timeout_keeps_valid_list_and_does_not_repeat_navigation(self):
        page = self.Page(self.clock, initial=[self.response],
                         error=RuntimeError('Page.goto: Timeout 10000ms exceeded. Call log: secret URL'))
        result = self.collect(page)
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual([item['item_id'] for item in result['items']], ['10001'])
        self.assertEqual(page.navigations, 1)

    def test_dom_timeout_waits_for_late_list_within_total_budget(self):
        def late_response(page):
            if page.waits == 2:
                page.callback(self.response)
        page = self.Page(self.clock, error=RuntimeError('Page.goto: Timeout 10000ms exceeded'),
                         on_wait=late_response)
        result = self.collect(page, {'timeout_seconds': 12})
        self.assertTrue(result['coverage']['complete'])
        self.assertLessEqual(result['coverage']['elapsed_seconds'], 12)
        self.assertEqual(page.navigations, 1)

    def test_timeout_without_list_fails_clearly_and_obeys_total_budget(self):
        page = self.Page(self.clock, error=RuntimeError(
            'Page.goto: Timeout 10000ms exceeded. Call log: https://private.example/author'))
        with self.assertRaisesRegex(RuntimeError, '页面加载超时') as raised:
            self.collect(page, {'timeout_seconds': 11})
        self.assertNotIn('Call log', str(raised.exception))
        self.assertNotIn('private.example', str(raised.exception))
        self.assertLessEqual(self.clock[0], 111)

    def test_slow_first_list_uses_total_budget_instead_of_pagination_stall_limit(self):
        def late_response(page):
            if page.waits == 45:
                page.callback(self.response)
        page = self.Page(self.clock, on_wait=late_response)
        result = self.collect(page, {'timeout_seconds': 15})
        self.assertTrue(result['coverage']['complete'])
        self.assertGreater(result['coverage']['elapsed_seconds'], 9)
        self.assertLessEqual(page.scrolls, 3)

    def test_first_api_list_can_be_triggered_by_scrolling_server_rendered_profile(self):
        def response_after_scroll(page,script):
            self.assertIn('user-post-list',script)
            page.callback(self.response)
        page=self.Page(self.clock,on_scroll=response_after_scroll)
        result=self.collect(page)
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(len(result['items']),1)

    def test_network_change_keeps_available_list_as_incomplete(self):
        page = self.Page(self.clock, initial=[self.response], error=RuntimeError(
            'Page.goto: net::ERR_NETWORK_CHANGED at https://private.example/author'))
        result = self.collect(page)
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['coverage']['reason'], 'network_changed')
        self.assertIn('网络连接发生变化', result['coverage']['detail'])
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(page.waits, 0)

    def test_network_change_without_list_is_never_empty_success(self):
        page = self.Page(self.clock, error=RuntimeError(
            'Page.goto: net::ERR_NETWORK_CHANGED at https://private.example/author'))
        with self.assertRaisesRegex(RuntimeError, '网络连接发生变化') as raised:
            self.collect(page)
        self.assertNotIn('private.example', str(raised.exception))

    def test_scroll_tolerates_missing_document_root_until_list_arrives(self):
        def missing_document(page, script):
            # The previous expression crashes before a root exists. A safe
            # scroll can report no document while response listening continues.
            if 'document.body.scrollHeight' in script:
                raise RuntimeError("Cannot read properties of null (reading 'scrollHeight')")
            return False

        def late_response(page):
            if page.waits == 13:
                page.callback(self.response)
        page = self.Page(self.clock, initial=[self.Response({**self.response.payload, 'has_more': 1})],
                         on_wait=late_response, on_scroll=missing_document)
        result = self.collect(page)
        self.assertEqual(page.scrolls, 1)
        self.assertTrue(result['coverage']['complete'])

    def test_redirect_context_change_waits_for_next_document(self):
        def redirected(page, script):
            raise RuntimeError('Execution context was destroyed, most likely because of a navigation')
        def late_response(page):
            if page.waits == 13:
                page.callback(self.response)
        page = self.Page(self.clock, initial=[self.Response({**self.response.payload, 'has_more': 1})],
                         on_wait=late_response, on_scroll=redirected)
        self.assertTrue(self.collect(page)['coverage']['complete'])
        self.assertEqual(page.navigations, 1)

    def test_scroll_connection_failure_keeps_partial_list(self):
        initial = self.Response({**self.response.payload, 'has_more': 1})
        def disconnected(page, script):
            raise RuntimeError('net::ERR_INTERNET_DISCONNECTED')
        page = self.Page(self.clock, initial=[initial], on_scroll=disconnected)
        result = self.collect(page)
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['coverage']['reason'], 'network_unavailable')
        self.assertEqual(len(result['items']), 1)

    def test_http_limit_is_not_replaced_by_json_error_or_success(self):
        page = self.Page(self.clock, initial=[self.Response(status=429)])
        with self.assertRaisesRegex(RuntimeError, 'HTTP 429'):
            self.collect(page)
        page = self.Page(self.clock, initial=[self.response], navigation_status=403)
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
            self.collect(page)

    def test_timeout_wait_remains_cancellable_and_closes_browser(self):
        cancel = threading.Event()
        def cancelled(page):
            cancel.set()
        page = self.Page(self.clock, error=RuntimeError('Page.goto: Timeout 10000ms exceeded'),
                         on_wait=cancelled)
        with self.assertRaises(ScanCancelled):
            self.collect(page, cancel=cancel)
        self.assertEqual(page.waits, 1)


if __name__ == '__main__':
    unittest.main()
