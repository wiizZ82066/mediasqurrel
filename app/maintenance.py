"""Confirmed, resumable index/copy/backup/restore plans.

Plans and checkpoints live in the application database. Source media and source
databases are never overwritten or removed. Restores prepare a separate data
directory; switching the running application is deliberately a separate action.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import uuid

from . import catalog, config, db
from .redaction import redact_text, redact_value


SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance_plans (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
 confirmation_token TEXT NOT NULL, source_path TEXT NOT NULL,
 destination TEXT, root_id TEXT, source_info_json TEXT NOT NULL,
 summary_json TEXT NOT NULL, result_json TEXT, error TEXT,
 cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS maintenance_files (
 plan_id TEXT NOT NULL REFERENCES maintenance_plans(id), ordinal INTEGER NOT NULL,
 source_path TEXT NOT NULL, relative_path TEXT NOT NULL, action TEXT NOT NULL,
 size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, expected_sha256 TEXT,
 status TEXT NOT NULL DEFAULT 'pending', copied_bytes INTEGER NOT NULL DEFAULT 0,
 sha256 TEXT, PRIMARY KEY(plan_id,ordinal), UNIQUE(plan_id,relative_path)
);
CREATE INDEX IF NOT EXISTS maintenance_file_status ON maintenance_files(plan_id,status,ordinal);
"""

SETTINGS_KEYS = {"default_download_dir", "download_concurrency", "timeout_seconds", "retries",
                 "cache_limit_mb", "scan_defaults", "notifications", "theme", "density",
                 "reduce_motion", "preview"}
_MARKER = ".mediasquirrel-plan.json"
_BACKUP_MANIFEST = "backup.json"
_RESERVE = 16 * 1024 * 1024
_locks_guard = threading.Lock()
_locks = {}
_cancel_events = {}


class PlanChangedError(ValueError):
    """The reviewed source or destination changed; a new plan is required."""


def _current_version():
    return max((version for version, _name, _schema in db._migrations()), default=0)


@contextlib.contextmanager
def _readonly(path):
    with contextlib.closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema=OFF")
        yield connection


def _database_info(path):
    if not Path(path).is_file():
        raise FileNotFoundError("源数据库不存在")
    with _readonly(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > _current_version():
            raise ValueError("源数据库版本高于当前程序，未修改源数据库")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("源数据库完整性检查失败")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("源数据库外键检查失败")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables & {"subscriptions", "tasks", "media_roots"}:
            raise ValueError("源文件不是受支持的 Media Squirrel 数据库")
        allowed = {"schema_migrations", "sqlite_sequence"}
        for _version, _name, schema in db._migrations():
            allowed.update(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)?\s+([a-zA-Z_][a-zA-Z_0-9]*)", schema, re.I))
        if tables - allowed:
            raise ValueError("源数据库包含未识别的数据表，不能作为默认无凭据备份")
        archives = []
        if "task_log_archives" in tables:
            archives = [dict(row) for row in connection.execute("SELECT path,bytes FROM task_log_archives ORDER BY path")]
    return {"schema_version": version, "archives": archives}


def _safe(root, relative):
    value = Path(relative)
    if value.is_absolute() or value.drive or str(relative).startswith(("/", "\\")):
        raise ValueError("工作清单路径必须是相对路径")
    result = (root / value).resolve()
    if result == root or not result.is_relative_to(root):
        raise ValueError("工作清单路径越界")
    return result


def _available(path):
    parent = Path(path).resolve()
    while not parent.exists():
        parent = parent.parent
    return shutil.disk_usage(parent).free


def _file(path, relative, action="copy", expected_sha256=None):
    path = Path(path).resolve()
    stat = path.stat()
    if not path.is_file():
        raise ValueError("工作清单包含非文件路径")
    return {"source_path": str(path), "relative_path": relative.replace("\\", "/"), "action": action,
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "expected_sha256": expected_sha256}


def _recognized_files(root):
    for _author, folder in catalog._folders(root):
        for item in catalog._inventory(folder):
            relative = (folder.relative_to(root) / item["path"]).as_posix()
            yield _file(_safe(root, relative), relative)


def _fingerprint(items):
    digest = hashlib.sha256()
    for item in items:
        digest.update(json.dumps([item["relative_path"], item["size"], item["mtime_ns"]], ensure_ascii=False).encode())
    return digest.hexdigest()


def _destination_review(destination, source, *, allow_child=False):
    if destination is None:
        return {"conflicts": [], "conflict_count": 0}
    target, source = Path(destination).resolve(), Path(source).resolve()
    if target == source or (target.is_relative_to(source) and not allow_child) or source.is_relative_to(target):
        raise ValueError("源目录和目标目录不能重叠")
    conflicts = []
    count = 0
    if target.exists():
        if not target.is_dir():
            raise ValueError("目标不是目录")
        for child in target.iterdir():
            count += 1
            if len(conflicts) < 50:
                conflicts.append(child.name)
    return {"conflicts": conflicts, "conflict_count": count}


def _create_plan(kind, source, destination, files, info, *, root_id=None, database=None):
    identity, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
    source = str(Path(source).resolve())
    destination = str(Path(destination).resolve()) if destination is not None else None
    review = _destination_review(destination, source, allow_child=kind == "backup")
    count, total, fingerprint = 0, 0, hashlib.sha256()
    with db.connect(database) as connection:
        connection.execute("INSERT INTO maintenance_plans(id,kind,status,confirmation_token,source_path,destination,root_id,source_info_json,summary_json,created_at) "
                           "VALUES(?,?,'planned',?,?,?,?,?,'{}',?)",
                           (identity, kind, token, source, destination, root_id, json.dumps(info), db.utc_now()))
        for count, item in enumerate(files, 1):
            _safe(Path(destination or source), item["relative_path"])
            total += item["size"]
            fingerprint.update(json.dumps([item["relative_path"], item["size"], item["mtime_ns"]], ensure_ascii=False).encode())
            connection.execute("INSERT INTO maintenance_files(plan_id,ordinal,source_path,relative_path,action,size,mtime_ns,expected_sha256) VALUES(?,?,?,?,?,?,?,?)",
                               (identity, count, item["source_path"], item["relative_path"], item["action"], item["size"], item["mtime_ns"], item.get("expected_sha256")))
        info["inventory_signature"] = fingerprint.hexdigest()
        required = total + _RESERVE if destination else 0
        if kind in ("backup", "upgrade_copy"):
            required += Path(info["source_database"]).stat().st_size
        summary = {"files": count, "bytes": total, "required_free_bytes": required,
                   "available_bytes": _available(destination) if destination else None,
                   "missing": info.get("missing", []), "missing_count": info.get("missing_count", 0), "mode": kind,
                   "source_preserved": True, "merges_databases": False, **review}
        connection.execute("UPDATE maintenance_plans SET source_info_json=?,summary_json=? WHERE id=?",
                           (json.dumps(info), json.dumps(summary), identity))
    return get_plan(identity, database=database)


def get_plan(plan_id, *, database=None, include_internal=False):
    with db.connect(database) as connection:
        row = connection.execute("SELECT * FROM maintenance_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError("维护计划不存在")
        value = dict(row)
        for key in ("summary", "result", "source_info"):
            raw = value.pop(key + "_json")
            value[key] = json.loads(raw) if raw else None
        counts = connection.execute("SELECT count(*) AS files,coalesce(sum(copied_bytes),0) AS bytes FROM maintenance_files WHERE plan_id=? AND status='copied'", (plan_id,)).fetchone()
        value["progress"] = {"completed_files": counts["files"], "completed_bytes": counts["bytes"],
                             "total_files": value["summary"]["files"], "total_bytes": value["summary"]["bytes"]}
    if not include_internal:
        value.pop("source_info", None)
    return value


def list_plans(*, limit=30, database=None):
    with db.connect(database) as connection:
        identities = [row[0] for row in connection.execute("SELECT id FROM maintenance_plans ORDER BY created_at DESC,id DESC LIMIT ?", (max(1, min(100, limit)),))]
    return [get_plan(identity, database=database) for identity in identities]


def list_plan_files(plan_id, *, page=1, page_size=100, database=None):
    page, size = max(1, int(page)), max(1, min(200, int(page_size)))
    with db.connect(database) as connection:
        rows = connection.execute("SELECT ordinal,relative_path,action,size,status,copied_bytes,sha256 FROM maintenance_files WHERE plan_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
                                  (plan_id, size, (page - 1) * size)).fetchall()
    return {"items": [dict(row) for row in rows], "page": page, "page_size": size}


def preflight_index(source_path, *, database=None):
    return catalog.preflight_root(source_path)


def plan_index(source_path, *, database=None):
    root = Path(source_path).resolve()
    preflight = catalog.preflight_root(root)
    return _create_plan("index_only", root, None, _recognized_files(root), {"preflight": preflight}, database=database)


def plan_copy(root_id, destination, *, database=None):
    root = catalog.get_root(root_id, database=database)
    source = Path(root["path"]).resolve()
    if not source.is_dir():
        raise FileNotFoundError("媒体源目录不可用")
    missing, missing_count = [], 0
    with db.connect(database) as connection:
        rows = connection.execute("SELECT e.rel_dir,f.rel_path FROM media_entry_files f JOIN media_entries e ON f.entry_id=e.id WHERE e.root_id=? AND e.availability='present'", (root_id,))
        for row in rows:
            relative = row["rel_dir"] + "/" + row["rel_path"]
            if not _safe(source, relative).is_file():
                missing_count += 1
                if len(missing) < 50:
                    missing.append(relative)
    return _create_plan("copy", source, destination, _recognized_files(source),
                        {"root_path_key": root["path_key"], "missing": missing, "missing_count": missing_count},
                        root_id=root_id, database=database)


def _backup_files(source_database, source_data_dir, info):
    yield _file(source_database, "app.db", "database_snapshot")
    for item in info["archives"]:
        relative = item["path"]
        if not relative.endswith(".jsonl.gz"):
            raise ValueError("数据库引用的日志归档类型不受支持")
        source = _safe(source_data_dir / "logs", relative)
        if not source.is_file():
            raise FileNotFoundError("数据库引用的压缩日志缺失，无法创建完整备份")
        if source.stat().st_size != item["bytes"]:
            raise ValueError("日志归档大小与数据库记录不符")
        yield _file(source, "logs/" + relative)
    settings = source_data_dir / "settings.json"
    if settings.is_file():
        yield _file(settings, "settings.json", "settings")


def plan_backup(destination, *, source_database=None, source_data_dir=None,
                include_media=False, root_ids=None, database=None):
    if include_media or root_ids:
        raise ValueError("备份包不包含媒体原件；请使用独立的媒体复制计划")
    source_database = Path(source_database or config.DB_PATH).resolve()
    source_data_dir = Path(source_data_dir or source_database.parent).resolve()
    info = _database_info(source_database)
    info.update(source_database=str(source_database), source_data_dir=str(source_data_dir))
    return _create_plan("backup", source_data_dir, destination,
                        _backup_files(source_database, source_data_dir, info), info, database=database)


def _backup_manifest(root):
    with open(root / _BACKUP_MANIFEST, encoding="utf-8") as stream:
        value = json.load(stream)
    if value.get("format_version") != 1 or value.get("kind") != "media-squirrel-backup":
        raise ValueError("备份包格式不受支持")
    files = value.get("files")
    if not isinstance(files, list) or not any(item.get("path") == "app.db" for item in files):
        raise ValueError("备份包缺少数据库清单")
    seen = set()
    for item in files:
        relative = item["path"]
        _safe(root, relative)
        if relative in seen or not (relative in ("app.db", "settings.json") or relative.startswith("logs/") and relative.endswith(".jsonl.gz")):
            raise ValueError("备份包包含重复或不受支持的文件")
        seen.add(relative)
    return value


def plan_restore(backup_dir, new_data_dir, *, source_kind="backup", database=None):
    if source_kind != "backup":
        raise ValueError("旧数据库升级请使用独立的副本升级计划")
    source = Path(backup_dir).resolve()
    manifest = _backup_manifest(source)
    info = _database_info(source / "app.db")
    if info["schema_version"] != manifest["db_schema_version"]:
        raise ValueError("备份数据库版本与清单不符")
    declared = {item["path"] for item in manifest["files"]}
    if any("logs/" + item["path"] not in declared for item in info["archives"]):
        raise ValueError("备份清单遗漏数据库引用的压缩日志")
    files = []
    for item in manifest["files"]:
        path = _safe(source, item["path"])
        if not path.is_file() or path.stat().st_size != item["size"] or _hash_file(path) != item["sha256"]:
            raise ValueError("备份包文件缺失或校验失败")
        files.append(_file(path, item["path"], expected_sha256=item["sha256"]))
    info.update(backup_manifest_sha256=_hash_file(source / _BACKUP_MANIFEST))
    return _create_plan("restore", source, new_data_dir, files, info, database=database)


def plan_upgrade_copy(source_database, new_data_dir, *, database=None):
    source = Path(source_database).resolve()
    info = _database_info(source)
    info.update(source_database=str(source), source_data_dir=str(source.parent))
    return _create_plan("upgrade_copy", source.parent, new_data_dir,
                        _backup_files(source, source.parent, info), info, database=database)


def cancel_plan(plan_id, *, database=None):
    with db.connect(database) as connection:
        connection.execute("UPDATE maintenance_plans SET cancel_requested=1 WHERE id=? AND status NOT IN ('complete')", (plan_id,))
    with _locks_guard:
        if plan_id in _cancel_events:
            _cancel_events[plan_id].set()
    return get_plan(plan_id, database=database)


def recover_interrupted(*, database=None):
    """Called on application startup; never starts or resumes work implicitly."""
    with db.connect(database) as connection:
        return connection.execute("UPDATE maintenance_plans SET status='interrupted',error=?,finished_at=? WHERE status='running'",
                                  ("程序退出前维护尚未完成；确认后可继续", db.utc_now())).rowcount


def _hash_file(path, cancelled=None):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if cancelled and cancelled():
                raise InterruptedError("维护操作已取消，原文件保留")
            digest.update(chunk)
    return digest.hexdigest()


def _plan_rows(plan_id, *, database=None):
    after = 0
    while True:
        with db.connect(database) as connection:
            rows = connection.execute("SELECT * FROM maintenance_files WHERE plan_id=? AND ordinal>? ORDER BY ordinal LIMIT 100", (plan_id, after)).fetchall()
        if not rows:
            return
        for row in rows:
            after = row["ordinal"]
            yield dict(row)


def _validate_source(plan, *, database=None):
    kind, source = plan["kind"], Path(plan["source_path"]).resolve()
    if os.path.normcase(str(source)) != os.path.normcase(plan["source_path"]):
        raise PlanChangedError("源目录链接已改变，请重新预检确认")
    info = plan["source_info"]
    if kind in ("copy", "index_only"):
        if _fingerprint(_recognized_files(source)) != info["inventory_signature"]:
            raise PlanChangedError("媒体源清单已改变，请重新预检确认")
        if kind == "copy" and catalog.get_root(plan["root_id"], database=database)["path_key"] != info["root_path_key"]:
            raise PlanChangedError("媒体根已改变，请重新预检确认")
    elif kind in ("backup", "upgrade_copy"):
        current = _database_info(info["source_database"])
        if current["schema_version"] != info["schema_version"] or current["archives"] != info["archives"]:
            raise PlanChangedError("数据库版本或日志引用已改变，请重新预检确认")
    elif kind == "restore" and _hash_file(source / _BACKUP_MANIFEST) != info["backup_manifest_sha256"]:
        raise PlanChangedError("备份清单已改变，请重新预检确认")
    for item in _plan_rows(plan["id"], database=database):
        if item["action"] == "database_snapshot":
            continue  # A live database is captured using SQLite's backup API.
        source_file = Path(item["source_path"])
        if not source_file.is_file():
            raise PlanChangedError("源文件缺失，请重新预检确认")
        stat = source_file.stat()
        if stat.st_size != item["size"] or stat.st_mtime_ns != item["mtime_ns"]:
            raise PlanChangedError("源文件已改变，请重新预检确认")


def _write_new(path, data):
    """Exclusive creation, including the plan marker and final package manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".ms-plan-", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _install_new(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _install_new(temporary, destination):
    # Windows rename is exclusive and works on NTFS as well as exFAT/FAT.
    # POSIX rename would replace an existing file, so use an exclusive hardlink.
    try:
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
    except FileExistsError as exc:
        raise PlanChangedError("目标文件在复制期间出现冲突，未覆盖") from exc


def _prepare_destination(plan):
    target = Path(plan["destination"]).resolve()
    if os.path.normcase(str(target)) != os.path.normcase(plan["destination"]):
        raise PlanChangedError("目标目录链接已改变，请重新预检确认")
    marker = target / _MARKER
    if marker.is_file():
        with open(marker, encoding="utf-8") as stream:
            value = json.load(stream)
        if value != {"plan_id": plan["id"], "kind": plan["kind"]}:
            raise PlanChangedError("目标属于其他计划，未覆盖任何文件")
    else:
        if target.exists() and any(target.iterdir()):
            raise PlanChangedError("目标目录不再为空，未覆盖任何文件")
        target.mkdir(parents=True, exist_ok=True)
        _write_new(marker, json.dumps({"plan_id": plan["id"], "kind": plan["kind"]}).encode())
    return target


def _save_checkpoint(plan_id, item, size, digest, *, database=None):
    with db.connect(database) as connection:
        connection.execute("UPDATE maintenance_files SET status='copied',copied_bytes=?,sha256=? WHERE plan_id=? AND ordinal=?",
                           (size, digest, plan_id, item["ordinal"]))


def _copy_one(plan, item, target, cancelled, *, database=None):
    source, destination = Path(item["source_path"]), _safe(target, item["relative_path"])
    if source.resolve() != source:
        raise PlanChangedError("源文件链接已改变，未复制")
    if destination.exists():
        # A crash after installing a file but before its DB checkpoint can be
        # recovered only by comparing bytes; no unrelated target is overwritten.
        if item["status"] in ("copied", "prepared") and item["sha256"]:
            digest = _hash_file(destination, cancelled)
            if destination.stat().st_size != item["copied_bytes"] or digest != item["sha256"]:
                raise PlanChangedError("已复制的目标文件已改变，未覆盖")
            if item["status"] == "prepared":
                _save_checkpoint(plan["id"], item, item["copied_bytes"], digest, database=database)
            return
        if item["action"] == "copy" and destination.is_file() and destination.stat().st_size == item["size"]:
            digest = _hash_file(source, cancelled)
            if _hash_file(destination, cancelled) == digest:
                _save_checkpoint(plan["id"], item, item["size"], digest, database=database)
                return
        raise PlanChangedError("目标文件冲突，未覆盖任何文件")
    if _available(target) < item["size"] + _RESERVE:
        raise OSError("可用空间不足，工作进度已保留")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".ms-copy-", suffix=".partial", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        if item["action"] == "database_snapshot":
            temporary.unlink()  # backup_database requires a fresh filename.
            db.backup_database(temporary, source=source)
            snapshot = _database_info(temporary)
            if snapshot["schema_version"] != plan["source_info"]["schema_version"] or snapshot["archives"] != plan["source_info"]["archives"]:
                raise PlanChangedError("数据库日志在备份期间改变，请重新预检")
        elif item["action"] == "settings":
            with open(source, encoding="utf-8") as stream:
                settings = json.load(stream)
            if not isinstance(settings, dict):
                raise ValueError("设置文件不是对象")
            safe = redact_value({key: value for key, value in settings.items() if key in SETTINGS_KEYS})
            temporary.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            digest = hashlib.sha256()
            with open(source, "rb") as src, open(temporary, "wb") as output:
                while chunk := src.read(1024 * 1024):
                    if cancelled():
                        raise InterruptedError("维护操作已取消，原文件保留")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            stat = source.stat()
            if stat.st_size != item["size"] or stat.st_mtime_ns != item["mtime_ns"]:
                raise PlanChangedError("复制期间源文件改变，未发布目标文件")
            if item["expected_sha256"] and digest.hexdigest() != item["expected_sha256"]:
                raise PlanChangedError("源文件与备份校验值不符")
            if _hash_file(temporary, cancelled) != digest.hexdigest():
                raise OSError("复制文件校验失败")
        if cancelled():
            raise InterruptedError("维护操作已取消，原文件保留")
        if item["action"] != "database_snapshot":
            stat = source.stat()
            if source.resolve() != source or stat.st_size != item["size"] or stat.st_mtime_ns != item["mtime_ns"]:
                raise PlanChangedError("处理期间源文件改变，未发布目标文件")
        size, digest = temporary.stat().st_size, _hash_file(temporary, cancelled)
        with open(temporary, "r+b") as stream:
            os.fsync(stream.fileno())
        # Persist the verified digest before publishing, so a crash immediately
        # after rename can resume even for a database snapshot or safe settings.
        with db.connect(database) as connection:
            connection.execute("UPDATE maintenance_files SET status='prepared',copied_bytes=?,sha256=? WHERE plan_id=? AND ordinal=?",
                               (size, digest, plan["id"], item["ordinal"]))
        _install_new(temporary, destination)
        _save_checkpoint(plan["id"], item, size, digest, database=database)
    finally:
        temporary.unlink(missing_ok=True)


def _complete_backup(plan, target, *, database=None):
    files = [{"path": item["relative_path"], "size": item["copied_bytes"], "sha256": item["sha256"]}
             for item in _plan_rows(plan["id"], database=database)]
    manifest = {"format_version": 1, "kind": "media-squirrel-backup", "created_at": db.utc_now(),
                "db_schema_version": plan["source_info"]["schema_version"], "files": files,
                "includes_media": False, "includes_credentials": False}
    path = target / _BACKUP_MANIFEST
    if path.exists():
        existing = _backup_manifest(target)
        if existing["files"] != files:
            raise PlanChangedError("目标备份清单冲突")
    else:
        _write_new(path, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())


def execute_plan(plan_id, confirmation_token, *, cancel=None, progress=None, database=None):
    plan = get_plan(plan_id, database=database, include_internal=True)
    if not secrets.compare_digest(str(confirmation_token), plan["confirmation_token"]):
        raise PermissionError("必须确认对应预检计划后才能执行")
    if plan["status"] == "complete":
        return get_plan(plan_id, database=database)
    if plan["summary"]["conflict_count"]:
        raise PlanChangedError("预检发现目标冲突，请选择空目录后重新生成计划")
    if plan["summary"]["missing_count"]:
        raise PlanChangedError("预检发现源文件缺失，请先核对媒体索引并重新预检")
    with _locks_guard:
        lock = _locks.setdefault(plan_id, threading.Lock())
        event = _cancel_events.setdefault(plan_id, threading.Event())
    if not lock.acquire(blocking=False):
        raise RuntimeError("该维护计划正在执行")
    event.clear()
    cancelled = lambda: event.is_set() or bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))
    try:
        try:
            _validate_source(plan, database=database)
        except (PlanChangedError, OSError, ValueError):
            with db.connect(database) as connection:
                connection.execute("UPDATE maintenance_plans SET status='needs_replan',error=?,finished_at=? WHERE id=?",
                                   ("源数据或清单已改变，请重新预检确认", db.utc_now(), plan_id))
            return get_plan(plan_id, database=database)
        with db.connect(database) as connection:
            connection.execute("UPDATE maintenance_plans SET status='running',cancel_requested=0,error=NULL,started_at=coalesce(started_at,?),finished_at=NULL WHERE id=?",
                               (db.utc_now(), plan_id))
        try:
            if cancelled():
                raise InterruptedError("维护操作已取消")
            if plan["kind"] == "index_only":
                root = catalog.register_root(plan["source_path"], database=database)
                scan = catalog.scan_root(root["id"], cancel=cancelled, progress=progress, database=database)
                if scan["status"] == "cancelled":
                    raise InterruptedError("索引已取消，已提交的索引批次可恢复")
                if scan["status"] != "success":
                    raise RuntimeError("索引未完成，原媒体未修改")
                with db.connect(database) as connection:
                    connection.execute("UPDATE maintenance_files SET status='copied',copied_bytes=size WHERE plan_id=?", (plan_id,))
                result = {"root_id": root["id"], "scan_id": scan["id"], "mode": "index_only"}
            else:
                remaining = max(0, plan["summary"]["bytes"] - plan["progress"]["completed_bytes"])
                if _available(plan["destination"]) < remaining + _RESERVE:
                    raise OSError("可用空间不足，未继续复制")
                target = _prepare_destination(plan)
                for item in _plan_rows(plan_id, database=database):
                    if cancelled():
                        raise InterruptedError("维护操作已取消，原文件保留")
                    _copy_one(plan, item, target, cancelled, database=database)
                    if progress:
                        progress(get_plan(plan_id, database=database))
                if plan["kind"] == "backup":
                    _complete_backup(plan, target, database=database)
                    result = {"backup_dir": str(target), "manifest": str(target / _BACKUP_MANIFEST), "includes_credentials": False}
                elif plan["kind"] in ("restore", "upgrade_copy"):
                    # No application connection points at this prepared DB.
                    db.migrate(target / "app.db")
                    _database_info(target / "app.db")
                    with db.connect(database) as connection:
                        connection.execute("UPDATE maintenance_files SET copied_bytes=?,sha256=? WHERE plan_id=? AND relative_path='app.db'",
                                           ((target / "app.db").stat().st_size, _hash_file(target / "app.db"), plan_id))
                    result = {"prepared_data_dir": str(target), "requires_restart": True,
                              "merges_databases": False, "media_root_mapping_required": True}
                else:
                    result = {"prepared_library_root": str(target), "root_id": plan["root_id"],
                              "requires_rebind": True, "source_preserved": True}
            status, error = "complete", None
        except InterruptedError as exc:
            status, error, result = "cancelled", str(exc), None
        except PlanChangedError as exc:
            status, error, result = "needs_replan", str(exc), None
        except Exception as exc:
            reason = redact_text(str(exc), diagnostic=True)[:500]
            status, error, result = "failed", f"{type(exc).__name__}: {reason}；原数据保留", None
        with db.connect(database) as connection:
            connection.execute("UPDATE maintenance_plans SET status=?,error=?,result_json=?,finished_at=? WHERE id=?",
                               (status, error, json.dumps(result) if result else None, db.utc_now(), plan_id))
        value = get_plan(plan_id, database=database)
        if progress:
            progress(value)
        return value
    finally:
        lock.release()
