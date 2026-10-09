"""Exercise the real desktop stdin watchdog and first media-library scan."""
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend-dist/MediaSquirrelBackend/MediaSquirrelBackend.exe"


def main():
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix="frozen-api-", dir=ROOT / "build") as directory:
        data = Path(directory)
        photos = data / "library/Regression/2026-01-01/photo"
        photos.mkdir(parents=True)
        for number in range(2):
            pixels = np.full((96, 96, 3), 80 + number * 80, dtype=np.uint8)
            ok, encoded = cv2.imencode(".jpg", pixels)
            assert ok
            encoded.tofile(photos / f"sample{number}.jpg")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = dict(os.environ, MS_DATA_DIR=str(data))
        with (data / "backend.log").open("wb") as log:
            process = subprocess.Popen(
                [str(BACKEND), "--no-browser", "--no-tray", "--port", str(port)],
                env=env, stdin=subprocess.PIPE, stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            def request(route):
                with client.open(f"http://127.0.0.1:{port}{route}", timeout=20) as response:
                    return json.load(response)

            try:
                deadline = time.monotonic() + 45
                while True:
                    try:
                        assert request("/api/health")["status"] == "ok"
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Frozen backend did not become ready")
                        time.sleep(0.2)
                # Startup warming and this refresh import OpenCV in a worker
                # while the watchdog is blocked on an otherwise idle stdin.
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    scan = pool.submit(request, "/api/library?refresh=1")
                    checks = [pool.submit(request, "/api/health") for _ in range(2)]
                    library = scan.result(timeout=25)
                    assert len(library) == 1 and library[0]["count"] == 1
                    assert all(check.result(timeout=25)["status"] == "ok" for check in checks)
                process.stdin.close()
                assert process.wait(timeout=10) == 0, "Parent EOF did not stop the backend"
            except Exception:
                print(f"Frozen backend failure; exit code: {process.poll()}")
                print((data / "backend.log").read_text(encoding="utf-8", errors="replace")[-8000:])
                raise
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if not process.stdin.closed:
                    process.stdin.close()
    print("Frozen media-library scan, responsive API and stdin EOF cleanup: PASS")


if __name__ == "__main__":
    main()
