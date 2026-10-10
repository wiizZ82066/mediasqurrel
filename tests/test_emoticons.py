"""Emoticon cache regressions use synthetic images and mocked HTTP only."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from app import config, emoticons


SOURCE = 'https://face.t.sinajs.cn/t4/appstyle/expression/ext/normal/ba/201810_tuzi_mobile.png'


def image_bytes(*, animated=False, size=(32, 32)):
    first = Image.new('RGBA', size, (0, 0, 0, 0))
    first.putpixel((size[0] // 2, size[1] // 2), (255, 0, 0, 255))
    output = io.BytesIO()
    options = {}
    if animated:
        second = Image.new('RGBA', size, 'blue')
        options = dict(save_all=True, append_images=[second], duration=100, loop=0)
    first.save(output, 'PNG', **options)
    return output.getvalue()


def http_session(body=None, *, status=200, headers=None, chunks=None):
    response = MagicMock(status_code=status, headers=headers or {})
    response.__enter__.return_value = response
    response.raw.read1.side_effect = list(chunks if chunks is not None else [body or image_bytes()]) + [b'']
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = response
    return session, response


class EmoticonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mapping = self.root / 'mapping.json'
        for owner, key, value in ((config, 'CACHE_DIR', str(self.root / 'cache')),
                                  (config, 'MAINTENANCE_ACTIVE', False),
                                  (emoticons, 'MAPPING_FILE', self.mapping)):
            replacement = patch.object(owner, key, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        emoticons._mapping.cache_clear()
        emoticons._negative.clear()
        self.addCleanup(emoticons._mapping.cache_clear)
        self.write_mapping({'[兔子]': SOURCE})

    def write_mapping(self, value):
        self.mapping.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        emoticons._mapping.cache_clear()

    def test_names_and_unknown_tokens_never_access_network_or_write_cache(self):
        self.write_mapping({'[兔子]': SOURCE, '[bad]': 'https://127.0.0.1/private',
                            '../escape': SOURCE, '[bad name]': SOURCE})
        with patch.object(emoticons.requests, 'Session') as session:
            self.assertEqual(emoticons.get_names(), ['[兔子]'])
            for name in ('[未知]', SOURCE, '../escape', '兔子', None):
                self.assertIsNone(emoticons.get_emoticon(name))
            session.assert_not_called()
        self.assertFalse(Path(config.CACHE_DIR).exists())
        self.mapping.write_text('not json', encoding='utf-8')
        emoticons._mapping.cache_clear()
        self.assertEqual(emoticons.get_names(), [])

    def test_exact_cdn_static_path_rejects_ssrf_redirect_targets_and_url_extras(self):
        self.assertTrue(emoticons.valid_source(SOURCE))
        special = 'https://face.t.sinajs.cn/t4/appstyle/expression/ext/normal/e7/2023_TheWanderingEarthⅡ_mobile.png'
        self.assertTrue(emoticons.valid_source(special))
        self.assertFalse(emoticons.valid_source(special.replace('Ⅱ', 'Ⅲ')))
        for source in (
            SOURCE.replace('https:', 'http:'), SOURCE + '?token=secret', SOURCE + '#fragment',
            SOURCE.replace('face.t.sinajs.cn', 'face.t.sinajs.cn:443'),
            SOURCE.replace('face.t.sinajs.cn', 'face.t.sinajs.cn:'),
            SOURCE.replace('face.t.sinajs.cn', 'user:pass@face.t.sinajs.cn'),
            SOURCE.replace('face.t.sinajs.cn', 'face.t.sinajs.cn.attacker.invalid'),
            SOURCE.replace('face.t.sinajs.cn', '127.0.0.1'),
            SOURCE.replace('face.t.sinajs.cn', '[::1]'),
            SOURCE.replace('/ba/', '/ba/../'), SOURCE.replace('/ba/', '/ba/%2e%2e/'),
            'https://face.t.sinajs.cn/arbitrary.png',
        ):
            with self.subTest(source=source), patch.object(emoticons.requests, 'Session') as session:
                self.assertFalse(emoticons.valid_source(source))
                self.assertIsNone(emoticons._download(source, time.monotonic() + 2))
                session.assert_not_called()

    def test_download_is_credential_free_and_preserves_transparency_as_static_first_frame(self):
        session, _ = http_session(image_bytes(animated=True))
        with patch.object(emoticons.requests, 'Session', return_value=session):
            data = emoticons._download(SOURCE, time.monotonic() + 5)
        self.assertFalse(session.trust_env)
        options = session.get.call_args.kwargs
        self.assertFalse(options['allow_redirects'])
        self.assertEqual(options['headers']['Accept-Encoding'], 'identity')
        self.assertNotIn('Cookie', options['headers'])
        self.assertNotIn('Authorization', options['headers'])
        self.assertNotIn('auth', options)
        self.assertNotIn('proxies', options)
        with Image.open(io.BytesIO(data)) as result:
            self.assertEqual((result.format, result.mode, result.n_frames), ('PNG', 'RGBA', 1))
            self.assertEqual(result.getpixel((0, 0))[3], 0)
            self.assertEqual(result.getpixel((16, 16)), (255, 0, 0, 255))
            self.assertNotIn('exif', result.info)

    def test_redirect_oversize_compressed_and_excessive_pixels_are_rejected(self):
        cases = (
            dict(status=302, headers={'Location': 'https://127.0.0.1/private'}),
            dict(headers={'Content-Length': str(emoticons.MAX_BYTES + 1)}),
            dict(headers={'Content-Encoding': 'gzip'}),
            dict(chunks=[b'x' * (emoticons.MAX_BYTES + 1)]),
            dict(body=image_bytes(size=(1001, 1000))),
        )
        for case in cases:
            with self.subTest(case=list(case)):
                session, _ = http_session(**case)
                with patch.object(emoticons.requests, 'Session', return_value=session):
                    self.assertIsNone(emoticons._download(SOURCE, time.monotonic() + 5))
        with patch.object(emoticons.requests, 'Session') as session:
            self.assertIsNone(emoticons._download(SOURCE, time.monotonic() - 1))
            session.assert_not_called()

    def test_cache_works_offline_and_does_not_contain_original_tokens_or_urls(self):
        with patch.object(emoticons, '_download', return_value=image_bytes()) as download:
            first = emoticons.get_emoticon('[兔子]')
            self.assertIsNotNone(first)
            self.assertEqual(download.call_count, 1)
        with patch.object(emoticons.requests, 'Session') as session:
            self.assertEqual(emoticons.get_emoticon('[兔子]'), first)
            session.assert_not_called()
        self.assertRegex(Path(first).name, r'^[0-9a-f]{64}\.png$')
        self.assertEqual(list(Path(config.CACHE_DIR).rglob('*.partial')), [])
        self.assertEqual(emoticons._locks, {})

    def test_failures_are_negatively_cached_without_partial_files(self):
        with patch.object(emoticons, '_download', return_value=None) as download:
            self.assertIsNone(emoticons.get_emoticon('[兔子]'))
            self.assertIsNone(emoticons.get_emoticon('[兔子]'))
            self.assertEqual(download.call_count, 1)
        self.assertFalse(Path(config.CACHE_DIR).exists())
        emoticons._negative.clear()
        with patch.object(emoticons, '_download', return_value=image_bytes()), \
                patch.object(emoticons.os, 'replace', side_effect=OSError('simulated disk full')):
            self.assertIsNone(emoticons.get_emoticon('[兔子]'))
        self.assertEqual(list(Path(config.CACHE_DIR).rglob('*.partial')), [])
        self.assertEqual(list(Path(config.CACHE_DIR).rglob('*.png')), [])

    def test_same_source_concurrent_calls_share_one_download(self):
        self.write_mapping({'[兔子]': SOURCE, '[别名]': SOURCE})
        barrier = threading.Barrier(2)
        started, release = threading.Event(), threading.Event()

        def download(*_):
            started.set()
            release.wait(2)
            return image_bytes()

        def get(name):
            barrier.wait(timeout=2)
            return emoticons.get_emoticon(name)

        with patch.object(emoticons, '_download', side_effect=download) as fetch, ThreadPoolExecutor(2) as pool:
            first, second = pool.submit(get, '[兔子]'), pool.submit(get, '[别名]')
            self.assertTrue(started.wait(2))
            release.set()
            first_path, second_path = first.result(timeout=3), second.result(timeout=3)
        self.assertIsNotNone(first_path)
        self.assertEqual(first_path, second_path)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(emoticons._locks, {})

    def test_stalled_worker_cannot_extend_caller_budget_or_write_a_late_cache(self):
        release, finished = threading.Event(), threading.Event()

        def download(*_):
            release.wait(2)
            finished.set()
            return image_bytes()

        with patch.object(emoticons, '_download', side_effect=download), patch.object(emoticons, 'MAX_SECONDS', .03):
            try:
                start = time.monotonic()
                self.assertIsNone(emoticons.get_emoticon('[兔子]'))
                self.assertLess(time.monotonic() - start, .5)
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
        self.assertFalse(Path(config.CACHE_DIR).exists())

    def test_maintenance_serves_existing_cache_without_fetching_or_touching_files(self):
        with patch.object(config, 'MAINTENANCE_ACTIVE', True), patch.object(emoticons, '_download') as download:
            self.assertIsNone(emoticons.get_emoticon('[兔子]'))
            download.assert_not_called()
        with patch.object(emoticons, '_download', return_value=image_bytes()):
            cached = emoticons.get_emoticon('[兔子]')
        with patch.object(config, 'MAINTENANCE_ACTIVE', True), \
                patch.object(emoticons, '_download') as download, patch.object(emoticons.os, 'utime') as touch:
            self.assertEqual(emoticons.get_emoticon('[兔子]'), cached)
            download.assert_not_called()
            touch.assert_not_called()

    def test_cache_evicts_oldest_files_to_keep_count_and_bytes_bounded(self):
        names = ['[一]', '[二]', '[三]']
        self.write_mapping({name: SOURCE.replace('tuzi', 'fixture_' + str(i)) for i, name in enumerate(names)})
        payload = image_bytes()
        with patch.object(emoticons, '_download', return_value=payload), \
                patch.object(emoticons, 'MAX_CACHE_FILES', 2), \
                patch.object(emoticons, 'MAX_CACHE_BYTES', len(payload) * 2):
            first, second = [emoticons.get_emoticon(name) for name in names[:2]]
            os.utime(first, (10, 10))
            os.utime(second, (20, 20))
            third = emoticons.get_emoticon(names[2])
        self.assertFalse(Path(first).exists())
        self.assertTrue(Path(second).exists())
        self.assertTrue(Path(third).exists())
        files = list((Path(config.CACHE_DIR) / 'emoticons').glob('*.png'))
        self.assertEqual(len(files), 2)
        self.assertLessEqual(sum(path.stat().st_size for path in files), len(payload) * 2)


if __name__ == '__main__':
    unittest.main()
