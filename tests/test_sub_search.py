"""Indexed author and browser lifecycle tests; no real platform requests."""
import asyncio
from contextlib import nullcontext
import json
import sqlite3
import threading
import unittest
from unittest.mock import MagicMock, patch

from app import sub_search as search


class SearchParsingTests(unittest.TestCase):
    def test_explicit_identity_parser_does_not_launch_browser(self):
        cases = [('weibo', 'UID: 123456', '123456'),
                 ('weibo', 'https://weibo.com/u/123456?from=profile', '123456'),
                 ('douyin', 'https://www.douyin.com/user/MS4wLjABAAAAfixture123?token=private', 'MS4wLjABAAAAfixture123')]
        with patch.object(search, 'sync_playwright', side_effect=AssertionError('browser not needed')):
            for platform, value, identity in cases:
                result = search.parse_identity(platform, value)
                self.assertEqual(result['blogger_id'], identity)
                self.assertTrue(result['direct_identity'])
                self.assertFalse(result['verified'])
                self.assertNotIn('token', result['homepage'])
        self.assertIsNone(search.parse_identity('weibo', '普通昵称'))
        self.assertIsNone(search.parse_identity('douyin', '123456'))
        for value in ('https://weibo.com.attacker.invalid/u/123', 'https://www.douyin.com/video/12345678',
                      'https://user:pass@weibo.com/u/123', 'https://weibo.com/u/123/extra'):
            with self.assertRaises(ValueError):
                search.parse_identity('weibo', value)

    def test_direct_weibo_online_search_resolves_profile(self):
        profile = {'nickname': 'Platform Name', 'avatar_source': 'https://tvax1.sinaimg.cn/avatar.jpg',
                   'homepage': 'https://weibo.com/u/123456', 'followers': 42, 'verified': False, 'desc': 'Example'}
        with patch.object(search, 'fetch_profile', return_value=profile) as fetch:
            result = search.search_online('weibo', 'UID: 123456')[0]
        self.assertEqual(fetch.call_args.args, ('123456',))
        self.assertEqual(result['nickname'], 'Platform Name')
        self.assertEqual(result['avatar'], profile['avatar_source'])
        self.assertTrue(result['profile_verified'])

    def test_local_authors_use_only_index_with_200_author_cap(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE media_entries(author,platform,meta_json,availability)')
        rows = [(f'Example {number:03}', 'weibo', json.dumps({'原文链接': f'https://weibo.com/{1000 + number}/post'}), 'present')
                for number in range(205)]
        rows += [('Top', 'douyin', json.dumps({'作者sec_uid': 'MS4fixture'}), 'present')] * 3
        rows += [('Missing', 'weibo', '{}', 'missing')] * 10
        connection.executemany('INSERT INTO media_entries VALUES(?,?,?,?)', rows)
        with patch.object(search.db, 'connect', return_value=nullcontext(connection)), \
                patch.object(search.os, 'listdir', side_effect=AssertionError('no filesystem scan')):
            values = search.local_authors()
        self.assertEqual(len(values), 200)
        self.assertEqual(values[0], {'name': 'Top', 'platforms': {'douyin': 'MS4fixture'}, 'entries': 3})
        self.assertEqual(values[1]['platforms'], {'weibo': '1000'})
        self.assertNotIn('Missing', [item['name'] for item in values])

    def test_failed_search_response_is_not_a_successful_empty_result(self):
        with self.assertRaisesRegex(RuntimeError, '未返回有效'):
            search._parse_weibo_users({'ok': 0})
        self.assertEqual(search._parse_weibo_users({'ok': 1, 'data': {'cards': []}}), [])
        values = search._parse_weibo_users({'ok': 1, 'data': {'cards': [{'user': {
            'id': 123, 'screen_name': 'Example', 'followers_count': '1.2万'}}]}})
        self.assertEqual(values[0]['followers'], 12000)
        self.assertEqual(values[0]['blogger_id'], '123')

    def test_douyin_navigation_failure_closes_created_context(self):
        playwright, context, page = MagicMock(), MagicMock(), MagicMock()
        playwright.chromium.launch_persistent_context.return_value = context
        context.pages = [page]
        page.goto.side_effect = RuntimeError('navigation failed')
        with patch('app.browser.get_ua', return_value='fixture'):
            with self.assertRaisesRegex(RuntimeError, 'navigation failed'):
                search._dy_open(playwright, 'Example', headless=True)
        context.close.assert_called_once()
        self.assertLessEqual(page.goto.call_args.kwargs['timeout'], 10000)


class SearchLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def until(self, condition):
        for _ in range(200):
            if condition():
                return
            await asyncio.sleep(.01)
        self.fail('worker did not reach expected state')

    async def asyncTearDown(self):
        await search.stop_searches()

    async def test_running_cancel_awaits_thread_cleanup(self):
        entered, cleaned = threading.Event(), threading.Event()
        def worker(_keyword):
            entered.set()
            try:
                while True:
                    search._check_search()
                    threading.Event().wait(.01)
            finally:
                cleaned.set()
        with patch.object(search, '_weibo_search_sync', side_effect=worker):
            task = asyncio.create_task(search.search_online_async('weibo', '昵称'))
            await self.until(entered.is_set)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cleaned.is_set())

    async def test_shutdown_closes_login_worker_without_waiting_for_login_timeout(self):
        entered, cleaned = threading.Event(), threading.Event()
        def login(_timeout):
            entered.set()
            try:
                while True:
                    search._check_search()
                    threading.Event().wait(.01)
            finally:
                cleaned.set()
        with patch.object(search, '_douyin_login', side_effect=login):
            task = asyncio.create_task(search.douyin_login_async())
            await self.until(entered.is_set)
            await search.stop_searches()
        self.assertTrue(task.done())
        self.assertTrue(cleaned.is_set())

    async def test_two_browser_limit_and_waiting_cancel_never_starts_third(self):
        release, lock = threading.Event(), threading.Lock()
        started, active, peak = [], 0, 0
        def worker(keyword):
            nonlocal active, peak
            with lock:
                started.append(keyword)
                active += 1
                peak = max(peak, active)
            try:
                while not release.wait(.01):
                    search._check_search()
                return []
            finally:
                with lock:
                    active -= 1
        with patch.object(search, '_weibo_search_sync', side_effect=worker):
            tasks = [asyncio.create_task(search.search_online_async('weibo', '昵称' + str(i))) for i in range(3)]
            try:
                await self.until(lambda: len(started) == 2)
                tasks[2].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[2]
                self.assertEqual(len(started), 2)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)

    async def test_shutdown_cancels_waiting_and_active_searches(self):
        entered, cleaned = [], []
        def worker(keyword):
            entered.append(keyword)
            try:
                while True:
                    search._check_search()
                    threading.Event().wait(.01)
            finally:
                cleaned.append(keyword)
        with patch.object(search, '_weibo_search_sync', side_effect=worker):
            tasks = [asyncio.create_task(search.search_online_async('weibo', f'昵称 {i}')) for i in range(3)]
            await self.until(lambda: len(entered) == 2)
            await search.stop_searches()
            self.assertEqual(len(cleaned), 2)
            self.assertTrue(all(task.done() for task in tasks))
            self.assertEqual(len(entered), 2)


if __name__ == '__main__':
    unittest.main()
