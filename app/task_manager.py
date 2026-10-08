"""任务管理器：下载队列 + 状态机 + 子进程执行 + WebSocket 广播。

状态流转: queued -> running -> success | failed | cancelled
日志实时逐行读取并广播到所有已连接的 WebSocket 客户端。
"""
import asyncio
import datetime as _dt
import os
import re
import uuid
import time
from typing import Optional

from fastapi import WebSocket

from . import config, script_registry

# 全部任务（内存保存，含日志；进程重启后清空）
TASKS: dict[str, dict] = {}
TASK_ORDER: list[str] = []

WS_CLIENTS: set[WebSocket] = set()

# 任务完成回调（main.py 注册：用于媒体库缓存失效等联动）
ON_TASK_DONE: list = []

_semaphore: Optional[asyncio.Semaphore] = None
_update_locked_until = 0.0


def update_state(acquire: bool = False) -> dict:
    """Called on the event loop, atomically with create() before its first await."""
    global _update_locked_until
    count = sum(t['status'] in ('queued', 'running') for t in TASKS.values())
    if acquire and count == 0:
        _update_locked_until = time.monotonic() + 30
    return {'active': count, 'locked': time.monotonic() < _update_locked_until}


def release_update_lock():
    global _update_locked_until
    _update_locked_until = 0.0


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_TASKS)
    return _semaphore


def _now() -> str:
    return _dt.datetime.now().strftime("%H:%M:%S")


async def broadcast(message: dict):
    """向所有 WS 客户端广播 JSON 消息，失效连接自动剔除。"""
    dead = []
    for ws in list(WS_CLIENTS):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        WS_CLIENTS.discard(ws)


def append_log(task_id: str, line: str, stream: str = "stdout") -> dict:
    entry = {"time": _now(), "stream": stream, "text": line.rstrip("\n")}
    TASKS[task_id]["logs"].append(entry)
    return entry


def public_task(t: dict) -> dict:
    """任务的可序列化视图（去掉进程对象）。"""
    return {k: v for k, v in t.items() if k != "proc"}


async def create(script_id: str, params: dict) -> dict:
    """校验参数并创建排队任务，返回任务对象。"""
    if time.monotonic() < _update_locked_until:
        raise ValueError('应用正在安装更新，请稍后重试')
    manifest = script_registry.get(script_id)
    if not manifest:
        raise ValueError(f"未知脚本: {script_id}")

    # 必填校验
    missing = [
        p["label"] for p in manifest.get("params", [])
        if p.get("required") and not str(params.get(p["name"], "")).strip()
    ]
    if missing:
        raise ValueError("以下必填项为空: " + "、".join(missing))

    task_id = uuid.uuid4().hex[:12]
    task = {
        "id": task_id,
        "script_id": script_id,
        "script_name": manifest["name"],
        "script_icon": manifest.get("icon", "📄"),
        "params": params,
        "command": script_registry.build_command(script_id, params),
        "status": "queued",
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "output_dir": None,
        "output_rel": None,
        "logs": [],
        "proc": None,
    }
    TASKS[task_id] = task
    TASK_ORDER.insert(0, task_id)
    # 只保留最近 200 条任务记录
    if len(TASK_ORDER) > 200:
        # Never evict queued/running work: update gating must see every task.
        old = next((tid for tid in reversed(TASK_ORDER)
                    if TASKS[tid]['status'] not in ('queued', 'running')), None)
        if old is not None:
            TASK_ORDER.remove(old)
            TASKS.pop(old, None)

    asyncio.create_task(_run(task_id))
    await broadcast({"type": "task_update", "task": public_task(task)})
    return public_task(task)


def _clean_dir_segment(seg: str) -> str:
    """把日志路径片段清理成纯目录：去大小尾巴/context.md/文件名。"""
    seg = seg.strip()
    # 去掉 "(1123882 bytes)" 之类的文件大小尾巴
    seg = re.sub(r"\s*\(\d+\s*bytes?\)\s*$", "", seg)
    for stop in ("\\context.md", "/context.md"):
        seg = seg.split(stop)[0]
    # "下载完成:" 后面跟的是完整文件路径 -> 取所在目录
    base = seg.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if "." in base and not seg.endswith(("\\", "/", ":")):
        parent = seg.rsplit("\\", 1)[0] if "\\" in seg else seg.rsplit("/", 1)[0]
        seg = parent
    return seg


def _detect_output_dir(task: dict, text: str):
    """从日志中提取输出目录（两个脚本都会打印保存/生成路径）。

    同时记录绝对路径(output_dir)与相对 LIBRARY_ROOT 的路径(output_rel)。
    """
    if task["output_dir"]:
        return
    for kw in ("保存目录:", "下载完成:", "官方封面已保存:", "context.md 已生成:"):
        idx = text.find(kw)
        if idx >= 0:
            seg = _clean_dir_segment(text[idx:].split(":", 1)[1])
            if seg and (":\\" in seg or ":/" in seg or seg.startswith("\\\\")):
                task["output_dir"] = seg
                try:
                    root = os.path.abspath(config.LIBRARY_ROOT)
                    rel = os.path.relpath(seg, root)
                    if not rel.startswith(".."):
                        task["output_rel"] = rel.replace("\\", "/")
                except ValueError:
                    pass
                return


async def _run(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        return

    async with _sem():
        # 领到信号量后再次确认未被取消
        if task["status"] != "queued":
            return
        task["status"] = "running"
        task["started_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        await broadcast({"type": "task_update", "task": public_task(task)})

        entry = append_log(task_id, "$ " + " ".join(task["command"]))
        await broadcast({"type": "task_log", "task_id": task_id, "log": entry})

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *task["command"],
                # Electron's stdin pipe is consumed by the parent watchdog.
                # Inheriting that busy Windows pipe can block a frozen child
                # before Python starts. Downloaders never read interactive input.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=config.BASE_DIR,
            )
            task["proc"] = proc

            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace")
                entry = append_log(task_id, line)
                _detect_output_dir(task, line)
                await broadcast({"type": "task_log", "task_id": task_id, "log": entry})

            code = await proc.wait()
            task["exit_code"] = code
            if task.get("_cancelling"):
                task["status"] = "cancelled"
            else:
                task["status"] = "success" if code == 0 else "failed"
        except asyncio.CancelledError:
            pass
        except Exception as e:
            entry = append_log(task_id, f"[管理器] 执行异常: {e}", "stderr")
            await broadcast({"type": "task_log", "task_id": task_id, "log": entry})
            task["status"] = "cancelled" if task.get("_cancelling") else "failed"
            task["exit_code"] = -1
        finally:
            task["proc"] = None
            task["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            if task["status"] == "running":  # 子进程被 kill 的场景
                task["status"] = "cancelled" if task.get("_cancelling") else "failed"
            await broadcast({"type": "task_update", "task": public_task(task)})
            # 完成回调（媒体库缓存失效等）
            if task["status"] == "success":
                for cb in ON_TASK_DONE:
                    try:
                        ret = cb(task)
                        if asyncio.iscoroutine(ret):
                            await ret
                    except Exception as e:
                        print(f"[task_manager] 完成回调异常: {e}")


async def cancel(task_id: str) -> bool:
    task = TASKS.get(task_id)
    if not task:
        return False
    if task["status"] == "queued":
        task["_cancelling"] = True
        task["status"] = "cancelled"
        task["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        await broadcast({"type": "task_update", "task": public_task(task)})
        return True
    if task["status"] == "running" and task.get("proc"):
        task["_cancelling"] = True
        entry = append_log(task_id, "[管理器] 取消：终止进程树 (taskkill /T /F)", "stderr")
        await broadcast({"type": "task_log", "task_id": task_id, "log": entry})
        try:
            # Windows 进程树终止：直接 kill 只杀 python，其派生的 Chromium 会残留
            proc = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(task["proc"].pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception:
            try:
                task["proc"].kill()
            except Exception:
                pass
        return True
    return False


def list_tasks() -> list[dict]:
    return [public_task(TASKS[tid]) for tid in TASK_ORDER if tid in TASKS]


def get_task(task_id: str) -> Optional[dict]:
    t = TASKS.get(task_id)
    return public_task(t) if t else None
