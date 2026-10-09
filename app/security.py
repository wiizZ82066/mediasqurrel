"""Loopback browser boundary and real-path checks for local media."""
import os
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.responses import JSONResponse

from . import config

MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
PRIVATE_SEGMENTS = {"app_data", "data", "cache", "logs", "backups", "app", "frontend", "electron", "node_modules", "scripts_manifest", "__pycache__", "browser-profile", "douyin_profile"}


def local_authority(authority):
    try:
        parsed = urlsplit("http://" + authority)
        return (parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.username is None and parsed.password is None
                and (parsed.port or 80) == config.PORT and not parsed.path)
    except ValueError:
        return False


def allowed_origin(origin):
    try:
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.path or parsed.query or parsed.fragment:
            return False
        if local_authority(parsed.netloc):
            return True
        return os.environ.get("MS_DEV") == "1" and origin in {
            "http://127.0.0.1:5173", "http://localhost:5173"}
    except ValueError:
        return False


class LocalOnlyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = {key.decode("latin-1").lower(): value.decode("latin-1")
                   for key, value in scope.get("headers", [])}
        origin = headers.get("origin")
        denied = (not local_authority(headers.get("host", "")) or
                  (origin is not None and not allowed_origin(origin)) or
                  headers.get("sec-fetch-site") == "cross-site")
        if denied:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "仅允许本机应用页面访问"}, 403)(scope, receive, send)
            return
        if (scope["type"] == "http" and scope.get("method") in {"POST", "PUT", "PATCH"}
                and headers.get("content-length", "0") not in {"", "0"}
                and not headers.get("content-type", "").split(";", 1)[0].strip() == "application/json"):
            await JSONResponse({"detail": "请求需要 JSON 格式"}, 415)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def media_path(relative, *, root=None, file_only=False, allow_root=False):
    value = str(relative or "").replace("\\", "/")
    windows = PureWindowsPath(value)
    parts = value.split("/")
    if ("\x00" in value or value.startswith("/") or windows.drive or
            any(part in {"..", "."} or ":" in part or
                part.startswith(".") or part.casefold() in PRIVATE_SEGMENTS for part in parts if part)):
        raise HTTPException(403, "非法媒体路径")
    if not value and not allow_root:
        raise HTTPException(403, "需要指定媒体路径")
    base = Path(root or config.LIBRARY_ROOT).resolve()
    target = (base / value).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(403, "媒体路径超出已选择的根目录")
    if file_only:
        if target.suffix.lower() not in MEDIA_EXTENSIONS:
            raise HTTPException(403, "只允许读取图片或视频")
        if not target.is_file():
            raise HTTPException(404, "媒体文件不存在")
    return str(target)
