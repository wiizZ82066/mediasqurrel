"""Login windows require an explicit POST; status is local and credential-free."""
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from app.main import app


class WeiboLoginApiTests(unittest.TestCase):
    def setUp(self):
        # No context manager: avoid the production database/scheduler lifespan.
        self.client = TestClient(app, base_url="http://127.0.0.1:8642")
        self.addCleanup(self.client.close)

    def test_status_does_not_open_window_and_post_is_explicit(self):
        with patch("app.weibo_auth.auth_status", return_value={"logged_in": False, "cookie_count": 0}), \
                patch("app.weibo_auth.login_async", new_callable=AsyncMock, return_value={"ok": True, "logged_in": True}) as login:
            response = self.client.get("/api/subs/weibo-auth")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["logged_in"])
            login.assert_not_awaited()
            response = self.client.post("/api/subs/login-weibo", json={})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])
            login.assert_awaited_once()

    def test_cross_origin_cannot_open_login_window(self):
        with patch("app.weibo_auth.login_async", new_callable=AsyncMock) as login:
            response = self.client.post("/api/subs/login-weibo", json={}, headers={"Origin": "https://unrelated.invalid"})
            self.assertEqual(response.status_code, 403)
            login.assert_not_awaited()

    def test_login_failure_is_actionable_and_redacted(self):
        with patch("app.weibo_auth.login_async", new_callable=AsyncMock, side_effect=RuntimeError("微博操作超时 token=fixture-secret")):
            response = self.client.post("/api/subs/login-weibo", json={})
            self.assertEqual(response.status_code, 502)
            self.assertIn("微博操作超时", response.json()["detail"])
            self.assertNotIn("fixture-secret", response.text)
