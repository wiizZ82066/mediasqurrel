from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from app import config, thumbs


class ThumbnailTests(unittest.TestCase):
    def test_header_pixel_edge_and_compressed_limits_precede_allocation(self):
        header = MagicMock()
        header.__enter__.return_value = header
        with patch.object(thumbs.os, 'stat', return_value=MagicMock(st_size=100)) as stat, \
                patch.object(thumbs.Image, 'open', return_value=header) as opened, \
                patch.object(thumbs.np, 'fromfile') as allocate:
            for dimensions in ((10000, 10000), (40000, 1)):
                header.size = dimensions
                self.assertIsNone(thumbs._imread_unicode('large-image.jpg'))
            allocate.assert_not_called()
            opened.reset_mock()
            stat.return_value.st_size = thumbs.MAX_IMAGE_BYTES + 1
            self.assertIsNone(thumbs._imread_unicode('large-file.jpg'))
            opened.assert_not_called()
            allocate.assert_not_called()

    def test_video_fallback_checks_size_and_space_before_copy(self):
        capture = MagicMock()
        capture.isOpened.return_value = False
        with patch.object(thumbs.cv2, 'VideoCapture', return_value=capture), \
                patch.object(thumbs.os, 'stat', return_value=MagicMock(st_size=thumbs.MAX_VIDEO_FALLBACK_BYTES + 1)) as stat, \
                patch.object(thumbs.shutil, 'copyfile') as copy, patch.object(thumbs.tempfile, 'mkstemp') as temporary, \
                patch.object(thumbs.tempfile, 'gettempdir', return_value='fixture'), \
                patch.object(thumbs.shutil, 'disk_usage', return_value=MagicMock(free=10)):
            self.assertIsNone(thumbs._video_first_frame('large-video.mp4'))
            stat.return_value.st_size = 100
            self.assertIsNone(thumbs._video_first_frame('no-space.mp4'))
            copy.assert_not_called()
            temporary.assert_not_called()

    def test_warm_hit_refreshes_before_cache_trim_can_remove_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / 'source.jpg'
            cv2.imwrite(str(original), np.full((30, 30, 3), 100, dtype=np.uint8))
            with patch.object(thumbs, 'THUMB_DIR', str(root / 'cache')), patch.object(config, 'CACHE_LIMIT_BYTES', 0), \
                    patch.object(thumbs, '_last_trim', 0):
                cached = thumbs.get_thumb(str(original))
                os.utime(cached, (time.time() - 1200, time.time() - 1200))
                thumbs._last_trim = 0
                self.assertEqual(thumbs.get_thumb(str(original)), cached)
                self.assertTrue(Path(cached).is_file())

    def test_concurrent_sizes_and_roots_have_distinct_atomic_cache_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / 'one' / '图片.jpg', root / 'two' / '图片.jpg'
            for path, color in ((first, 40), (second, 200)):
                path.parent.mkdir()
                ok, data = cv2.imencode('.jpg', np.full((400, 800, 3), color, dtype=np.uint8))
                self.assertTrue(ok)
                path.write_bytes(data.tobytes())
                os.utime(path, ns=(1_700_000_000_000_000_000,) * 2)
            cache = root / '缓存'
            requests = [(first, 120), (first, 480), (second, 120)] * 5
            with patch.object(thumbs, 'THUMB_DIR', str(cache)), ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(lambda args: thumbs.get_thumb(str(args[0]), '图片.jpg', args[1]), requests))
                self.assertEqual(len(set(results)), 3)
                for result, (_source, width) in zip(results, requests):
                    self.assertIsNotNone(result)
                    self.assertEqual(thumbs._imread_unicode(result).shape[1], width)
                prior = results[0]
                os.utime(first, ns=(1_700_000_000_000_001_000,) * 2)
                self.assertNotEqual(thumbs.get_thumb(str(first), width=120), prior)
                self.assertEqual(list(cache.glob('*.part')), [])
                self.assertGreater(thumbs._imread_unicode(results[2]).mean(), thumbs._imread_unicode(results[0]).mean())


if __name__ == '__main__':
    unittest.main()
