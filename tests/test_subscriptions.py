"""Subscription state-machine regressions using isolated databases and files."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import config, db, subscription_store as storage, task_manager, watcher


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        for name, value in (("DB_PATH", str(self.root / "app.db")), ("LIBRARY_ROOT", str(self.library))):
            replacement = patch.object(config, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        broadcast = patch.object(task_manager, "broadcast", new=AsyncMock())
        broadcast.start()
        self.addCleanup(broadcast.stop)
        watcher._SCANNING.clear()
        watcher.SCAN_STATES.clear()
        watcher.init_db()
        self.sub = watcher.add_sub("weibo", "sample", "Example")

    def item(self, item_id, platform="weibo"):
        return {"item_id": item_id, "script_id": platform, "title": "Example post",
                "params": {"url": f"https://example.com/{item_id}"}}

    async def scan(self, *ids, sub=None):
        sub = sub or self.sub
        items = [self.item(item_id, sub["platform"]) for item_id in ids]
        with patch.dict(watcher.SCANNERS, {sub["platform"]: AsyncMock(return_value=items)}):
            return await watcher.scan_sub(sub)

    def state(self, item_id, platform="weibo", blogger_id="sample"):
        with db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM subscription_items WHERE platform=? AND blogger_id=? AND item_id=?",
                (platform, blogger_id, item_id),
            ).fetchone()
        return dict(row) if row else None

    def manifest(self, item_id, *, platform="weibo", status="complete", marked=True):
        folder = self.library / "Example" / f"2026-01-01_{platform}_{item_id}"
        folder.mkdir(parents=True, exist_ok=True)
        text = folder / "context.md"
        text.write_text("Example archived post", encoding="utf-8")
        if marked:
            marker = {"schema_version": 1, "status": status, "platform": platform,
                      "item_id": item_id, "files": [{"path": "context.md", "size": text.stat().st_size}]}
            (folder / "entry.json").write_text(json.dumps(marker), encoding="utf-8")
        return folder

    async def discover(self, item_id="new"):
        await self.scan("baseline")
        result = await self.scan("baseline", item_id)
        self.assertEqual([item["item_id"] for item in result["new_items"]], [item_id])

    def enqueue(self, item_id="new", task_id="task-one"):
        token = watcher.claim_item("weibo", "sample", item_id)
        self.assertIsNotNone(token)
        self.assertTrue(watcher.mark_enqueued("weibo", "sample", item_id, task_id, token))
        return token

    async def test_second_scan_and_restart_do_not_download_baseline(self):
        first = await self.scan("old", "old")
        self.assertEqual(first["baseline"], 1)
        self.assertEqual(first["new_items"], [])
        watcher.SCAN_STATES.clear()
        watcher.init_db()
        second = await self.scan("old")
        self.assertNotIn("baseline", second)
        self.assertEqual(second["new_items"], [])
        self.assertEqual(self.state("old")["status"], "baseline")

    async def test_empty_successful_baseline_does_not_swallow_first_new_post(self):
        first = await self.scan()
        self.assertEqual(first["baseline"], 0)
        result = await self.scan("new")
        self.assertEqual(len(result["new_items"]), 1)

    async def test_failed_first_scan_does_not_establish_baseline(self):
        with patch.dict(watcher.SCANNERS, weibo=AsyncMock(side_effect=RuntimeError("network unavailable"))):
            failure = await watcher.scan_sub(self.sub)
        self.assertIsNotNone(failure["error"])
        recovered = await self.scan("old")
        self.assertEqual(recovered["baseline"], 1)
        self.assertEqual(recovered["new_items"], [])

    async def test_atomic_discovery_failure_preserves_empty_baseline(self):
        bad = [self.item("old"), {"title": "missing identity"}]
        with patch.dict(watcher.SCANNERS, weibo=AsyncMock(return_value=bad)):
            result = await watcher.scan_sub(self.sub)
        self.assertIsNotNone(result["error"])
        self.assertIsNone(self.state("old"))
        self.assertEqual((await self.scan("old"))["baseline"], 1)

    async def test_active_claim_suppresses_cross_scan_and_concurrent_dispatch(self):
        await self.discover()
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda _: watcher.claim_item("weibo", "sample", "new"), range(8)))
        self.assertEqual(sum(token is not None for token in claims), 1)
        self.assertEqual((await self.scan("new"))["new_items"], [])
        token = next(token for token in claims if token)
        self.assertTrue(watcher.mark_enqueued("weibo", "sample", "new", "task-one", token))
        self.assertFalse(watcher.mark_enqueued("weibo", "sample", "new", "other", "wrong"))
        self.assertEqual((await self.scan("new"))["new_items"], [])

    async def test_failed_download_and_failed_dispatch_remain_retryable(self):
        await self.discover()
        token = watcher.claim_item("weibo", "sample", "new")
        watcher.mark_dispatch_failed("weibo", "sample", "new", "queue unavailable", token)
        self.assertEqual(self.state("new")["status"], "failed")
        self.assertEqual(len((await self.scan("new"))["new_items"]), 1)
        self.enqueue()
        watcher.record_task_result("task-one", "failed", "network unavailable")
        self.assertEqual(len((await self.scan("new"))["new_items"]), 1)

    async def test_success_requires_complete_manifest_and_survives_media_deletion(self):
        await self.discover()
        self.enqueue()
        watcher.record_task_result("task-one", "success")
        self.assertEqual(self.state("new")["status"], "failed")
        self.enqueue(task_id="task-two")
        folder = self.manifest("new")
        task = {"output_dir": str(folder), "metadata": {"subscription": {"platform": "weibo", "item_id": "new"}}}
        self.assertTrue(watcher.task_complete(task))
        watcher.record_task_result("task-two", "success", complete_verified=watcher.task_complete(task))
        (folder / "context.md").unlink()
        watcher.init_db()
        self.assertEqual((await self.scan("new"))["new_items"], [])
        self.assertEqual(self.state("new")["status"], "downloaded")

    async def test_failed_item_can_retry_after_it_leaves_platform_first_page(self):
        await self.discover()
        self.enqueue()
        watcher.record_task_result("task-one", "failed", "network unavailable")
        result = await self.scan()
        self.assertEqual([item["item_id"] for item in result["new_items"]], ["new"])
        self.assertEqual(result["new_items"][0]["params"], self.item("new")["params"])

    async def test_legacy_context_and_partial_manifest_are_not_download_success(self):
        await self.scan("baseline")
        self.manifest("legacy", marked=False)
        self.manifest("partial", status="partial")
        result = await self.scan("legacy", "partial")
        self.assertEqual({item["item_id"] for item in result["new_items"]}, {"legacy", "partial"})

    async def test_complete_text_post_is_valid_and_platform_identity_is_namespaced(self):
        await self.scan("baseline")
        self.manifest("same", platform="douyin")
        self.manifest("text", platform="weibo")
        result = await self.scan("same", "text")
        self.assertEqual([item["item_id"] for item in result["new_items"]], ["same"])
        self.assertEqual(self.state("text")["status"], "downloaded")

    async def test_indexed_completion_checks_only_candidates_and_deleted_files(self):
        from app import archive, catalog
        folder = self.manifest('wanted')
        self.manifest('unrelated')
        root = catalog.register_root(str(self.library))
        catalog.scan_root(root['id'])
        with patch.object(watcher, '_bounded_legacy_items', side_effect=AssertionError('unexpected legacy walk')), \
                patch.object(archive, 'entry_status', wraps=archive.entry_status) as verify:
            self.assertEqual(watcher._local_item_ids([self.item('wanted')], 'weibo'), {'weibo:wanted'})
            self.assertEqual(verify.call_count, 1)
        (folder / 'context.md').unlink()
        self.assertEqual(watcher._local_item_ids([self.item('wanted')], 'weibo'), set())

    async def test_unindexed_compatibility_is_cached_and_bounded(self):
        from app import archive
        for number in range(6):
            self.manifest(str(number))
        with patch.object(watcher, '_LOCAL_ENTRY_LIMIT', 3), \
                patch.object(archive, 'read_manifest', wraps=archive.read_manifest) as read:
            first = watcher._bounded_legacy_items()
            self.assertEqual(len(first), 3)
            self.assertEqual(read.call_count, 3)
            self.assertEqual(watcher._bounded_legacy_items(), first)
            self.assertEqual(read.call_count, 3)

    async def test_cancelled_download_only_retries_explicitly(self):
        await self.discover()
        self.enqueue()
        watcher.record_task_result("task-one", "cancelled")
        self.assertEqual((await self.scan("new"))["new_items"], [])
        self.assertTrue(watcher.link_retry("task-one", "task-two"))
        watcher.record_task_result("task-one", "failed", "late callback")
        self.assertEqual(self.state("new")["task_id"], "task-two")
        self.assertEqual(self.state("new")["status"], "queued")

    async def test_recovery_repairs_create_before_bind_crash_from_task_metadata(self):
        await self.discover()
        token = watcher.claim_item("weibo", "sample", "new")
        task = {"id": "restored", "status": "interrupted", "metadata": {"subscription": {
            "platform": "weibo", "blogger_id": "sample", "item_id": "new", "claim_token": token}}}
        watcher.reconcile_tasks(task for task in [task])
        self.assertEqual(self.state("new")["task_id"], "restored")
        self.assertEqual(self.state("new")["status"], "interrupted")
        self.assertEqual(len((await self.scan("new"))["new_items"]), 1)

    async def test_orphan_dispatch_is_retryable_after_restart(self):
        await self.discover()
        watcher.claim_item("weibo", "sample", "new")
        watcher.reconcile_tasks(iter(()))
        self.assertEqual(self.state("new")["status"], "interrupted")
        self.assertIsNotNone(watcher.claim_item("weibo", "sample", "new"))

    async def test_scan_history_survives_restart_and_marks_interrupted(self):
        await self.scan("baseline")
        run_id = storage.begin_scan(self.sub, "interval")
        storage.save_scan_snapshot(run_id, {"status": "running", "revision": 2,
                                            "progress": {"label": "working", "percent": 50}})
        watcher.init_db()
        latest = watcher.list_subs()[0]["scan"]
        self.assertEqual(latest["status"], "interrupted")
        self.assertEqual(latest["progress"]["percent"], 50)
        history = watcher.list_scans(self.sub["id"], limit=1)
        self.assertEqual(history[0]["status"], "interrupted")
        self.assertEqual(history[0]["trigger"], "interval")
        self.assertEqual(len(watcher.list_scans(self.sub["id"], limit=1, offset=1)), 1)

    async def test_scan_errors_are_redacted_in_live_and_persistent_records(self):
        sensitive = "fixture" + "-private-value"
        with patch.dict(watcher.SCANNERS, weibo=AsyncMock(side_effect=RuntimeError("Cookie: " + sensitive))):
            result = await watcher.scan_sub(self.sub)
        self.assertNotIn(sensitive, result["error"])
        self.assertNotIn(sensitive, watcher.list_scans()[0]["error"])
        self.assertNotIn(sensitive, watcher.list_subs()[0]["last_error"])

    async def test_weibo_profile_refresh_survives_list_failure_without_baseline(self):
        response = {'items': [], 'profile': {'blogger_id': 'sample', 'nickname': 'Platform Name',
                    'avatar_source': 'https://tvax1.sinaimg.cn/avatar.jpg'},
                    'error': '微博内容列表需要登录', 'coverage': {'complete': False, 'reason': 'login_required'}}
        with patch.dict(watcher.SCANNERS, weibo=AsyncMock(return_value=response)):
            result = await watcher.scan_sub(self.sub)
        self.assertIn('需要登录', result['error'])
        self.assertNotIn('baseline', result)
        with db.connect() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM subscription_state').fetchone()[0], 0)
        actual = watcher.list_subs()[0]
        self.assertEqual(actual['nickname'], 'Platform Name')
        self.assertEqual(actual['avatar_url'], f"/api/subs/{self.sub['id']}/avatar")
        self.assertEqual(watcher.list_scans(self.sub['id'])[0]['coverage']['reason'], 'login_required')

    async def test_wrong_profile_identity_or_placeholder_never_overwrites_name(self):
        for profile in ({'blogger_id': 'different', 'nickname': 'Wrong'},
                        {'blogger_id': 'sample', 'nickname': 'sample'},
                        {'blogger_id': 'sample', 'nickname': ''}):
            with patch.dict(watcher.SCANNERS, weibo=AsyncMock(return_value={'items': [], 'profile': profile})):
                await watcher.scan_sub(self.sub)
            self.assertEqual(watcher.list_subs()[0]['nickname'], 'Example')

    async def test_history_coverage_survives_restart_without_raw_snapshot(self):
        response = {'items': [], 'coverage': {'complete': False, 'reason': 'page_limit', 'pages': 5,
                    'items': 120, 'scope': 'overlap_window', 'private_internal': 'must not return'}}
        with patch.dict(watcher.SCANNERS, weibo=AsyncMock(return_value=response)):
            await watcher.scan_sub(self.sub)
        watcher.init_db()
        row = watcher.list_scans(self.sub['id'])[0]
        self.assertEqual(row['coverage']['reason'], 'page_limit')
        self.assertFalse(row['coverage']['complete'])
        self.assertNotIn('private_internal', row['coverage'])
        self.assertNotIn('snapshot_json', row)
        self.assertEqual(storage.public_coverage('x' * 16385), {})
        self.assertEqual(storage.public_coverage('[]'), {})

    async def test_overlapping_manual_and_interval_share_one_scan(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def scanner(_):
            started.set()
            await release.wait()
            return [self.item("baseline")]

        with patch.dict(watcher.SCANNERS, weibo=scanner):
            running = asyncio.create_task(watcher.scan_sub(self.sub, trigger="interval"))
            await started.wait()
            duplicate = await watcher.scan_sub(self.sub)
            self.assertTrue(duplicate["already_running"])
            release.set()
            await running
        self.assertEqual(len(watcher.list_scans()), 1)

    async def test_scheduler_shutdown_awaits_cancelled_scan_cleanup(self):
        started = asyncio.Event()

        async def scanner(_):
            started.set()
            await asyncio.Event().wait()

        with patch.dict(watcher.SCANNERS, weibo=scanner):
            watcher.start_scheduler()
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
            finally:
                await watcher.stop_scheduler()
        self.assertIsNone(watcher._loop_task)
        self.assertFalse(watcher._SCANNING)
        self.assertEqual(watcher.list_scans()[0]["status"], "cancelled")


class LegacySubscriptionMigrationTests(unittest.TestCase):
    def test_legacy_seen_is_not_falsely_claimed_as_downloaded(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "legacy.db")
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("CREATE TABLE seen_items(platform TEXT,blogger_id TEXT,item_id TEXT,created_at TEXT,"
                             "PRIMARY KEY(platform,blogger_id,item_id))")
                conn.execute("INSERT INTO seen_items VALUES ('weibo','sample','old','2025-01-01T00:00:00')")
            with patch.object(config, "DB_PATH", path):
                watcher.init_db()
                sub = watcher.add_sub("weibo", "sample")
                first, candidates = storage.record_discoveries(sub, [{"item_id": "old"}, {"item_id": "new"}])
                self.assertFalse(first)
                self.assertEqual([item["item_id"] for item in candidates], ["new"])
                with db.connect() as conn:
                    row = conn.execute("SELECT status FROM subscription_items WHERE item_id='old'").fetchone()
                self.assertEqual(row["status"], "legacy_seen")


if __name__ == "__main__":
    unittest.main()
