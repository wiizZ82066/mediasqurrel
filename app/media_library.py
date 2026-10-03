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


_FACE_DETECTOR = None


def _get_face_detector():
    """惰性加载 YuNet 人脸检测器（OpenCV 5.x FaceDetectorYN，模型 232KB 本地）。

    模型查找: app_data/models/face_detection_yunet.onnx
    （源码运行在项目根；打包后在 exe 旁或资源目录）
    """
    global _FACE_DETECTOR
    if _FACE_DETECTOR is None:
        try:
            import cv2
            for base in (config.BASE_DIR, getattr(config, "RESOURCE_DIR", config.BASE_DIR)):
                model = os.path.join(base, "app_data", "models", "face_detection_yunet.onnx")
                if os.path.isfile(model):
                    _FACE_DETECTOR = cv2.FaceDetectorYN.create(
                        model, "", (320, 320), score_threshold=0.55,
                    )
                    break
            else:
                _FACE_DETECTOR = False  # 无模型，退化为纯清晰度算法
        except Exception:
            _FACE_DETECTOR = False
    return _FACE_DETECTOR or None


def _detect_face(img_bgr, detector) -> tuple[float, Optional[dict]]:
    """在 BGR 图上检测最大人脸，返回 (人脸面积占比, 人脸中心或None)。"""
    h, w = img_bgr.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(img_bgr)
    if faces is None or len(faces) == 0:
        return 0.0, None
    # faces 每行: x,y,w,h, 5个关键点x/y…, score(末列)；取置信度最高的
    best = max(faces, key=lambda f: float(f[-1]))
    x, y, fw, fh = [float(v) for v in best[:4]]
    return (fw * fh) / (w * h), {
        "x": round((x + fw / 2) / w, 3),
        "y": round((y + fh / 2) / h, 3),
    }


def _pick_best_cover(base_path: str, rel_paths: list[str]) -> tuple[str, Optional[dict]]:
    """从候选图选出最佳封面（人物优先），返回 (最佳图, 人脸中心或None)。

    评分:
      含人脸: 人脸占比 40% + 清晰度 40% + 分辨率 20%
              （特写 > 远景，清晰特写最优——人像封面最佳实践）
      无人脸: 清晰度 60% + 分辨率 40%，总分 × 0.85（让位于含人图）

    显示定位: 最佳图含人脸时返回归一化人脸中心 {x, y}，
    前端用于 object-position 对准人脸（避免裁剪切脸）。
    缓存: 目录内容签名不变则直接复用。
    """
    if len(rel_paths) == 1:
        face = _face_center_of(os.path.join(base_path, rel_paths[0].replace("/", os.sep)))
        return rel_paths[0], face

    sig_source = "|".join(
        f"{p}:{int(os.path.getmtime(os.path.join(base_path, p)) * 1000)}"
        for p in rel_paths
    )
    sig = hashlib.md5(sig_source.encode("utf-8", errors="replace")).hexdigest()

    cache = _load_cover_cache()
    hit = cache.get(base_path)
    if (
        hit and hit.get("sig") == sig and hit.get("best") in rel_paths
        and ("face" in hit)
    ):
        return hit["best"], hit.get("face")

    import cv2
    import numpy as np

    detector = _get_face_detector()
    best, best_score, best_face = rel_paths[0], -1.0, None
    for rel in rel_paths:
        full = os.path.join(base_path, rel.replace("/", os.sep))
        try:
            data = np.fromfile(full, dtype=np.uint8)  # 中文路径兼容
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is None:
                continue
            h, w = img.shape[:2]
            if w > 240:
                small = cv2.resize(img, (240, max(1, int(h * 240 / w))),
                                   interpolation=cv2.INTER_AREA)
            else:
                small = img
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
            sharp_s = min(sharpness, 1500) / 1500
            mp_s = min(w * h / 1e6, 4) / 4

            # 人脸检测（YuNet，缩图上跑）
            face_ratio, face_center = 0.0, None
            if detector is not None:
                face_ratio, face_center = _detect_face(small, detector)

            if face_ratio > 0:
                score = min(face_ratio * 4, 1.0) * 0.4 + sharp_s * 0.4 + mp_s * 0.2
            else:
                score = (sharp_s * 0.6 + mp_s * 0.4) * 0.85

            if score > best_score:
                best_score, best, best_face = score, rel, face_center
        except Exception:
            continue

    cache[base_path] = {"sig": sig, "best": best, "face": best_face}
    _save_cover_cache(cache)
    return best, best_face


def _face_center_of(full_path: str) -> Optional[dict]:
    """单张图的人脸中心（缓存未命中时的单图场景）。"""
    try:
        import cv2
        import numpy as np
        detector = _get_face_detector()
        if detector is None:
            return None
        data = np.fromfile(full_path, dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            return None
        if img.shape[1] > 240:
            img = cv2.resize(img, (240, max(1, int(img.shape[0] * 240 / img.shape[1]))),
                             interpolation=cv2.INTER_AREA)
        _, center = _detect_face(img, detector)
        return center
    except Exception:
        return None


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
    cover_face = None  # 最佳封面的人脸中心（前端 object-position 对准人脸）
    if normal_photos:
        cover, cover_face = _pick_best_cover(path, normal_photos)  # 人物优先算法
        cover_type = "image"
    elif live_covers:
        cover, cover_face = _pick_best_cover(path, live_covers)    # 多 Live 同样算法
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
        "cover_face": cover_face,
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
