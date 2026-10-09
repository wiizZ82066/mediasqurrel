"""订阅模块：博主订阅 CRUD + 定时扫描调度框架。

数据层（SQLite）已就绪；扫描引擎采用插件式 Scanner 接口，
后续新增平台扫描器只需实现 scan(blogger) -> list[Item] 并注册。
"""
import asyncio
import datetime as _dt
import os
from typing import Callable, Optional

from . import config, db, subscription_store
from .redaction import redact_text

# Live snapshots are cached in memory and persisted with their scan run.
SCAN_STATES: dict[int, dict] = {}
_SCANNING: set[int] = set()


# ---------------------------------------------------------------- 数据层

_connect = db.connect


def init_db():
    db.migrate()
    subscription_store.recover_scans()
    SCAN_STATES.clear()
    SCAN_STATES.update(subscription_store.latest_snapshots())


def list_subs() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions ORDER BY id DESC"
        ).fetchall()
    return [{**dict(r), "scan": SCAN_STATES.get(r["id"])} for r in rows]


def add_sub(platform: str, blogger_id: str, nickname: str = "",
            homepage: str = "", interval_minutes: int = 30) -> dict:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions"
            " (platform, blogger_id, nickname, homepage, interval_minutes)"
            " VALUES (?,?,?,?,?)",
            (platform, blogger_id, nickname, homepage, interval_minutes),
        )
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE platform=? AND blogger_id=?",
            (platform, blogger_id),
        ).fetchone()
    return dict(row)


def update_sub(sub_id: int, **fields) -> bool:
    allowed = {"nickname", "homepage", "enabled", "interval_minutes"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return False
    vals.append(sub_id)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE subscriptions SET {', '.join(sets)} WHERE id=?", vals
        )
        return cur.rowcount > 0


def remove_sub(sub_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
        if sub_id not in _SCANNING:
            SCAN_STATES.pop(sub_id, None)
        return cur.rowcount > 0


def has_seen(platform: str, blogger_id: str, item_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_items WHERE platform=? AND blogger_id=? AND item_id=?",
            (platform, blogger_id, item_id),
        ).fetchone()
    return row is not None


def mark_seen(platform: str, blogger_id: str, item_id: str):
    """Compatibility helper: explicitly establish a baseline, not a download."""
    timestamp = subscription_store.now()
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_items (platform, blogger_id, item_id, created_at)"
            " VALUES (?,?,?,?)",
            (platform, blogger_id, item_id, timestamp),
        )
        conn.execute(
            "INSERT OR IGNORE INTO subscription_items "
            "(platform,blogger_id,item_id,status,discovered_at,updated_at) VALUES (?,?,?,'baseline',?,?)",
            (platform, blogger_id, item_id, timestamp, timestamp),
        )
        conn.execute(
            "INSERT OR IGNORE INTO subscription_state(platform,blogger_id,baseline_completed_at) "
            "VALUES (?,?,?)", (platform, blogger_id, timestamp),
        )


# ---------------------------------------------------------------- 本地完整内容校验


def _local_item_ids() -> set[str]:
    """Only verified manifests establish completion, never legacy context files.

    This shallow metadata walk intentionally does not invoke cover generation or
    face detection. A later media index can provide the same namespaced keys.
    """
    from .archive import entry_status, read_manifest
    ids: set[str] = set()
    try:
        with os.scandir(config.LIBRARY_ROOT) as authors:
            for author in authors:
                if not author.is_dir(follow_symlinks=False) or author.name.startswith("."):
                    continue
                try:
                    with os.scandir(author.path) as entries:
                        for entry in entries:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                            if entry_status(entry.path, verify_files=True) != "complete":
                                continue
                            manifest = read_manifest(entry.path) or {}
                            if manifest.get("platform") and manifest.get("item_id"):
                                ids.add(f"{manifest['platform']}:{manifest['item_id']}")
                except OSError:
                    continue
    except OSError:
        pass
    return ids


def task_complete(task):
    """Verify the actual output manifest before accepting a successful task."""
    from .archive import entry_status, read_manifest
    folder = task.get("output_dir")
    if not folder or entry_status(folder, verify_files=True) != "complete":
        return False
    manifest = read_manifest(folder) or {}
    source = (task.get("metadata") or {}).get("subscription") or {}
    if source:
        return (manifest.get("platform") == source.get("platform") and
                str(manifest.get("item_id")) == str(source.get("item_id")))
    return True


claim_item = subscription_store.claim_item
mark_enqueued = subscription_store.mark_enqueued
mark_dispatch_failed = subscription_store.mark_dispatch_failed
record_task_result = subscription_store.record_task_result
link_retry = subscription_store.link_retry
list_scans = subscription_store.list_scans


def reconcile_tasks(tasks, complete_checker=None):
    subscription_store.reconcile_tasks(tasks, complete_checker or task_complete)
    SCAN_STATES.update(subscription_store.latest_snapshots())


# ---------------------------------------------------------------- 扫描引擎（插件式）

# Scanner: async fn(sub: dict) -> list[dict]
# 每个元素: {"item_id": str, "url": str, "params": dict(给下载脚本的参数), "title": str}
SCANNERS: dict[str, Callable[[dict], list]] = {}


def register_scanner(platform: str, fn):
    SCANNERS[platform] = fn


async def _scan_progress(sub_id, status, label, percent=None, **fields):
    from .task_manager import broadcast

    previous = SCAN_STATES.get(sub_id, {})
    state = {
        **previous, "status": status, "revision": previous.get("revision", 0) + 1,
        "progress": {"label": label, "percent": percent, **fields},
    }
    SCAN_STATES[sub_id] = state
    if state.get("scan_id"):
        subscription_store.save_scan_snapshot(state["scan_id"], state)
    await broadcast({"type": "sub_scan", "sub_id": sub_id, "scan": state})


async def scan_sub(sub: dict, trigger: str = "manual") -> dict:
    """扫描单个订阅，发现新内容即返回待下载列表（由调用方入队）。"""
    platform = sub["platform"]
    scanner = SCANNERS.get(platform)
    result = {
        "sub_id": sub["id"],
        "platform": platform,
        "blogger_id": sub["blogger_id"],
        "new_items": [],
        "error": None,
    }
    # Manual and scheduled scans share one state and must not dispatch duplicates.
    if sub["id"] in _SCANNING:
        return {**result, "already_running": True}
    _SCANNING.add(sub["id"])
    scan_id = None
    items = []
    try:
        scan_id = subscription_store.begin_scan(sub, trigger)
        SCAN_STATES[sub["id"]] = {**SCAN_STATES.get(sub["id"], {}), "scan_id": scan_id}
        result["scan_id"] = scan_id
        await _scan_progress(sub["id"], "running", "正在获取博主最新内容…")
        try:
            if not scanner:
                raise ValueError(f"平台 '{platform}' 的扫描器尚未实现（等待接入）")
            response = scanner(sub)
            items = await response if asyncio.iscoroutine(response) else response
            await _scan_progress(sub["id"], "running", "正在检查完整存档与下载记录…")
            local_ids = await asyncio.to_thread(_local_item_ids)
            for index, it in enumerate(items):
                await _scan_progress(
                    sub["id"], "running", "正在比对最新内容", index * 100 / len(items),
                    completed=index, total=len(items), unit="条内容",
                )
            first_scan, result["new_items"] = subscription_store.record_discoveries(sub, items, local_ids)
            if first_scan:
                result["baseline"] = len({str(it["item_id"]) for it in items})
        except Exception as e:
            result["error"] = redact_text(str(e))

        last_scan_at = subscription_store.now()
        last_status = "error" if result["error"] else "ok"
        with _connect() as conn:
            conn.execute(
                "UPDATE subscriptions SET last_scan_at=?, last_status=?, last_error=? WHERE id=?",
                (last_scan_at, last_status, result["error"] or "", sub["id"]),
            )
        SCAN_STATES[sub["id"]].update(
            last_scan_at=last_scan_at, last_status=last_status,
            last_error=result["error"] or "",
        )
        if result["error"]:
            progress = SCAN_STATES[sub["id"]]["progress"]
            await _scan_progress(sub["id"], "failed", "扫描失败，请稍后重试",
                                 progress.get("percent"))
        else:
            if "baseline" in result:
                label = f"已建立基线，共 {result['baseline']} 条内容"
            elif result["new_items"]:
                label = f"扫描完成，{len(result['new_items'])} 条内容待下载或重试"
            else:
                label = "扫描完成，暂无新内容"
            await _scan_progress(sub["id"], "success", label, 100,
                                 completed=len(items), total=len(items), unit="条内容")
        subscription_store.finish_scan(
            scan_id, "failed" if result["error"] else "success", SCAN_STATES[sub["id"]],
            items_count=len(items), new_count=len(result["new_items"]),
            baseline="baseline" in result, error=result["error"],
        )
    except asyncio.CancelledError:
        await _scan_progress(sub["id"], "cancelled", "扫描已中断")
        if scan_id:
            subscription_store.finish_scan(scan_id, "cancelled", SCAN_STATES[sub["id"]], error="扫描已中断")
        raise
    except Exception as e:
        await _scan_progress(sub["id"], "failed", "扫描失败，请稍后重试")
        if scan_id:
            subscription_store.finish_scan(scan_id, "failed", SCAN_STATES[sub["id"]], error=redact_text(str(e)))
        raise
    finally:
        _SCANNING.discard(sub["id"])
    return result


# ---------------------------------------------------------------- 调度循环

_loop_task: Optional[asyncio.Task] = None


async def _scheduler_loop(on_new_items=None):
    """每分钟检查一遍所有启用的订阅，到期的执行扫描。

    on_new_items: async fn(new_items: list[dict]) —— 供 main 注入"自动创建下载任务"。
    """
    while True:
        try:
            for sub in list_subs():
                if not sub["enabled"]:
                    continue
                interval = max(5, int(sub["interval_minutes"] or 30))
                last = sub["last_scan_at"] or ""
                due = True
                if last:
                    try:
                        last_dt = _dt.datetime.fromisoformat(last)
                        current = _dt.datetime.now(last_dt.tzinfo) if last_dt.tzinfo else _dt.datetime.now()
                        due = (current - last_dt).total_seconds() >= interval * 60
                    except ValueError:
                        pass
                if not due:
                    continue
                result = await scan_sub(sub, trigger="interval")
                if result["new_items"] and on_new_items:
                    try:
                        await on_new_items(result["new_items"])
                    except Exception as e:
                        print(f"[watcher] 入队失败: {redact_text(str(e))}")
                elif result["error"]:
                    print(f"[watcher] {sub.get('nickname') or sub['blogger_id']}: {result['error']}")
        except Exception as e:
            print(f"[watcher] 调度循环异常: {redact_text(str(e))}")
        await asyncio.sleep(60)


def start_scheduler(on_new_items=None):
    global _loop_task
    if _loop_task and not _loop_task.done():
        return
    _loop_task = asyncio.create_task(_scheduler_loop(on_new_items))


async def stop_scheduler():
    """Stop dispatch before task-manager shutdown and await scan cleanup."""
    global _loop_task
    task, _loop_task = _loop_task, None
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
