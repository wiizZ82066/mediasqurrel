"""Persistent subscription discovery and scan history.

The application database owns schema versioning. Importing this module never
opens a database; ``db.migrate`` applies SCHEMA with the other application tables.
"""
import datetime as dt
import json
import math
import uuid

from . import db
from .redaction import redact_text


SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    blogger_id TEXT NOT NULL,
    nickname TEXT DEFAULT '',
    homepage TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    interval_minutes INTEGER DEFAULT 30,
    last_scan_at TEXT DEFAULT '',
    last_status TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    UNIQUE(platform, blogger_id)
);
CREATE TABLE IF NOT EXISTS seen_items (
    platform TEXT NOT NULL,
    blogger_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    created_at TEXT DEFAULT '',
    PRIMARY KEY(platform, blogger_id, item_id)
);
CREATE TABLE IF NOT EXISTS subscription_state (
    platform TEXT NOT NULL,
    blogger_id TEXT NOT NULL,
    baseline_completed_at TEXT NOT NULL,
    PRIMARY KEY(platform, blogger_id)
);
CREATE TABLE IF NOT EXISTS subscription_items (
    platform TEXT NOT NULL,
    blogger_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    script_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    task_id TEXT,
    claim_token TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(platform, blogger_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_subscription_items_task
    ON subscription_items(task_id);
CREATE INDEX IF NOT EXISTS idx_subscription_items_content
    ON subscription_items(platform, item_id, status);
CREATE TABLE IF NOT EXISTS subscription_scans (
    id TEXT PRIMARY KEY,
    sub_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    blogger_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    items_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    baseline INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_subscription_scans_sub_started
    ON subscription_scans(sub_id, started_at DESC, id DESC);
INSERT OR IGNORE INTO subscription_state(platform, blogger_id, baseline_completed_at)
    SELECT platform, blogger_id, COALESCE(NULLIF(MAX(created_at), ''), datetime('now'))
    FROM seen_items GROUP BY platform, blogger_id;
INSERT OR IGNORE INTO subscription_items(
    platform, blogger_id, item_id, status, discovered_at, updated_at)
    SELECT platform, blogger_id, item_id, 'legacy_seen',
        COALESCE(NULLIF(created_at, ''), datetime('now')),
        COALESCE(NULLIF(created_at, ''), datetime('now'))
    FROM seen_items;
"""

RETRYABLE = {"discovered", "failed", "interrupted"}
ACTIVE = {"dispatching", "queued", "running"}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def record_discoveries(sub, items, complete_keys=()):
    """Commit a whole successful baseline atomically, including an empty one.

Legacy seen records and explicit baseline entries remain suppressed. File
deletion never changes a previously downloaded item back into new content.
Only a verified complete manifest can establish a local download as complete.
"""
    timestamp = now()
    platform, blogger = sub["platform"], sub["blogger_id"]
    candidates = []
    complete_keys = set(complete_keys)
    unique = {str(item["item_id"]): item for item in items}
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        first = conn.execute(
            "SELECT 1 FROM subscription_state WHERE platform=? AND blogger_id=?",
            (platform, blogger),
        ).fetchone() is None
        for item_id, item in unique.items():
            identity = (platform, blogger, item_id)
            complete = f"{platform}:{item_id}" in complete_keys
            initial = "downloaded" if complete else ("baseline" if first else "discovered")
            conn.execute(
                "INSERT OR IGNORE INTO subscription_items "
                "(platform,blogger_id,item_id,status,discovered_at,updated_at) VALUES (?,?,?,?,?,?)",
                (*identity, initial, timestamp, timestamp),
            )
            conn.execute(
                "UPDATE subscription_items SET title=?,params_json=?,script_id=? WHERE "
                "platform=? AND blogger_id=? AND item_id=?",
                (str(item.get("title") or ""), _json(item.get("params") or {}),
                 item.get("script_id") or platform, *identity),
            )
            if complete:
                # An active attempt remains owned by its task until settlement.
                conn.execute(
                    "UPDATE subscription_items SET status='downloaded',updated_at=?,last_error='' "
                    "WHERE platform=? AND blogger_id=? AND item_id=? "
                    "AND status NOT IN ('dispatching','queued','running')",
                    (timestamp, *identity),
                )
            conn.execute(
                "INSERT OR IGNORE INTO seen_items(platform,blogger_id,item_id,created_at) VALUES (?,?,?,?)",
                (*identity, timestamp),
            )
        conn.execute(
            "INSERT OR IGNORE INTO subscription_state(platform,blogger_id,baseline_completed_at) "
            "VALUES (?,?,?)", (platform, blogger, timestamp),
        )
        if not first:
            pending = conn.execute(
                "SELECT * FROM subscription_items WHERE platform=? AND blogger_id=? "
                "AND status IN ('discovered','failed','interrupted') ORDER BY discovered_at,item_id",
                (platform, blogger),
            ).fetchall()
            for row in pending:
                item_id = row["item_id"]
                if f"{platform}:{item_id}" in complete_keys:
                    conn.execute(
                        "UPDATE subscription_items SET status='downloaded',updated_at=?,last_error='' "
                        "WHERE platform=? AND blogger_id=? AND item_id=?",
                        (timestamp, platform, blogger, item_id),
                    )
                    continue
                # A failed download remains retryable even after leaving the
                # platform's current first page. Its saved URL is the source.
                item = unique.get(item_id) or {
                    "item_id": item_id, "script_id": row["script_id"],
                    "params": json.loads(row["params_json"]), "title": row["title"],
                }
                candidates.append({**item, "item_id": item_id, "platform": platform,
                                   "blogger_id": blogger, "sub_id": sub["id"]})
    return first, candidates


def claim_item(platform, blogger_id, item_id):
    """Reserve dispatch before creating a task; concurrent scans cannot duplicate it."""
    token = uuid.uuid4().hex
    identity = (platform, blogger_id, str(item_id))
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT 1 FROM subscription_items WHERE platform=? AND item_id=? "
            "AND status IN ('dispatching','queued','running','downloaded') LIMIT 1",
            (platform, str(item_id)),
        ).fetchone()
        if active:
            return None
        cursor = conn.execute(
            "UPDATE subscription_items SET status='dispatching',claim_token=?,task_id=NULL,"
            "updated_at=?,last_error='' WHERE platform=? AND blogger_id=? AND item_id=? "
            "AND status IN ('discovered','failed','interrupted')",
            (token, now(), *identity),
        )
    return token if cursor.rowcount else None


def mark_enqueued(platform, blogger_id, item_id, task_id, claim_token):
    with db.connect() as conn:
        cursor = conn.execute(
            "UPDATE subscription_items SET status='queued',task_id=?,updated_at=? "
            "WHERE platform=? AND blogger_id=? AND item_id=? AND claim_token=? "
            "AND status='dispatching'",
            (task_id, now(), platform, blogger_id, str(item_id), claim_token),
        )
    return bool(cursor.rowcount)


def mark_dispatch_failed(platform, blogger_id, item_id, error, claim_token):
    with db.connect() as conn:
        conn.execute(
            "UPDATE subscription_items SET status='failed',updated_at=?,last_error=?,claim_token=NULL "
            "WHERE platform=? AND blogger_id=? AND item_id=? AND claim_token=? AND status='dispatching'",
            (now(), redact_text(str(error)), platform, blogger_id, str(item_id), claim_token),
        )


def record_task_result(task_id, status, error="", complete_verified=False):
    if status not in {"queued", "running", "success", "failed", "interrupted", "cancelled"}:
        return False
    state = status
    if status == "success":
        state = "downloaded" if complete_verified else "failed"
        if not complete_verified:
            error = "下载进程已结束，但完整性清单未通过验证；等待重试"
    with db.connect() as conn:
        cursor = conn.execute(
            "UPDATE subscription_items SET status=?,updated_at=?,last_error=? WHERE task_id=?",
            (state, now(), redact_text(str(error or "")), task_id),
        )
    return bool(cursor.rowcount)


def link_retry(parent_task_id, task_id):
    """An explicit retry can revive a cancelled item while preserving old tasks."""
    with db.connect() as conn:
        cursor = conn.execute(
            "UPDATE subscription_items SET status='queued',task_id=?,updated_at=?,last_error='' "
            "WHERE task_id=? AND status IN ('failed','interrupted','cancelled')",
            (task_id, now(), parent_task_id),
        )
    return bool(cursor.rowcount)


def begin_scan(sub, trigger):
    scan_id = uuid.uuid4().hex
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO subscription_scans(id,sub_id,platform,blogger_id,trigger,status,started_at) "
            "VALUES (?,?,?,?,?,'running',?)",
            (scan_id, sub["id"], sub["platform"], sub["blogger_id"], trigger, now()),
        )
    return scan_id


def save_scan_snapshot(scan_id, snapshot):
    with db.connect() as conn:
        conn.execute("UPDATE subscription_scans SET snapshot_json=? WHERE id=?",
                     (_json(snapshot), scan_id))


def finish_scan(scan_id, status, snapshot, *, items_count=0, new_count=0, baseline=False, error=""):
    with db.connect() as conn:
        conn.execute(
            "UPDATE subscription_scans SET status=?,finished_at=?,items_count=?,new_count=?,baseline=?,"
            "error=?,snapshot_json=? WHERE id=?",
            (status, now(), items_count, new_count, int(baseline), redact_text(str(error or "")),
             _json(snapshot), scan_id),
        )


def latest_snapshots():
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT sub_id,snapshot_json FROM subscription_scans WHERE rowid IN "
            "(SELECT MAX(rowid) FROM subscription_scans GROUP BY sub_id)"
        ).fetchall()
    result = {}
    for row in rows:
        try:
            snapshot = json.loads(row["snapshot_json"])
            if snapshot:
                result[row["sub_id"]] = snapshot
        except (ValueError, TypeError):
            continue
    return result


def public_coverage(snapshot_json):
    """Read a small whitelisted projection, never return an arbitrary snapshot."""
    if not isinstance(snapshot_json, str) or len(snapshot_json) > 16384:
        return {}
    try:
        snapshot = json.loads(snapshot_json)
    except (ValueError, TypeError):
        return {}
    coverage = snapshot.get('coverage') if isinstance(snapshot, dict) else None
    if not isinstance(coverage, dict):
        return {}
    result = {}
    if isinstance(coverage.get('complete'), bool):
        result['complete'] = coverage['complete']
    for key in ('reason', 'scope', 'oldest_at', 'cutoff_at'):
        if isinstance(coverage.get(key), str):
            result[key] = redact_text(coverage[key][:200])
    for key in ('pages', 'items', 'elapsed_seconds'):
        value = coverage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            result[key] = value
    return result


def list_scans(sub_id=None, *, limit=50, offset=0):
    limit, offset = min(200, max(1, int(limit))), max(0, int(offset))
    clause = " WHERE sub_id=?" if sub_id is not None else ""
    values = (sub_id,) if sub_id is not None else ()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id,sub_id,platform,blogger_id,trigger,status,started_at,finished_at,"
            "items_count,new_count,baseline,error,substr(snapshot_json,1,16385) AS snapshot_json FROM subscription_scans" + clause +
            " ORDER BY started_at DESC,rowid DESC LIMIT ? OFFSET ?", (*values, limit, offset),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['coverage'] = public_coverage(item.pop('snapshot_json'))
        result.append(item)
    return result


def recover_scans():
    """A killed scan is interrupted, never silently converted into success."""
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM subscription_scans WHERE status='running'").fetchall()
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot_json"])
            except (ValueError, TypeError):
                snapshot = {}
            snapshot.update(status="interrupted", revision=snapshot.get("revision", 0) + 1,
                            last_status="error", last_error="上次扫描因程序退出而中断")
            snapshot["progress"] = {**snapshot.get("progress", {}), "label": "扫描已中断，请重新扫描"}
            conn.execute(
                "UPDATE subscription_scans SET status='interrupted',finished_at=?,error=?,snapshot_json=? "
                "WHERE id=?", (now(), snapshot["last_error"], _json(snapshot), row["id"]),
            )
            conn.execute("UPDATE subscriptions SET last_status='error',last_error=? WHERE id=?",
                         (snapshot["last_error"], row["sub_id"]))


def reconcile_tasks(tasks, complete_checker=None):
    """Repair task links after a restart, including a crash during dispatch."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM subscription_items WHERE status IN ('dispatching','queued','running')"
        ).fetchall()
    wanted_ids = {row["task_id"] for row in rows if row["task_id"]}
    wanted_claims = {row["claim_token"] for row in rows if row["claim_token"]}
    by_id, by_claim = {}, {}
    for task in tasks:
        if task["id"] in wanted_ids:
            by_id[task["id"]] = task
        metadata = task.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = {}
        source = metadata.get("subscription") or {}
        if source.get("claim_token") in wanted_claims:
            by_claim[source["claim_token"]] = task
    for row in rows:
        task = by_id.get(row["task_id"]) or by_claim.get(row["claim_token"])
        if task:
            if row["status"] == "dispatching":
                mark_enqueued(row["platform"], row["blogger_id"], row["item_id"],
                              task["id"], row["claim_token"])
            complete = bool(complete_checker and task["status"] == "success" and complete_checker(task))
            record_task_result(task["id"], task["status"], task.get("error") or "", complete)
        else:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE subscription_items SET status='interrupted',updated_at=?,last_error=?,"
                    "claim_token=NULL WHERE platform=? AND blogger_id=? AND item_id=?",
                    (now(), "上次入队或下载中断，等待重新扫描", row["platform"], row["blogger_id"], row["item_id"]),
                )
