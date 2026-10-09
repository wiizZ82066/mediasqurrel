from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app import thumbs


class ThumbnailTests(unittest.TestCase):
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
