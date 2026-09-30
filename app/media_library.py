"""媒体库：扫描存档根目录，生成 作者 -> 条目 的结构化视图。

目录约定（两个下载脚本共同遵守）:
  <root>/<作者>/<日期目录>/
      context.md      元数据
      photo/img*.jpg  微博普通图片
      live/live*.mov|.jpg  微博 Live 图
      <videoid>.mp4   抖音视频
"""
import os
import re
from typing import Optional

from . import config

# 跳过的目录（非存档内容）
_SKIP_DIRS = {
    ".git", ".ab-profile", "app", "frontend", "scripts_manifest",
    "app_data", "node_modules", "__pycache__", ".venv", ".idea", ".vscode",
}
_SKIP_FILES = {"README.md", "DESIGN.md"}

_DATE_RE = re.compile(r"^\d{2,4}-\d{1,2}-\d{1,2}")


def _is_date_dir(name: str) -> bool:
    return bool(_DATE_RE.match(name))


def _scan_entry(path: str) -> Optional[dict]:
    """扫描一个日期目录，返回条目信息；空目录返回 None。"""
    photos, lives, videos, others = [], [], [], []
    total_size = 0
    try:
        entries = os.listdir(path)
    except OSError:
        return None

    for name in entries:
        full = os.path.join(path, name)
        if os.path.isdir(full):
            if name == "photo":
                for f in os.listdir(full):
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                        photos.append(f"photo/{f}")
                        total_size += os.path.getsize(os.path.join(full, f))
            elif name == "live":
                for f in os.listdir(full):
                    if f.lower().endswith(".mov"):
                        lives.append(f"live/{f}")
                        total_size += os.path.getsize(os.path.join(full, f))
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext == ".md" and name == "context.md":
            continue
        if ext == ".mp4":
            videos.append(name)
            total_size += os.path.getsize(full)
        elif ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            photos.append(name)
            total_size += os.path.getsize(full)

    if not (photos or lives or videos):
        return None

    # 封面：优先 live jpg 封面 > 第一张 photo > 第一张根目录图
    cover = None
    live_jpgs = sorted(
        p for p in photos if p.startswith("live/") and p.endswith(".jpg")
    )
    if live_jpgs:
        cover = live_jpgs[0]
    elif photos:
        cover = sorted(p for p in photos if not p.startswith("live/"))[0] or photos[0]
    elif videos:
        cover = None  # 视频封面由前端用图标占位

    return {
        "photos": sorted(photos),
        "lives": sorted(lives),
        "videos": sorted(videos),
        "cover": cover,
        "size": total_size,
    }


def _parse_context(path: str) -> dict:
    """解析 context.md 里的元数据字段（宽松解析）。"""
    meta = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(8192)
    except OSError:
        return meta
    for line in content.splitlines():
        m = re.match(r"^- \*\*(.+?)\*\*: (.*)$", line.strip())
        if m:
            meta[m.group(1)] = m.group(2).strip()
    return meta


def scan_root() -> list[dict]:
    """返回全部作者：[{name, entries: [{date_dir, ..., meta}]}]"""
    root = config.LIBRARY_ROOT
    authors = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return authors

    for name in names:
        author_path = os.path.join(root, name)
        if not os.path.isdir(author_path):
            continue
        if name in _SKIP_DIRS or name.startswith("."):
            continue
        entries = []
        for sub in sorted(os.listdir(author_path), reverse=True):
            sub_path = os.path.join(author_path, sub)
            if not os.path.isdir(sub_path) or not _is_date_dir(sub):
                continue
            info = _scan_entry(sub_path)
            if not info:
                continue
            meta = _parse_context(os.path.join(sub_path, "context.md"))
            info.update({
                "author": name,
                "date_dir": sub,
                "meta": meta,
                "path": sub_path,
            })
            entries.append(info)
        if entries:
            authors.append({
                "name": name,
                "count": len(entries),
                "total_size": sum(e["size"] for e in entries),
                "entries": entries,
            })
    return authors


def authors_summary() -> list[dict]:
    """轻量版：不带 entries。"""
    return [
        {k: v for k, v in a.items() if k != "entries"}
        for a in scan_root()
    ]
