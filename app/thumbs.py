"""缩略图引擎：为图片/视频生成缩略图，磁盘缓存于 app_data/thumbs/。

- 图片: np.fromfile + cv2.imdecode（OpenCV Windows 中文路径兼容）
- 视频: cv2.VideoCapture 抓第一帧；中文路径失败时退回复制临时文件
- 缓存 key: 相对路径 + 文件 mtime 的 sha1，源文件变动自动失效
"""
import hashlib
import os
import shutil
import tempfile

import cv2
import numpy as np

from . import config

THUMB_DIR = os.path.join(config.BASE_DIR, "app_data", "thumbs")
THUMB_WIDTH = 480
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


def _cache_key(rel_path: str, mtime: float) -> tuple[str, str]:
    raw = f"{rel_path}|{int(mtime)}|{THUMB_WIDTH}".encode("utf-8", errors="replace")
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


def get_thumb(abs_path: str, rel_path: str) -> str | None:
    """返回缩略图绝对路径；生成失败返回 None。"""
    mtime = os.path.getmtime(abs_path)
    h, thumb_path = _cache_key(rel_path, mtime)
    if os.path.isfile(thumb_path):
        return thumb_path

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
        # 生成一张 1x1 占位，避免反复重试失败文件
        frame = np.zeros((1, 1, 3), dtype=np.uint8)

    h_px, w_px = frame.shape[:2]
    if w_px > THUMB_WIDTH:
        scale = THUMB_WIDTH / w_px
        frame = cv2.resize(frame, (THUMB_WIDTH, max(1, int(h_px * scale))), interpolation=cv2.INTER_AREA)

    os.makedirs(THUMB_DIR, exist_ok=True)
    ok = cv2.imwrite(thumb_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    # imwrite 中文目录兜底
    if not ok:
        try:
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                buf.tofile(thumb_path)
                ok = True
        except Exception:
            ok = False
    return thumb_path if ok else None
