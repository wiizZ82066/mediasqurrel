"""File-based download identity and atomic completion markers.

Existing date-only archives are never moved or adopted by a new download. A
versioned marker belongs to one platform post; an interrupted download remains
partial until every declared file has been written successfully.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit, urlunsplit
import uuid


SCHEMA_VERSION = 1
MARKER_NAME = "entry.json"
_POST_ID = re.compile(r"^[A-Za-z0-9]{1,128}$")
_DATE_PREFIX = re.compile(r"^\d{2}(?:\d{2})?-\d{2}-\d{2}(?:-\d{2}-\d{2})?$")
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _author_component(author: str) -> str:
    result = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(author)).strip().rstrip(". ")
    if not result or result in (".", ".."):
        result = "unknown"
    if _RESERVED.match(result):
        result = "_" + result
    if len(result) > 80:
        result = result[:64] + "_" + hashlib.sha256(result.encode()).hexdigest()[:12]
    return result


def _inside(folder: Path, relative: str) -> Path:
    """Resolve manifest paths without following a link outside its entry."""
    if Path(relative).is_absolute() or Path(relative).drive or relative.startswith(("/", "\\")):
        raise ValueError("存档文件路径必须是相对路径")
    candidate = (folder / relative).resolve()
    if candidate == folder or os.path.commonpath((str(folder), str(candidate))) != str(folder):
        raise ValueError("存档文件路径越界")
    return candidate


@contextlib.contextmanager
def atomic_output(path: str | Path):
    """Replace a file only after a successful write; preserve any previous file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".part", dir=path.parent)
    os.close(fd)
    try:
        yield temporary
        with open(temporary, "r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_write_text(path: str | Path, text: str) -> None:
    with atomic_output(path) as temporary:
        with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)


def read_manifest(folder: str | Path) -> dict | None:
    """Read a marker. Missing or malformed markers return None, never success."""
    try:
        with open(Path(folder) / MARKER_NAME, encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def entry_status(folder: str | Path, *, verify_files: bool = False) -> str:
    """Return legacy, partial, or complete; callers decide legacy compatibility."""
    folder = Path(folder).resolve()
    if not (folder / MARKER_NAME).exists():
        return "legacy"
    marker = read_manifest(folder)
    if not marker or marker.get("schema_version") != SCHEMA_VERSION or marker.get("status") != "complete":
        return "partial"
    files = marker.get("files")
    if not isinstance(files, list) or not files or not any(
        isinstance(item, dict) and item.get("path") == "context.md" for item in files
    ):
        return "partial"
    if verify_files:
        try:
            for item in files:
                path = _inside(folder, item["path"])
                if not path.is_file() or path.stat().st_size != item["size"] or item["size"] <= 0:
                    return "partial"
        except (OSError, ValueError, TypeError, KeyError):
            return "partial"
    return "complete"


class ArchiveEntry:
    def __init__(self, folder: Path, marker: dict, existing_complete: bool):
        self.folder = str(folder)
        self.marker = marker
        self.existing_complete = existing_complete

    def complete(self, files: list[str]) -> None:
        """Validate declared outputs before atomically publishing success."""
        folder = Path(self.folder).resolve()
        if "context.md" not in files:
            raise ValueError("完成存档前必须保存 context.md")
        inventory = []
        for relative in sorted(set(files)):
            path = _inside(folder, relative)
            if not path.is_file() or path.stat().st_size <= 0:
                raise ValueError("存档文件缺失或为空，不能标记下载完成")
            inventory.append({"path": relative.replace("\\", "/"), "size": path.stat().st_size})
        self.marker.update(status="complete", completed_at=_now(), files=inventory)
        atomic_write_text(folder / MARKER_NAME, json.dumps(self.marker, ensure_ascii=False, indent=2) + "\n")


def begin_entry(root: str, author: str, date_prefix: str, platform: str,
                item_id: str, source_url: str = "") -> ArchiveEntry:
    """Reuse the same post on retry, including a changed date or author label.

    Only versioned, matching entries are reused. Unmarked old archives remain
    untouched. The caller must finish with complete() after all required media.
    """
    if platform not in ("weibo", "douyin") or not _POST_ID.fullmatch(str(item_id)):
        raise ValueError("无法确定有效的平台内容 ID，已停止保存以避免覆盖其他内容")
    if not _DATE_PREFIX.fullmatch(date_prefix):
        raise ValueError("存档日期格式无效")
    item_id = str(item_id)
    root_path = Path(root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    suffix = f"_{platform}_{item_id}"
    matches = []
    # Looking at names first avoids opening every archive's metadata. This also
    # reuses a post whose nickname/date changed since its interrupted attempt.
    for author_dir in root_path.iterdir():
        if author_dir.is_symlink() or not author_dir.is_dir() or author_dir.name.startswith("."):
            continue
        if not author_dir.resolve().is_relative_to(root_path):
            continue
        for entry_dir in author_dir.iterdir():
            if entry_dir.name.endswith(suffix) and not entry_dir.is_symlink() and entry_dir.is_dir():
                if not entry_dir.resolve().is_relative_to(root_path):
                    continue
                marker = read_manifest(entry_dir)
                if marker and marker.get("platform") == platform and marker.get("item_id") == item_id:
                    matches.append((entry_dir, marker))
    if len(matches) > 1:
        raise ValueError("同一内容存在多个存档位置，请先检查冲突；未覆盖任何目录")
    if matches:
        folder, previous = matches[0]
        if previous.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("存档标记版本不兼容，未覆盖已有数据")
        if entry_status(folder, verify_files=True) == "complete":
            return ArchiveEntry(folder, previous, True)
    else:
        folder = _inside(root_path, f"{_author_component(author)}/{date_prefix}{suffix}")
        if folder.exists():
            raise FileExistsError("目标目录已有未确认归属的数据，已停止保存")
        folder.mkdir(parents=True)
        previous = {}
    parsed = urlsplit(source_url)
    safe_url = urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    marker = {
        "schema_version": SCHEMA_VERSION,
        "entry_id": previous.get("entry_id") or str(uuid.uuid4()),
        "platform": platform,
        "item_id": item_id,
        "source_url": safe_url,
        "status": "partial",
        "started_at": previous.get("started_at") or _now(),
        "updated_at": _now(),
        "attempt": int(previous.get("attempt", 0)) + 1,
        "files": [],
    }
    atomic_write_text(folder / MARKER_NAME, json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
    return ArchiveEntry(folder, marker, False)
