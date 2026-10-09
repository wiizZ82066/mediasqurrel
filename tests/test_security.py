import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import config
from app.security import LocalOnlyMiddleware, media_path


class MediaBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "library"
        self.root.mkdir()
        self.author = self.root / "author" / "2026-01-01"
        self.author.mkdir(parents=True)
        (self.author / "photo.jpg").write_bytes(b"test-media")

    def test_only_media_inside_root_can_be_read(self):
        from fastapi import HTTPException
        for value in ("../secret.jpg", "/secret.jpg", "C:/secret.jpg", "author/../../secret.jpg", "author/2026-01-01/context.md", "app_data/secret.jpg", "author/.hidden.jpg"):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                media_path(value, root=self.root, file_only=True)
        self.assertEqual(media_path("author/2026-01-01/photo.jpg", root=self.root, file_only=True), str(self.author / "photo.jpg"))

    def test_resolved_link_cannot_escape_media_root(self):
        from fastapi import HTTPException
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "photo.jpg").write_bytes(b"outside")
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symbolic link creation unavailable on this host")
        with self.assertRaises(HTTPException):
            media_path("linked/photo.jpg", root=self.root, file_only=True)


class LocalRequestTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.add_middleware(LocalOnlyMiddleware)

        @app.get("/read")
        def read():
            return {"ok": True}

        @app.post("/write")
        def write(body: dict):
            return {"ok": True}

        @app.websocket("/ws")
        async def socket(ws: WebSocket):
            await ws.accept()
            await ws.close()

        self.client = TestClient(app, base_url=f"http://127.0.0.1:{config.PORT}")

    def test_local_client_and_same_origin_work(self):
        self.assertEqual(self.client.get("/read").status_code, 200)
        response = self.client.post("/write", json={}, headers={"Origin": f"http://127.0.0.1:{config.PORT}"})
        self.assertEqual(response.status_code, 200)

    def test_cross_origin_dns_rebinding_and_form_posts_are_rejected(self):
        for headers in ({"Origin": "https://external.invalid"}, {"Host": "external.invalid"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.client.get("/read", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/write", content="{}", headers={"Content-Type": "text/plain"}).status_code, 415)

    def test_foreign_websocket_origin_is_rejected(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f"ws://127.0.0.1:{config.PORT}/ws", headers={"Origin": "https://external.invalid"}):
                pass

    def test_same_origin_websocket_can_connect(self):
        with self.client.websocket_connect(f"ws://127.0.0.1:{config.PORT}/ws", headers={"Origin": f"http://127.0.0.1:{config.PORT}"}):
            pass
