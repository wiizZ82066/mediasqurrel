import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from app import task_manager as tm


class UpdateGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tm.TASKS.clear()
        tm.TASK_ORDER.clear()
        tm.release_update_lock()

    async def test_queue_and_lock_are_atomic_in_both_orders(self):
        self.assertTrue(tm.update_state(acquire=True)['locked'])
        with self.assertRaisesRegex(ValueError, '更新'):
            await tm.create('weibo', {'url': 'https://weibo.com/test'})
        tm.release_update_lock()
        with patch.object(tm, '_run', new=AsyncMock()):
            await tm.create('weibo', {'url': 'https://weibo.com/test'})
        self.assertEqual(tm.update_state(acquire=True), {'active': 1, 'locked': False})
        await asyncio.sleep(0)

    async def test_active_task_cannot_be_evicted_by_history_limit(self):
        with patch.object(tm, '_run', new=AsyncMock()):
            for _ in range(201):
                await tm.create('weibo', {'url': 'https://weibo.com/test'})
        self.assertEqual(tm.update_state(acquire=True)['active'], 201)
        await asyncio.sleep(0)

    async def test_lost_request_lock_expires(self):
        with patch.object(tm.time, 'monotonic', return_value=100):
            tm.update_state(acquire=True)
        with patch.object(tm.time, 'monotonic', return_value=131):
            self.assertFalse(tm.update_state()['locked'])

    async def test_internal_route_requires_per_launch_token(self):
        from app.main import api_update_lock
        from fastapi import HTTPException
        from starlette.requests import Request
        with patch.dict(os.environ, {'MS_DESKTOP_TOKEN': 'test-token'}):
            request = Request({'type': 'http', 'headers': []})
            with self.assertRaises(HTTPException) as error:
                await api_update_lock(request, {'action': 'acquire'})
            self.assertEqual(error.exception.status_code, 403)
            request = Request({'type': 'http', 'headers': [(b'x-desktop-token', b'test-token')]})
            self.assertTrue((await api_update_lock(request, {'action': 'acquire'}))['locked'])

    def tearDown(self):
        tm.release_update_lock()
        tm.TASKS.clear()
        tm.TASK_ORDER.clear()
