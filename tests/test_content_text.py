"""Text fidelity fixtures: no platform calls, real archives or database writes."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.content_text import html_to_text, weibo_post_text
from app.catalog import _context
import weibo_downloader as weibo


class ContentTextTests(unittest.TestCase):
    def test_raw_text_is_not_html_or_entity_decoded(self):
        raw = "  1 < 2 > 0 &amp; <unknown> [笑cry]\n\t尾部  "
        self.assertEqual(weibo_post_text({"text_raw": raw, "text": "wrong"}), raw)

    def test_html_keeps_known_and_unknown_alt_line_breaks_and_entities(self):
        markup = ('<p>前<img alt="[笑cry]" src="https://example.invalid/emoji.png">后'
                  '<br>下一行 &amp; &#128514; &lt;保留&gt;</p>'
                  '<div><a href="javascript:alert(1)">链接</a>'
                  '<img alt="[尚未收录&amp;表情]" /><img src="missing-alt"></div>')
        expected = "前[笑cry]后\n下一行 & 😂 <保留>\n链接[尚未收录&表情]"
        self.assertEqual(weibo_post_text({"text_raw": "", "text": markup}), expected)
        self.assertEqual(weibo_post_text({"text": markup}), expected)

    def test_unicode_sequences_are_preserved_without_normalization(self):
        text = "👨‍👩‍👧‍👦 👍🏽 🇨🇳 ❤️ 1️⃣ e\u0301"
        self.assertEqual(weibo_post_text({"text_raw": text}), text)
        self.assertEqual(html_to_text("<p>" + text + "</p>"), text)

    def test_untrusted_markup_produces_only_text_and_does_not_follow_urls(self):
        markup = ('<script>alert("secret")</script><style>.x{}</style>'
                  '<template>hidden<template>nested</template>hidden</template>'
                  '<img src="https://example.invalid/tracker" onerror="alert(1)" '
                  'alt="&lt;script&gt;literal&lt;/script&gt;">'
                  '<a href="javascript:alert(2)">safe label</a>')
        self.assertEqual(html_to_text(markup), "<script>literal</script>safe label")
        # Even script-looking raw text stays text. The caller must not render it as HTML.
        raw = '<img src=x onerror="alert(3)">'
        self.assertEqual(weibo_post_text({"text_raw": raw}), raw)


class DownloaderTextTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(weibo.time, "sleep"))
        self.stack.enter_context(patch.object(weibo, "scroll_page_gradually"))
        self.page = Mock()
        self.page.title.return_value = "fixture"

    def test_api_extraction_and_archive_round_trip_preserve_raw_text(self):
        raw = "1 < 2 > 0\n👨‍👩‍👧‍👦 👍🏽 [未知表情] &amp;"
        payload = {"text_raw": raw, "text": "must not replace raw", "mblogid": "Abc123",
                   "user": {"screen_name": "Fixture"},
                   "created_at": "Sun Aug 09 19:56:59 +0800 2026"}
        with patch.object(weibo, "fetch_status_api", return_value=payload):
            data = weibo.extract_post(self.page, "https://weibo.com/123/Abc123")
        self.assertEqual(data["text"], raw)
        data["url"] = "https://weibo.com/123/Abc123"
        with tempfile.TemporaryDirectory(prefix="media-squirrel-text-test-") as temporary:
            folder = Path(weibo.save_content(data, temporary))
            self.assertEqual(_context(folder)[1], raw)
            self.assertIn(raw, (folder / "context.md").read_text(encoding="utf-8"))

    def test_api_html_fallback_keeps_inline_emoji(self):
        with patch.object(weibo, "fetch_status_api", return_value={
            "text": '正文<img alt="[笑cry]" src="fixture"><br>&#128514;',
            "user": {"screen_name": "Fixture"},
        }):
            result = weibo.extract_post(self.page, "https://weibo.com/123/Abc123")
        self.assertEqual(result["text"], "正文[笑cry]\n😂")

    def test_dom_fallback_keeps_alt_and_existing_visible_metadata(self):
        article = Mock()
        article.inner_text.return_value = "Fixture\n公开\n26-8-9 15:51\n发布于 测试\n正文\n下一行"
        article.inner_html.return_value = (
            '<header><span>Fixture</span><span>公开</span><span>26-8-9 15:51</span></header>'
            '<div>发布于 测试</div><div>正文<img src="fixture" alt="[未知新表情]"><br>下一行 ❤️</div>')
        body = Mock()
        body.inner_html.return_value = '正文<img src="fixture" alt="[未知新表情]"><br>下一行 ❤️'
        article.query_selector.side_effect = lambda selector: None if selector == 'video' else body
        photo = Mock()
        photo.get_attribute.side_effect = lambda key: 'https://wx1.sinaimg.cn/large/fixture.jpg' if key == 'src' else ''
        article.query_selector_all.side_effect = lambda selector: [] if 'customPoster' in selector else [photo]
        self.page.query_selector.return_value = article
        self.page.query_selector_all.return_value = []
        self.page.evaluate.return_value = []
        with patch.object(weibo, "fetch_status_api", return_value=None):
            result = weibo.extract_post(self.page, "https://weibo.com/123/Abc123")
        self.assertFalse(result["api"])
        self.assertIn("正文[未知新表情]\n下一行 ❤️", result["text"])
        self.assertNotIn('公开', result['text'])
        self.assertNotIn('发布于', result['text'])
        self.assertEqual(len(result['normal_imgs']), 1)
        self.assertEqual(result["username"], "Fixture")
        self.assertEqual(result["publish_time"], "26-08-09")
        self.assertEqual(result['publish_datetime'], '26-08-09 15:51')
        self.assertEqual(result["ip_region"], "测试")

    def test_unverified_dom_text_without_media_is_not_success(self):
        article = Mock()
        article.inner_text.return_value = 'Fixture\n公开\n26-8-9 15:51\n正文'
        article.query_selector.return_value.inner_html.return_value = '正文'
        article.query_selector_all.return_value = []
        self.page.query_selector.return_value = article
        self.page.evaluate.return_value = []
        with patch.object(weibo, 'fetch_status_api', return_value=None):
            with self.assertRaisesRegex(ValueError, '媒体列表'):
                weibo.extract_post(self.page, 'https://weibo.com/123/Abc123')

    def test_api_declared_media_cannot_silently_be_saved_as_text(self):
        payload = {'mblogid': 'Abc123', 'text_raw': '正文', 'user': {'screen_name': 'Fixture'},
                   'pic_num': 2, 'pic_ids': ['one', 'missing'],
                   'pic_infos': {'one': {'largest': {'url': 'https://wx1.sinaimg.cn/large/one.jpg'}}}}
        with patch.object(weibo, 'fetch_status_api', return_value=payload):
            with self.assertRaisesRegex(ValueError, '图片列表不完整'):
                weibo.extract_post(self.page, 'https://weibo.com/123/Abc123')

    def test_status_api_retries_transport_but_rejects_access_denials_and_wrong_posts(self):
        from app.platform_errors import PlatformAccessError
        payload = {'ok': 1, 'mblogid': 'Abc123', 'text_raw': '正文'}
        self.page.evaluate.side_effect = [None, {'status': 200, 'data': payload}]
        self.assertEqual(weibo.fetch_status_api(self.page, 'https://weibo.com/123/Abc123'), payload)
        self.assertEqual(self.page.evaluate.call_count, 2)
        for response in [{'status': 429, 'data': None},
                         {'status': 200, 'data': {'ok': -100}},
                         {'status': 200, 'data': dict(payload, mblogid='OtherPost')}]:
            self.page.evaluate.reset_mock(side_effect=True)
            self.page.evaluate.return_value = response
            with self.assertRaises(PlatformAccessError):
                weibo.fetch_status_api(self.page, 'https://weibo.com/123/Abc123')
            self.page.evaluate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
