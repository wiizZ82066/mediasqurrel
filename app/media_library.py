"""媒体库：扫描存档根目录，生成 作者 -> 条目 的结构化视图。

目录约定（两个下载脚本共同遵守）:
  <root>/<作者>/<日期目录>/
      context.md      元数据
      photo/img*.jpg  微博普通图片
      live/live*.mov|.jpg  微博 Live 图（jpg 为封面）
      <videoid>.mp4   抖音视频（可含 {id}_cover.jpg 官方封面）

封面策略（"最佳图"算法 _pick_best_cover）:
  - 多图候选逐张评分: 清晰度(拉普拉斯方差, 缩图计算) 70% + 分辨率 30%
    —— 自动跳过糊图/纯色图，选出最清晰最有代表性的一张
  - 评分结果按目录缓存（内容变化才重算）
  - 视频优先使用官方封面 {id}_cover.jpg（无则前端抽帧）
"""
import hashlib
import json
import os
import re
import hashlib
import json
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

_COVER_CACHE_PATH = os.path.join(config.BASE_DIR, "app_data", "cover_cache.json")


def _load_cover_cache() -> dict:
    try:
        with open(_COVER_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cover_cache(cache: dict):
    try:
        os.makedirs(os.path.dirname(_COVER_CACHE_PATH), exist_ok=True)
        with open(_COVER_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError:
        pass


def _pick_best_cover(base_path: str, rel_paths: list[str]) -> str:
    """从候选图选出最佳封面：清晰度(70%) + 分辨率(30%) 综合评分。

    - 清晰度: 拉普拉斯方差（缩到宽 240 计算，单张几毫秒）——
      糊图/纯色图/截图得分低，自动被跳过
    - 分辨率: 百万像素数归一化——高清原图优先
    - 缓存: 目录内容签名（文件名+mtime）不变则直接用上次结果
    """
    if len(rel_paths) == 1:
        return rel_paths[0]

    sig_source = "|".join(
        f"{p}:{int(os.path.getmtime(os.path.join(base_path, p)) * 1000)}"
        for p in rel_paths
    )
    sig = hashlib.md5(sig_source.encode("utf-8", errors="replace")).hexdigest()

    cache = _load_cover_cache()
    hit = cache.get(base_path)
    if hit and hit.get("sig") == sig and hit.get("best") in rel_paths:
        return hit["best"]

    import cv2
    import numpy as np

    best, best_score = rel_paths[0], -1.0
    for rel in rel_paths:
        full = os.path.join(base_path, rel.replace("/", os.sep))
        try:
            data = np.fromfile(full, dtype=np.uint8)  # 中文路径兼容
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is None:
                continue
            h, w = img.shape[:2]
            # 缩图算清晰度（快）
            if w > 240:
                small = cv2.resize(img, (240, max(1, int(h * 240 / w))),
                                   interpolation=cv2.INTER_AREA)
            else:
                small = img
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
            # 归一化评分: 清晰度(封顶1500) 70% + 百万像素(封顶4MP) 30%
            score = min(sharpness, 1500) / 1500 * 0.7 + min(w * h / 1e6, 4) / 4 * 0.3
            if score > best_score:
                best_score, best = score, rel
        except Exception:
            continue

    cache[base_path] = {"sig": sig, "best": best}
    _save_cover_cache(cache)
    return best


def _is_date_dir(name: str) -> bool:
    return bool(_DATE_RE.match(name))


def _scan_entry(path: str) -> Optional[dict]:
    """扫描一个日期目录，返回条目信息；空目录返回 None。"""
    normal_photos, live_movs, live_covers, videos = [], [], [], []
    official_covers = []  # 抖音官方封面（{aweme_id}_cover.jpg）
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
            if name.endswith("_cover.jpg"):
                # 抖音官方封面（{aweme_id}_cover.jpg）：作为对应视频的封面
                official_covers.append(name)
            else:
                normal_photos.append(name)
            total_size += os.path.getsize(full)

    if not (normal_photos or live_movs or videos):
        return None

    # ---- 封面（最佳图策略） ----
    # live 封面与 mov 同名配对（live01.jpg <-> live01.mov）
    live_poster = {}
    for mov in live_movs:
        stem = mov.rsplit(".", 1)[0]
        match = next((c for c in live_covers if c.rsplit(".", 1)[0] == stem), None)
        if match:
            live_poster[mov] = match

    # 官方封面与视频配对（{aweme_id}_cover.jpg <-> {aweme_id}.mp4）
    video_cover = {}
    for v in videos:
        stem = v.rsplit(".", 1)[0]
        match = next((c for c in official_covers if c.rsplit(".", 1)[0] == stem + "_cover"), None)
        if match:
            video_cover[v] = match

    cover = None
    cover_type = None
    if normal_photos:
        cover = _pick_best_cover(path, normal_photos)  # 最佳图算法
        cover_type = "image"
    elif live_covers:
        cover = _pick_best_cover(path, live_covers)    # 多 Live 时同样选最佳
        cover_type = "live"          # Live 图封面：前端显示 LIVE 角标
    elif videos:
        # 官方封面优先（抖音 origin_cover），否则视频首帧（前端 /api/thumb 抽帧）
        cover = video_cover.get(videos[0]) or videos[0]
        cover_type = "video"
    elif live_movs:
        cover = live_poster.get(live_movs[0]) or live_movs[0]
        cover_type = "live"

    # ---- 画廊：全部媒体（图片可点开、视频可播放；live 带封面 poster） ----
    gallery = (
        [{"type": "image", "rel": p} for p in normal_photos]
        + [
            {
                "type": "video",
                "rel": m,
                "live": True,
                **({"poster": live_poster[m]} if m in live_poster else {}),
            }
            for m in live_movs
        ]
        + [
            {
                "type": "video",
                "rel": v,
                **({"poster": video_cover[v]} if v in video_cover else {}),
            }
            for v in videos
        ]
    )

    return {
        "photos": normal_photos,
        "lives": live_movs,
        "videos": videos,
        "cover": cover,
        "cover_type": cover_type,
        "gallery": gallery,
        # live 封面 -> mov 的映射（前端悬停播放用）: {"live/xx.jpg": "live/xx.mov"}
        "live_map": {v: k for k, v in live_poster.items()},
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
    # 作者默认按作品数量从大到小
    authors.sort(key=lambda a: a["count"], reverse=True)
    return authors


def authors_summary() -> list[dict]:
    """轻量版：不带 entries。"""
    return [
        {k: v for k, v in a.items() if k != "entries"}
        for a in scan_root()
    ]
