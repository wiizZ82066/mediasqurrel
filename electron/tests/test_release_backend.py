import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from weibo_downloader import extract_cover_from_mov


class UnicodeCoverTests(unittest.TestCase):
    def test_real_video_cover_is_written_to_unicode_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / 'source.avi'
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), 5, (32, 32))
            self.assertTrue(writer.isOpened())
            try:
                writer.write(np.full((32, 32, 3), 127, dtype=np.uint8))
            finally:
                writer.release()
            destination = root / '中文作者' / '封面.jpg'
            destination.parent.mkdir()
            self.assertTrue(extract_cover_from_mov(str(video), str(destination)))
            decoded = cv2.imdecode(np.frombuffer(destination.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(decoded.shape, (32, 32, 3))
            self.assertFalse(extract_cover_from_mov(str(video), str(root / 'missing' / 'cover.jpg')))


if __name__ == '__main__':
    unittest.main()
