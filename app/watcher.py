"""订阅模块：博主订阅 CRUD + 定时扫描调度框架。

数据层（SQLite）已就绪；扫描引擎采用插件式 Scanner 接口，
后续新增平台扫描器只需实现 scan(blogger) -> list[Item] 并注册。
"""
import asyncio
from collections import OrderedDict
import datetime as _dt
import os
from pathlib import Path
import threading
from typing import Callable, Optional

from . import config, db, subscription_store, scheduling
from .redaction import redact_text

# Live snapshots are cached in memory and persisted with their scan run.
SCAN_STATES: dict[int, dict] = {}
_SCANNING: set[int] = set()
_SCAN_FUTURES = {}
_SCAN_CANCEL_EVENTS = {}
_SCAN_TASKS = set()
_scan_semaphore = None
_LOCAL_LEGACY_CACHE = OrderedDict()
_LOCAL_LEGACY_LOCK = threading.Lock()
_LOCAL_AUTHOR_LIMIT = 64
_LOCAL_ENTRY_LIMIT = 512


# ---------------------------------------------------------------- 数据层

_connect = db.connect


def init_db():
    global _scan_semaphore
    db.migrate()
    subscription_store.recover_scans()
    SCAN_STATES.clear()
    SCAN_STATES.update(subscription_store.latest_snapshots())
    scheduling.recover_runs()
    _scan_semaphore = None
    _LOCAL_LEGACY_CACHE.clear()
    for sub in list_subs():
        scheduling.ensure_interval(sub)


def list_subs() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions ORDER BY id DESC"
        ).fetchall()
    result = []
    for row in rows:
        sub = dict(row)
        source = sub.pop('avatar_source', '')
        sub.update(scan=SCAN_STATES.get(sub['id']), avatar_url=f"/api/subs/{sub['id']}/avatar" if source else '')
        result.append(sub)
    return result


def add_sub(platform: str, blogger_id: str, nickname: str = "",
            homepage: str = "", interval_minutes: int = 30, avatar_url: str = '') -> dict:
    interval_minutes = scheduling.normalize_plan({'kind': 'interval', 'minutes': interval_minutes})['minutes']
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions"
            " (platform, blogger_id, nickname, homepage, interval_minutes, avatar_source)"
            " VALUES (?,?,?,?,?,?)",
            (platform, blogger_id, nickname, homepage, interval_minutes, str(avatar_url or '')),
        )
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE platform=? AND blogger_id=?",
            (platform, blogger_id),
        ).fetchone()
    result = dict(row)
    source = result.pop('avatar_source', '')
    result['avatar_url'] = f"/api/subs/{result['id']}/avatar" if source else ''
    scheduling.ensure_interval(result)
    return result


def update_sub(sub_id: int, **fields) -> bool:
    if 'interval_minutes' in fields:
        fields['interval_minutes'] = scheduling.normalize_plan({'kind': 'interval', 'minutes': fields['interval_minutes']})['minutes']
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
        changed = cur.rowcount > 0
        row = conn.execute('SELECT * FROM subscriptions WHERE id=?', (sub_id,)).fetchone()
    if changed and row and 'interval_minutes' in fields:
        scheduling.ensure_interval(dict(row))
    return changed


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


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _bounded_legacy_items():
    """Small, cached compatibility window; larger old libraries require indexing.

    Directory mtimes detect additions without rereading every entry/medium on
    every scan. Both enumeration and cache memory have fixed upper bounds.
    """
    from .archive import read_manifest
    root = Path(config.LIBRARY_ROOT).resolve()
    key = str(root)
    with _LOCAL_LEGACY_LOCK:
        cached = _LOCAL_LEGACY_CACHE.get(key)
        if cached and all(_mtime(path) == modified for path, modified in cached['watched']):
            _LOCAL_LEGACY_CACHE.move_to_end(key)
            return cached['items']
        watched, items, examined = [(key, _mtime(root))], {}, 0
        try:
            with os.scandir(root) as authors:
                for number, author in enumerate(authors):
                    if number >= _LOCAL_AUTHOR_LIMIT or examined >= _LOCAL_ENTRY_LIMIT:
                        break
                    if not author.is_dir(follow_symlinks=False) or author.name.startswith('.'):
                        continue
                    if not Path(author.path).resolve().is_relative_to(root):
                        continue
                    watched.append((author.path, _mtime(author.path)))
                    try:
                        with os.scandir(author.path) as entries:
                            for entry in entries:
                                examined += 1
                                if examined > _LOCAL_ENTRY_LIMIT:
                                    break
                                if not entry.is_dir(follow_symlinks=False) or not Path(entry.path).resolve().is_relative_to(root):
                                    continue
                                marker = Path(entry.path) / 'entry.json'
                                if not marker.is_file() or marker.stat().st_size > 1024 * 1024:
                                    continue
                                manifest = read_manifest(entry.path) or {}
                                if manifest.get('platform') and manifest.get('item_id'):
                                    identity = f"{manifest['platform']}:{manifest['item_id']}"
                                    items.setdefault(identity, entry.path)
                    except OSError:
                        continue
        except OSError:
            pass
        _LOCAL_LEGACY_CACHE[key] = {'watched': watched, 'items': items}
        _LOCAL_LEGACY_CACHE.move_to_end(key)
        while len(_LOCAL_LEGACY_CACHE) > 8:
            _LOCAL_LEGACY_CACHE.popitem(last=False)
        return items


def _local_item_ids(items, platform) -> set[str]:
    """Indexed point lookups for this scan only; verify actual matching files."""
    from .archive import entry_status, read_manifest
    ids, candidates = set(), list(dict.fromkeys(str(item['item_id']) for item in items))[:2000]
    if not candidates:
        return ids

    def verified(folder, identity):
        marker = Path(folder) / 'entry.json'
        try:
            if marker.stat().st_size > 1024 * 1024:
                return False
        except OSError:
            return False
        manifest = read_manifest(folder) or {}
        return (f"{manifest.get('platform')}:{manifest.get('item_id')}" == identity and
                entry_status(folder, verify_files=True) == 'complete')

    with _connect() as conn:
        for item_id in candidates:
            identity = f'{platform}:{item_id}'
            rows = conn.execute('SELECT e.rel_dir,r.path FROM media_entries e JOIN media_roots r ON r.id=e.root_id '
                                "WHERE e.platform=? AND e.item_id=? AND e.archive_status='complete' "
                                "AND e.availability='present' LIMIT 8", (platform, item_id))
            for row in rows:
                root = Path(row['path']).resolve()
                folder = (root / row['rel_dir']).resolve()
                if folder != root and folder.is_relative_to(root) and verified(folder, identity):
                    ids.add(identity)
                    break
    missing = {f'{platform}:{item_id}' for item_id in candidates} - ids
    if missing:
        for identity, folder in _bounded_legacy_items().items():
            if identity in missing and verified(folder, identity):
                ids.add(identity)
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
    return await _scan_guarded(sub, trigger)


async def _scan_guarded(sub, trigger, *, join_running=False):
    global _scan_semaphore
    if getattr(config, 'MAINTENANCE_ACTIVE', False):
        raise ValueError('正在进行数据维护，完成后可继续扫描')
    sub_id = sub['id']
    if sub_id in _SCANNING:
        future = _SCAN_FUTURES.get(sub_id)
        if join_running and future:
            return await asyncio.shield(future)
        return {'sub_id': sub_id, 'platform': sub['platform'], 'blogger_id': sub['blogger_id'],
                'new_items': [], 'error': None, 'already_running': True}
    _SCANNING.add(sub_id)
    future = _SCAN_FUTURES[sub_id] = asyncio.get_running_loop().create_future()
    cancel = _SCAN_CANCEL_EVENTS[sub_id] = threading.Event()
    task = asyncio.current_task()
    _SCAN_TASKS.add(task)
    if _scan_semaphore is None:
        _scan_semaphore = asyncio.Semaphore(max(1, min(4, getattr(config, 'MAX_SCAN_CONCURRENCY', 2))))
    try:
        async with _scan_semaphore:
            if getattr(config, 'MAINTENANCE_ACTIVE', False):
                raise ValueError('正在进行数据维护，完成后可继续扫描')
            context = {**sub, 'scan_options': scheduling.scan_options(sub_id), '_cancel_event': cancel}
            result = await _perform_scan(context, trigger)
            if not future.done():
                future.set_result(result)
            return result
    except BaseException as error:
        cancel.set()
        if not future.done():
            future.set_result({'sub_id': sub_id, 'new_items': [], 'error': redact_text(str(error)) or '扫描已取消'})
        raise
    finally:
        _SCANNING.discard(sub_id)
        _SCAN_FUTURES.pop(sub_id, None)
        _SCAN_CANCEL_EVENTS.pop(sub_id, None)
        _SCAN_TASKS.discard(task)


async def _perform_scan(sub: dict, trigger: str = 'manual') -> dict:
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
            response = await response if asyncio.iscoroutine(response) else response
            if isinstance(response, dict):
                items = response.get('items')
                result['coverage'] = response.get('coverage') or {}
                if response.get('avatar_source'):
                    from .avatars import update_source
                    update_source(sub['id'], response['avatar_source'])
                if not isinstance(items, list):
                    raise ValueError('扫描器未返回有效内容列表')
            else:
                items = response
                result['coverage'] = {'complete': True, 'reason': 'legacy_scanner'}
            await _scan_progress(sub["id"], "running", "正在检查完整存档与下载记录…")
            local_ids = await asyncio.to_thread(_local_item_ids, items, platform)
            for index, it in enumerate(items):
                await _scan_progress(
                    sub["id"], "running", "正在比对最新内容", index * 100 / len(items),
                    completed=index, total=len(items), unit="条内容",
                )
            first_scan, result["new_items"] = subscription_store.record_discoveries(sub, items, local_ids)
            if first_scan:
                result["baseline"] = len({str(it["item_id"]) for it in items})
            scheduling.record_coverage(sub['id'], result['coverage'])
            SCAN_STATES[sub['id']]['coverage'] = result['coverage']
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
            if result.get('coverage', {}).get('complete') is False:
                label += '；回溯未覆盖完整窗口（详见扫描记录）'
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


_scheduled_jobs = {}


def has_active_scans():
    """Includes scans waiting for a slot and claimed jobs not started yet.

    Call from the event loop before setting the maintenance flag, without an
    await between the check and flag assignment.
    """
    return bool(_SCANNING or any(not task.done() for task in _scheduled_jobs.values()))


async def _scheduled_scan(sub, group, on_new_items):
    result = {}
    try:
        if getattr(config, 'MAINTENANCE_ACTIVE', False):
            scheduling.finish_group(group['id'], {}, interrupted=True)
            return
        result = await _scan_guarded(sub, group['trigger'], join_running=True)
        if result.get('new_items') and on_new_items:
            await on_new_items(result['new_items'])
        scheduling.finish_group(group['id'], result)
    except asyncio.CancelledError:
        scheduling.finish_group(group['id'], result, interrupted=True)
        raise
    except Exception as error:
        result['error'] = redact_text(str(error))
        scheduling.finish_group(group['id'], result)
        print('[watcher] 计划扫描失败: ' + result['error'])


async def scheduler_tick(on_new_items=None, *, now=None):
    if getattr(config, 'MAINTENANCE_ACTIVE', False):
        return
    subs = {sub['id']: sub for sub in list_subs() if sub['enabled']}
    available = max(1, min(4, getattr(config, 'MAX_SCAN_CONCURRENCY', 2))) - len(_scheduled_jobs)
    if available <= 0:
        return
    for sub_id in scheduling.due_groups(now, limit=100):
        if sub_id not in subs or sub_id in _scheduled_jobs:
            continue
        group = scheduling.claim_group(sub_id, now)
        if not group:
            continue
        job = asyncio.create_task(_scheduled_scan(subs[sub_id], group, on_new_items))
        _scheduled_jobs[sub_id] = job
        def finished(task, identity=sub_id):
            _scheduled_jobs.pop(identity, None)
            if not task.cancelled():
                error = task.exception()
                if error:
                    print('[watcher] ' + redact_text(str(error)))
        job.add_done_callback(finished)
        available -= 1
        if available <= 0:
            break


async def _scheduler_loop(on_new_items=None):
    while True:
        try:
            await scheduler_tick(on_new_items)
        except Exception as error:
            print('[watcher] 调度检查失败: ' + redact_text(str(error)))
        await asyncio.sleep(10)


def start_scheduler(on_new_items=None):
    global _loop_task
    if _loop_task and not _loop_task.done():
        return
    _loop_task = asyncio.create_task(_scheduler_loop(on_new_items))


async def stop_scheduler():
    """Signal thread-owned browsers, cancel requests and await their cleanup."""
    global _loop_task
    for cancel in list(_SCAN_CANCEL_EVENTS.values()):
        cancel.set()
    jobs = set(_scheduled_jobs.values()) | set(_SCAN_TASKS)
    if _loop_task:
        jobs.add(_loop_task)
    _loop_task = None
    jobs.discard(asyncio.current_task())
    for job in jobs:
        if not job.done():
            job.cancel()
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)
    _scheduled_jobs.clear()
