"""订阅模块：博主订阅 CRUD + 定时扫描调度框架。

数据层（SQLite）已就绪；扫描引擎采用插件式 Scanner 接口，
后续新增平台扫描器只需实现 scan(blogger) -> list[Item] 并注册。
"""
import asyncio
from contextlib import contextmanager
import datetime as _dt
import json
import os
import re
import sqlite3
from typing import Callable, Optional

from . import config

# Kept in memory like download tasks; included in REST snapshots for reconnects.
SCAN_STATES: dict[int, dict] = {}
_SCANNING: set[int] = set()


# ---------------------------------------------------------------- 数据层

@contextmanager
def _connect():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,          -- douyin / weibo
                blogger_id TEXT NOT NULL,        -- 平台 UID 或主页标识
                nickname TEXT DEFAULT '',
                homepage TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                interval_minutes INTEGER DEFAULT 30,
                last_scan_at TEXT DEFAULT '',
                last_status TEXT DEFAULT '',     -- ok / error / pending
                last_error TEXT DEFAULT '',
                UNIQUE(platform, blogger_id)
            );
            CREATE TABLE IF NOT EXISTS seen_items (
                platform TEXT NOT NULL,
                blogger_id TEXT NOT NULL,
                item_id TEXT NOT NULL,           -- 视频/帖子 id
                created_at TEXT DEFAULT '',
                PRIMARY KEY (platform, blogger_id, item_id)
            );
            """
        )


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


def _has_any_seen(platform: str, blogger_id: str) -> bool:
    """该订阅是否已有任何已见记录（用于区分首次扫描）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_items WHERE platform=? AND blogger_id=? LIMIT 1",
            (platform, blogger_id),
        ).fetchone()
    return row is not None


def mark_seen(platform: str, blogger_id: str, item_id: str):
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_items (platform, blogger_id, item_id, created_at)"
            " VALUES (?,?,?,?)",
            (platform, blogger_id, item_id, _dt.datetime.now().isoformat(timespec="seconds")),
        )


# ---------------------------------------------------------------- 本地内容索引（去重首选）

_ITEM_LINK_RE = re.compile(
    r"weibo\.com/\d+/([A-Za-z0-9]+)|douyin\.com/video/(\d+)"
)
_MBLOGID_RE = re.compile(r"mblogid\*\*:\s*([A-Za-z0-9]+)")


def _local_item_ids() -> set[str]:
    """收集本地存档已有的内容 ID（微博 bid / 抖音 aweme_id）。

    数据源: media_library 的库级缓存（内存/磁盘两级）——
    避免本函数再全盘遍历，与媒体库共用一份缓存。
    """
    from . import media_library

    ids: set[str] = set()
    for author in media_library.scan_root():
        for e in author.get("entries", []):
            # 抖音: mp4 文件名即 aweme_id
            for v in e.get("videos", []):
                stem = v.rsplit(".", 1)[0]
                if stem:
                    ids.add(stem)
            # 两平台: context.md 的原文链接 + 微博 mblogid 字段
            link = (e.get("meta") or {}).get("原文链接", "")
            for m in _ITEM_LINK_RE.finditer(link):
                ids.add(m.group(1) or m.group(2) or "")
            mid = (e.get("meta") or {}).get("mblogid", "")
            if mid:
                ids.add(mid)
    ids.discard("")
    return ids


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
    await broadcast({"type": "sub_scan", "sub_id": sub_id, "scan": state})


async def scan_sub(sub: dict) -> dict:
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
    try:
        await _scan_progress(sub["id"], "running", "正在获取博主最新内容…")
        try:
            if not scanner:
                raise ValueError(f"平台 '{platform}' 的扫描器尚未实现（等待接入）")
            items = scanner(sub)
            if asyncio.iscoroutine(items):
                items = await items
            # 去重策略：本地已有优先，数据库兜底
            #   - 本地文件存在 -> 跳过（并自愈数据库标记）
            #   - 本地文件不存在 -> 视为新内容（即使数据库记过"已见"，
            #     文件被删后重新下载——修复删档后无法重新检测的 bug）
            await _scan_progress(sub["id"], "running", "正在检查本地存档…")
            local_ids = await asyncio.to_thread(_local_item_ids)
            first_scan = not _has_any_seen(platform, sub["blogger_id"])
            dispatched = set()  # 同轮兜底去重（扫描器可能返回重复 item）
            for index, it in enumerate(items):
                await _scan_progress(
                    sub["id"], "running", "正在比对最新内容", index * 100 / len(items),
                    completed=index, total=len(items), unit="条内容",
                )
                iid = it["item_id"]
                if iid in dispatched:
                    continue
                dispatched.add(iid)
                if iid in local_ids:
                    mark_seen(platform, sub["blogger_id"], iid)  # 自愈
                    continue
                if first_scan:
                    mark_seen(platform, sub["blogger_id"], iid)
                    continue  # 首次扫描仅对齐基线，不把历史内容全部入队
                mark_seen(platform, sub["blogger_id"], iid)
                result["new_items"].append(it)
            if first_scan:
                result["baseline"] = len(items)
        except Exception as e:
            result["error"] = str(e)

        last_scan_at = _dt.datetime.now().isoformat(timespec="seconds")
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
                label = f"扫描完成，发现 {len(result['new_items'])} 条新内容"
            else:
                label = "扫描完成，暂无新内容"
            await _scan_progress(sub["id"], "success", label, 100,
                                 completed=len(items), total=len(items), unit="条内容")
    except asyncio.CancelledError:
        await _scan_progress(sub["id"], "cancelled", "扫描已中断")
        raise
    except Exception:
        await _scan_progress(sub["id"], "failed", "扫描失败，请稍后重试")
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
                        due = (_dt.datetime.now() - last_dt).total_seconds() >= interval * 60
                    except ValueError:
                        pass
                if not due:
                    continue
                result = await scan_sub(sub)
                if result["new_items"] and on_new_items:
                    try:
                        await on_new_items(result["new_items"])
                    except Exception as e:
                        print(f"[watcher] 入队失败: {e}")
                elif result["error"]:
                    print(f"[watcher] {sub.get('nickname') or sub['blogger_id']}: {result['error']}")
        except Exception as e:
            print(f"[watcher] 调度循环异常: {e}")
        await asyncio.sleep(60)


def start_scheduler(on_new_items=None):
    global _loop_task
    if _loop_task and not _loop_task.done():
        return
    _loop_task = asyncio.create_task(_scheduler_loop(on_new_items))
