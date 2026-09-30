"""FastAPI 入口：REST API + WebSocket + 媒体静态资源 + 前端托管。"""
import asyncio
import os

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, media_library, script_registry, task_manager, watcher
from . import scanners  # noqa: F401  (import 即注册各平台扫描器)

app = FastAPI(title="Media Squirrel", docs_url=None, redoc_url=None)

# 开发期 Vite (5173) 跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    watcher.init_db()

    async def on_new_items(items):
        """扫描发现新内容 -> 自动创建下载任务。"""
        for it in items:
            await task_manager.create(it["script_id"], it["params"])

    watcher.start_scheduler(on_new_items)


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
def api_library():
    return media_library.scan_root()


@app.get("/api/library/authors")
def api_library_authors():
    return media_library.authors_summary()


# ---------------------------------------------------------------- 订阅

@app.get("/api/subs")
def api_list_subs():
    return watcher.list_subs()


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
        for it in result["new_items"]:
            await task_manager.create(it["script_id"], it["params"])
    return result


# ---------------------------------------------------------------- 静态资源

# 媒体文件直读（仅本地使用；目录遍历由 StaticFiles 保护）
app.mount("/media", StaticFiles(directory=config.LIBRARY_ROOT), name="media")

# 前端构建产物（存在才挂载）
_DIST = os.path.join(config.BASE_DIR, "frontend", "dist")
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
