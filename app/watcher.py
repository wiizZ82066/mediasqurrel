"""订阅模块：博主订阅 CRUD + 定时扫描调度框架。

数据层（SQLite）已就绪；扫描引擎采用插件式 Scanner 接口，
后续新增平台扫描器只需实现 scan(blogger) -> list[Item] 并注册。
"""
import asyncio
import datetime as _dt
import json
import os
import sqlite3
from typing import Callable, Optional

from . import config


# ---------------------------------------------------------------- 数据层

def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
    return [dict(r) for r in rows]


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
        return cur.rowcount > 0


def has_seen(platform: str, blogger_id: str, item_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_items WHERE platform=? AND blogger_id=? AND item_id=?",
            (platform, blogger_id, item_id),
        ).fetchone()
    return row is not None


def mark_seen(platform: str, blogger_id: str, item_id: str):
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_items (platform, blogger_id, item_id, created_at)"
            " VALUES (?,?,?,?)",
            (platform, blogger_id, item_id, _dt.datetime.now().isoformat(timespec="seconds")),
        )


# ---------------------------------------------------------------- 扫描引擎（插件式）

# Scanner: async fn(sub: dict) -> list[dict]
# 每个元素: {"item_id": str, "url": str, "params": dict(给下载脚本的参数), "title": str}
SCANNERS: dict[str, Callable[[dict], list]] = {}


def register_scanner(platform: str, fn):
    SCANNERS[platform] = fn


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
    if not scanner:
        result["error"] = f"平台 '{platform}' 的扫描器尚未实现（等待接入）"
    else:
        try:
            items = scanner(sub)
            if asyncio.iscoroutine(items):
                items = await items
            for it in items:
                if not has_seen(platform, sub["blogger_id"], it["item_id"]):
                    mark_seen(platform, sub["blogger_id"], it["item_id"])
                    result["new_items"].append(it)
        except Exception as e:
            result["error"] = str(e)

    with _connect() as conn:
        conn.execute(
            "UPDATE subscriptions SET last_scan_at=?, last_status=?, last_error=? WHERE id=?",
            (
                _dt.datetime.now().isoformat(timespec="seconds"),
                "error" if result["error"] else "ok",
                result["error"] or "",
                sub["id"],
            ),
        )
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
