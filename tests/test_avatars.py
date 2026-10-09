"""Avatar cache tests use synthetic images and mocked HTTP only."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from app import avatars, config, watcher


def image_bytes():
    output = io.BytesIO()
    Image.new('RGB', (32, 32), 'blue').save(output, 'PNG')
    return output.getvalue()


class AvatarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for key, value in (('DB_PATH', str(Path(self.temp.name) / 'app.db')),
                           ('CACHE_DIR', str(Path(self.temp.name) / 'cache'))):
            replacement = patch.object(config, key, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        avatars._negative.clear()
        watcher.init_db()
        self.source = 'https://tvax1.sinaimg.cn/example.jpg?auth_key=fixture-one'
        self.sub = watcher.add_sub('weibo', '123', avatar_url=self.source)

    def test_https_exact_domain_port_and_no_userinfo(self):
        for source in ('http://tvax1.sinaimg.cn/a', 'https://sinaimg.cn.attacker.invalid/a',
                       'https://127.0.0.1/a', 'https://tvax1.sinaimg.cn:8080/a',
                       'https://user:pass@tvax1.sinaimg.cn/a', 'https://tvax1.sinaimg.cn/a#token=x'):
            self.assertFalse(avatars.valid_source(source))
        self.assertTrue(avatars.valid_source(self.source))
        self.assertTrue(avatars.valid_source('https://p3.douyinpic.com/avatar.jpeg'))

    def test_http_has_no_proxy_or_redirect_and_normalizes_real_image(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code, response.headers = 200, {}
        response.iter_content.return_value = [image_bytes()]
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response
        with patch.object(avatars.requests, 'Session', return_value=session):
            payload = avatars._download(self.source, 'weibo')
        self.assertFalse(session.trust_env)
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        self.assertNotIn('Cookie', session.get.call_args.kwargs['headers'])
        with Image.open(io.BytesIO(payload)) as result:
            self.assertEqual(result.format, 'JPEG')
            self.assertLessEqual(max(result.size), 256)

    def test_oversize_and_redirect_response_are_rejected(self):
        for status, headers, chunks in ((302, {}, []), (200, {'Content-Length': str(avatars.MAX_BYTES + 1)}, []),
                                        (200, {}, [b'x' * (avatars.MAX_BYTES + 1)])):
            response = MagicMock(status_code=status, headers=headers)
            response.__enter__.return_value = response
            response.iter_content.return_value = chunks
            session = MagicMock()
            session.__enter__.return_value = session
            session.get.return_value = response
            with patch.object(avatars.requests, 'Session', return_value=session):
                self.assertIsNone(avatars._download(self.source, 'weibo'))

    def test_cache_hit_expiry_fallback_and_refreshed_source(self):
        with patch.object(avatars, '_download', return_value=image_bytes()) as download:
            original = avatars.get_avatar(self.sub['id'])
            self.assertEqual(avatars.get_avatar(self.sub['id']), original)
            self.assertEqual(download.call_count, 1)
        self.assertNotIn('fixture', Path(original).name)
        avatars.update_source(self.sub['id'], 'https://tvax1.sinaimg.cn/a?auth_key=fixture-expired')
        with patch.object(avatars, '_download', return_value=None) as download:
            self.assertEqual(avatars.get_avatar(self.sub['id']), original)
            self.assertEqual(avatars.get_avatar(self.sub['id']), original)
            self.assertEqual(download.call_count, 1)
        avatars.update_source(self.sub['id'], 'https://tvax1.sinaimg.cn/a?auth_key=fixture-refreshed')
        with patch.object(avatars, '_download', return_value=image_bytes()):
            refreshed = avatars.get_avatar(self.sub['id'])
        self.assertNotEqual(refreshed, original)
        self.assertEqual(len(list(Path(config.CACHE_DIR).rglob('*.jpg'))), 1)
        self.assertEqual(avatars._locks, {})

    def test_missing_image_defaults_and_maintenance_does_not_fetch(self):
        with patch.object(config, 'MAINTENANCE_ACTIVE', True, create=True), patch.object(avatars, '_download') as download:
            self.assertIsNone(avatars.get_avatar(self.sub['id']))
            download.assert_not_called()
        with patch.object(avatars, '_download', return_value=None):
            self.assertIsNone(avatars.get_avatar(self.sub['id']))
        self.assertIsNone(avatars.get_avatar(999))


if __name__ == '__main__':
    unittest.main()
