import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import task_manager as tm, watcher
from app.progress import DownloadProgress, PREFIX, parse_progress


class DownloadProgressTests(unittest.TestCase):
    def test_bytes_retries_unknown_length_and_opt_in(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"MS_PROGRESS": "1"}), contextlib.redirect_stdout(output):
            progress = DownloadProgress(2)
            progress.start_file("100")
            progress.advance(50)
            self.assertEqual(progress._percent(), 25)
            progress.start_file("100")  # retry resets only the current file
            self.assertEqual(progress._percent(), 0)
            progress.advance(100)
            progress.finish_file()
            progress.start_file(None)
            progress.advance(50)
            self.assertIsNone(progress._percent())
            progress.finish_file()
        events = [parse_progress(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[-1]["completed"], 2)
        self.assertEqual(events[-1]["percent"], 99)
        self.assertIsNone(events[-2]["percent"])
        with patch.dict(os.environ, {"MS_PROGRESS": "0"}), contextlib.redirect_stdout(io.StringIO()) as output:
            DownloadProgress(1).start_file(100)
        self.assertEqual(output.getvalue(), "")

    def test_malformed_events_do_not_become_progress(self):
        for payload in ('[]', '{', '{"label":"x","percent":NaN}',
                        '{"label":"x","percent":true}', '{"label":99}'):
            self.assertIsNone(parse_progress(PREFIX + payload))
        self.assertIsNone(parse_progress("ordinary log"))
        self.assertEqual(parse_progress(PREFIX + '{"label":"x","percent":100}')["percent"], 99)


class TaskProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tm.TASKS.clear()
        tm.TASK_ORDER.clear()
        tm._semaphore = None
        tm.release_update_lock()

    async def _run_child(self, exit_code):
        payload = PREFIX + json.dumps({"label": "下载中", "percent": 40})
        command = [sys.executable, "-u", "-c",
                   f"import os,sys; assert os.environ['MS_PROGRESS']=='1'; "
                   f"print({payload!r}); print('normal log'); sys.exit({exit_code})"]
        with patch.object(tm.script_registry, "build_command", return_value=command), \
                patch.object(tm, "_run", new=AsyncMock()):
            task = await tm.create("weibo", {"url": "https://example.com/post"})
        with patch.object(tm, "broadcast", new=AsyncMock()) as broadcast:
            await tm._run(task["id"])
        return tm.get_task(task["id"]), [call.args[0] for call in broadcast.call_args_list]

    async def test_real_subprocess_stream_and_success(self):
        task, events = await self._run_child(0)
        self.assertTrue(any(e["type"] == "task_progress" and e["progress"]["percent"] == 40 for e in events))
        self.assertEqual(task["progress"]["percent"], 100)
        self.assertEqual(task["status"], "success")
        # Command echo is intentionally a log; protocol output itself is not.
        self.assertEqual([line["text"] for line in task["logs"]][1:], ["normal log"])

    async def test_failed_process_retains_partial_progress(self):
        task, _ = await self._run_child(1)
        self.assertEqual(task["progress"]["percent"], 40)
        self.assertEqual(task["status"], "failed")

    async def test_cancelled_queue_is_not_complete(self):
        with patch.object(tm, "_run", new=AsyncMock()):
            task = await tm.create("weibo", {"url": "https://example.com/post"})
        await tm.cancel(task["id"])
        self.assertEqual(tm.get_task(task["id"])["progress"]["percent"], 0)
        self.assertEqual(tm.get_task(task["id"])["status"], "cancelled")
        await asyncio.sleep(0)

    def tearDown(self):
        tm.TASKS.clear()
        tm.TASK_ORDER.clear()
        tm._semaphore = None


class ScanProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db = patch.object(watcher.config, "DB_PATH", os.path.join(self.temp.name, "app.db"))
        db.start()
        self.addCleanup(db.stop)
        local = patch.object(watcher, "_local_item_ids", return_value=set())
        local.start()
        self.addCleanup(local.stop)
        watcher.SCAN_STATES.clear()
        watcher._SCANNING.clear()
        watcher.init_db()
        self.sub = watcher.add_sub("test", "sample", "示例博主")

    async def test_baseline_progress_and_refresh_snapshot(self):
        items = [{"item_id": "1"}, {"item_id": "2"}]
        with patch.dict(watcher.SCANNERS, test=AsyncMock(return_value=items)), \
                patch.object(tm, "broadcast", new=AsyncMock()) as broadcast:
            result = await watcher.scan_sub(self.sub)
        self.assertEqual(result["baseline"], 2)
        self.assertEqual(result["new_items"], [])
        states = [call.args[0]["scan"] for call in broadcast.call_args_list]
        self.assertIsNone(states[0]["progress"]["percent"])
        self.assertTrue(any(s["progress"]["percent"] == 50 for s in states))
        self.assertEqual(states[-1]["progress"]["percent"], 100)
        self.assertEqual(watcher.list_subs()[0]["scan"], states[-1])
        with patch.dict(watcher.SCANNERS, test=AsyncMock(return_value=[])):
            await watcher.scan_sub(self.sub)
        self.assertIn("暂无新内容", watcher.list_subs()[0]["scan"]["progress"]["label"])

    async def test_overlapping_manual_and_scheduled_scan_does_not_duplicate(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def scanner(_):
            started.set()
            await release.wait()
            return [{"item_id": "new"}]

        watcher.mark_seen("test", "sample", "old")
        with patch.dict(watcher.SCANNERS, test=scanner):
            running = asyncio.create_task(watcher.scan_sub(self.sub))
            await started.wait()
            self.assertEqual(watcher.list_subs()[0]["scan"]["status"], "running")
            duplicate = await watcher.scan_sub(self.sub)
            self.assertTrue(duplicate["already_running"])
            self.assertEqual(duplicate["new_items"], [])
            release.set()
            result = await running
        self.assertEqual(len(result["new_items"]), 1)
        self.assertNotIn(self.sub["id"], watcher._SCANNING)

    async def test_error_and_cancel_stop_progress_and_allow_retry(self):
        with patch.dict(watcher.SCANNERS, test=AsyncMock(side_effect=RuntimeError("network unavailable"))):
            result = await watcher.scan_sub(self.sub)
        self.assertEqual(result["error"], "network unavailable")
        self.assertEqual(watcher.list_subs()[0]["scan"]["status"], "failed")
        with patch.dict(watcher.SCANNERS, test=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await watcher.scan_sub(self.sub)
        self.assertEqual(watcher.list_subs()[0]["scan"]["status"], "cancelled")
        self.assertNotIn(self.sub["id"], watcher._SCANNING)


if __name__ == "__main__":
    unittest.main()
