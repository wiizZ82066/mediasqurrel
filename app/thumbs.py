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

import cv2
import numpy as np

from . import config
from .archive import atomic_output

THUMB_DIR = os.path.join(config.CACHE_DIR, "thumbs")
THUMB_WIDTH = 480
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
_LOCKS = [threading.Lock() for _ in range(32)]
_DECODE_SLOTS = threading.BoundedSemaphore(2)


def _cache_key(abs_path: str, stat, width: int) -> tuple[str, str]:
    raw = f"v2|{os.path.normcase(os.path.realpath(abs_path))}|{stat.st_mtime_ns}|{stat.st_size}|{width}".encode("utf-8", errors="replace")
    h = hashlib.sha1(raw).hexdigest()
    return h, os.path.join(THUMB_DIR, f"{h}.jpg")


def _imread_unicode(path: str):
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _video_first_frame(path: str):
    cap = cv2.VideoCapture(path)
    try:
        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                return frame
    finally:
        cap.release()
    # Windows 下 OpenCV 打不开中文路径的兜底：复制到 ASCII 临时文件
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        shutil.copyfile(path, tmp)
        cap = cv2.VideoCapture(tmp)
        try:
            if cap.isOpened():
                ret, frame = cap.read()
                if ret:
                    return frame
        finally:
            cap.release()
    except Exception:
        pass
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    return None


def get_thumb(abs_path: str, rel_path: str = "", width: int = 480) -> str | None:
    """返回缩略图绝对路径；生成失败返回 None。"""
    width = max(120, min(1280, int(width)))
    try:
        stat = os.stat(abs_path)
    except OSError:
        return None
    h, thumb_path = _cache_key(abs_path, stat, width)
    if os.path.isfile(thumb_path):
        return thumb_path

    with _LOCKS[int(h[:2], 16) % len(_LOCKS)]:
        if os.path.isfile(thumb_path):
            return thumb_path
        with _DECODE_SLOTS:
            return _generate(abs_path, thumb_path, width)


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
