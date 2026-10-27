"""Synthetic images and response replay only; no accounts or platform traffic."""
import contextlib
from http.client import IncompleteRead
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

import douyin_downloader as downloader
from app import archive
from app.douyin_content import album_images, content_id, is_album
from app.platform_errors import PlatformAccessError
from app.douyin_page import detail_from_scripts
from app.scanners.douyin import parse_page


def picture(kind='JPEG'):
    output = io.BytesIO()
    Image.new('RGB', (12, 9), 'green').save(output, format=kind)
    return output.getvalue()


class Response(io.BytesIO):
    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {'Content-Length': str(len(body) if length is None else length)}


class DouyinAlbumTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.dict(os.environ, {'MS_DOWNLOAD_RETRIES': '0', 'MS_PROGRESS': '1'}))
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(downloader, '_ua', return_value='fixture'))

    def post(self, images=None):
        return {'aweme_id': '1234567', 'aweme_type': 68, 'author': {'nickname': 'Fixture'},
                'desc': '正文 😀 #旅行#', 'create_time': 1791518400,
                'images': images if images is not None else [
                    {'url_list': ['https://media.invalid/bad', 'https://media.invalid/photo.webp']},
                    {'url_list': ['https://media.invalid/next.jpg']}]}

    def test_album_order_real_extensions_fallback_manifest_and_progress(self):
        bodies = {'/bad': b'<html>blocked</html>', '/photo.webp': picture('JPEG'), '/next.jpg': picture('PNG')}
        visited = []
        def response(request, **kwargs):
            visited.append(request.full_url)
            return Response(bodies['/' + request.full_url.rsplit('/', 1)[-1]])
        with patch.object(downloader.urllib.request, 'urlopen', side_effect=response):
            folder = Path(downloader.save_content(self.post(), str(self.root)))
        self.assertEqual(visited, ['https://media.invalid/bad', 'https://media.invalid/photo.webp', 'https://media.invalid/next.jpg'])
        self.assertEqual(sorted(path.name for path in (folder / 'photo').iterdir()), ['img0001.jpg', 'img0002.png'])
        self.assertEqual(archive.entry_status(folder, verify_files=True), 'complete')
        marker = archive.read_manifest(folder)
        self.assertEqual(marker['source_url'], 'https://www.douyin.com/note/1234567')
        self.assertEqual({value['path'] for value in marker['files']}, {'context.md', 'photo/img0001.jpg', 'photo/img0002.png'})
        text = (folder / 'context.md').read_text(encoding='utf-8')
        self.assertIn('正文 😀 #旅行#', text)
        events = [json.loads(line.split('__MS_PROGRESS__', 1)[1]) for line in self.output.getvalue().splitlines() if line.startswith('__MS_PROGRESS__')]
        self.assertTrue(events)
        self.assertEqual(max(value['completed'] for value in events), 2)
        self.assertTrue(all(value['total'] == 2 for value in events))
        with patch.object(downloader.urllib.request, 'urlopen', side_effect=AssertionError('completed archive fetched again')):
            self.assertEqual(downloader.save_content(self.post(), str(self.root)), str(folder))

    def test_failure_stays_partial_retry_same_identity_and_format_replacement(self):
        post = self.post([{'url_list': ['https://media.invalid/one']}, {'url_list': ['https://media.invalid/two']}])
        with patch.object(downloader.urllib.request, 'urlopen', side_effect=[Response(picture()), OSError('offline')]):
            with self.assertRaises(RuntimeError):
                downloader.save_content(post, str(self.root))
        folder = next((self.root / 'Fixture').iterdir())
        identity = archive.read_manifest(folder)['entry_id']
        self.assertEqual(archive.entry_status(folder), 'partial')
        self.assertTrue((folder / 'context.md').exists())
        self.assertEqual(len(list(folder.rglob('*.part'))), 0)
        with patch.object(downloader.urllib.request, 'urlopen', side_effect=[Response(picture('PNG')), Response(picture('WEBP'))]):
            result = downloader.save_content(post, str(self.root))
        self.assertEqual(Path(result), folder)
        self.assertEqual(archive.read_manifest(folder)['entry_id'], identity)
        self.assertEqual(sorted(path.name for path in (folder / 'photo').iterdir()), ['img0001.png', 'img0002.webp'])
        self.assertEqual(archive.entry_status(folder, verify_files=True), 'complete')

    def test_missing_image_or_empty_list_is_not_silently_completed(self):
        for images in ([], [{'url_list': []}], [{'url_list': ['https://media.invalid/a']}, {}]):
            with self.subTest(images=images), patch.object(downloader.urllib.request, 'urlopen') as request:
                with self.assertRaises(ValueError):
                    downloader.save_content(self.post(images), str(self.root))
                request.assert_not_called()
                folder = next((self.root / 'Fixture').iterdir())
                self.assertEqual(archive.entry_status(folder), 'partial')

    def test_truncated_transfer_or_invalid_pixels_never_publish_image(self):
        for body, length in ((picture(), 10000), (picture()[:-20], None), (b'<html>no image</html>', None)):
            with patch.object(downloader.urllib.request, 'urlopen', return_value=Response(body, length)):
                with self.assertRaises(RuntimeError):
                    downloader.download_image(['https://media.invalid/one'], self.root / 'image')
            self.assertEqual(list(self.root.iterdir()), [])

    def test_interrupted_chunked_response_uses_next_candidate(self):
        with patch.object(downloader.urllib.request, 'urlopen', side_effect=[IncompleteRead(b'partial', 40), Response(picture('PNG'))]):
            result = downloader.download_image(['https://media.invalid/broken', 'https://media.invalid/good'], self.root / 'image')
        self.assertEqual(result.name, 'image.png')
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['image.png'])

    def test_images_without_type_are_album_and_topic_scanner_keeps_note_url(self):
        post = self.post()
        post.pop('aweme_type')
        post.update(is_top=True, text_extra=[{'hashtag_name': '旅行'}])
        self.assertTrue(is_album(post))
        rows, more = parse_page({'status_code': 0, 'aweme_list': [post], 'has_more': 0})
        self.assertFalse(more)
        self.assertTrue(rows[0]['pinned'])
        self.assertTrue(rows[0]['content_complete'])
        self.assertIn('旅行', rows[0]['hashtags'])
        self.assertEqual(rows[0]['params']['input'], 'https://www.douyin.com/note/1234567')
        self.assertEqual(len(album_images(post)), 2)


class DouyinLinkTests(unittest.TestCase):
    def test_note_video_and_modal_links_are_canonical_without_network(self):
        for url, expected in (('https://www.douyin.com/note/123?x=1', 'note/123'),
                              ('https://www.douyin.com/slides/123', 'note/123'),
                              ('https://www.douyin.com/video/234', 'video/234'),
                              ('https://www.douyin.com/?modal_id=345', 'video/345'),
                              ('https://www.iesdouyin.com/share/video/456/', 'video/456')):
            with patch.object(downloader.requests, 'get', side_effect=AssertionError('unneeded network')):
                self.assertEqual(downloader.resolve_to_video_url('分享 ' + url), 'https://www.douyin.com/' + expected)
        for url in ('https://douyin.com.evil.invalid/note/123', 'https://evil.invalid/?douyin.com/video/123', 'file:///note/123'):
            self.assertIsNone(content_id(url))
            with self.assertRaises(ValueError):
                downloader.resolve_to_video_url(url)

    def test_short_redirect_retains_note_and_rejects_unrelated_location(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 302
        response.headers = {'Location': 'https://www.douyin.com/slides/123'}
        with patch.object(downloader.requests, 'get', return_value=response) as request, patch.object(downloader, '_ua', return_value='fixture'):
            self.assertEqual(downloader.resolve_to_video_url('https://v.douyin.com/abc/'), 'https://www.douyin.com/note/123')
            self.assertFalse(request.call_args.kwargs['allow_redirects'])
            response.headers = {'Location': 'http://127.0.0.1/note/123'}
            with self.assertRaises(ValueError):
                downloader.resolve_to_video_url('https://v.douyin.com/abc/')
            self.assertEqual(request.call_count, 2)


class DouyinFetchTests(unittest.TestCase):
    def test_detail_arriving_after_six_seconds_is_captured_in_same_session(self):
        clock = [0.0]
        detail = {'aweme_id': '123', 'video': {'play_addr': {'url_list': ['https://media.invalid/video']}}}
        class DelayedHunter:
            def attach(self, page):
                return self
            @property
            def results(self):
                return [] if clock[0] < 8 else [{'status': 200, 'body': json.dumps({'status_code': 0, 'aweme_detail': detail})}]
        browser, context = MagicMock(), MagicMock()
        page = context.new_page.return_value
        page.goto.return_value.status = 200
        page.locator.return_value.evaluate_all.return_value = []
        page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
        with patch.object(downloader, 'sync_playwright'), patch.object(downloader, 'launch_chrome', return_value=browser), \
                patch.object(downloader, 'stealth_context', return_value=context), patch.object(downloader, '_attach_dy_cookies'), \
                patch.object(downloader, 'XHRHunter', return_value=DelayedHunter()), \
                patch.object(downloader.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.dict(os.environ, {'MS_DOWNLOAD_TIMEOUT': '120'}):
            self.assertEqual(downloader._fetch_aweme_detail_once('https://www.douyin.com/video/123'), detail)
        self.assertGreaterEqual(clock[0], 8)
        self.assertLess(clock[0], 9)
        browser.close.assert_called_once()

    def test_streamed_note_detail_normalizes_images_without_account_fields(self):
        post = {'awemeId': '1234567', 'awemeType': 68, 'authorInfo': {'nickname': 'Fixture', 'secUid': 'fixture-author'},
                'desc': '图集 😀', 'createTime': 1791518400, 'authenticationToken': 'not-a-real-secret',
                'images': [{'urlList': ['https://media.invalid/preview'], 'downloadUrlList': ['https://media.invalid/original']}]}
        frame = ['$', '$L9', None, {'awemeId': '1234567', 'aweme': {'detail': post}, 'accountInfo': {'private': 'excluded'}}]
        script = 'self.__pace_f.push(' + json.dumps([1, '7:' + json.dumps(frame)]) + ')'
        detail = detail_from_scripts([script], '1234567')
        self.assertEqual(detail['images'][0]['url_list'], ['https://media.invalid/original', 'https://media.invalid/preview'])
        self.assertEqual(detail['author']['sec_uid'], 'fixture-author')
        self.assertNotIn('authenticationToken', detail)
        self.assertNotIn('accountInfo', detail)
        self.assertIsNone(detail_from_scripts([script], '7654321'))
        self.assertIsNone(detail_from_scripts([script + '; runAnything()'], '1234567'))
        post['pcNeedLogin'] = True
        with self.assertRaises(PlatformAccessError) as raised:
            detail_from_scripts([json.dumps(post)], '1234567')
        self.assertEqual(raised.exception.reason, 'login_required')

    def test_page_parser_bounds_and_incomplete_image_slots(self):
        with self.assertRaises(PlatformAccessError):
            detail_from_scripts([' ' * (8 * 1024 * 1024 + 1)], '1234567')
        post = {'awemeId': '1234567', 'awemeType': 68, 'authorInfo': {'nickname': 'Fixture'},
                'desc': 'fixture', 'images': [{}, None]}
        detail = detail_from_scripts([json.dumps(post)], '1234567')
        self.assertEqual(len(detail['images']), 2)
        with self.assertRaises(ValueError):
            album_images(detail)

    def test_matching_response_returns_before_wait_and_closes_browser(self):
        detail = {'aweme_id': '123', 'images': [{'url_list': ['https://media.invalid/a']}]}
        hunter = MagicMock()
        hunter.attach.return_value = hunter
        hunter.results = [{'status': 200, 'body': json.dumps({'status_code': 0, 'aweme_detail': detail})}]
        browser, context = MagicMock(), MagicMock()
        page = context.new_page.return_value
        page.goto.return_value.status = 200
        with patch.object(downloader, 'sync_playwright'), patch.object(downloader, 'launch_chrome', return_value=browser), \
                patch.object(downloader, 'stealth_context', return_value=context), patch.object(downloader, '_attach_dy_cookies'), \
                patch.object(downloader, 'XHRHunter', return_value=hunter):
            self.assertEqual(downloader._fetch_aweme_detail_once('https://www.douyin.com/note/123'), detail)
        page.wait_for_timeout.assert_not_called()
        browser.close.assert_called_once()

    def test_access_failure_and_empty_response_are_never_retried(self):
        for reason in ('login_required', 'risk_control', 'rate_limited', 'invalid_response', 'platform_error'):
            with patch.dict(os.environ, {'MS_DOUYIN_DETAIL_CHANNEL': 'browser', 'MS_DOWNLOAD_RETRIES': '3'}), \
                    patch.object(downloader, '_fetch_aweme_detail_once', side_effect=PlatformAccessError('stopped', reason)) as fetch:
                with self.assertRaises(PlatformAccessError):
                    downloader.fetch_aweme_detail('https://www.douyin.com/video/123')
                fetch.assert_called_once()

    def test_adapter_fallback_only_for_local_unavailability(self):
        for reason, fallback in (('unavailable', True), ('unsupported', True), ('risk_control', False), ('timeout', False), ('invalid_response', False)):
            with patch.dict(os.environ, {'MS_DOUYIN_DETAIL_CHANNEL': 'catbus'}), \
                    patch('app.douyin_detail_adapter.fetch_detail', side_effect=PlatformAccessError('fixture', reason)), \
                    patch.object(downloader, '_fetch_aweme_detail_once', return_value={'aweme_id': '123'}) as browser, \
                    contextlib.redirect_stdout(io.StringIO()):
                if fallback:
                    self.assertEqual(downloader.fetch_aweme_detail('https://www.douyin.com/video/123')['aweme_id'], '123')
                    browser.assert_called_once()
                else:
                    with self.assertRaises(PlatformAccessError):
                        downloader.fetch_aweme_detail('https://www.douyin.com/video/123')
                    browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
