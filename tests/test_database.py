from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from app import db


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "store.db"
        self.first = (1, "initial", "CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT);\n")

    def test_failed_migration_rolls_back_schema_data_and_version(self):
        db.migrate(self.path, migrations=[self.first])
        with db.connect(self.path) as connection:
            connection.execute("INSERT INTO entries VALUES (1,'kept')")
        failing = (2, "failing", "ALTER TABLE entries ADD COLUMN extra TEXT;\nINSERT INTO missing VALUES (1);\n")
        with self.assertRaises(sqlite3.Error):
            db.migrate(self.path, migrations=[self.first, failing])
        with db.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT value FROM entries").fetchone()[0], "kept")
            self.assertEqual([row[1] for row in connection.execute("PRAGMA table_info(entries)")], ["id", "value"])
        self.assertTrue(list(self.path.parent.glob("backups/schema-v1-*.db")))

    def test_backup_includes_wal_and_refuses_overwrite(self):
        db.migrate(self.path, migrations=[self.first])
        with db.connect(self.path) as connection:
            connection.execute("INSERT INTO entries VALUES (1,'committed')")
            connection.commit()
            backup = self.path.parent / "backup.db"
            db.backup_database(backup, source=self.path)
            with closing(sqlite3.connect(backup)) as saved:
                self.assertEqual(saved.execute("SELECT value FROM entries").fetchone()[0], "committed")
            with self.assertRaises(FileExistsError):
                db.backup_database(backup, source=self.path)

    def test_future_schema_rejected_and_repeated_migrate_is_idempotent(self):
        db.migrate(self.path, migrations=[self.first])
        db.migrate(self.path, migrations=[self.first])
        with db.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)
            connection.execute("PRAGMA user_version=99")
        with self.assertRaises(RuntimeError):
            db.migrate(self.path, migrations=[self.first])
