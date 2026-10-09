"""HTTP + real short-lived subprocess tests, all paths isolated from user data."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, media_library, script_registry, task_manager
from app.main import app


class ApplicationPersistenceTests(unittest.TestCase):
    def test_history_and_redacted_logs_survive_restart_and_deletes_are_separate(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            data, library, manifests = root / "data", root / "library", root / "manifests"
            manifests.mkdir()
            script = root / "fixture.py"
            script.write_text("print('normal output')\nprint('Authorization: Bearer fixture-secret')\n", encoding="utf-8")
            (manifests / "fixture.json").write_text(json.dumps({"id": "fixture", "name": "Fixture",
                "script": str(script), "params": []}), encoding="utf-8")
            for key, value in {"DATA_DIR": data, "LIBRARY_ROOT": library, "LOG_DIR": data / "logs",
                    "CACHE_DIR": data / "cache", "DB_PATH": data / "app.db", "MANIFEST_DIR": manifests}.items():
                stack.enter_context(patch.object(config, key, str(value)))
            stack.enter_context(patch.object(media_library, "_LIB_CACHE_PATH", str(data / "cache" / "library.json")))
            stack.enter_context(patch.object(media_library, "_COVER_CACHE_PATH", str(data / "cache" / "covers.json")))
            media_library._lib_cache_mem = None
            script_registry.reload()
            base = f"http://127.0.0.1:{config.PORT}"
            with TestClient(app, base_url=base) as client:
                response = client.post("/api/tasks", json={"script_id": "fixture", "params": {}})
                self.assertEqual(response.status_code, 200, response.text)
                task_id = response.json()["id"]
                for _ in range(200):
                    task = client.get(f"/api/tasks/{task_id}").json()
                    if task["status"] not in {"queued", "running"}:
                        break
                    time.sleep(.02)
                self.assertEqual(task["status"], "success", task)
                self.assertNotIn("logs", task)
                self.assertNotIn("command", task)
                logs = client.get(f"/api/tasks/{task_id}/logs").json()["items"]
                self.assertIn("normal output", str(logs))
                self.assertNotIn("fixture-secret", str(logs))
            media = library / "author" / "2026-01-01" / "photo.jpg"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"unchanged-media")
            with TestClient(app, base_url=base) as client:
                page = client.get("/api/tasks/page").json()
                self.assertEqual(page["items"][0]["id"], task_id)
                self.assertTrue(client.get(f"/api/tasks/{task_id}/logs").json()["items"])
                self.assertEqual(client.get("/media/app.db").status_code, 403)
                self.assertEqual(client.get("/media/author/2026-01-01/photo.jpg").content, b"unchanged-media")
                self.assertTrue(client.delete(f"/api/tasks/{task_id}/logs").json()["ok"])
                self.assertEqual(client.get(f"/api/tasks/{task_id}").status_code, 200)
                self.assertEqual(client.get(f"/api/tasks/{task_id}/logs").json()["items"], [])
                self.assertTrue(client.delete(f"/api/tasks/{task_id}").json()["ok"])
                self.assertEqual(media.read_bytes(), b"unchanged-media")
            script_registry.reload()
            media_library._lib_cache_mem = None
