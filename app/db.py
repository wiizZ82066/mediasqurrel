"""Versioned SQLite transactions and consistent, non-destructive backups."""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import uuid

from . import config

_migration_lock = threading.RLock()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def connect(path=None):
    target = os.fspath(path or config.DB_PATH)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _statements(script):
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        yield statement


def backup_database(destination, *, source=None):
    """SQLite backup includes committed WAL data; never overwrite a backup."""
    source = Path(source or config.DB_PATH).resolve()
    destination = Path(destination)
    if not source.is_file():
        raise FileNotFoundError("数据库不存在")
    if destination.exists():
        raise FileExistsError("备份目标已存在")
    destination.parent.mkdir(parents=True, exist_ok=True)
    required = source.stat().st_size * 2 + 16 * 1024 * 1024
    if shutil.disk_usage(destination.parent).free < required:
        raise OSError("可用空间不足，未开始数据库备份")
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".partial")
    try:
        src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(temporary)
            try:
                src.backup(dst)
                if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("备份完整性检查失败")
            finally:
                dst.close()
        finally:
            src.close()
        if destination.exists():
            raise FileExistsError("备份目标已存在")
        temporary.rename(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return str(destination)


def _migrations():
    from .subscription_store import SCHEMA as subscriptions
    from .task_store import SCHEMA as tasks
    from .catalog import SCHEMA as catalog
    from .maintenance import SCHEMA as maintenance
    from .scheduling import SCHEMA as schedules
    from .path_transition import SCHEMA as paths
    return [(1, "subscription-history", subscriptions), (2, "task-history", tasks),
            (3, "media-catalog", catalog), (4, "data-maintenance", maintenance),
            (5, "subscription-schedules", schedules), (6, "restart-path-transitions", paths)]


def migrate(path=None, *, migrations=None):
    target = Path(path or config.DB_PATH)
    steps = _migrations() if migrations is None else migrations
    latest = max((version for version, _name, _script in steps), default=0)
    with _migration_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        with connect(target) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > latest:
            raise RuntimeError("数据库版本高于当前程序；请使用更新版本，旧数据库未被修改")
        if version == latest:
            return version
        if target.stat().st_size:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_database(target.parent / "backups" / f"schema-v{version}-{stamp}.db", source=target)
        with connect(target) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
            for number, name, script in steps:
                if number <= version:
                    continue
                for statement in _statements(script):
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (number, name, utc_now()))
                connection.execute(f"PRAGMA user_version={int(number)}")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("数据库迁移外键检查失败，已回滚")
        return latest
