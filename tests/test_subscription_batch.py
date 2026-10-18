import asyncio
from contextlib import ExitStack
import unittest
from unittest.mock import AsyncMock, patch

from app import config, subscription_batch as batch, watcher


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.subs = [dict(id=i, enabled=i != 5) for i in range(6)]
        self.stack.enter_context(patch.object(watcher, 'list_subs', side_effect=lambda: self.subs))
        self.stack.enter_context(patch.object(watcher, 'get_sub', side_effect=lambda id: next((s for s in self.subs if s['id'] == id), None)))
        self.stack.enter_context(patch.object(batch, 'publish', new=AsyncMock()))
        self.stack.enter_context(patch.object(config, 'MAX_SCAN_CONCURRENCY', 2))
        self.stack.enter_context(patch.object(config, 'MAINTENANCE_ACTIVE', False))

    async def asyncTearDown(self):
        await batch.stop()

    async def test_bounded_deduplicated_refresh_skips_paused_and_keeps_other_results(self):
        gate = asyncio.Event()
        active = 0
        peak = 0
        async def scan(sub):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await gate.wait()
                return {'new_items': [sub['id']] if sub['id'] == 0 else [],
                        'error': 'fixture failure' if sub['id'] == 2 else None,
                        'already_running': sub['id'] == 3}
            finally:
                active -= 1
        dispatch = AsyncMock()
        with patch.object(watcher, 'scan_sub', side_effect=scan) as scanner:
            started = batch.start(dispatch)
            self.assertEqual(started['total'], 5)
            self.assertEqual(started['paused'], 1)
            owner = batch._task
            self.assertTrue(batch.start(dispatch)['already_running'])
            self.assertIs(batch._task, owner)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(peak, 2)
            self.subs[4]['enabled'] = False
            gate.set()
            await owner
            self.assertEqual(scanner.await_count, 4)
        result = batch.snapshot()
        self.assertEqual((result['status'], result['completed'], result['failed'], result['skipped']), ('partial', 5, 1, 2))
        self.assertEqual(result['running_ids'], [])
        dispatch.assert_awaited_once_with([0])

    async def test_shutdown_cancels_workers_and_does_not_start_remaining_subscriptions(self):
        entered = asyncio.Event()
        finished = []
        async def scan(sub):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(sub['id'])
        with patch.object(watcher, 'scan_sub', side_effect=scan) as scanner:
            batch.start(AsyncMock())
            await entered.wait()
            await batch.stop()
            self.assertEqual(scanner.await_count, 2)
            self.assertEqual(len(finished), 2)
            self.assertEqual(batch.snapshot()['status'], 'interrupted')
            self.assertEqual(batch.snapshot()['running_ids'], [])

    async def test_maintenance_rejects_without_scanning(self):
        with patch.object(config, 'MAINTENANCE_ACTIVE', True), patch.object(watcher, 'scan_sub') as scan:
            with self.assertRaises(ValueError):
                batch.start(AsyncMock())
            scan.assert_not_called()
