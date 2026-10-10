"""FastAPI 入口：REST API + WebSocket + 媒体静态资源 + 前端托管。"""
import asyncio
import contextlib
import datetime as _dt
import json
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, media_library, script_registry, task_manager, watcher, library_service
from .library_api import router as library_router
from .security import LocalOnlyMiddleware, media_path
from .redaction import redact_text
from .runtime import InstanceLock, instance_id
from . import settings
from . import settings_api, maintenance, db
from . import scanners  # noqa: F401  (import 即注册各平台扫描器)


def _app_version() -> str:
    """读取应用版本：打包资源内 package.json > 根目录 package.json > dev。"""
    for base in (getattr(config, "RESOURCE_DIR", ""), config.BASE_DIR):
        pj = os.path.join(base, "package.json")
        if os.path.isfile(pj):
            try:
                with open(pj, "r", encoding="utf-8") as f:
                    return json.load(f).get("version", "dev")
            except (OSError, ValueError):
                continue
    return "dev"


_DEV_MODE = os.environ.get("MS_DEV") == "1"

app = FastAPI(title="Media Squirrel", docs_url=None, redoc_url=None)

# CORS：仅开发模式放行 Vite dev server；桌面生产为同源访问，无需 CORS
if _DEV_MODE:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.add_middleware(LocalOnlyMiddleware)


async def _dispatch_new_items(items):
    """扫描发现新内容的统一处理：页面内通知 + 自动创建下载任务。"""
    queued, failed = 0, 0
    for it in items:
        identity = (it["platform"], it["blogger_id"], str(it["item_id"]))
        claim = watcher.claim_item(*identity)
        if not claim:
            continue
        try:
            task = await task_manager.create(
                it["script_id"], it["params"],
                content_key=f"{identity[0]}:{identity[2]}",
                metadata={"subscription": dict(platform=identity[0], blogger_id=identity[1],
                    item_id=identity[2], sub_id=it.get("sub_id"), claim_token=claim)},
            )
            watcher.mark_enqueued(*identity, task["id"], claim)
            _record_task_result(task_manager.get_task(task["id"]) or task)
            queued += 1
        except Exception as error:
            watcher.mark_dispatch_failed(*identity, str(error), claim)
            failed += 1
    if queued or failed:
        await task_manager.broadcast({"type": "notification", "level": "error" if failed else "info",
            "title": "订阅扫描结果", "text": f"已入队 {queued} 条，入队失败 {failed} 条",
            "time": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")})


def _record_task_result(task):
    watcher.record_task_result(task["id"], task["status"], task.get("error") or "",
                               complete_verified=task["status"] == "success" and watcher.task_complete(task))


def _invalidate_library(task):
    if (task.get('metadata') or {}).get('diagnostic'):
        return
    media_library.invalidate()
    selected = library_service.task_root_id(task)
    if not selected:
        return  # External and diagnostic outputs are not the default library.
    scan = library_service.start_scan(selected, force_followup=True)
    state = library_service._scans.get(scan['id'])
    if state:
        def link(finished):
            if finished.cancelled() or finished.exception() is not None:
                return
            try:
                entry = library_service.task_entry(task)
                if entry:
                    # A scan can return failed/cancelled without raising. Only an
                    # actual linked entry proves this task can now be opened.
                    asyncio.create_task(task_manager.broadcast({'type': 'library_indexed',
                        'task_id': task['id'], 'entry_id': entry['id'], 'root_id': entry['root_id']}))
            except Exception as error:
                print('[catalog] 任务媒体关联失败: ' + redact_text(str(error)))
        state['task'].add_done_callback(link)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    lock = InstanceLock().acquire()
    try:
        config.ensure_runtime_dirs()
        db.migrate()
        from .path_transition import apply_pending_startup
        await asyncio.to_thread(apply_pending_startup)
        settings.apply(settings.load())
        watcher.init_db()
        maintenance.recover_interrupted()
        library_service.start()
        task_manager.ON_TASK_FINISHED.append(_record_task_result)
        task_manager.ON_TASK_DONE.append(_invalidate_library)
        await task_manager.initialize()
        watcher.reconcile_tasks(task_manager.iter_subscription_tasks())
        watcher.start_scheduler(_dispatch_new_items)
        yield
    finally:
        try:
            from .sub_search import stop_searches
            # Each owner receives shutdown even if a different cleanup fails.
            for stop in (settings_api.stop, stop_searches, watcher.stop_scheduler, task_manager.shutdown, library_service.stop):
                try:
                    await stop()
                except Exception as error:
                    print('[shutdown] ' + redact_text(str(error)))
        finally:
            if _record_task_result in task_manager.ON_TASK_FINISHED:
                task_manager.ON_TASK_FINISHED.remove(_record_task_result)
            if _invalidate_library in task_manager.ON_TASK_DONE:
                task_manager.ON_TASK_DONE.remove(_invalidate_library)
            lock.release()


# 将 lifespan 附加到已创建的 app（保持中间件顺序）
app.router.lifespan_context = lifespan
app.include_router(library_router)
app.include_router(settings_api.router)


# ---------------------------------------------------------------- 健康检查

@app.get("/api/health")
def api_health():
    """Local launcher identity and readiness check."""
    return {"status": "ok", "application": "media-squirrel", "version": _app_version(), "instance_id": instance_id()}


# ---------------------------------------------------------------- 脚本清单

@app.post('/api/desktop/update-lock')
async def api_update_lock(request: Request, body: dict):
    # Only the owning Electron main process receives this per-launch token.
    token = os.environ.get('MS_DESKTOP_TOKEN', '')
    supplied = request.headers.get('x-desktop-token', '')
    if not token or not secrets.compare_digest(token, supplied):
        raise HTTPException(status_code=403, detail='Forbidden')
    action = body.get('action')
    if action == 'release':
        task_manager.release_update_lock()
    elif action not in ('status', 'acquire'):
        raise HTTPException(status_code=400, detail='Invalid action')
    return task_manager.update_state(acquire=action == 'acquire')

@app.get("/api/scripts")
def api_scripts():
    return [
        {
            k: v for k, v in m.items() if not k.startswith("_")
        } | {"available": m["_available"]}
        for m in script_registry.all_scripts()
    ]


# ---------------------------------------------------------------- 任务

@app.post("/api/tasks")
async def api_create_task(body: dict):
    script_id = body.get("script_id", "")
    params = body.get("params", {}) or {}
    try:
        task = await task_manager.create(script_id, params)
        return task
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/tasks")
def api_list_tasks():
    return task_manager.list_page(limit=50)["items"]


@app.get("/api/tasks/page")
def api_task_page(limit: int = 50, cursor: str | None = None, status: str | None = None, q: str | None = None, include_hidden: bool = False):
    try:
        return task_manager.list_page(limit=limit, cursor=cursor, status=status, q=q, include_hidden=include_hidden)
    except ValueError as error:
        raise HTTPException(400, str(error))


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: str):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.post("/api/tasks/{task_id}/cancel")
async def api_cancel_task(task_id: str):
    ok = await task_manager.cancel(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务无法取消（可能已结束）")
    return {"ok": True}


@app.get("/api/tasks/{task_id}/logs")
def api_task_logs(task_id: str, after: int = 0, limit: int = 200, tail: bool = False):
    if not task_manager.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    return task_manager.read_logs(task_id, after=after, limit=limit, tail=tail)


@app.post("/api/tasks/{task_id}/retry")
async def api_retry_task(task_id: str):
    try:
        task = await task_manager.retry(task_id)
        watcher.link_retry(task_id, task["id"])
        _record_task_result(task_manager.get_task(task["id"]) or task)
        return task
    except ValueError as error:
        raise HTTPException(400, str(error))


@app.delete("/api/tasks/{task_id}")
def api_delete_task(task_id: str):
    try:
        return {"ok": task_manager.delete_record(task_id)}
    except ValueError as error:
        raise HTTPException(400, str(error))


@app.delete("/api/tasks/{task_id}/logs")
def api_delete_task_logs(task_id: str):
    if config.MAINTENANCE_ACTIVE:
        raise HTTPException(409, '维护期间不能删除日志')
    try:
        task_manager.delete_logs(task_id)
        return {"ok": True}
    except ValueError as error:
        raise HTTPException(400, str(error))


@app.post("/api/tasks/{task_id}/restore")
def api_restore_task(task_id: str):
    return {"ok": task_manager.restore_record(task_id)}


# ---------------------------------------------------------------- WebSocket

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    task_manager.WS_CLIENTS.add(ws)
    try:
        while True:
            # 客户端心跳/空消息
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        task_manager.WS_CLIENTS.discard(ws)


# ---------------------------------------------------------------- 媒体库

def _safe_join(rel: str) -> str:
    """相对路径 -> 绝对路径，带穿越防护。"""
    return media_path(rel)


IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VID_EXT = {".mp4", ".mov", ".m4v", ".webm"}


@app.get("/api/browse")
def api_browse(path: str = ""):
    """目录浏览器：列出指定相对目录下的子目录（供输出目录选择器）。"""
    from . import media_library as ml

    abs_path = _safe_join(path) if path else os.path.abspath(config.LIBRARY_ROOT)
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=404, detail="目录不存在")
    dirs = []
    try:
        for name in sorted(os.listdir(abs_path)):
            full = os.path.join(abs_path, name)
            if not os.path.isdir(full):
                continue
            try:
                media_path(os.path.relpath(full, config.LIBRARY_ROOT))
            except HTTPException:
                continue
            if name in ml._SKIP_DIRS or name.startswith("."):
                continue
            rel = os.path.relpath(full, config.LIBRARY_ROOT).replace("\\", "/")
            count = sum(
                1 for e in os.listdir(full)
                if os.path.isdir(os.path.join(full, e)) and ml._is_date_dir(e)
            )
            dirs.append({"name": name, "rel": rel, "entries": count})
    except OSError:
        pass
    return {"current": path or ".", "dirs": dirs}


@app.get("/api/preview")
def api_preview(dir: str):
    """输出预览：返回目录内媒体文件（图片/视频分类，相对路径）。"""
    abs_path = _safe_join(dir)
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=404, detail="目录不存在")

    images, videos = [], []
    for root, _dirs, files in os.walk(abs_path, followlinks=False):
        _dirs[:] = [name for name in _dirs if not name.startswith(".") and not os.path.islink(os.path.join(root, name))]
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(root, f)
            rel = os.path.relpath(full, config.LIBRARY_ROOT).replace("\\", "/")
            try:
                media_path(rel, file_only=True)
            except HTTPException:
                continue
            if f.endswith("_cover.jpg"):
                # 官方封面是元数据（视频已自带画面），预览不单独展示
                continue
            if ext in IMG_EXT:
                images.append(rel)
            elif ext in VID_EXT:
                videos.append(rel)
    return {"dir": dir, "images": images, "videos": videos}


# ---------------------------------------------------------------- 订阅

@app.get("/api/subs")
def api_list_subs():
    return watcher.list_subs()


@app.get("/api/subs/scans")
def api_scan_history(sub_id: int | None = None, limit: int = 50, offset: int = 0):
    return {"items": watcher.list_scans(sub_id, limit=limit, offset=offset)}


@app.get("/api/subs/local-authors")
def api_local_authors():
    """本地存档作者（含可订阅的平台身份）。"""
    from . import sub_search
    return sub_search.local_authors()


@app.get("/api/subs/search")
async def api_search_blogger(request: Request, platform: str, q: str):
    """线上博主搜索：按粉丝数降序前 5。

    抖音触发人机验证时返回 captcha_required=True，前端引导用户完成一次
    有头验证（POST /api/subs/verify-douyin）后自动重搜。
    """
    from . import sub_search

    kw = (q or "").strip()
    if not kw or platform not in ("weibo", "douyin"):
        return {"results": []}
    if len(kw) > 500:
        raise HTTPException(400, '搜索内容过长，请输入昵称、ID 或主页链接')
    task = asyncio.create_task(sub_search.search_online_async(platform, kw))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=.2)
            if await request.is_disconnected():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise HTTPException(499, '搜索已取消')
        results = await task
    except sub_search.CaptchaRequiredError:
        return {"results": [], "captcha_required": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"搜索失败: {redact_text(str(e))}")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return {"results": results}


@app.get("/api/subs/douyin-auth")
def api_douyin_auth_status():
    """抖音登录状态查询（供 UI 展示）。"""
    from . import douyin_auth
    return douyin_auth.auth_status()


@app.get("/api/subs/weibo-auth")
def api_weibo_auth_status():
    """Only local login metadata; never expose saved credentials."""
    from . import weibo_auth
    return weibo_auth.auth_status()


@app.post("/api/subs/login-weibo")
async def api_login_weibo():
    """A visible login window opens only after the user's explicit click."""
    from . import weibo_auth
    try:
        return await weibo_auth.login_async()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise HTTPException(502, redact_text(str(error))) from error


@app.get('/api/subs/{sub_id}/avatar')
def api_avatar(sub_id: int):
    from .avatars import get_avatar
    path = get_avatar(sub_id)
    if not path:
        raise HTTPException(404, '暂无可用头像')
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=3600'})


@app.post("/api/subs/login-douyin")
async def api_login_douyin():
    """打开可见浏览器窗口：用户完成滑块验证 + 扫码登录（最长 180 秒）。

    登录 cookies 导出到 app_data/douyin_cookies.json（仅本地），
    之后的抖音搜索/扫描/下载自动注入登录态。
    """
    from . import sub_search
    return await sub_search.douyin_login_async()


@app.post("/api/subs/verify-douyin")
async def api_verify_douyin():
    """兼容旧路径：等价 login-douyin。"""
    return await api_login_douyin()


@app.post("/api/subs")
def api_add_sub(body: dict):
    platform = (body.get("platform") or "").strip()
    blogger_id = (body.get("blogger_id") or "").strip()
    if platform not in ('weibo', 'douyin') or not blogger_id:
        raise HTTPException(status_code=400, detail="platform 与 blogger_id 必填")
    try:
        return watcher.add_sub(
            platform=platform, blogger_id=blogger_id,
            nickname=(body.get('nickname') or '').strip(),
            homepage=(body.get('homepage') or '').strip(),
            interval_minutes=int(body.get('interval_minutes') or 30),
            avatar_url=str(body.get('avatar_url') or body.get('avatar') or ''),
        )
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error))


@app.patch("/api/subs/{sub_id}")
def api_update_sub(sub_id: int, body: dict):
    try:
        ok = watcher.update_sub(sub_id, **body)
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error))
    if not ok:
        raise HTTPException(status_code=400, detail="无有效更新字段")
    return {"ok": True}


@app.delete("/api/subs/{sub_id}")
def api_delete_sub(sub_id: int):
    ok = watcher.remove_sub(sub_id)
    return {"ok": ok}


@app.post("/api/subs/{sub_id}/scan")
async def api_scan_sub(sub_id: int):
    subs = [s for s in watcher.list_subs() if s["id"] == sub_id]
    if not subs:
        raise HTTPException(status_code=404, detail="订阅不存在")
    try:
        result = await watcher.scan_sub(subs[0])
    except ValueError as error:
        raise HTTPException(409, str(error))
    if result["new_items"]:
        await _dispatch_new_items(result["new_items"])
    return result


# ---------------------------------------------------------------- 静态资源

# Never expose the entire directory: credentials, databases and source are not media.
@app.get("/media/{path:path}")
def api_media_file(path: str):
    return FileResponse(media_path(path, file_only=True), headers={"X-Content-Type-Options": "nosniff"})

# 前端构建产物（存在才挂载）
_DIST = config.FRONTEND_DIST
if os.path.isfile(os.path.join(_DIST, 'index.html')) and os.path.isdir(os.path.join(_DIST, 'assets')):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")
    if os.path.isdir(os.path.join(_DIST, 'licenses')):
        app.mount('/licenses', StaticFiles(directory=os.path.join(_DIST, 'licenses')), name='licenses')

    @app.get('/squirrel.svg')
    def serve_logo():
        return FileResponse(os.path.join(_DIST, 'squirrel.svg'), media_type='image/svg+xml')

    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(_DIST, "index.html"))

    # SPA fallback：未知路径（非 API/媒体）返回 index.html，交给前端路由
    @app.exception_handler(404)
    async def spa_fallback(request, exc):
        path = request.url.path
        if (
            not path.startswith(("/api", "/media", "/assets", "/ws"))
            and not path.startswith("/docs")
            and "text/html" in request.headers.get("accept", "")
        ):
            return FileResponse(os.path.join(_DIST, "index.html"))
        return JSONResponse({"detail": "Not Found"}, status_code=404)
else:
    @app.get("/")
    async def serve_index_dev():
        return JSONResponse(
            {"status": "frontend 未构建，请先运行: cd frontend && npm run build"}
        )
