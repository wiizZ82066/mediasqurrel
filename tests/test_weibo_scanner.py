"""Weibo contract fixtures; no platform, saved login or real database access."""
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from app import weibo_auth, weibo_client
from app.scanners import weibo
from app.scanners.common import ScanCancelled


def profile(uid='123'):
    return {'ok': 1, 'data': {'userInfo': {'id': uid, 'screen_name': 'Example &amp; Author',
                                         'avatar_hd': 'http://tvax1.sinaimg.cn/avatar.jpg'},
                            'tabsInfo': {'tabs': [{'tab_type': 'album', 'containerid': '888'},
                                                 {'tab_type': 'weibo', 'containerid': '999123'}]}}}


def page(*ids, next_page=None, more=None, uid='123'):
    data = {'cards': [{'mblog': {'bid': identity, 'user': {'id': uid},
                               'created_at': 'Thu Oct 08 08:00:00 +0800 2026', 'text': '<p>Example</p>'}}
                      for identity in ids], 'cardlistInfo': {}}
    if next_page is not None:
        data['cardlistInfo']['page'] = next_page
    if more is not None:
        data['has_more'] = more
    return {'ok': 1, 'data': data}


class WeiboParsingTests(unittest.TestCase):
    def test_profile_resolves_correct_identity_avatar_and_actual_tab(self):
        value = weibo_client.parse_profile(profile(), '123')
        self.assertEqual(value['nickname'], 'Example & Author')
        self.assertEqual(value['avatar_source'], 'https://tvax1.sinaimg.cn/avatar.jpg')
        self.assertEqual(value['container_id'], '999123')
        with self.assertRaisesRegex(ValueError, '不一致'):
            weibo_client.parse_profile(profile('456'), '123')
        payload = profile()
        payload['data']['userInfo']['avatar_hd'] = 'https://example.invalid/private'
        self.assertEqual(weibo_client.parse_profile(payload, '123')['avatar_source'], '')

    def test_nested_posts_skip_other_authors_retweets_and_unknown_author(self):
        payload = page('own')
        payload['data']['cards'] += page('other', uid='456')['data']['cards']
        payload['data']['cards'] += [{'card_group': [
            {'mblog': {'bid': 'nested', 'user': {'idstr': '123'}, 'title': {'text': '置顶'}}},
            {'mblog': {'bid': 'retweet', 'user': {'id': '123'}, 'retweeted_status': {'id': 9}}},
            {'mblog': {'bid': 'unknown'}}]}]
        items, _more = weibo.parse_page(payload, '123')
        self.assertEqual([item['item_id'] for item in items], ['own', 'nested'])
        self.assertTrue(items[1]['pinned'])
        self.assertEqual(items[0]['created_at'], '2026-10-08T00:00:00+00:00')

    def test_denial_or_changed_structure_is_never_an_empty_success(self):
        for payload in ({'ok': -100}, {'ok': 0, 'data': {'cards': []}}, {'ok': 1, 'data': {}}):
            with self.assertRaises(ValueError):
                weibo.parse_page(payload, '123')
        self.assertEqual(weibo.parse_page(page(more='0'), '123'), ([], False))


class WeiboScanTests(unittest.TestCase):
    def scan(self, responses, *, baseline=False, max_pages=3):
        client = MagicMock()
        client.profile.return_value = weibo_client.parse_profile(profile(), '123')
        client.posts.side_effect = responses
        with patch.object(weibo, 'BrowserSession') as session:
            session.return_value.__enter__.return_value = client
            result = weibo._scan_sync({'blogger_id': '123', 'scan_options': {
                'baseline': baseline, 'max_pages': max_pages, 'max_items': 100, 'timeout_seconds': 30}})
        return result, client

    def test_baseline_is_one_explicit_page_and_includes_independent_profile(self):
        result, client = self.scan([page('first', next_page=2)], baseline=True)
        self.assertEqual([item['item_id'] for item in result['items']], ['first'])
        self.assertEqual(result['coverage']['reason'], 'baseline_window')
        self.assertTrue(result['coverage']['complete'])
        client.posts.assert_called_once_with('999123', 1)
        self.assertEqual(result['profile']['nickname'], 'Example & Author')

    def test_explicit_pagination_deduplicates_and_respects_limit(self):
        result, client = self.scan([page('first', next_page=2), page('first', 'second', next_page=3)], max_pages=2)
        self.assertEqual([call.args for call in client.posts.call_args_list], [('999123', 1), ('999123', 2)])
        self.assertEqual([item['item_id'] for item in result['items']], ['first', 'second'])
        self.assertEqual(result['coverage']['reason'], 'page_limit')
        self.assertFalse(result['coverage']['complete'])

    def test_second_page_login_required_preserves_first_page_with_explicit_incomplete_coverage(self):
        result, _client = self.scan([page('first', next_page=2), {'ok': -100}])
        self.assertNotIn('error', result)
        self.assertEqual(len(result['items']), 1)
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['coverage']['reason'], 'login_required')
        self.assertIn('微博登录', result['coverage']['detail'])

    def test_later_page_timeout_or_network_failure_keeps_discovered_items(self):
        for error, reason in ((TimeoutError('scan budget exceeded'), 'time_limit'),
                              (weibo_client.WeiboAccessError('network unavailable', 'network_error'), 'network_error')):
            with self.subTest(reason=reason):
                result, client = self.scan([page('first', next_page=2), error])
                self.assertNotIn('error', result)
                self.assertEqual([item['item_id'] for item in result['items']], ['first'])
                self.assertFalse(result['coverage']['complete'])
                self.assertEqual(result['coverage']['reason'], reason)
                self.assertEqual(result['coverage']['pages'], 1)
                self.assertEqual(result['profile']['blogger_id'], '123')
                self.assertEqual(client.posts.call_count, 2)

    def test_first_request_timeout_or_network_failure_cannot_form_baseline(self):
        for error in (TimeoutError('scan budget exceeded'),
                      weibo_client.WeiboAccessError('network unavailable', 'network_error')):
            with self.subTest(error=type(error).__name__):
                result, _client = self.scan([error], baseline=True)
                self.assertTrue(result['error'])
                self.assertEqual(result['items'], [])
                self.assertEqual(result['coverage']['pages'], 0)
                self.assertFalse(result['coverage']['complete'])

    def test_cancellation_after_valid_page_does_not_turn_into_partial_success(self):
        with self.assertRaises(ScanCancelled):
            self.scan([page('first', next_page=2), ScanCancelled('cancelled')])

    def test_first_page_denial_preserves_profile_but_has_error(self):
        result, _client = self.scan([{'ok': -100}], baseline=True)
        self.assertEqual(result['items'], [])
        self.assertIn('微博登录', result['error'])
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['profile']['blogger_id'], '123')

    def test_empty_profile_can_still_refresh_avatar_without_posts(self):
        result, _client = self.scan([page(more=0)], baseline=True)
        self.assertEqual(result['items'], [])
        self.assertTrue(result['coverage']['complete'])
        self.assertTrue(result['avatar_source'])

    def test_cancellation_propagates_instead_of_becoming_failed_empty(self):
        with self.assertRaises(ScanCancelled):
            self.scan([ScanCancelled('cancelled')])


class WeiboBrowserAndAuthTests(unittest.TestCase):
    def test_request_rejects_http_limits_and_non_json_without_echoing_body(self):
        client = weibo_client.BrowserSession()
        client.page = MagicMock()
        for status in (403, 432, 429):
            client.page.evaluate.return_value = {'status': status}
            with self.assertRaises(weibo_client.WeiboAccessError):
                client.request('/api/container/getIndex')
        client.page.evaluate.return_value = {'status': 200, 'invalid': True}
        with self.assertRaisesRegex(ValueError, 'JSON'):
            client.request('/api/container/getIndex')
        with self.assertRaises(ValueError):
            client.request('//unrelated.invalid')

    def test_transport_failures_are_classified_without_exposing_browser_details(self):
        client = weibo_client.BrowserSession()
        client.page = MagicMock()
        client.page.evaluate.side_effect = RuntimeError('signed_url=fixture-secret')
        with self.assertRaises(weibo_client.WeiboAccessError) as raised:
            client.request('/api/container/getIndex')
        self.assertEqual(raised.exception.reason, 'network_error')
        self.assertNotIn('fixture-secret', str(raised.exception))
        client.page.evaluate.side_effect = None
        for failure in ('timeout', 'network'):
            with self.subTest(failure=failure):
                client.page.evaluate.return_value = {'transport_error': failure}
                with self.assertRaises(weibo_client.WeiboAccessError) as raised:
                    client.request('/api/container/getIndex')
                self.assertEqual(raised.exception.reason, 'network_error')

    def test_transport_exception_preserves_cancellation_and_budget_expiration(self):
        client = weibo_client.BrowserSession()
        client.page = MagicMock()
        def cancelled():
            client.cancel.set()
        def expired():
            client.deadline = 0
        for change, expected in ((cancelled, ScanCancelled), (expired, TimeoutError)):
            with self.subTest(expected=expected.__name__):
                client.cancel.clear()
                def fail(*_args):
                    change()
                    raise RuntimeError('transport interrupted')
                client.page.evaluate.side_effect = fail
                with self.assertRaises(expected):
                    client.request('/api/container/getIndex')

    def test_browser_creation_failure_still_closes_manager_and_browser(self):
        manager, playwright, browser = MagicMock(), MagicMock(), MagicMock()
        manager.__enter__.return_value = playwright
        playwright.chromium.launch.return_value = browser
        browser.new_context.side_effect = RuntimeError('context failure')
        with patch.object(weibo_client, 'sync_playwright', return_value=manager):
            with self.assertRaisesRegex(RuntimeError, 'context failure'):
                with weibo_client.BrowserSession():
                    pass
        browser.close.assert_called_once()
        manager.__exit__.assert_called_once()

    def test_cancelled_browser_operation_does_not_launch(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(weibo_client, 'sync_playwright') as launch:
            with self.assertRaises(ScanCancelled):
                with weibo_client.BrowserSession(cancel=cancel):
                    pass
        launch.assert_not_called()

    def test_visitor_cookie_is_not_proof_of_user_login(self):
        # Sanitized field shape observed from the real mobile /api/config;
        # desktop /ajax/config is HTML and get_config only contains ab_test.
        visitor = {'ok': 1, 'preferQuickapp': False, 'data': {'login': False, 'st': 'fixture',
            'user_token': 'fixture', 'loginUrl': 'https://passport.weibo.cn/signin/login',
            'wx_callback': '', 'wx_authorize': '', 'passport_login_url': 'https://passport.weibo.cn/signin/login'}}
        self.assertFalse(weibo_auth._confirmed_login(visitor))
        self.assertFalse(weibo_auth._confirmed_login({'ok': 1, 'data': {'ab_test': {}}}))
        self.assertFalse(weibo_auth._confirmed_login({'ok': 0, 'data': {'login': True}}))
        self.assertTrue(weibo_auth._confirmed_login({'ok': 1, 'data': {'login': True}}))
        self.assertEqual(weibo_auth._login_url(visitor), 'https://passport.weibo.cn/signin/login')
        self.assertIsNone(weibo_auth._login_url({'data': {'loginUrl': 'https://weibo.cn.attacker.invalid/'}}))

    def test_invalid_cookie_cannot_claim_saved_or_echo_secret(self):
        self.assertFalse(weibo_auth._allowed_cookie({'name': 'SUB', 'value': '', 'domain': '.weibo.com'}))
        self.assertFalse(weibo_auth._allowed_cookie({'name': '', 'value': 'fixture', 'domain': '.weibo.com'}))
        context = MagicMock()
        context.add_cookies.side_effect = ValueError('fixture-secret-content')
        with patch.object(weibo_auth, 'load_cookies', return_value=[{'name': 'SUB', 'value': 'fixture'}]):
            with self.assertRaisesRegex(ValueError, '重新登录') as raised:
                weibo_auth.attach_cookies(context)
        self.assertNotIn('fixture-secret', str(raised.exception))

    def test_explicit_login_saves_only_verified_platform_cookies(self):
        client = MagicMock()
        client.page.url = 'https://m.weibo.cn/'
        client.request.return_value = {'ok': 1, 'data': {'login': True}}
        client.context.cookies.return_value = [
            {'name': 'SUB', 'value': 'fixture-only', 'domain': '.weibo.com', 'path': '/', 'expires': -1},
            {'name': 'other', 'value': 'unrelated', 'domain': '.example.invalid', 'path': '/', 'expires': -1}]
        with patch.object(weibo_client, 'BrowserSession') as session, \
                patch.object(weibo_auth, 'atomic_write_text') as write, \
                patch.object(weibo_auth, 'auth_status', return_value={'saved': True}):
            session.return_value.__enter__.return_value = client
            self.assertEqual(weibo_auth._login_sync(), {'ok': True, 'saved': True})
        saved = json.loads(write.call_args.args[1])
        self.assertTrue(saved['verified'])
        self.assertEqual([cookie['name'] for cookie in saved['cookies']], ['SUB'])
        self.assertFalse(session.call_args.kwargs['use_saved_cookies'])
        client.request.assert_called_once_with('/api/config', '登录状态')
        session.return_value.__exit__.assert_called_once()

    def test_fresh_login_can_recover_from_invalid_saved_credentials(self):
        manager = MagicMock()
        with patch.object(weibo_client, 'sync_playwright', return_value=manager), \
                patch.object(weibo_auth, 'attach_cookies', side_effect=ValueError('invalid saved data')) as attach:
            with weibo_client.BrowserSession(use_saved_cookies=False):
                pass
        attach.assert_not_called()
        manager.__exit__.assert_called_once()

    def test_login_lock_releases_after_cancellation(self):
        with patch.object(weibo_auth, '_login_browser', side_effect=ScanCancelled('cancelled')):
            with self.assertRaises(ScanCancelled):
                weibo_auth._login_sync()
        with patch.object(weibo_auth, '_login_browser', return_value={'saved': True}):
            self.assertEqual(weibo_auth._login_sync(), {'saved': True})


if __name__ == '__main__':
    unittest.main()
