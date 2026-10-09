"""Durable task summaries and bounded log batches; archives are never expired.

The application migration owns SCHEMA. No real data is opened at import time.
Logs are committed every 100 lines / 250ms by the runtime. A power failure may
lose the final uncommitted batch; successful terminal states always flush first.
"""
import base64
import contextlib
import gzip
import json
import os
from pathlib import Path
import threading

from .redaction import redact_text, redact_value, redact_task_metadata

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, script_id TEXT NOT NULL, script_name TEXT NOT NULL,
 script_icon TEXT NOT NULL DEFAULT '', params_json TEXT NOT NULL,
 status TEXT NOT NULL, progress_json TEXT NOT NULL, created_at TEXT NOT NULL,
 started_at TEXT, finished_at TEXT, exit_code INTEGER, output_dir TEXT,
 output_rel TEXT, parent_task_id TEXT, attempt INTEGER NOT NULL DEFAULT 1,
 error TEXT, next_log_seq INTEGER NOT NULL DEFAULT 1,
 content_key TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
 deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_task_id);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_active_content ON tasks(content_key)
 WHERE content_key IS NOT NULL AND status IN ('queued','running');
CREATE TABLE IF NOT EXISTS task_logs (
 task_id TEXT NOT NULL, seq INTEGER NOT NULL, at TEXT NOT NULL,
 stream TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(task_id,seq)
);
CREATE TABLE IF NOT EXISTS task_log_archives (
 task_id TEXT NOT NULL, start_seq INTEGER NOT NULL, end_seq INTEGER NOT NULL,
 path TEXT NOT NULL, line_count INTEGER NOT NULL, bytes INTEGER NOT NULL,
 PRIMARY KEY(task_id,start_seq)
);
"""

BATCH_LINES = 100
BATCH_BYTES = 1024 * 1024
MAX_LINE_CHARS = 16384
ARCHIVE_LINES = 1000
_FIELDS = ("id", "script_id", "script_name", "script_icon", "status", "created_at",
           "started_at", "finished_at", "exit_code", "output_dir", "output_rel",
           "parent_task_id", "attempt", "error", "content_key")


class TaskStore:
    def __init__(self, connect_factory=None, log_dir=None):
        if connect_factory is None:
            from . import db
            connect_factory = db.connect
        if log_dir is None:
            from . import config
            log_dir = config.LOG_DIR
        self.connect = connect_factory
        self.log_dir = Path(log_dir)
        self._lock = threading.RLock()
        self._pending = []
        self._pending_bytes = 0
        self._sequences = {}
        self._log_locks = {}

    @contextlib.contextmanager
    def _task_log_lock(self, task_id):
        """Serialize archive/read/delete of one task, never block other tasks' writes."""
        with self._lock:
            slot = self._log_locks.setdefault(task_id, [threading.RLock(), 0])
            slot[1] += 1
        try:
            with slot[0]:
                yield
        finally:
            with self._lock:
                slot[1] -= 1
                if not slot[1]:
                    self._log_locks.pop(task_id, None)

    @contextlib.contextmanager
    def _db(self):
        resource = self.connect()
        if hasattr(resource, "execute"):  # standalone test factory
            try:
                with resource:
                    yield resource
            finally:
                resource.close()
        else:  # app.db.connect owns commit/rollback/close
            with resource as connection:
                yield connection

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        item = dict(row)
        item["params"] = json.loads(item.pop("params_json"))
        item["progress"] = json.loads(item.pop("progress_json"))
        item["metadata"] = json.loads(item.pop("metadata_json"))
        item['hidden'] = bool(item.pop("deleted", 0))
        item['last_log_seq'] = item['next_log_seq'] - 1
        return item

    def save(self, task):
        data = redact_value({key: task.get(key) for key in _FIELDS})
        data["attempt"] = task.get("attempt", 1)
        data["params_json"] = json.dumps(redact_value(task.get("params", {})), ensure_ascii=False)
        data["progress_json"] = json.dumps(redact_value(task.get("progress", {})), ensure_ascii=False)
        data["metadata_json"] = json.dumps(redact_task_metadata(task.get("metadata", {})), ensure_ascii=False)
        columns = list(data)
        updates = ",".join(f"{key}=excluded.{key}" for key in columns if key != "id")
        with self._lock, self._db() as conn:
            conn.execute(f"INSERT INTO tasks ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                         f"ON CONFLICT(id) DO UPDATE SET {updates}", list(data.values()))

    def get(self, task_id, *, include_deleted=False):
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?" + ("" if include_deleted else " AND deleted=0"),
                               (task_id,)).fetchone()
            return self._decode(row)

    def list_page(self, *, limit=50, cursor=None, status=None, q=None, include_hidden=False):
        limit = max(1, min(100, int(limit)))
        clauses, args = ["1=1" if include_hidden else "deleted=0"], []
        if status:
            clauses.append("status=?")
            args.append(status)
        if q:
            clauses.append("(script_name LIKE ? ESCAPE '\\' OR params_json LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')")
            escaped = str(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.extend(["%" + escaped + "%"] * 3)
        if cursor:
            try:
                created, task_id = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if not isinstance(created, str) or not isinstance(task_id, str):
                    raise ValueError()
            except Exception as error:
                raise ValueError("无效的任务分页游标") from error
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            args.extend([created, created, task_id])
        with self._lock, self._db() as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE " + " AND ".join(clauses) +
                                " ORDER BY created_at DESC,id DESC LIMIT ?", [*args, limit + 1]).fetchall()
        items = [self._decode(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last["created_at"], last["id"]]).encode()).decode()
        return {"items": items, "next_cursor": next_cursor}

    def active_count(self):
        with self._lock, self._db() as conn:
            return conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running')").fetchone()[0]

    def active(self, *, limit=1000, status=None, after_id='', exclude_ids=()):
        clauses, args = ["status IN ('queued','running')", "id> ?"], [after_id]
        if status:
            clauses.append("status=?")
            args.append(status)
        if exclude_ids:
            clauses.append("id NOT IN (" + ",".join("?" for _ in exclude_ids) + ")")
            args.extend(exclude_ids)
        order = 'id' if status == 'running' else 'created_at,id'
        with self._lock, self._db() as conn:
            return [self._decode(row) for row in conn.execute(
                "SELECT * FROM tasks WHERE " + " AND ".join(clauses) + " ORDER BY " + order + " LIMIT ?",
                [*args, max(1, min(1000, limit))])]

    def by_content_key(self, content_key):
        with self._lock, self._db() as conn:
            return self._decode(conn.execute("SELECT * FROM tasks WHERE content_key=? AND status IN ('queued','running')",
                                             (content_key,)).fetchone())

    def iter_subscription_tasks(self, batch_size=100):
        """Bounded traversal for reconciliation, including terminal attempts."""
        after = ""
        while True:
            with self._lock, self._db() as conn:
                rows = conn.execute("SELECT * FROM tasks WHERE id>? AND json_type(metadata_json,'$.subscription')='object' "
                                    "ORDER BY id LIMIT ?", (after, max(1, min(1000, batch_size)))).fetchall()
            if not rows:
                return
            for row in rows:
                yield self._decode(row)
            after = rows[-1]["id"]

    def append(self, task_id, at, stream, text):
        text = redact_text(text.rstrip("\r\n"))
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + "… [日志行已截断]"
        with self._lock:
            if self._pending and (len(self._pending) >= BATCH_LINES or self._pending_bytes >= BATCH_BYTES):
                self.flush()
            if task_id not in self._sequences:
                task = self.get(task_id, include_deleted=True)
                if task is None:
                    raise KeyError(task_id)
                self._sequences[task_id] = task["next_log_seq"]
            seq = self._sequences[task_id]
            self._sequences[task_id] += 1
            self._pending.append((task_id, seq, at, stream, text))
            self._pending_bytes += len(text.encode("utf-8"))
            if len(self._pending) >= BATCH_LINES or self._pending_bytes >= BATCH_BYTES:
                self.flush()
            return {"seq": seq, "time": at, "stream": stream, "text": text}

    def flush(self):
        with self._lock:
            if not self._pending:
                return
            with self._db() as conn:
                conn.executemany("INSERT INTO task_logs(task_id,seq,at,stream,text) VALUES (?,?,?,?,?)", self._pending)
                touched = {row[0] for row in self._pending}
                conn.executemany("UPDATE tasks SET next_log_seq=MAX(next_log_seq,?) WHERE id=?",
                                 [(self._sequences[task_id], task_id) for task_id in touched])
            self._pending.clear()
            self._pending_bytes = 0

    def _archive_path(self, relative):
        path = (self.log_dir / relative).resolve()
        if not path.is_relative_to(self.log_dir.resolve()):
            raise ValueError("非法日志归档路径")
        return path

    def archive(self, task_id):
        """Each gzip chunk is installed before its rows are transactionally retired."""
        with self._task_log_lock(task_id):
            self.flush()
            task = self.get(task_id, include_deleted=True)
            if not task or task["status"] in ("queued", "running"):
                return
            while True:
                with self._lock, self._db() as conn:
                    rows = conn.execute("SELECT seq,at,stream,text FROM task_logs WHERE task_id=? ORDER BY seq LIMIT ?",
                                        (task_id, ARCHIVE_LINES)).fetchall()
                if not rows:
                    with self._lock:
                        if not any(row[0] == task_id for row in self._pending):
                            self._sequences.pop(task_id, None)
                    return
                # IDs are generated internally, still reject caller-controlled path components.
                if not task_id.isalnum():
                    raise ValueError("非法任务标识")
                relative = f"{task_id}/{rows[0]['seq']:012d}-{rows[-1]['seq']:012d}.jsonl.gz"
                target = self._archive_path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix(".tmp")
                try:
                    with open(temp, "wb") as raw:
                        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                            for row in rows:
                                compressed.write((json.dumps(dict(row), ensure_ascii=False) + "\n").encode("utf-8"))
                        raw.flush()
                        os.fsync(raw.fileno())
                    os.replace(temp, target)
                    with self._lock, self._db() as conn:
                        conn.execute("INSERT OR REPLACE INTO task_log_archives VALUES (?,?,?,?,?,?)",
                                     (task_id, rows[0]["seq"], rows[-1]["seq"], relative, len(rows), target.stat().st_size))
                        conn.execute("DELETE FROM task_logs WHERE task_id=? AND seq BETWEEN ? AND ?",
                                     (task_id, rows[0]["seq"], rows[-1]["seq"]))
                finally:
                    if temp.exists():
                        temp.unlink()

    def read_logs(self, task_id, *, after=0, limit=200, diagnostic=False, tail=False):
        after, limit = max(0, int(after)), max(1, min(1000, int(limit)))
        with self._task_log_lock(task_id):
            self.flush()
            task = self.get(task_id, include_deleted=True)
            last_seq = task['next_log_seq'] - 1 if task else 0
            if tail:
                after = max(0, last_seq - limit)
            rows = []
            with self._db() as conn:
                archives = conn.execute("SELECT path FROM task_log_archives WHERE task_id=? AND end_seq>? ORDER BY start_seq LIMIT ?",
                                        (task_id, after, limit + 1)).fetchall()
                for archive in archives:
                    with gzip.open(self._archive_path(archive["path"]), "rt", encoding="utf-8") as stream:
                        for line in stream:
                            row = json.loads(line)
                            if row["seq"] > after:
                                rows.append(row)
                                if len(rows) > limit:
                                    break
                    if len(rows) > limit:
                        break
                if len(rows) <= limit:
                    rows.extend(dict(row) for row in conn.execute(
                        "SELECT seq,at,stream,text FROM task_logs WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                        (task_id, after, limit + 1 - len(rows))))
            items = [{"seq": row["seq"], "time": row["at"], "stream": row["stream"],
                      "text": redact_text(row["text"], diagnostic=diagnostic)} for row in rows[:limit]]
            return {"items": items, "next_cursor": items[-1]["seq"] if items else after,
                    "has_more": len(rows) > limit, "last_log_seq": last_seq}

    def delete_record(self, task_id):
        with self._lock, self._db() as conn:
            return bool(conn.execute("UPDATE tasks SET deleted=1 WHERE id=? AND status NOT IN ('queued','running')",
                                     (task_id,)).rowcount)

    def restore_record(self, task_id):
        with self._lock, self._db() as conn:
            return bool(conn.execute('UPDATE tasks SET deleted=0 WHERE id=? AND deleted=1', (task_id,)).rowcount)

    def delete_logs(self, task_id):
        with self._task_log_lock(task_id):
            self.flush()
            task = self.get(task_id, include_deleted=True)
            if task and task["status"] in ("queued", "running"):
                raise ValueError("运行中的任务不能删除日志")
            with self._lock, self._db() as conn:
                paths = [self._archive_path(row["path"]) for row in conn.execute(
                    "SELECT path FROM task_log_archives WHERE task_id=?", (task_id,))]
                conn.execute("DELETE FROM task_logs WHERE task_id=?", (task_id,))
                conn.execute("DELETE FROM task_log_archives WHERE task_id=?", (task_id,))
            for path in paths:
                path.unlink(missing_ok=True)
            with self._lock:
                self._sequences.pop(task_id, None)
