"""媒体库：扫描存档根目录，生成 作者 -> 条目 的结构化视图。

目录约定（两个下载脚本共同遵守）:
  <root>/<作者>/<日期目录>/
      context.md      元数据
      photo/img*.jpg  微博普通图片
      live/live*.mov|.jpg  微博 Live 图（jpg 为封面）
      <videoid>.mp4   抖音视频

封面策略（"最佳图"）:
  - 多图: 取中间位（9 宫格 -> 第 5 张，4 图 -> 第 2 张），比首张更有代表性
  - 仅 Live: 取第一张 Live 封面
  - 仅视频: 由前端经 /api/thumb 用视频首帧作封面
"""
import os
import re
from typing import Optional

from . import config

# 跳过的目录（非存档内容）
_SKIP_DIRS = {
    ".git", ".ab-profile", "app", "frontend", "scripts_manifest",
    "app_data", "node_modules", "__pycache__", ".venv", ".idea", ".vscode",
    ".npm-cache", ".agent-browser",
}

_DATE_RE = re.compile(r"^\d{2,4}-\d{1,2}-\d{1,2}")


def _is_date_dir(name: str) -> bool:
    return bool(_DATE_RE.match(name))


def _scan_entry(path: str) -> Optional[dict]:
    """扫描一个日期目录，返回条目信息；空目录返回 None。"""
    normal_photos, live_movs, live_covers, videos = [], [], [], []
    total_size = 0
    try:
        entries = os.listdir(path)
    except OSError:
        return None

    for name in entries:
        full = os.path.join(path, name)
        if os.path.isdir(full):
            if name == "photo":
                for f in sorted(os.listdir(full)):
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                        normal_photos.append(f"photo/{f}")
                        total_size += os.path.getsize(os.path.join(full, f))
            elif name == "live":
                for f in sorted(os.listdir(full)):
                    low = f.lower()
                    fp = os.path.join(full, f)
                    if low.endswith(".mov"):
                        live_movs.append(f"live/{f}")
                        total_size += os.path.getsize(fp)
                    elif low.endswith((".jpg", ".jpeg")):
                        live_covers.append(f"live/{f}")
                        total_size += os.path.getsize(fp)
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext == ".md":
            continue
        if ext == ".mp4":
            videos.append(name)
            total_size += os.path.getsize(full)
        elif ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            normal_photos.append(name)
            total_size += os.path.getsize(full)

    if not (normal_photos or live_movs or videos):
        return None

    # ---- 封面（最佳图策略） ----
    cover = None
    cover_type = None
    if normal_photos:
        cover = normal_photos[len(normal_photos) // 2]  # 中间位
        cover_type = "image"
    elif live_covers:
        cover = live_covers[0]
        cover_type = "image"
    elif videos:
        cover = videos[0]
        cover_type = "video"
    elif live_movs:
        # 理论上 live_covers 为空才会到这里（mov 无 jpg）
        cover = live_movs[0]
        cover_type = "video"

    # ---- 画廊：全部媒体（图片可点开、视频可播放） ----
    gallery = (
        [{"type": "image", "rel": p} for p in normal_photos]
        + [{"type": "video", "rel": m} for m in live_movs]
        + [{"type": "video", "rel": v} for v in videos]
    )

    return {
        "photos": normal_photos,
        "lives": live_movs,
        "videos": videos,
        "cover": cover,
        "cover_type": cover_type,
        "gallery": gallery,
        "size": total_size,
    }


def _parse_context(path: str) -> tuple[dict, str]:
    """解析 context.md：返回 (元数据字段, 正文摘要)。

    正文在 "## 页面文本" 的代码块里（微博）或视频标题字段（抖音）。
    """
    meta = {}
    body = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(16384)
    except OSError:
        return meta, body
    in_code = False
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            body += line + "\n"
        else:
            m = re.match(r"^- \*\*(.+?)\*\*: (.*)$", s)
            if m:
                meta[m.group(1)] = m.group(2).strip()
    return meta, body.strip()


def scan_root() -> list[dict]:
    """返回全部作者：[{name, entries: [{date_dir, ..., meta}]}]

    所有路径字段均为相对 LIBRARY_ROOT 的相对路径，不泄漏本机绝对路径。
    """
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
            meta, body = _parse_context(os.path.join(sub_path, "context.md"))
            info.update({
                "author": name,
                "date_dir": sub,
                "meta": meta,
                "text_preview": (meta.get("视频标题") or body or "")[:120],
                "rel_dir": f"{name}/{sub}",  # 相对路径（拼接媒体 URL 用）
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
