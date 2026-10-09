"""Size-specific thumbnails in the configured rebuildable cache.

- 图片: np.fromfile + cv2.imdecode（OpenCV Windows 中文路径兼容）
- 视频: cv2.VideoCapture 抓第一帧；中文路径失败时退回复制临时文件
- 缓存键包括已解析原文件路径、纳秒修改时间、文件大小和目标尺寸。
- 每个缓存文件原子写入；共享解码并发上限为 2。
"""
import hashlib
import os
import shutil
import tempfile
import threading
import time
import re
import warnings

import cv2
import numpy as np
from PIL import Image

from . import config
from .archive import atomic_output

THUMB_DIR = os.path.join(config.CACHE_DIR, "thumbs")
THUMB_WIDTH = 480
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
_LOCKS = [threading.Lock() for _ in range(32)]
_DECODE_SLOTS = threading.BoundedSemaphore(2)
_TRIM_LOCK = threading.Lock()
_last_trim = 0.0
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_EDGE = 32000
MAX_VIDEO_FALLBACK_BYTES = 512 * 1024 * 1024


def trim_cache(*, force=False, now=None):
    """Bound the rebuildable thumbnail cache, sparing recent/in-flight responses.

    The ten-minute grace period permits a temporary burst above the target.
    Only this module's hashed JPEG files may be removed, never source media.
    """
    global _last_trim
    now = time.time() if now is None else now
    if not force and now - _last_trim < 30:
        return
    if not _TRIM_LOCK.acquire(blocking=False):
        return
    try:
        _last_trim = now
        files = []
        try:
            for item in os.scandir(THUMB_DIR):
                if not item.is_file(follow_symlinks=False) or not re.fullmatch(r'[0-9a-f]{40}\.jpg', item.name):
                    continue
                stat = item.stat(follow_symlinks=False)
                files.append((stat.st_mtime, stat.st_size, item.path))
        except OSError:
            return
        size = sum(item[1] for item in files)
        for modified, length, path in sorted(files):
            if size <= config.CACHE_LIMIT_BYTES:
                break
            if now - modified < 600:
                continue
            lock = _LOCKS[int(os.path.basename(path)[:2], 16) % len(_LOCKS)]
            if not lock.acquire(blocking=False):
                continue
            try:
                # A cached response may have refreshed this file since the
                # inventory was read. Never unlink an actively used cache hit.
                current = os.stat(path)
                if current.st_mtime != modified or now - current.st_mtime < 600:
                    continue
                os.unlink(path)
                size -= length
            except OSError:
                continue
            finally:
                lock.release()
        return {'bytes': size, 'target_bytes': config.CACHE_LIMIT_BYTES}
    finally:
        _TRIM_LOCK.release()


def _cache_key(abs_path: str, stat, width: int) -> tuple[str, str]:
    raw = f"v2|{os.path.normcase(os.path.realpath(abs_path))}|{stat.st_mtime_ns}|{stat.st_size}|{width}".encode("utf-8", errors="replace")
    h = hashlib.sha1(raw).hexdigest()
    return h, os.path.join(THUMB_DIR, f"{h}.jpg")


def _imread_unicode(path: str):
    try:
        length = os.stat(path).st_size
        if not 0 < length <= MAX_IMAGE_BYTES:
            return None
        # Pillow identifies dimensions lazily; no pixel allocation occurs until
        # OpenCV below, after the explicit memory budget check.
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as header:
                if not _allowed_dimensions(*header.size):
                    return None
        data = np.fromfile(path, dtype=np.uint8, count=length + 1)
        if data.size != length:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def _allowed_dimensions(width, height):
    return 0 < width <= MAX_IMAGE_EDGE and 0 < height <= MAX_IMAGE_EDGE and width * height <= MAX_IMAGE_PIXELS


def _read_frame(cap):
    if not cap.isOpened():
        return None
    width, height = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    if not _allowed_dimensions(width, height):
        return None
    ret, frame = cap.read()
    if ret and frame is not None and _allowed_dimensions(frame.shape[1], frame.shape[0]):
        return frame
    return None


def _video_first_frame(path: str):
    cap = cv2.VideoCapture(path)
    try:
        frame = _read_frame(cap)
        if frame is not None:
            return frame
    finally:
        cap.release()
    # Windows 下 OpenCV 打不开中文路径的兜底：复制到 ASCII 临时文件
    tmp = None
    try:
        size = os.stat(path).st_size
        if size > MAX_VIDEO_FALLBACK_BYTES or shutil.disk_usage(tempfile.gettempdir()).free < size + 64 * 1024 * 1024:
            return None
        fd, tmp = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        shutil.copyfile(path, tmp)
        cap = cv2.VideoCapture(tmp)
        try:
            frame = _read_frame(cap)
            if frame is not None:
                return frame
        finally:
            cap.release()
    except Exception:
        pass
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    return None


def _cached_path(path):
    if not os.path.isfile(path):
        return None
    try:
        # Refresh before trim's ten-minute grace expires, even for warm hits.
        if time.time() - os.path.getmtime(path) > 300:
            os.utime(path, None)
    except OSError:
        return None
    return path


def cached_thumb(abs_path: str, *, width=480):
    """Cache-only lookup: never enqueue/decode/trim during maintenance."""
    try:
        h, path = _cache_key(abs_path, os.stat(abs_path), max(120, min(1280, int(width))))
    except OSError:
        return None
    with _LOCKS[int(h[:2], 16) % len(_LOCKS)]:
        return _cached_path(path)


def get_thumb(abs_path: str, rel_path: str = "", width: int = 480) -> str | None:
    """返回缩略图绝对路径；生成失败返回 None。"""
    width = max(120, min(1280, int(width)))
    try:
        stat = os.stat(abs_path)
    except OSError:
        return None
    h, thumb_path = _cache_key(abs_path, stat, width)
    with _LOCKS[int(h[:2], 16) % len(_LOCKS)]:
        result = _cached_path(thumb_path)
        if not result:
            with _DECODE_SLOTS:
                result = _generate(abs_path, thumb_path, width)
    trim_cache()
    return result


def _generate(abs_path, thumb_path, width):

    ext = os.path.splitext(abs_path)[1].lower()
    frame = None
    try:
        if ext in VIDEO_EXT:
            frame = _video_first_frame(abs_path)
        else:
            frame = _imread_unicode(abs_path)
    except Exception:
        return None

    if frame is None:
        return None

    h_px, w_px = frame.shape[:2]
    if w_px > width:
        scale = width / w_px
        frame = cv2.resize(frame, (width, max(1, int(h_px * scale))), interpolation=cv2.INTER_AREA)

    os.makedirs(THUMB_DIR, exist_ok=True)
    try:
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return None
        with atomic_output(thumb_path) as temporary:
            with open(temporary, "wb") as stream:
                stream.write(buf.tobytes())
        return thumb_path
    except OSError:
        return None
