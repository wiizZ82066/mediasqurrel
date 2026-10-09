"""Migration/backup rehearsals use synthetic media and independent databases."""
import contextlib
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import catalog, db, maintenance


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mediasquirrel-maintenance-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.control = self.base / "control.db"
        with contextlib.closing(sqlite3.connect(self.control)) as connection:
            connection.executescript(catalog.SCHEMA + maintenance.SCHEMA)
            connection.commit()
        self.media = self.base / "source-media"
        folder = self.media / "Example" / "26-10-09"
        (folder / "photo").mkdir(parents=True)
        (folder / "photo" / "img01.jpg").write_bytes(b"synthetic media")
        (folder / "context.md").write_text("# Example\n", encoding="utf-8")
        (self.media / "cookies.json").write_text('{"private": "must stay behind"}', encoding="utf-8")
        (self.media / "program.py").write_text("# not media", encoding="utf-8")
        root = catalog.register_root(self.media, database=self.control)
        self.root_id = root["id"]
        catalog.scan_root(self.root_id, database=self.control)

    def execute(self, plan, **kwargs):
        return maintenance.execute_plan(plan["id"], plan["confirmation_token"], database=self.control, **kwargs)

    def app_database(self):
        directory = self.base / "source-data"
        directory.mkdir(exist_ok=True)
        source = directory / "app.db"
        db.migrate(source)
        log = directory / "logs" / "testtask" / "000000000001-000000000001.jsonl.gz"
        log.parent.mkdir(parents=True)
        with gzip.open(log, "wt", encoding="utf-8") as stream:
            stream.write('{"seq": 1, "text": "example log"}\n')
        with db.connect(source) as connection:
            connection.execute("INSERT INTO task_log_archives VALUES(?,?,?,?,?,?)",
                               ("testtask", 1, 1, "testtask/" + log.name, 1, log.stat().st_size))
        (directory / "settings.json").write_text(json.dumps({"theme": "dark", "notifications": {"enabled": True},
                                                              "cookie": "must-not-copy", "profile_path": "must-not-copy"}), encoding="utf-8")
        (directory / "cookies.json").write_text("private", encoding="utf-8")
        (directory / "browser-profile").mkdir()
        (directory / "browser-profile" / "private.txt").write_text("private", encoding="utf-8")
        return directory, source

    def test_token_confirmation_and_media_whitelist_preserve_originals(self):
        target = self.base / "copied-media"
        plan = maintenance.plan_copy(self.root_id, target, database=self.control)
        self.assertEqual(plan["summary"]["files"], 2)
        self.assertFalse(target.exists())
        with self.assertRaises(PermissionError):
            maintenance.execute_plan(plan["id"], "wrong-token", database=self.control)
        result = self.execute(plan)
        self.assertEqual(result["status"], "complete", result.get("error"))
        self.assertEqual(result["result"]["prepared_library_root"], str(target.resolve()))
        self.assertFalse((target / "cookies.json").exists())
        self.assertFalse((target / "program.py").exists())
        self.assertEqual((target / "Example/26-10-09/photo/img01.jpg").read_bytes(), b"synthetic media")
        self.assertEqual(catalog.get_root(self.root_id, database=self.control)["path"], str(self.media.resolve()))
        self.assertTrue((self.media / "Example/26-10-09/photo/img01.jpg").is_file())

    def test_copy_cancel_and_resume_uses_persisted_checkpoint(self):
        plan = maintenance.plan_copy(self.root_id, self.base / "resume-media", database=self.control)
        event = threading.Event()
        cancelled = self.execute(plan, cancel=event, progress=lambda _state: event.set())
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["progress"]["completed_files"], 1)
        resumed = self.execute(plan)
        self.assertEqual(resumed["status"], "complete", resumed.get("error"))
        self.assertEqual(resumed["progress"]["completed_files"], 2)

    def test_resume_after_file_publication_before_final_checkpoint(self):
        plan = maintenance.plan_copy(self.root_id, self.base / "crash-target", database=self.control)
        with patch.object(maintenance, "_save_checkpoint", side_effect=RuntimeError("simulated process stop")):
            result = self.execute(plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(maintenance.list_plan_files(plan["id"], database=self.control)["items"][0]["status"], "prepared")
        self.assertEqual(self.execute(plan)["status"], "complete")

    def test_source_or_destination_change_requires_new_preflight(self):
        plan = maintenance.plan_copy(self.root_id, self.base / "changed-target", database=self.control)
        image = self.media / "Example/26-10-09/photo/img01.jpg"
        image.write_bytes(b"source has changed")
        self.assertEqual(self.execute(plan)["status"], "needs_replan")
        fresh = maintenance.plan_copy(self.root_id, self.base / "fresh-target", database=self.control)
        Path(fresh["destination"]).mkdir()
        untouched = Path(fresh["destination"]) / "existing.txt"
        untouched.write_text("keep", encoding="utf-8")
        self.assertEqual(self.execute(fresh)["status"], "needs_replan")
        self.assertEqual(untouched.read_text(encoding="utf-8"), "keep")

    def test_existing_target_conflicts_are_bounded_and_never_overwritten(self):
        target = self.base / "occupied"
        target.mkdir()
        for index in range(60):
            (target / f"file{index}").write_bytes(b"keep")
        plan = maintenance.plan_copy(self.root_id, target, database=self.control)
        self.assertEqual(plan["summary"]["conflict_count"], 60)
        self.assertEqual(len(plan["summary"]["conflicts"]), 50)
        with self.assertRaises(maintenance.PlanChangedError):
            self.execute(plan)
        self.assertEqual(len(list(target.iterdir())), 60)

    def test_missing_indexed_file_is_reported_in_preflight(self):
        (self.media / "Example/26-10-09/photo/img01.jpg").unlink()
        plan = maintenance.plan_copy(self.root_id, self.base / "missing-target", database=self.control)
        self.assertEqual(plan["summary"]["missing_count"], 1)
        with self.assertRaises(maintenance.PlanChangedError):
            self.execute(plan)

    def test_insufficient_disk_space_keeps_source_and_can_resume_after_space_available(self):
        plan = maintenance.plan_copy(self.root_id, self.base / "space-target", database=self.control)
        with patch.object(maintenance, "_available", return_value=0):
            failed = self.execute(plan)
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(Path(plan["destination"]).exists())
        self.assertEqual(self.execute(plan)["status"], "complete")

    def test_index_only_does_not_copy_move_or_modify_source(self):
        before = {str(p.relative_to(self.media)): p.read_bytes() for p in self.media.rglob("*") if p.is_file()}
        plan = maintenance.plan_index(self.media, database=self.control)
        self.assertIsNone(plan["destination"])
        self.assertEqual(self.execute(plan)["status"], "complete")
        after = {str(p.relative_to(self.media)): p.read_bytes() for p in self.media.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_backup_and_restore_consistent_database_referenced_logs_and_safe_settings(self):
        directory, source = self.app_database()
        backup = directory / "backups" / "manual"
        plan = maintenance.plan_backup(backup, source_database=source, source_data_dir=directory, database=self.control)
        result = self.execute(plan)
        self.assertEqual(result["status"], "complete", result.get("error"))
        self.assertTrue((backup / "app.db").is_file())
        settings = json.loads((backup / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings, {"theme": "dark", "notifications": {"enabled": True}})
        self.assertFalse((backup / "cookies.json").exists())
        self.assertFalse((backup / "browser-profile").exists())
        prepared = self.base / "restored-data"
        restore = maintenance.plan_restore(backup, prepared, database=self.control)
        restored = self.execute(restore)
        self.assertEqual(restored["status"], "complete", restored.get("error"))
        self.assertTrue(restored["result"]["requires_restart"])
        self.assertTrue(source.is_file())
        with db.connect(prepared / "app.db") as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("SELECT count(*) FROM task_log_archives").fetchone()[0], 1)
        self.assertEqual(len(list((prepared / "logs").rglob("*.jsonl.gz"))), 1)

    def test_backup_excludes_uncommitted_wal_transaction(self):
        directory, source = self.app_database()
        writer = sqlite3.connect(source)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO subscriptions(platform,blogger_id,nickname) VALUES('weibo','pending','uncommitted')")
        plan = maintenance.plan_backup(self.base / "wal-backup", source_database=source, source_data_dir=directory, database=self.control)
        result = self.execute(plan)
        self.assertEqual(result["status"], "complete", result.get("error"))
        with db.connect(Path(plan["destination"]) / "app.db") as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM subscriptions WHERE blogger_id='pending'").fetchone()[0], 0)
        writer.rollback()

    def test_restore_tampered_package_and_database_merge_are_rejected(self):
        directory, source = self.app_database()
        plan = maintenance.plan_backup(self.base / "package", source_database=source, source_data_dir=directory, database=self.control)
        self.assertEqual(self.execute(plan)["status"], "complete")
        backup = Path(plan["destination"])
        (backup / "settings.json").write_text('{"theme":"altered"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            maintenance.plan_restore(backup, self.base / "tampered-restore", database=self.control)
        occupied = self.base / "occupied-data"
        occupied.mkdir()
        (occupied / "app.db").write_bytes(b"do not overwrite")
        upgrade = maintenance.plan_upgrade_copy(source, occupied, database=self.control)
        with self.assertRaises(maintenance.PlanChangedError):
            self.execute(upgrade)
        self.assertEqual((occupied / "app.db").read_bytes(), b"do not overwrite")

    def test_future_database_version_and_media_in_backup_are_explicitly_rejected(self):
        directory, source = self.app_database()
        with self.assertRaises(ValueError):
            maintenance.plan_backup(self.base / "bad-backup", source_database=source, source_data_dir=directory,
                                    include_media=True, database=self.control)
        with db.connect(source) as connection:
            connection.execute("PRAGMA user_version=999")
        with self.assertRaises(ValueError):
            maintenance.plan_upgrade_copy(source, self.base / "future-data", database=self.control)

    def test_unrecognized_credential_table_is_not_silently_included_in_backup(self):
        directory, source = self.app_database()
        with db.connect(source) as connection:
            connection.execute("CREATE TABLE media_credentials (value TEXT)")
        with self.assertRaises(ValueError):
            maintenance.plan_backup(self.base / "unrecognized-backup", source_database=source,
                                    source_data_dir=directory, database=self.control)


if __name__ == "__main__":
    unittest.main()
