import tempfile
import unittest
from pathlib import Path
import os
import queue
import subprocess
import sys
import threading

import cv2
import numpy as np

from weibo_downloader import extract_cover_from_mov


@unittest.skipUnless(os.name == 'nt', 'Windows pipe and native-library regression')
class ParentWatchdogTests(unittest.TestCase):
    def test_idle_parent_pipe_allows_native_import_and_eof_stops_backend(self):
        child = subprocess.Popen([
            sys.executable, '-u', '-c',
            'import run, threading\n'
            'run._stdin_watchdog()\n'
            'worker = threading.Thread(target=lambda: __import__("cv2"))\n'
            'worker.start()\nworker.join()\n'
            'print("IMPORTED", flush=True)\nthreading.Event().wait()\n',
        ], cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True)
        reader.start()
        try:
            self.assertEqual(lines.get(timeout=20).strip(), b'IMPORTED')
            child.stdin.close()
            self.assertEqual(child.wait(timeout=10), 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            reader.join(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()


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
