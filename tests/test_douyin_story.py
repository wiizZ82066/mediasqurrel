"""Synthetic daily-video responses; never read live credentials or platforms."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from playwright.sync_api import Error as BrowserError

import douyin_downloader as downloader
from app import archive, douyin_story as story
from app.platform_errors import PlatformAccessError


def post(identity='123', author='456'):
    return {'aweme_id': identity, 'aweme_type': 0, 'is_25_story': 1,
            'author': {'uid': author, 'nickname': 'Fixture', 'sec_uid': 'fixture'},
            'desc': '', 'create_time': 1700000000, 'authentication_token': 'excluded',
            'video': {'play_addr_h264': {'url_list': ['https://media.invalid/video']},
                      'origin_cover': {'url_list': ['https://media.invalid/cover']}}}


def payload(items=None, **values):
    return {'status_code': 0, 'active_data': {'story_group_v2_items': [
        {'story_group_v2': {'aweme_list': items if items is not None else [post()]}}]},
            'has_more': False, **values}


class DouyinStoryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(story.config, 'DATA_DIR', str(self.root)))
        self.stack.enter_context(patch.object(story, 'load_cookies', return_value=[
            {'name': 'sessionid', 'value': 'fixture-session', 'domain': '.douyin.com', 'expires': -1}]))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_share_redirect_keeps_author_but_discards_other_share_parameters(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 302
        response.headers = {'Location': 'https://www.iesdouyin.com/share/video/123/?' + urlencode({
            'activity_info': json.dumps({'social_author_id': '456', 'social_share_user_id': '789'}),
            'share_sign': 'fixture', 'device_id': '789'})}
        with patch.object(downloader.requests, 'get', return_value=response), patch.object(downloader, '_ua', return_value='fixture'):
            self.assertEqual(downloader.resolve_douyin_link('分享 https://v.douyin.com/fixture/'),
                             ('https://www.douyin.com/video/123', '456'))
        for value in ('[]', '{broken', json.dumps({'social_author_id': '../456'}), 'x' * 16385):
            self.assertEqual(downloader._share_author('https://www.douyin.com/video/123?' + urlencode({'activity_info': value})), '')

    def test_only_explicit_story_filter_switches_once_to_daily_access(self):
        with patch.object(downloader, '_fetch_aweme_detail_once', side_effect=PlatformAccessError('daily', 'story_web_unsupported')) as web, \
                patch.object(story, 'fetch_story_detail', return_value=post()) as mobile:
            self.assertEqual(downloader.fetch_aweme_detail('https://www.douyin.com/video/123', author_uid='456'), post())
            web.assert_called_once()
            self.assertEqual(mobile.call_args.args, ('123', '456'))
        for reason in ('login_required', 'risk_control', 'rate_limited', 'content_unavailable', 'invalid_response'):
            with patch.object(downloader, '_fetch_aweme_detail_once', side_effect=PlatformAccessError('stop', reason)), \
                    patch.object(story, 'fetch_story_detail') as mobile:
                with self.assertRaises(PlatformAccessError):
                    downloader.fetch_aweme_detail('https://www.douyin.com/video/123', author_uid='456')
                mobile.assert_not_called()

    def test_redirect_race_preserves_specific_daily_reason_and_single_browser(self):
        filtered = {'status_code': 0, 'aweme_detail': None,
                    'filter_detail': {'aweme_id': '123', 'filter_reason': 'story_25_filter'}}
        clock = [0.0]
        class Hunter:
            def attach(self, page):
                return self
            @property
            def results(self):
                return [{'status': 200, 'body': json.dumps(filtered)}] if clock[0] else []
        browser, context = MagicMock(), MagicMock()
        page = context.new_page.return_value
        page.goto.return_value.status = 200
        page.locator.return_value.evaluate_all.side_effect = BrowserError('Execution context was destroyed, most likely because of a navigation')
        page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
        with patch.object(downloader, 'sync_playwright'), patch.object(downloader, 'launch_chrome', return_value=browser), \
                patch.object(downloader, 'stealth_context', return_value=context), patch.object(downloader, '_attach_dy_cookies'), \
                patch.object(downloader, 'XHRHunter', return_value=Hunter()), patch.object(downloader.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(PlatformAccessError) as raised:
                downloader._fetch_aweme_detail_once('https://www.douyin.com/video/123')
        self.assertEqual(raised.exception.reason, 'story_web_unsupported')
        self.assertLess(clock[0], 1)
        browser.close.assert_called_once()

    def test_unrelated_story_filter_does_not_override_matching_normal_response(self):
        hunter, browser, context = MagicMock(), MagicMock(), MagicMock()
        hunter.attach.return_value = hunter
        hunter.results = [
            {'status': 200, 'body': json.dumps({'status_code': 0, 'filter_detail': {'aweme_id': '789', 'filter_reason': 'story_25_filter'}})},
            {'status': 200, 'body': json.dumps({'status_code': 0, 'aweme_detail': post()})}]
        context.new_page.return_value.goto.return_value.status = 200
        with patch.object(downloader, 'sync_playwright'), patch.object(downloader, 'launch_chrome', return_value=browser), \
                patch.object(downloader, 'stealth_context', return_value=context), patch.object(downloader, '_attach_dy_cookies'), \
                patch.object(downloader, 'XHRHunter', return_value=hunter):
            self.assertEqual(downloader._fetch_aweme_detail_once('https://www.douyin.com/video/123'), post())

    def test_matching_daily_retains_empty_caption_and_excludes_account_data(self):
        with patch.object(story, '_request', return_value=payload([post('789'), post()])) as request:
            result = story.fetch_story_detail('123', '456')
        self.assertTrue(result['is_story'])
        self.assertEqual(result['desc'], '')
        self.assertEqual(result['author']['nickname'], 'Fixture')
        self.assertNotIn('authentication_token', result)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0]['to_uid'], '456')

    def test_no_matching_post_wrong_author_or_private_post_never_succeeds(self):
        private = post(); private['status'] = {'is_private': True}
        for items in ([post('789')], [post(author='789')], [private], []):
            with self.subTest(items=items), patch.object(story, '_request', return_value=payload(items)) as request:
                with self.assertRaises(PlatformAccessError):
                    story.fetch_story_detail('123', '456')
                request.assert_called_once()

    def test_missing_login_or_author_stops_before_request_or_device_creation(self):
        for cookies, author in (([], '456'), ([], ''), ([{'name': 'sessionid', 'value': 'fixture', 'domain': '.evil.invalid'}], '456')):
            with patch.object(story, 'load_cookies', return_value=cookies), patch.object(story, '_request') as request:
                with self.assertRaises(PlatformAccessError):
                    story.fetch_story_detail('123', author)
                request.assert_not_called()
                self.assertFalse((self.root / 'douyin_mobile_device.json').exists())

    def test_session_header_is_scoped_unexpired_and_does_not_include_unneeded_cookies(self):
        cookies = [
            {'name': 'sessionid', 'value': 'fixture-expired', 'domain': '.douyin.com', 'expires': 1},
            {'name': 'sessionid', 'value': 'fixture-session', 'domain': '.douyin.com', 'expires': -1},
            {'name': 'uid_tt', 'value': 'fixture-wrong-domain', 'domain': '.invalid'},
            {'name': 'sid_tt', 'value': 'fixture\r\nheader', 'domain': '.douyin.com'},
            {'name': 'unneeded', 'value': 'fixture', 'domain': '.douyin.com'},
        ]
        with patch.object(story, 'load_cookies', return_value=cookies):
            self.assertEqual(story._session_header(), 'sessionid=fixture-session')

    def test_local_identity_reused_and_corrupt_configuration_not_overwritten(self):
        device = story._device()
        self.assertEqual(story._device(), device)
        path = self.root / 'douyin_mobile_device.json'
        path.write_text('{incomplete', encoding='utf8')
        with self.assertRaises(PlatformAccessError):
            story._device()
        self.assertEqual(path.read_text(encoding='utf8'), '{incomplete')

    def test_bounded_pagination_matches_target_and_stops_on_stalled_cursor(self):
        first = payload([post('789')], has_more=True, next_offset=20)
        with patch.object(story, '_request', side_effect=[first, payload()]) as request:
            self.assertEqual(story.fetch_story_detail('123', '456')['aweme_id'], '123')
            self.assertEqual(request.call_args_list[1].args[0]['offset'], '20')
        with patch.object(story, '_request', return_value=first) as request:
            with self.assertRaises(PlatformAccessError):
                story.fetch_story_detail('123', '456')
            self.assertEqual(request.call_count, 2)
        expanded = payload([]); expanded['active_data']['has_more'] = 1
        with patch.object(story, '_request', return_value=expanded) as request:
            with self.assertRaises(PlatformAccessError):
                story.fetch_story_detail('123', '456')
            self.assertEqual([call.args[0]['active_data_size'] for call in request.call_args_list], ['20', '40', '80'])

    def test_http_limits_redirects_and_auth_failures_are_safe_and_non_retrying(self):
        for status, data, reason in (
                (302, {}, 'platform_error'), (401, {}, 'login_required'),
                (403, {}, 'risk_control'), (429, {}, 'rate_limited'),
                (200, {'status_code': 8}, 'login_required'),
                (200, {'status_code': 5}, 'invalid_response'), (200, {}, 'platform_error'),
                (200, [1], 'invalid_response')):
            response = MagicMock(); response.__enter__.return_value = response
            response.status_code = status; response.iter_content.return_value = [json.dumps(data).encode()]
            with patch.object(story.requests, 'get', return_value=response) as request:
                with self.assertRaises(PlatformAccessError) as raised:
                    story._request({'to_uid': '456'}, 'sessionid=fixture-session', 10)
                self.assertEqual(raised.exception.reason, reason)
                if data == {'status_code': 5}:
                    self.assertIn('状态 5', str(raised.exception))
                    self.assertIn('不能判定登录失效', str(raised.exception))
                    self.assertNotIn('重新登录', str(raised.exception))
                request.assert_called_once()
                self.assertFalse(request.call_args.kwargs['allow_redirects'])
        response.status_code = 200; response.iter_content.return_value = [b' ' * (story._LIMIT + 1)]
        with patch.object(story.requests, 'get', return_value=response):
            with self.assertRaises(PlatformAccessError):
                story._request({}, 'fixture-session', 10)
        with patch.object(story.requests, 'get', side_effect=story.requests.Timeout('private-url')):
            with self.assertRaises(PlatformAccessError) as raised:
                story._request({}, 'fixture-session', 10)
            self.assertNotIn('private-url', str(raised.exception))

    def test_daily_uses_existing_archive_and_deduplication(self):
        with patch.object(story, '_request', return_value=payload()):
            detail = story.fetch_story_detail('123', '456')
        def transfer(url, path, progress=None):
            Path(path).write_bytes(b'fixture-media')
            return 13
        with patch.object(downloader, 'download', side_effect=transfer) as download:
            folder = Path(downloader.save_content(detail, str(self.root / 'library')))
        self.assertEqual(download.call_count, 2)
        self.assertEqual(archive.entry_status(folder, verify_files=True), 'complete')
        text = (folder / 'context.md').read_text(encoding='utf8')
        self.assertIn('内容类型**: 日常视频', text)
        self.assertIn('视频标题**: （无文案）', text)
        with patch.object(downloader, 'download') as download:
            self.assertEqual(downloader.save_content(detail, str(self.root / 'library')), str(folder))
            download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
