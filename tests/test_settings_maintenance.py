"""Path activation rehearsals use only synthetic, isolated data directories."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import catalog, config, db, maintenance, path_transition, settings_api, subscription_store


class PathTransitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mediasquirrel-path-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.data = self.base / "active-data"
        self.data.mkdir()
        self.database = self.data / "app.db"
        db.migrate(self.database)
        self.media = self.base / "old-media"
        self.entry = self.media / "Example" / "26-10-09"
        (self.entry / "photo").mkdir(parents=True)
        (self.entry / "photo/img01.jpg").write_bytes(b"synthetic media")
        (self.entry / "context.md").write_text("# Example\n", encoding="utf-8")
        self.root_id = catalog.register_root(self.media, database=self.database)["id"]
        catalog.scan_root(self.root_id, database=self.database)
        self.paths_file = self.base / "paths.json"
        self.old_paths = (json.dumps({"data_dir": str(self.data), "library_root": str(self.media)},
                                     indent=2) + "\r\n").encode()
        self.paths_file.write_bytes(self.old_paths)
        (self.data / "settings.json").write_text(json.dumps({"theme": "dark", "default_download_dir": str(self.entry)}), encoding="utf-8")
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(config, DB_PATH=str(self.database), DATA_DIR=str(self.data),
                                               LIBRARY_ROOT=str(self.media), PATHS_FILE=str(self.paths_file), MAINTENANCE_ACTIVE=False))
        self.stack.enter_context(patch.dict(os.environ, {key: "" for key in path_transition._OVERRIDES}))
        self.stack.enter_context(patch.object(settings_api, "_paths_pending", False))
        self.stack.enter_context(patch.object(settings_api, "_work", {}))
        self.stack.enter_context(patch.object(settings_api.task_manager, "_store", None))
        self.stack.enter_context(patch.object(settings_api.task_manager, "_archive_jobs", set()))
        self.stack.enter_context(patch.object(settings_api.task_manager, "update_state", return_value={"active": 0}))
        self.stack.enter_context(patch.object(settings_api.watcher, "has_active_scans", return_value=False))
        self.stack.enter_context(patch.object(settings_api.library_service, "_scans", {}))
        self.stack.enter_context(patch.object(settings_api.media_jobs, "jobs", None))

    def prepare(self, plan):
        captured = path_transition.capture_sources(plan["id"], database=self.database)
        result = maintenance.execute_plan(plan["id"], plan["confirmation_token"], database=self.database)
        self.assertEqual(result["status"], "complete", result.get("error"))
        return path_transition.seal_plan(plan["id"], captured, database=self.database)

    def library_copy(self):
        return self.prepare(maintenance.plan_copy(self.root_id, self.base / "new-media", database=self.database))

    def data_copy(self, source=None):
        return self.prepare(maintenance.plan_upgrade_copy(source or self.database, self.base / "new-data", database=self.database))

    def add_task(self, identity, output=None):
        with db.connect(self.database) as connection:
            connection.execute("INSERT INTO tasks(id,script_id,script_name,params_json,status,progress_json,created_at,output_dir) VALUES(?,?,?,?,?,?,?,?)",
                               (identity, "weibo", "Example", "{}", "completed", "{}", db.utc_now(), str(output or self.entry)))

    def current_root(self, database=None):
        return catalog.get_root(self.root_id, database=database or self.database)["path"]

    async def test_library_only_waits_for_restart_and_preserves_exact_config_backup(self):
        self.add_task("in-root")
        self.add_task("different-root", self.base / "unrelated-media/Example/26-10-09")
        plan = self.library_copy()
        response = await settings_api.apply_paths({"confirmed": True, "library_plan_id": plan["id"]})
        self.assertTrue(response["restart_required"])
        self.assertTrue(config.MAINTENANCE_ACTIVE)
        self.assertEqual(self.current_root(), str(self.media))
        staged = json.loads(self.paths_file.read_text(encoding="utf-8"))
        self.assertEqual(Path(staged["pending_transition"]["previous_paths_backup"]).read_bytes(), self.old_paths)
        path_transition.apply_pending_startup(paths_file=self.paths_file, database=self.database)
        self.assertEqual(self.current_root(), str(self.base / "new-media"))
        self.assertNotIn("pending_transition", json.loads(self.paths_file.read_text(encoding="utf-8")))
        self.assertEqual(json.loads((self.data / "settings.json").read_text(encoding="utf-8"))["default_download_dir"], "")
        with db.connect(self.database) as connection:
            self.assertEqual([row[0] for row in connection.execute("SELECT task_id FROM task_media")], ["in-root"])
        self.assertTrue((self.entry / "photo/img01.jpg").exists())

    async def test_config_write_failure_leaves_current_config_and_database_untouched(self):
        plan = self.library_copy()
        before = path_transition.business_snapshot(self.database)
        with patch.object(path_transition, "atomic_write_text", side_effect=OSError("simulated full disk")):
            with self.assertRaises(HTTPException):
                await settings_api.apply_paths({"confirmed": True, "library_plan_id": plan["id"]})
        self.assertFalse(config.MAINTENANCE_ACTIVE)
        self.assertEqual(self.paths_file.read_bytes(), self.old_paths)
        self.assertEqual(path_transition.business_snapshot(self.database), before)

    async def test_task_backfill_uses_deepest_entry_within_exact_root(self):
        relative = "Collection/Example/26-10-09"
        self.add_task("nested-entry", self.media / relative)
        self.add_task("nested-asset", self.media / relative / "photo/img01.jpg")
        self.add_task("other-root", self.base / "unrelated-media" / relative)
        mapping = {"root_id": self.root_id, "source_path": str(self.media),
                   "target_path": str(self.base / "nested-target")}
        with db.connect(self.database) as connection:
            connection.execute("UPDATE media_entries SET rel_dir=? WHERE root_id=?", (relative, self.root_id))
            path_transition._mapping(connection, mapping, update=True)
            linked = [row[0] for row in connection.execute("SELECT task_id FROM task_media ORDER BY task_id")]
        self.assertEqual(linked, ["nested-asset", "nested-entry"])

    async def test_new_business_rows_reject_stale_data_copy(self):
        plan = self.data_copy()
        self.add_task("created-after-copy")
        before = path_transition.business_snapshot(self.database)
        with self.assertRaises(HTTPException) as raised:
            await settings_api.apply_paths({"confirmed": True, "data_plan_id": plan["id"]})
        self.assertIn("重新预检", raised.exception.detail)
        self.assertEqual(self.paths_file.read_bytes(), self.old_paths)
        self.assertEqual(path_transition.business_snapshot(self.database), before)

    async def test_old_schema_copy_gets_explicit_root_mapping_without_mutating_originals(self):
        old = self.base / "legacy-data/app.db"
        db.migrate(old, migrations=[(1, "subscriptions", subscription_store.SCHEMA)])
        with db.connect(old) as connection:
            connection.execute("INSERT INTO subscriptions(platform,blogger_id,nickname) VALUES('weibo','123','Example')")
        old_snapshot = path_transition.business_snapshot(old)
        library_plan = self.library_copy()
        data_plan = self.data_copy(old)
        active_before = path_transition.business_snapshot(self.database)
        await settings_api.apply_paths({"confirmed": True, "library_plan_id": library_plan["id"], "data_plan_id": data_plan["id"]})
        prepared_db = self.base / "new-data/app.db"
        self.assertEqual(catalog.list_roots(database=prepared_db), [])
        path_transition.apply_pending_startup(paths_file=self.paths_file, database=prepared_db)
        self.assertEqual(self.current_root(prepared_db), str(self.base / "new-media"))
        self.assertEqual(path_transition.business_snapshot(old), old_snapshot)
        self.assertEqual(path_transition.business_snapshot(self.database), active_before)
        with db.connect(prepared_db) as connection:
            self.assertEqual(connection.execute("SELECT nickname FROM subscriptions").fetchone()[0], "Example")

    async def test_environment_override_rejects_ineffective_switch(self):
        plan = self.library_copy()
        with patch.dict(os.environ, {"MS_LIBRARY_DIR": str(self.media)}):
            with self.assertRaises(HTTPException) as raised:
                await settings_api.apply_paths({"confirmed": True, "library_plan_id": plan["id"]})
        self.assertIn("MS_LIBRARY_DIR", raised.exception.detail)
        self.assertEqual(self.paths_file.read_bytes(), self.old_paths)
        self.assertEqual(self.current_root(), str(self.media))

    async def test_new_rows_between_confirmation_and_restart_preserve_pending_and_old_root(self):
        plan = self.library_copy()
        await settings_api.apply_paths({"confirmed": True, "library_plan_id": plan["id"]})
        self.add_task("external-change-before-restart")
        with self.assertRaisesRegex(ValueError, "重新预检"):
            path_transition.apply_pending_startup(paths_file=self.paths_file, database=self.database)
        self.assertEqual(self.current_root(), str(self.media))
        self.assertIn("pending_transition", json.loads(self.paths_file.read_text(encoding="utf-8")))
        with db.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM path_transitions").fetchone()[0], 0)

    async def test_committed_rebind_with_failed_final_config_write_recovers_idempotently(self):
        plan = self.library_copy()
        await settings_api.apply_paths({"confirmed": True, "library_plan_id": plan["id"]})
        real_write = path_transition.atomic_write_text
        def fail_paths(path, text):
            if Path(path) == self.paths_file:
                raise OSError("simulated final config failure")
            return real_write(path, text)
        with patch.object(path_transition, "atomic_write_text", side_effect=fail_paths):
            with self.assertRaises(OSError):
                path_transition.apply_pending_startup(paths_file=self.paths_file, database=self.database)
        self.assertEqual(self.current_root(), str(self.base / "new-media"))
        self.assertIn("pending_transition", json.loads(self.paths_file.read_text(encoding="utf-8")))
        path_transition.apply_pending_startup(paths_file=self.paths_file, database=self.database)
        self.assertNotIn("pending_transition", json.loads(self.paths_file.read_text(encoding="utf-8")))
        with db.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM path_transitions").fetchone()[0], 1)

    async def test_changed_prepared_settings_rejected_without_any_path_write(self):
        plan = self.data_copy()
        (self.base / "new-data/settings.json").write_text('{"theme":"changed"}', encoding="utf-8")
        with self.assertRaises(HTTPException):
            await settings_api.apply_paths({"confirmed": True, "data_plan_id": plan["id"]})
        self.assertEqual(self.paths_file.read_bytes(), self.old_paths)
        self.assertEqual(self.current_root(), str(self.media))

    async def test_other_source_writer_blocks_startup_without_target_changes(self):
        plan = self.data_copy()
        await settings_api.apply_paths({"confirmed": True, "data_plan_id": plan["id"]})
        target = self.base / "new-data/app.db"
        before = path_transition.business_snapshot(target)
        with db.connect(self.database) as source:
            source.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                path_transition.apply_pending_startup(paths_file=self.paths_file, database=target)
        self.assertEqual(path_transition.business_snapshot(target), before)
        self.assertIn("pending_transition", json.loads(self.paths_file.read_text(encoding="utf-8")))
        path_transition.apply_pending_startup(paths_file=self.paths_file, database=target)
        self.assertNotIn("pending_transition", json.loads(self.paths_file.read_text(encoding="utf-8")))

    async def test_execute_preparation_exception_becomes_durable_failure(self):
        plan = maintenance.plan_copy(self.root_id, self.base / "worker-copy", database=self.database)
        with patch.object(path_transition, "capture_sources", side_effect=ValueError("capture failed Cookie: secret-value")):
            await settings_api.execute(plan["id"], {"confirmation_token": plan["confirmation_token"]})
            worker = settings_api._work[plan["id"]][0]
            await worker
        saved = maintenance.get_plan(plan["id"], database=self.database)
        self.assertEqual(saved["status"], "failed")
        self.assertTrue(saved["finished_at"])
        self.assertNotIn("secret-value", saved["error"])
        self.assertFalse(config.MAINTENANCE_ACTIVE)

    async def test_conflict_preflight_rejected_before_background_worker(self):
        target = self.base / "occupied"
        target.mkdir()
        (target / "keep.txt").write_text("keep", encoding="utf-8")
        plan = maintenance.plan_copy(self.root_id, target, database=self.database)
        with self.assertRaises(HTTPException) as raised:
            await settings_api.execute(plan["id"], {"confirmation_token": plan["confirmation_token"]})
        self.assertEqual(raised.exception.status_code, 409)
        self.assertFalse(settings_api._work)
        self.assertFalse(config.MAINTENANCE_ACTIVE)


if __name__ == "__main__":
    unittest.main()
