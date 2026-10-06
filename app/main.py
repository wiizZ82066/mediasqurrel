"""FastAPI 入口：REST API + WebSocket + 媒体静态资源 + 前端托管。"""
import asyncio
import contextlib
import datetime as _dt
import json
import os

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, media_library, script_registry, task_manager, watcher
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


_DEV_MODE = os.environ.get("MS_DEV") == "1" or not getattr(
    __import__("sys"), "frozen", False
)

app = FastAPI(title="Media Squirrel", docs_url=None, redoc_url=None)

# CORS：仅开发模式放行 Vite dev server；桌面生产为同源访问，无需 CORS
if _DEV_MODE:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


async def _dispatch_new_items(items):
    """扫描发现新内容的统一处理：页面内通知 + 自动创建下载任务。"""
    for it in items:
        await task_manager.broadcast({
            "type": "notification",
            "level": "info",
            "title": "发现新内容",
            "text": f"{it.get('title', '')[:60]}，已自动开始下载",
            "time": _dt.datetime.now().strftime("%H:%M:%S"),
        })
        await task_manager.create(it["script_id"], it["params"])


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # ---- startup ----
    watcher.init_db()
    watcher.start_scheduler(_dispatch_new_items)

    # 下载任务成功 -> 媒体库缓存自动失效（下次访问即为新数据）
    task_manager.ON_TASK_DONE.append(lambda task: media_library.invalidate())

    # 启动预热：后台线程扫描一次媒体库写缓存，用户首次访问毫秒级出数据
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _warmup)

    yield

    # ---- shutdown ----
    watcher_stop = getattr(watcher, "stop_scheduler", None)
    if watcher_stop:
        watcher_stop()


def _warmup():
    try:
        media_library.scan_root()
        print("[startup] 媒体库缓存预热完成")
    except Exception as e:
        print(f"[startup] 媒体库预热失败: {e}")


app = FastAPI(title="Media Squirrel", lifespan=lifespan,
              docs_url=None, redoc_url=None) if False else app

# 将 lifespan 附加到已创建的 app（保持中间件顺序）
app.router.lifespan_context = lifespan


# ---------------------------------------------------------------- 健康检查

@app.get("/api/health")
def api_health():
    """Electron 健康检查端点（200 + status=ok 即就绪）。"""
    return {"status": "ok", "version": _app_version()}


# ---------------------------------------------------------------- 脚本清单

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
    return task_manager.list_tasks()


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

@app.get("/api/library")
def api_library(refresh: int = 0):
    """媒体库（库级缓存）。refresh=1 强制全盘重扫并更新缓存。"""
    return media_library.scan_root(refresh=bool(refresh))


@app.get("/api/library/authors")
def api_library_authors():
    return media_library.authors_summary()


@app.get("/api/search")
def api_search(q: str, limit: int = 60):
    """全局搜索：匹配 作者名 / 日期目录 / 视频标题 / 微博正文 / 链接。

    全部基于相对路径的本地存档，无任何固定绝对路径。
    """
    kw = (q or "").strip().lower()
    if not kw:
        return {"q": q, "results": []}

    results = []
    for author in media_library.scan_root():
        for e in author["entries"]:
            haystacks = [
                author["name"],
                e["date_dir"],
                e.get("text_preview") or "",
                (e.get("meta") or {}).get("视频标题") or "",
                (e.get("meta") or {}).get("原文链接") or "",
            ]
            if any(kw in h.lower() for h in haystacks):
                results.append(e)
                if len(results) >= limit:
                    break
        if len(results) >= limit:
            break
    return {"q": q, "results": results}


@app.get("/api/thumb")
def api_thumb(p: str, w: int = 480):
    """缩略图：p = 相对 LIBRARY_ROOT 的路径（图片直接缩，视频抽首帧）。"""
    from . import thumbs

    rel = (p or "").replace("\\", "/").lstrip("/")
    abs_path = os.path.abspath(os.path.join(config.LIBRARY_ROOT, rel))
    # 防目录穿越
    if not os.path.normcase(abs_path).startswith(
        os.path.normcase(os.path.abspath(config.LIBRARY_ROOT) + os.sep)
    ):
        raise HTTPException(status_code=403, detail="非法路径")
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    thumbs.THUMB_WIDTH = max(120, min(1280, w))
    thumb = thumbs.get_thumb(abs_path, rel)
    if not thumb:
        raise HTTPException(status_code=500, detail="缩略图生成失败")
    return FileResponse(thumb, media_type="image/jpeg", headers={
        "Cache-Control": "public, max-age=86400",
    })


def _safe_join(rel: str) -> str:
    """相对路径 -> 绝对路径，带穿越防护。"""
    rel = (rel or "").replace("\\", "/").strip("/")
    abs_path = os.path.abspath(os.path.join(config.LIBRARY_ROOT, rel))
    if not os.path.normcase(abs_path).startswith(
        os.path.normcase(os.path.abspath(config.LIBRARY_ROOT) + os.sep)
    ):
        raise HTTPException(status_code=403, detail="非法路径")
    return abs_path


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
    for root, _dirs, files in os.walk(abs_path):
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(root, f)
            rel = os.path.relpath(full, config.LIBRARY_ROOT).replace("\\", "/")
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


@app.get("/api/subs/local-authors")
def api_local_authors():
    """本地存档作者（含可订阅的平台身份）。"""
    from . import sub_search
    return sub_search.local_authors()


@app.get("/api/subs/search")
async def api_search_blogger(platform: str, q: str):
    """线上博主搜索：按粉丝数降序前 5。

    抖音触发人机验证时返回 captcha_required=True，前端引导用户完成一次
    有头验证（POST /api/subs/verify-douyin）后自动重搜。
    """
    from . import sub_search

    kw = (q or "").strip()
    if not kw or platform not in ("weibo", "douyin"):
        return {"results": []}
    try:
        results = await sub_search.search_online_async(platform, kw)
    except sub_search.CaptchaRequiredError:
        return {"results": [], "captcha_required": True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"搜索失败: {e}")
    return {"results": results}


@app.get("/api/subs/douyin-auth")
def api_douyin_auth_status():
    """抖音登录状态查询（供 UI 展示）。"""
    from . import douyin_auth
    return douyin_auth.auth_status()


@app.post("/api/subs/login-douyin")
async def api_login_douyin():
    """打开可见浏览器窗口：用户完成滑块验证 + 扫码登录（最长 180 秒）。

    登录 cookies 导出到 app_data/douyin_cookies.json（仅本地），
    之后的抖音搜索/扫描/下载自动注入登录态。
    """
    from . import sub_search
    return await asyncio.to_thread(sub_search.douyin_login_sync)


@app.post("/api/subs/verify-douyin")
async def api_verify_douyin():
    """兼容旧路径：等价 login-douyin。"""
    return await api_login_douyin()


@app.post("/api/subs")
def api_add_sub(body: dict):
    platform = (body.get("platform") or "").strip()
    blogger_id = (body.get("blogger_id") or "").strip()
    if not platform or not blogger_id:
        raise HTTPException(status_code=400, detail="platform 与 blogger_id 必填")
    return watcher.add_sub(
        platform=platform,
        blogger_id=blogger_id,
        nickname=(body.get("nickname") or "").strip(),
        homepage=(body.get("homepage") or "").strip(),
        interval_minutes=int(body.get("interval_minutes") or 30),
    )


@app.patch("/api/subs/{sub_id}")
def api_update_sub(sub_id: int, body: dict):
    ok = watcher.update_sub(sub_id, **body)
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
    result = await watcher.scan_sub(subs[0])
    if result["new_items"]:
        await _dispatch_new_items(result["new_items"])
    return result


# ---------------------------------------------------------------- 静态资源

# 媒体文件直读（仅本地使用；目录遍历由 StaticFiles 保护）
app.mount("/media", StaticFiles(directory=config.LIBRARY_ROOT), name="media")

# 前端构建产物（存在才挂载）
_DIST = config.FRONTEND_DIST
if os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")

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
