"""Persistent, metadata-only media catalog. Imports never touch real data.

Scanning reads source files and writes only SQLite. Image decoding and face
analysis belong to the bounded media worker queue, never to list queries.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid

from . import archive, db


SCHEMA = """
CREATE TABLE IF NOT EXISTS media_roots (
 id TEXT PRIMARY KEY, path TEXT NOT NULL, path_key TEXT NOT NULL UNIQUE,
 label TEXT NOT NULL DEFAULT '', generation INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, last_scan_at TEXT
);
CREATE TABLE IF NOT EXISTS media_entries (
 id TEXT PRIMARY KEY, root_id TEXT NOT NULL REFERENCES media_roots(id),
 rel_dir TEXT NOT NULL, author TEXT NOT NULL, date_dir TEXT NOT NULL,
 platform TEXT, item_id TEXT, archive_status TEXT NOT NULL,
 availability TEXT NOT NULL DEFAULT 'present', sort_at TEXT NOT NULL,
 date_source TEXT NOT NULL, meta_json TEXT NOT NULL, text_preview TEXT NOT NULL,
 search_text TEXT NOT NULL, signature TEXT NOT NULL,
 size INTEGER NOT NULL, photo_count INTEGER NOT NULL, video_count INTEGER NOT NULL,
 live_count INTEGER NOT NULL, file_count INTEGER NOT NULL,
 cover_rel TEXT, cover_type TEXT, cover_face_json TEXT, cover_signature TEXT,
 imported_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 scan_generation INTEGER NOT NULL, conflict TEXT,
 UNIQUE(root_id, rel_dir)
);
CREATE INDEX IF NOT EXISTS media_entry_date ON media_entries(root_id, availability, sort_at DESC, id);
CREATE INDEX IF NOT EXISTS media_entry_author ON media_entries(root_id, author, availability, sort_at DESC, id);
CREATE INDEX IF NOT EXISTS media_entry_identity ON media_entries(platform, item_id);
CREATE TABLE IF NOT EXISTS media_assets (
 id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES media_entries(id),
 rel_path TEXT NOT NULL, kind TEXT NOT NULL, poster_rel TEXT,
 size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
 UNIQUE(entry_id, rel_path)
);
CREATE INDEX IF NOT EXISTS media_asset_entry ON media_assets(entry_id);
CREATE TABLE IF NOT EXISTS task_media (
 task_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES media_entries(id),
 root_id TEXT NOT NULL REFERENCES media_roots(id)
);
CREATE TABLE IF NOT EXISTS media_entry_files (
 entry_id TEXT NOT NULL REFERENCES media_entries(id), rel_path TEXT NOT NULL,
 size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
 PRIMARY KEY(entry_id, rel_path)
);
CREATE TABLE IF NOT EXISTS media_scans (
 id TEXT PRIMARY KEY, root_id TEXT NOT NULL REFERENCES media_roots(id),
 status TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
 changed INTEGER NOT NULL DEFAULT 0, total INTEGER, error TEXT,
 started_at TEXT NOT NULL, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS media_scan_root ON media_scans(root_id, started_at DESC);
"""

_DATE = re.compile(r"^(\d{2}(?:\d{2})?)-(\d{1,2})-(\d{1,2})(?:[ T-](\d{1,2})[:-](\d{1,2})(?::(\d{2}))?)?")
_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEOS = {".mp4", ".m4v", ".webm"}
_LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
_locks_guard = threading.Lock()
_scan_locks = {}
_SUMMARY_COLUMNS = ('id,root_id,rel_dir,author,date_dir,platform,item_id,archive_status,availability,'
    'sort_at,date_source,text_preview,signature,size,photo_count,video_count,live_count,file_count,'
    'cover_rel,cover_type,cover_face_json,cover_signature,imported_at,updated_at,conflict')


def _key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _safe(root, relative):
    value = Path(relative)
    if value.is_absolute() or value.drive or str(relative).startswith(("/", "\\")):
        raise ValueError("媒体路径必须是相对路径")
    target = (root / value).resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("媒体路径越界")
    return target


def _cancelled(cancel):
    return bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))


def _folders(root, cancel=None):
    """Find author/date entries, including chosen download subdirectories.

    A recognized entry is a leaf; never descend into its photo/live directories.
    Private/code containers are excluded when indexing a legacy source checkout.
    """
    from .security import PRIVATE_SEGMENTS
    excluded = PRIVATE_SEGMENTS | {'build', 'dist', 'release', 'portable', 'src-tauri', 'backend-dist'}
    pending = [root]
    while pending:
        if _cancelled(cancel):
            return
        parent = pending.pop()
        children = []
        for entry in sorted(parent.iterdir(), key=lambda p: p.name):
            if entry.name.startswith('.') or entry.name.casefold() in excluded or not entry.is_dir() or entry.is_symlink():
                continue
            if not entry.resolve().is_relative_to(root):
                raise ValueError('媒体条目包含越界目录链接')
            if _DATE.match(entry.name) and parent != root:
                yield parent.name, entry
            else:
                children.append(entry)
        pending.extend(reversed(children))


def _inventory(folder):
    result = []
    for child in sorted(folder.iterdir(), key=lambda p: p.name):
        if child.name.startswith(".") or child.name.endswith(".part"):
            continue
        paths = sorted(child.iterdir()) if child.name in ("photo", "live") and child.is_dir() else [child]
        for path in paths:
            if not path.is_file():
                continue
            _safe(folder, path.relative_to(folder).as_posix())
            if path.suffix.lower() not in _IMAGES | _VIDEOS | {".mov"} and path.name not in ("context.md", "entry.json"):
                continue
            stat = path.stat()
            result.append({"path": path.relative_to(folder).as_posix(), "size": stat.st_size,
                           "mtime_ns": stat.st_mtime_ns})
    return result


def preflight_root(path):
    root = Path(path).resolve()
    if not root.is_dir():
        raise FileNotFoundError("媒体根目录不存在")
    authors, entries, files, size = set(), 0, 0, 0
    for author, folder in _folders(root):
        inventory = _inventory(folder)
        if inventory:
            entries += 1
            authors.add(author)
            files += len(inventory)
            size += sum(item["size"] for item in inventory)
    return {"path": str(root), "authors": len(authors), "entries": entries,
            "files": files, "bytes": size, "mode": "index_only"}


def register_root(path, label="", *, database=None):
    root = Path(path).resolve()
    if not root.is_dir():
        raise FileNotFoundError("媒体根目录不存在；索引不会创建或移动媒体目录")
    path_key = _key(root)
    with db.connect(database) as connection:
        existing = connection.execute("SELECT * FROM media_roots WHERE path_key=?", (path_key,)).fetchone()
        if existing:
            return dict(existing)
        for item in connection.execute("SELECT path FROM media_roots"):
            other = Path(item["path"]).resolve()
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise ValueError("媒体根目录不能重叠")
        identity = str(uuid.uuid4())
        connection.execute("INSERT INTO media_roots(id,path,path_key,label,created_at) VALUES(?,?,?,?,?)",
                           (identity, str(root), path_key, label or root.name, db.utc_now()))
    return get_root(identity, database=database)


def list_roots(*, database=None):
    with db.connect(database) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM media_roots ORDER BY created_at,id")]


def get_root(root_id, *, database=None):
    with db.connect(database) as connection:
        row = connection.execute("SELECT * FROM media_roots WHERE id=?", (root_id,)).fetchone()
    if row is None:
        raise KeyError("媒体根未注册")
    return dict(row)


def _context(folder):
    meta, body, in_code = {}, [], False
    try:
        with open(folder / "context.md", encoding="utf-8", errors="replace") as stream:
            text = stream.read(1024 * 1024)
    except FileNotFoundError:
        return meta, ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
        elif in_code:
            body.append(line)
        else:
            match = re.match(r"^- \*\*(.+?)\*\*: (.*)$", stripped)
            if match:
                meta[match[1]] = match[2].strip()
    return meta, "\n".join(body).strip()


def _date_value(value):
    match = _DATE.match(value or "")
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        value = dt.datetime(2000 + int(year) if len(year) == 2 else int(year), int(month), int(day),
                            int(hour or 0), int(minute or 0), int(second or 0), tzinfo=_LOCAL_TZ)
        return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def _metadata(folder, inventory):
    meta, body = _context(folder)
    marker = archive.read_manifest(folder) or {}
    status = archive.entry_status(folder, verify_files=True)
    platform, item_id = marker.get("platform"), marker.get("item_id")
    link = meta.get("原文链接", "")
    if not platform:
        platform = "douyin" if "douyin.com/" in link else "weibo" if "weibo." in link else None
    if not item_id:
        item_id = meta.get("mblogid")
        if not item_id:
            match = re.search(r"/(?:video/|detail/|status/|\d+/)([A-Za-z0-9]+)(?:[/?#]|$)", link)
            item_id = match[1] if match else None
    date = _date_value(meta.get("发布时间"))
    date_source = "published"
    if date is None:
        date, date_source = _date_value(folder.name), "directory"
    if date is None:
        try:
            date = dt.datetime.fromisoformat(marker["started_at"]).astimezone(dt.timezone.utc).isoformat()
            date_source = "downloaded"
        except (KeyError, ValueError, TypeError):
            date, date_source = db.utc_now(), "imported"
    assets = []
    available = {item["path"] for item in inventory}
    # A complete marker is the authoritative output set. Stale files from an
    # earlier partial attempt remain on disk but are not silently added to it.
    if status == "complete":
        available &= {item["path"] for item in marker["files"]}
    for item in inventory:
        relative, extension = item["path"], Path(item["path"]).suffix.lower()
        if relative not in available or relative in ("context.md", "entry.json"):
            continue
        poster = None
        if relative.startswith("live/") and extension == ".mov":
            kind = "live"
            poster = next((str(Path(relative).with_suffix(ext)).replace("\\", "/")
                           for ext in (".jpg", ".jpeg")
                           if str(Path(relative).with_suffix(ext)).replace("\\", "/") in available), None)
        elif extension in _VIDEOS:
            kind = "video"
            candidate = str(Path(relative).with_suffix("")).replace("\\", "/") + "_cover.jpg"
            poster = candidate if candidate in available else None
        elif extension in _IMAGES:
            kind = "cover" if relative.startswith("live/") or relative.endswith("_cover.jpg") else "image"
        else:
            continue
        assets.append({"rel_path": relative, "kind": kind, "poster_rel": poster,
                       "size": item["size"], "mtime_ns": item["mtime_ns"]})
    candidates = _candidates(assets)
    cover = candidates[0] if candidates else None
    return dict(meta=meta, text_preview=(meta.get("视频标题") or body)[:120],
                search_text="\n".join((folder.parent.name, folder.name, body, *meta.values())).lower(),
                platform=platform, item_id=str(item_id) if item_id else None, archive_status=status,
                sort_at=date, date_source=date_source, assets=assets, marker=marker,
                size=sum(item["size"] for item in assets),
                photo_count=sum(a["kind"] == "image" for a in assets),
                video_count=sum(a["kind"] == "video" for a in assets),
                live_count=sum(a["kind"] == "live" for a in assets),
                cover_rel=cover["rel"] if cover else None,
                cover_type=("video" if cover["kind"].startswith("video") else cover["kind"]) if cover else None)


def _candidates(assets):
    candidates = []
    for asset in assets:
        kind, relative = asset["kind"], asset["rel_path"]
        if kind == "image":
            candidates.append({"rel": relative, "kind": "image"})
        elif kind == "live":
            candidates.append({"rel": asset["poster_rel"] or relative,
                               "kind": "live" if asset["poster_rel"] else "video_frame"})
        elif kind == "video":
            candidates.append({"rel": asset["poster_rel"] or relative,
                               "kind": "video_file" if asset["poster_rel"] else "video_frame"})
    return candidates


def _upsert(connection, root_id, generation, author, folder, inventory, *, base=None):
    relative = folder.relative_to(base).as_posix() if base else f"{author}/{folder.name}"
    signature = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
    old = connection.execute("SELECT * FROM media_entries WHERE root_id=? AND rel_dir=?", (root_id, relative)).fetchone()
    if old and old["signature"] == signature:
        connection.execute("UPDATE media_entries SET scan_generation=?,availability='present' WHERE id=?",
                           (generation, old["id"]))
        return False
    value = _metadata(folder, inventory)
    # The archive UUID identifies a moved entry only when its former location
    # is gone. An actual second copy keeps its own row and an explicit conflict.
    root_path = Path(connection.execute("SELECT path FROM media_roots WHERE id=?", (root_id,)).fetchone()[0]).resolve()
    if not old:
        marker_id = value["marker"].get("entry_id")
        if marker_id:
            candidate = connection.execute("SELECT * FROM media_entries WHERE id=? AND root_id=?", (marker_id, root_id)).fetchone()
            if candidate and not _safe(root_path, candidate["rel_dir"]).exists():
                old = candidate
        else:
            candidates = connection.execute("SELECT * FROM media_entries WHERE root_id=? AND signature=?", (root_id, signature)).fetchall()
            absent = [candidate for candidate in candidates if not _safe(root_path, candidate["rel_dir"]).exists()]
            if len(absent) == 1:
                old = absent[0]
    identity = old["id"] if old else value["marker"].get("entry_id")
    try:
        identity = str(uuid.UUID(str(identity)))
    except (ValueError, TypeError, AttributeError):
        identity = str(uuid.uuid4())
    conflict = None
    if not old and connection.execute("SELECT id FROM media_entries WHERE id=?", (identity,)).fetchone():
        identity, conflict = str(uuid.uuid4()), "duplicate_entry_id"
    if value["item_id"]:
        duplicates = connection.execute("SELECT id FROM media_entries WHERE platform=? AND item_id=? AND id<>?",
                                        (value["platform"], value["item_id"], identity)).fetchall()
        if duplicates:
            conflict = "duplicate_platform_id"
            connection.executemany("UPDATE media_entries SET conflict='duplicate_platform_id' WHERE id=?",
                                   [(item["id"],) for item in duplicates])
    now = db.utc_now()
    row = dict(id=identity, root_id=root_id, rel_dir=relative, author=author, date_dir=folder.name,
               availability="present", meta_json=json.dumps(value["meta"], ensure_ascii=False),
               signature=signature, cover_face_json=None, cover_signature=None, file_count=len(inventory),
               imported_at=old["imported_at"] if old else now, updated_at=now,
               scan_generation=generation, conflict=conflict,
               **{key: value[key] for key in ("platform", "item_id", "archive_status", "sort_at", "date_source",
                  "text_preview", "search_text", "size", "photo_count", "video_count", "live_count", "cover_rel", "cover_type")})
    columns = list(row)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns if column not in ("id", "imported_at"))
    connection.execute(f"INSERT INTO media_entries({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
                       f"ON CONFLICT(id) DO UPDATE SET {updates}", list(row.values()))
    old_assets = {a["rel_path"]: a["id"] for a in connection.execute("SELECT id,rel_path FROM media_assets WHERE entry_id=?", (identity,))}
    connection.execute("DELETE FROM media_assets WHERE entry_id=?", (identity,))
    connection.executemany("INSERT INTO media_assets(id,entry_id,rel_path,kind,poster_rel,size,mtime_ns) VALUES(?,?,?,?,?,?,?)",
                           [(old_assets.get(a["rel_path"]) or str(uuid.uuid4()), identity, a["rel_path"],
                             a["kind"], a["poster_rel"], a["size"], a["mtime_ns"]) for a in value["assets"]])
    connection.execute("DELETE FROM media_entry_files WHERE entry_id=?", (identity,))
    connection.executemany("INSERT INTO media_entry_files VALUES(?,?,?,?)",
                           [(identity, item["path"], item["size"], item["mtime_ns"]) for item in inventory])
    return True


def get_scan(scan_id, *, database=None):
    with db.connect(database) as connection:
        row = connection.execute("SELECT * FROM media_scans WHERE id=?", (scan_id,)).fetchone()
    return dict(row) if row else None


def list_scans(root_id=None, *, limit=20, database=None):
    with db.connect(database) as connection:
        where, params = ("WHERE root_id=?", [root_id]) if root_id else ("", [])
        rows = connection.execute(f"SELECT * FROM media_scans {where} ORDER BY started_at DESC,id DESC LIMIT ?",
                                  [*params, max(1, min(100, int(limit)))]).fetchall()
    return [dict(row) for row in rows]


def scan_root(root_id, *, cancel=None, progress=None, database=None, batch_size=100, scan_id=None):
    """Checkpoint batches; only a complete successful scan can mark files absent."""
    lock_key = (str(database), root_id)
    with _locks_guard:
        lock = _scan_locks.setdefault(lock_key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise RuntimeError("该媒体根正在建立索引")
    scan_id, processed, changed = scan_id or str(uuid.uuid4()), 0, 0
    try:
        root = get_root(root_id, database=database)
        folder = Path(root["path"]).resolve()
        with db.connect(database) as connection:
            connection.execute("UPDATE media_scans SET status='interrupted',finished_at=? WHERE root_id=? AND status='running'",
                               (db.utc_now(), root_id))
            connection.execute("UPDATE media_roots SET generation=generation+1 WHERE id=?", (root_id,))
            generation = connection.execute("SELECT generation FROM media_roots WHERE id=?", (root_id,)).fetchone()[0]
            connection.execute("INSERT INTO media_scans(id,root_id,status,started_at) VALUES(?,?,'running',?)",
                               (scan_id, root_id, db.utc_now()))
        status, error, batch = "success", None, []
        try:
            if not folder.is_dir():
                raise FileNotFoundError("媒体根不可用，未将任何条目标为缺失")
            for author, entry in _folders(folder, cancel=cancel):
                if _cancelled(cancel):
                    status = "cancelled"
                    break
                inventory = _inventory(entry)
                if not inventory:
                    continue
                batch.append((author, entry, inventory))
                if len(batch) >= max(1, min(1000, batch_size)):
                    with db.connect(database) as connection:
                        for args in batch:
                            changed += _upsert(connection, root_id, generation, *args, base=folder)
                        processed += len(batch)
                        connection.execute("UPDATE media_scans SET processed=?,changed=? WHERE id=?", (processed, changed, scan_id))
                    batch.clear()
                    if progress:
                        progress(get_scan(scan_id, database=database))
            if _cancelled(cancel):
                status = "cancelled"
            if batch:
                with db.connect(database) as connection:
                    for args in batch:
                        changed += _upsert(connection, root_id, generation, *args, base=folder)
                    processed += len(batch)
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: 媒体索引未完整完成；未清除旧记录"
        with db.connect(database) as connection:
            if status == "success":
                connection.execute("UPDATE media_entries SET availability='missing' WHERE root_id=? AND scan_generation<>?",
                                   (root_id, generation))
                connection.execute("UPDATE media_roots SET last_scan_at=? WHERE id=?", (db.utc_now(), root_id))
            connection.execute("UPDATE media_scans SET status=?,processed=?,changed=?,total=?,error=?,finished_at=? WHERE id=?",
                               (status, processed, changed, processed if status == "success" else None, error, db.utc_now(), scan_id))
        result = get_scan(scan_id, database=database)
        if progress:
            progress(result)
        return result
    finally:
        lock.release()


def _page(page, page_size):
    return max(1, int(page)), max(1, min(200, int(page_size)))


def live_for_cover(assets, cover):
    """Return only the Live video paired with this exact cover, never any Live."""
    if not cover:
        return None
    return min((asset["rel_path"] for asset in assets
                if asset["kind"] == "live" and asset["poster_rel"] == cover), default=None)


def _cover_live_paths(connection, rows):
    # One bounded query for this page; retain the covers read in the page
    # snapshot instead of racing a second read of media_entries.cover_rel.
    covers = [(row["id"], row["cover_rel"]) for row in rows if row["cover_rel"]]
    if not covers:
        return {}
    placeholders = ",".join("(?,?)" for _ in covers)
    query = (f"WITH cover_page(id,cover_rel) AS (VALUES {placeholders}) "
             "SELECT p.id,MIN(a.rel_path) AS live_rel FROM cover_page p "
             "JOIN media_assets a ON a.entry_id=p.id AND a.kind='live' AND a.poster_rel=p.cover_rel "
             "GROUP BY p.id")
    return {row["id"]: row["live_rel"] for row in connection.execute(query, [value for pair in covers for value in pair])}


def _summary(row):
    result = dict(row)
    for key in ("search_text", "meta_json", "scan_generation", "cover_face_json"):
        result.pop(key, None)
    result["cover"] = row["cover_rel"]
    result["cover_live_rel"] = None
    result["cover_face"] = json.loads(row["cover_face_json"]) if row["cover_face_json"] else None
    result["media_count"] = row["photo_count"] + row["video_count"] + row["live_count"]
    result["counts"] = {"photos": row["photo_count"], "videos": row["video_count"], "lives": row["live_count"]}
    return result


def list_entries(*, root_id=None, author=None, q="", date_from=None, date_to=None,
                 page=1, page_size=60, status=None, sort="desc", platform=None,
                 media_type=None, database=None):
    page, page_size = _page(page, page_size)
    conditions, params = [], []
    if root_id:
        conditions.append("root_id=?")
        params.append(root_id)
    if author:
        conditions.append("author=?")
        params.append(author)
    if platform:
        if platform not in ("weibo", "douyin"):
            raise ValueError("未知媒体平台")
        conditions.append("platform=?")
        params.append(platform)
    if media_type:
        column = {"image": "photo_count", "video": "video_count", "live": "live_count"}.get(media_type)
        if column:
            conditions.append(f"{column}>0")
        elif media_type == "text":
            conditions.append("photo_count+video_count+live_count=0")
        else:
            raise ValueError("未知媒体类型")
    if status == "missing":
        conditions.append("availability='missing'")
    else:
        conditions.append("availability='present'")
        if status:
            if status not in ("partial", "complete", "legacy"):
                raise ValueError("未知媒体状态")
            conditions.append("archive_status=?")
            params.append(status)
    if q.strip():
        conditions.append("instr(search_text,?)>0")
        params.append(q.strip().lower())
    for value, comparison in ((date_from, ">="), (date_to, "<")):
        if value:
            value = dt.date.fromisoformat(value)
            if comparison == "<":
                value += dt.timedelta(days=1)
            conditions.append(f"sort_at{comparison}?")
            params.append(dt.datetime.combine(value, dt.time(), tzinfo=_LOCAL_TZ).astimezone(dt.timezone.utc).isoformat(timespec="seconds"))
    where = " AND ".join(conditions)
    direction = "ASC" if sort == "asc" else "DESC"
    with db.connect(database) as connection:
        total = connection.execute(f"SELECT count(*) FROM media_entries WHERE {where}", params).fetchone()[0]
        rows = connection.execute(f"SELECT {_SUMMARY_COLUMNS} FROM media_entries WHERE {where} ORDER BY sort_at {direction},id {direction} LIMIT ? OFFSET ?",
                                  [*params, page_size, (page - 1) * page_size]).fetchall()
        live_paths = _cover_live_paths(connection, rows)
    items = [_summary(row) | {"cover_live_rel": live_paths.get(row["id"])} for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def date_groups(*, root_id=None, author=None, q="", group="month", limit=240, database=None):
    """Bounded UTC+8 date navigation matching the date filters, newest first."""
    formats = {"year": "%Y", "month": "%Y-%m", "day": "%Y-%m-%d"}
    if group not in formats:
        raise ValueError("日期分组必须是year、month或day")
    conditions, params = ["availability='present'"], []
    for column, value in (("root_id", root_id), ("author", author)):
        if value:
            conditions.append(f"{column}=?")
            params.append(value)
    if q.strip():
        conditions.append("instr(search_text,?)>0")
        params.append(q.strip().lower())
    with db.connect(database) as connection:
        rows = connection.execute(
            f"SELECT strftime('{formats[group]}',sort_at,'+8 hours') AS date,count(*) AS count "
            f"FROM media_entries WHERE {' AND '.join(conditions)} GROUP BY date ORDER BY date DESC LIMIT ?",
            [*params, max(1, min(1000, int(limit)))],
        ).fetchall()
    return [dict(row) for row in rows]


def list_authors(*, root_id=None, page=1, page_size=60, q="", database=None):
    page, page_size = _page(page, page_size)
    conditions, params = ["availability='present'"], []
    if root_id:
        conditions.append("root_id=?")
        params.append(root_id)
    if q.strip():
        conditions.append("instr(lower(author),?)>0")
        params.append(q.strip().lower())
    where = " AND ".join(conditions)
    with db.connect(database) as connection:
        total = connection.execute(f"SELECT count(DISTINCT author) FROM media_entries WHERE {where}", params).fetchone()[0]
        rows = connection.execute(f"SELECT author AS name,count(*) AS count,sum(size) AS total_size FROM media_entries WHERE {where} "
                                  "GROUP BY author ORDER BY count DESC,author LIMIT ? OFFSET ?",
                                  [*params, page_size, (page - 1) * page_size]).fetchall()
    return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}


def get_entry(entry_id, *, database=None):
    with db.connect(database) as connection:
        row = connection.execute("SELECT * FROM media_entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            return None
        assets = [dict(asset) for asset in connection.execute("SELECT * FROM media_assets WHERE entry_id=? ORDER BY rel_path", (entry_id,))]
    result = _summary(row)
    result["meta"] = json.loads(row["meta_json"])
    # Full text is read for one opened entry only, never for list pages.
    result['text'] = result['meta'].get('视频标题') or result['text_preview']
    if result['availability'] == 'present':
        try:
            root_path = Path(get_root(result['root_id'], database=database)['path']).resolve()
            folder = _safe(root_path, result['rel_dir'])
            _safe(root_path, (folder / 'context.md').relative_to(root_path))
            _meta, body = _context(folder)
            result['text'] = result['meta'].get('视频标题') or body
        except (OSError, ValueError):
            pass
    result["assets"] = assets
    result["cover_live_rel"] = live_for_cover(assets, result["cover"])
    result["photos"] = [a["rel_path"] for a in assets if a["kind"] == "image"]
    result["videos"] = [a["rel_path"] for a in assets if a["kind"] == "video"]
    result["lives"] = [a["rel_path"] for a in assets if a["kind"] == "live"]
    result["live_map"] = {a["poster_rel"]: a["rel_path"] for a in assets if a["kind"] == "live" and a["poster_rel"]}
    result["gallery"] = [{"id": a["id"], "type": "image" if a["kind"] == "image" else "video", "rel": a["rel_path"],
                          **({"live": True} if a["kind"] == "live" else {}),
                          **({"poster": a["poster_rel"]} if a["poster_rel"] else {})}
                         for kind in ("image", "live", "video") for a in assets if a["kind"] == kind]
    result["cover_candidates"] = _candidates(assets)
    return result


def find_entry(root_id, rel_dir, *, database=None):
    with db.connect(database) as connection:
        row = connection.execute("SELECT id FROM media_entries WHERE root_id=? AND rel_dir=?", (root_id, rel_dir.replace("\\", "/").strip("/"))).fetchone()
    return get_entry(row["id"], database=database) if row else None


def locate_entry(abs_path, *, database=None):
    path = Path(abs_path).resolve()
    for root in list_roots(database=database):
        root_path = Path(root["path"]).resolve()
        if path.is_relative_to(root_path):
            parts = path.relative_to(root_path).parts
            for count in range(len(parts), 1, -1):
                entry = find_entry(root['id'], '/'.join(parts[:count]), database=database)
                if entry:
                    return entry
    return None


def update_cover(entry_id, cover, cover_type, face=None, *, signature=None, database=None):
    with db.connect(database) as connection:
        entry = connection.execute("SELECT signature FROM media_entries WHERE id=?", (entry_id,)).fetchone()
        if not entry or (signature is not None and signature != entry["signature"]):
            return False
        if cover is not None and not connection.execute("SELECT 1 FROM media_assets WHERE entry_id=? AND (rel_path=? OR poster_rel=?)", (entry_id, cover, cover)).fetchone():
            raise ValueError("封面不是该条目的媒体文件")
        connection.execute("UPDATE media_entries SET cover_rel=?,cover_type=?,cover_face_json=?,cover_signature=? WHERE id=?",
                           (cover, cover_type, json.dumps(face) if face else None, entry["signature"], entry_id))
    return True


def _digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def rebind_root(root_id, new_path, *, verify=True, cancel=None, progress=None, database=None):
    """Validate a copied tree, then atomically switch only its root location.

    Full verification compares old/new source bytes. With verify=False only
    recorded sizes are checked (explicit recovery when the old disk is offline).
    Source directories and originals are never deleted, copied, or overwritten.
    """
    root = get_root(root_id, database=database)
    old, target = Path(root["path"]).resolve(), Path(new_path).resolve()
    if not target.is_dir():
        raise FileNotFoundError("新媒体根目录不存在")
    if target == old:
        return root
    if not root["last_scan_at"]:
        raise ValueError("请先完整建立索引，再验证媒体根重绑定")
    if target.is_relative_to(old) or old.is_relative_to(target):
        raise ValueError("新旧媒体根不能互相包含")
    for item in list_roots(database=database):
        if item["id"] != root_id:
            other = Path(item["path"]).resolve()
            if target.is_relative_to(other) or other.is_relative_to(target):
                raise ValueError("新媒体根与已注册根重叠")
    with db.connect(database) as connection:
        inventory = connection.execute("SELECT e.rel_dir,f.rel_path,f.size FROM media_entry_files f JOIN media_entries e ON e.id=f.entry_id "
                                       "WHERE e.root_id=? AND e.availability='present' ORDER BY e.rel_dir,f.rel_path", (root_id,)).fetchall()
    for number, item in enumerate(inventory, 1):
        if _cancelled(cancel):
            raise InterruptedError("媒体根验证已取消，原目录配置未改变")
        relative = f"{item['rel_dir']}/{item['rel_path']}"
        destination = _safe(target, relative)
        if not destination.is_file() or destination.stat().st_size != item["size"]:
            raise ValueError("新媒体根文件缺失或大小不符，原目录配置未改变")
        if verify and _digest(_safe(old, relative)) != _digest(destination):
            raise ValueError("媒体文件内容校验失败，原目录配置未改变")
        if progress:
            progress({"processed": number, "total": len(inventory)})
    if _cancelled(cancel):
        raise InterruptedError("媒体根验证已取消，原目录配置未改变")
    with db.connect(database) as connection:
        changed = connection.execute("UPDATE media_roots SET path=?,path_key=? WHERE id=? AND path_key=?",
                                     (str(target), _key(target), root_id, root["path_key"])).rowcount
        if not changed:
            raise RuntimeError("媒体根已被其他操作修改，请重新预检")
    return get_root(root_id, database=database)
