"""Two-phase path activation: review now, change catalog references at restart.

Business snapshots reject stale prepared databases. A small SQLite journal makes
the database commit and subsequent paths-file update recoverable without ever
rewriting the live catalog from the settings request.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from . import catalog, config, db, maintenance
from .archive import atomic_write_text


SCHEMA = """
CREATE TABLE IF NOT EXISTS path_transitions (
 id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
 post_snapshot_json TEXT NOT NULL, applied_at TEXT NOT NULL
);
"""

_REBUILDABLE = {"schema_migrations", "sqlite_sequence", "maintenance_plans", "maintenance_files",
                "path_transitions", "media_scans", "media_entries", "media_assets", "media_entry_files"}
_VOLATILE_COLUMNS = {"media_roots": {"generation", "last_scan_at"}}
_OVERRIDES = ("MS_DATA_DIR", "MS_APP_DATA_DIR", "MS_LIBRARY_DIR")


def reject_environment_overrides():
    active = [key for key in _OVERRIDES if os.environ.get(key)]
    if active:
        raise ValueError("环境变量覆盖路径配置，请先移除覆盖并重启：" + ", ".join(active))


def _canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=lambda item: {"bytes_hex": item.hex()} if isinstance(item, bytes) else str(item))


def _snapshot_connection(connection, spec=None):
    if spec is None:
        spec = {}
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in tables:
            if table in _REBUILDABLE:
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")
                       if row[1] not in _VOLATILE_COLUMNS.get(table, set())]
            if columns:
                spec[table] = columns
    digest = hashlib.sha256()
    for table, columns in sorted(spec.items()):
        quoted = '"' + table.replace('"', '""') + '"'
        fields = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
        digest.update(_json([table, columns]).encode())
        cursor = connection.execute(f"SELECT {fields} FROM {quoted} ORDER BY {fields}")
        while rows := cursor.fetchmany(500):
            for row in rows:
                digest.update(_json(list(row)).encode())
                digest.update(b"\n")
    return {"tables": spec, "sha256": digest.hexdigest()}


def business_snapshot(database, *, spec=None):
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError("需校验的源数据库不存在，未切换路径")
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        return _snapshot_connection(connection, spec)


def _file_digest(path):
    path = Path(path)
    if not path.exists():
        return None
    if path.resolve() != path:
        raise ValueError("设置文件链接已改变，未切换路径")
    return maintenance._hash_file(path)


def _guard(database):
    path = Path(database).resolve()
    return {"database": str(path), "snapshot": business_snapshot(path),
            "settings_path": str(path.parent / "settings.json"),
            "settings_sha256": _file_digest(path.parent / "settings.json")}


def _check_guard(value, *, settings=True):
    path = Path(value["database"])
    if path.resolve() != path:
        raise ValueError("数据库路径链接已改变，请重新预检")
    current = business_snapshot(path, spec=value["snapshot"]["tables"])
    if current["sha256"] != value["snapshot"]["sha256"]:
        raise ValueError("准备副本后已有新的任务、日志、订阅或其他业务变更，请重新预检并准备副本")
    if settings and _file_digest(Path(value["settings_path"])) != value["settings_sha256"]:
        raise ValueError("准备副本后设置已改变，请重新预检并准备副本")


@contextlib.contextmanager
def _lock_source_databases(request, target):
    """Hold write reservations without changing source rows during activation.

    The current application is stopped at this point. Reservations also reject
    concurrent writers from another instance instead of losing their last rows
    between the freshness check and committing the prepared database.
    """
    paths = sorted({str(Path(guard["database"])) for guard in request["sources"]
                    if _canonical(guard["database"]) != _canonical(target)})
    with contextlib.ExitStack() as stack:
        for source in paths:
            connection = stack.enter_context(contextlib.closing(sqlite3.connect(
                Path(source).as_uri() + "?mode=rw", uri=True, timeout=1)))
            connection.execute("BEGIN IMMEDIATE")
        yield


def capture_sources(plan_id, *, database=None):
    """Capture while the app is quiescent, immediately before executing a plan."""
    active = Path(database or config.DB_PATH).resolve()
    plan = maintenance.get_plan(plan_id, database=database, include_internal=True)
    guards = [_guard(active)]
    if plan["kind"] == "upgrade_copy":
        source = Path(plan["source_info"]["source_database"]).resolve()
    elif plan["kind"] == "restore":
        source = Path(plan["source_path"]) / "app.db"
    else:
        source = active
    if _canonical(source) != _canonical(active):
        guards.append(_guard(source))
    return {"version": 1, "sources": guards}


def seal_plan(plan_id, captured, *, database=None):
    """Record snapshots after a successful preparation, without changing sources."""
    plan = maintenance.get_plan(plan_id, database=database)
    if plan["status"] != "complete" or plan["kind"] not in ("restore", "upgrade_copy", "copy"):
        return plan
    result = dict(plan["result"])
    try:
        for guard in captured["sources"]:
            _check_guard(guard)
        guard = dict(captured)
        if result.get("prepared_data_dir"):
            guard["prepared"] = _guard(Path(result["prepared_data_dir"]) / "app.db")
        result["apply_guard"] = guard
    except (OSError, ValueError, sqlite3.Error) as error:
        result["apply_guard"] = {"version": 1, "invalid": str(error)}
    with db.connect(database) as connection:
        connection.execute("UPDATE maintenance_plans SET result_json=? WHERE id=?", (_json(result), plan_id))
    return maintenance.get_plan(plan_id, database=database)


def _check_library_plan(plan, *, database=None):
    internal = maintenance.get_plan(plan["id"], database=database, include_internal=True)
    maintenance._validate_source(internal, database=database)
    target = Path(plan["result"]["prepared_library_root"])
    if target.resolve() != target or not target.is_dir():
        raise ValueError("已准备的媒体目标不存在或链接已改变")
    for item in maintenance._plan_rows(plan["id"], database=database):
        file = maintenance._safe(target, item["relative_path"])
        if item["status"] != "copied" or not file.is_file() or file.stat().st_size != item["copied_bytes"] or maintenance._hash_file(file) != item["sha256"]:
            raise ValueError("准备好的媒体文件已改变，请重新核验复制计划")
    return internal


def _check_prepared_files(plan, *, database=None, skip_settings=False):
    target = Path(plan["result"]["prepared_data_dir"]).resolve()
    for item in maintenance._plan_rows(plan["id"], database=database):
        # SQLite layout can change during a legitimate schema migration; its
        # business rows are checked separately using the captured column set.
        if item["relative_path"] == "app.db" or skip_settings and item["relative_path"] == "settings.json":
            continue
        path = maintenance._safe(target, item["relative_path"])
        if item["status"] != "copied" or not path.is_file() or path.stat().st_size != item["copied_bytes"] or maintenance._hash_file(path) != item["sha256"]:
            raise ValueError("准备副本中的日志或设置文件已改变，请重新预检")


def _mapping(connection, mapping, *, update=False):
    source_key, target_key = _canonical(mapping["source_path"]), _canonical(mapping["target_path"])
    rows = [dict(row) for row in connection.execute("SELECT * FROM media_roots")]
    by_id = next((row for row in rows if row["id"] == mapping["root_id"]), None)
    if by_id and by_id["path_key"] not in (source_key, target_key):
        raise ValueError("准备数据库中的根标识属于另一个路径，不能覆盖")
    matches = [row for row in rows if row["path_key"] == source_key]
    if len(matches) > 1:
        raise ValueError("准备数据库中的媒体根映射不明确")
    selected = by_id or (matches[0] if matches else None)
    identity = selected["id"] if selected else mapping["root_id"]
    target = Path(mapping["target_path"]).resolve()
    for row in rows:
        if row["id"] == identity:
            continue
        other = Path(row["path"]).resolve()
        if target.is_relative_to(other) or other.is_relative_to(target):
            raise ValueError("新媒体根与准备数据库中另一个媒体根重叠")
    if update:
        # Historical tasks may never have opened their media preview. Resolve
        # only absolute outputs under this exact old root before its path moves.
        source_root = Path(mapping["source_path"]).resolve()
        tasks = connection.execute("SELECT id,output_dir FROM tasks WHERE output_dir IS NOT NULL AND id NOT IN (SELECT task_id FROM task_media)")
        links = []
        for task in tasks:
            output = Path(task["output_dir"])
            if not output.is_absolute():
                continue
            output = output.resolve()
            if not output.is_relative_to(source_root):
                continue
            parts = output.relative_to(source_root).parts
            if len(parts) < 2:
                continue
            entry = None
            for length in range(len(parts), 1, -1):
                entry = connection.execute("SELECT id FROM media_entries WHERE root_id=? AND rel_dir=?",
                                           (identity, "/".join(parts[:length]))).fetchone()
                if entry:
                    break
            if entry:
                links.append((task["id"], entry["id"], identity))
        connection.executemany("INSERT OR IGNORE INTO task_media(task_id,entry_id,root_id) VALUES(?,?,?)", links)
        if selected:
            connection.execute("UPDATE media_roots SET path=?,path_key=? WHERE id=?", (str(target), target_key, identity))
        else:
            connection.execute("INSERT INTO media_roots(id,path,path_key,label,created_at) VALUES(?,?,?,?,?)",
                               (identity, str(target), target_key, "迁移媒体库", db.utc_now()))
    return identity


def prepare_transition(data_plan, library_plan, *, database=None):
    """Read-only validation; return a pending request, never mutate a catalog."""
    reject_environment_overrides()
    active = Path(database or config.DB_PATH).resolve()
    data_dir, library_root = Path(config.DATA_DIR).resolve(), Path(config.LIBRARY_ROOT).resolve()
    sources = [_guard(active)]
    if data_plan:
        guard = (data_plan.get("result") or {}).get("apply_guard")
        if not guard or guard.get("version") != 1 or guard.get("invalid"):
            raise ValueError("此数据副本缺少有效的新鲜度证明，请重新预检并准备副本")
        for source in guard["sources"]:
            _check_guard(source)
        _check_guard(guard["prepared"])
        _check_prepared_files(data_plan, database=database)
        sources = guard["sources"]
        data_dir = Path(data_plan["result"]["prepared_data_dir"]).resolve()
        if _canonical(data_dir / "app.db") == _canonical(active):
            raise ValueError("数据副本不能是当前运行数据库")
    target_database = data_dir / "app.db"
    maintenance._database_info(target_database)
    mappings = []
    if library_plan:
        internal = _check_library_plan(library_plan, database=database)
        library_root = Path(library_plan["result"]["prepared_library_root"]).resolve()
        mappings.append({"root_id": library_plan["root_id"], "source_path": internal["source_path"],
                         "target_path": str(library_root), "plan_id": library_plan["id"],
                         "plan_database": str(active)})
        with maintenance._readonly(target_database) as connection:
            for mapping in mappings:
                mapping["target_root_id"] = _mapping(connection, mapping)
    # Reset a download subdirectory only after successful startup activation.
    settings_path = data_dir / "settings.json"
    settings_value = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    if not isinstance(settings_value, dict):
        raise ValueError("准备副本的设置格式错误")
    new_settings = dict(settings_value)
    if data_plan or library_plan:
        new_settings["default_download_dir"] = ""
    return {"id": uuid.uuid4().hex, "version": 1, "data_dir": str(data_dir), "library_root": str(library_root),
            "target_database": str(target_database), "target_guard": _guard(target_database),
            "sources": sources, "mappings": mappings, "new_settings": new_settings,
            "settings_before_sha256": _file_digest(settings_path),
            "data_plan_id": data_plan["id"] if data_plan else None, "plan_database": str(active)}


def write_pending(request, *, paths_file=None):
    """Preserve the exact old configuration, then atomically stage activation."""
    path = Path(paths_file or config.PATHS_FILE).resolve()
    old = path.read_bytes() if path.exists() else None
    if old is not None:
        value = json.loads(old.decode("utf-8"))
        if value.get("pending_transition"):
            raise ValueError("已有路径切换等待重启")
        backup = path.with_name(path.name + ".before-" + request["id"] + ".json")
        if backup.exists():
            raise FileExistsError("配置备份已经存在")
        maintenance._write_new(backup, old)
        request = dict(request, previous_paths_backup=str(backup))
    else:
        request = dict(request, previous_paths_backup=None)
    value = {"data_dir": request["data_dir"], "library_root": request["library_root"],
             "pending_transition": request}
    atomic_write_text(path, _json(value))
    return request


def apply_pending_startup(*, paths_file=None, database=None):
    """Call after schema migration and before recovery, jobs, or business writes."""
    path = Path(paths_file or config.PATHS_FILE).resolve()
    if not path.exists():
        return None
    config_value = json.loads(path.read_text(encoding="utf-8"))
    request = config_value.get("pending_transition")
    if not request:
        return None
    reject_environment_overrides()
    if request.get("version") != 1:
        raise ValueError("路径切换记录版本不受支持，原配置备份和pending已保留")
    target = Path(database or config.DB_PATH).resolve()
    if _canonical(target) != _canonical(request["target_database"]):
        raise ValueError("启动数据库与已确认目标不一致，pending已保留")
    request_hash = hashlib.sha256(_json(request).encode()).hexdigest()
    with db.connect(target) as connection:
        journal = connection.execute("SELECT * FROM path_transitions WHERE id=?", (request["id"],)).fetchone()
    if request.get("data_plan_id"):
        data_plan = maintenance.get_plan(request["data_plan_id"], database=request["plan_database"])
        _check_prepared_files(data_plan, database=request["plan_database"], skip_settings=bool(journal))
    if journal:
        if journal["request_hash"] != request_hash:
            raise ValueError("路径切换请求与已提交记录不一致")
        post = json.loads(journal["post_snapshot_json"])
        if business_snapshot(target, spec=post["tables"])["sha256"] != post["sha256"]:
            raise ValueError("切换提交后数据库又发生变化，pending已保留")
    else:
        with _lock_source_databases(request, target):
            for source in request["sources"]:
                _check_guard(source)
            _check_guard(request["target_guard"])
            for mapping in request["mappings"]:
                plan = maintenance.get_plan(mapping["plan_id"], database=mapping["plan_database"])
                _check_library_plan(plan, database=mapping["plan_database"])
            with db.connect(target) as connection:
                connection.execute("BEGIN IMMEDIATE")
                # A final in-transaction digest closes the verification/update gap.
                guarded = request["target_guard"]["snapshot"]
                if _snapshot_connection(connection, guarded["tables"])["sha256"] != guarded["sha256"]:
                    raise ValueError("启动验证期间数据库已改变，未更新媒体根")
                for mapping in request["mappings"]:
                    actual = _mapping(connection, mapping, update=True)
                    if actual != mapping["target_root_id"]:
                        raise ValueError("根映射在准备后改变，事务已回滚")
                post = _snapshot_connection(connection)
                connection.execute("INSERT INTO path_transitions VALUES(?,?,?,?)",
                                   (request["id"], request_hash, _json(post), db.utc_now()))
    settings_path = target.parent / "settings.json"
    settings_text = _json(request["new_settings"])
    current_settings = _file_digest(settings_path)
    new_digest = hashlib.sha256(settings_text.encode()).hexdigest()
    if current_settings not in (request["settings_before_sha256"], new_digest):
        raise ValueError("设置在切换期间被更改，pending已保留")
    if current_settings != new_digest:
        atomic_write_text(settings_path, settings_text)
    # Journal remains if this write fails; the next startup validates the
    # committed state and retries this final, idempotent step.
    config_value.pop("pending_transition")
    atomic_write_text(path, _json(config_value))
    return {"id": request["id"], "data_dir": request["data_dir"], "library_root": request["library_root"],
            "root_ids": [mapping["target_root_id"] for mapping in request["mappings"]]}
