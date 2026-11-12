"""Runtime paths separate from source. Importing this module never writes data.

MS_DATA_DIR retains the legacy portable layout (app_data/ and library/).
New installs use data/ and library/. Local path choices are never tracked.
"""
import json
import os
import sys
from pathlib import Path
from uuid import UUID

DOUYIN_DETAIL_CHANNEL = 'browser'
_FROZEN = getattr(sys, "frozen", False)
SOURCE_DIR = str(Path(__file__).resolve().parent.parent)
RESOURCE_DIR = str(getattr(sys, "_MEIPASS", SOURCE_DIR))


def _default_home():
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Media Squirrel")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "media-squirrel")


PATHS_FILE = os.environ.get("MS_PATHS_FILE") or os.path.join(
    _default_home() if _FROZEN else SOURCE_DIR,
    "paths.json" if _FROZEN else ".mediasquirrel.local.json",
)


def _read_paths():
    try:
        with open(PATHS_FILE, encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("路径配置必须是对象")
        for key in ("data_dir", "library_root"):
            if key in value and (not isinstance(value[key], str) or not os.path.isabs(value[key])):
                raise ValueError(f"{key} 必须是绝对路径")
        return value
    except FileNotFoundError:
        return {}


def _store_matches(marker, expected):
    try:
        with marker.open(encoding="utf-8") as stream:
            record = json.load(stream)
            return isinstance(record, dict) and record.get("id") == expected
    except FileNotFoundError:
        return False
    except OSError as error:
        if getattr(error, "winerror", None) in (15, 21, 1167):  # Unmounted, not ready, disconnected.
            return False
        raise


def _pair_requested(paths):
    overrides = ("MS_HOME", "MS_DATA_DIR", "MS_APP_DATA_DIR", "MS_LIBRARY_DIR")
    return paths.get("preferred_store") is not None and not paths.get("pending_transition") and not any(
        os.environ.get(name) for name in overrides)


def _preferred_store(paths):
    """Select an already prepared library/database pair without creating either."""
    if not _pair_requested(paths):
        return None
    spec = paths["preferred_store"]
    if not isinstance(spec, dict) or not isinstance(spec.get("root"), str) or not Path(spec["root"]).is_absolute():
        raise ValueError("首选媒体库根目录必须是绝对路径")
    identity = spec.get("id")
    if not isinstance(identity, str) or UUID(identity).hex != identity:
        raise ValueError("首选媒体库标识无效")
    root = Path(spec["root"])

    def child(name):
        raw = spec.get(name)
        if not isinstance(raw, str) or not raw:
            raise ValueError("首选媒体库子路径无效")
        if name == "library" and raw == ".":
            if root.parent == root:
                raise ValueError("媒体库需要独立目录")
            return root
        rel = Path(raw)
        if not rel.parts or rel.anchor or ".." in rel.parts:
            raise ValueError("首选媒体库子路径必须位于已配对目录内")
        return root / rel

    database, library = child("database"), child("library")
    marker = root / ".media-squirrel-ready.json"
    if not _store_matches(marker, identity):
        return None
    real_root = root.resolve(strict=True)
    if not all(p.resolve().is_relative_to(real_root) for p in (database, library, marker)):
        raise ValueError("首选媒体库路径越界")
    if library.resolve() == real_root and database.resolve().parent == real_root:
        raise ValueError("直接使用配对目录作为媒体库时，数据库需要独立子目录")
    if not database.is_file() or not library.is_dir():
        return None
    with database.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise ValueError("首选媒体库的数据库文件无效，未进行写入")
    return str(database), str(library), marker, identity


_paths = _read_paths()
_legacy_home = os.environ.get("MS_DATA_DIR")
BASE_DIR = os.path.abspath(_legacy_home or os.environ.get("MS_HOME") or _default_home())
DATA_DIR = os.path.abspath(os.environ.get("MS_APP_DATA_DIR") or
                          (_paths.get("data_dir") if not _legacy_home else None) or
                          os.path.join(BASE_DIR, "app_data" if _legacy_home or _FROZEN else "data"))
LIBRARY_ROOT = os.path.abspath(os.environ.get("MS_LIBRARY_DIR") or
                              (_paths.get("library_root") if not _legacy_home else None) or
                              os.path.join(BASE_DIR, "library"))
DB_PATH = os.path.join(DATA_DIR, "app.db")
_store = _preferred_store(_paths)
_paired = _pair_requested(_paths)
_nested_data_dir = None
if _store:
    DB_PATH, LIBRARY_ROOT = _store[:2]
    data_parent = Path(DB_PATH).resolve().parent
    if Path(LIBRARY_ROOT).resolve() in data_parent.parents:
        _nested_data_dir = data_parent


def is_library_data_path(resolved_path):
    """Keep an embedded database directory out of the media namespace."""
    return _nested_data_dir is not None and Path(resolved_path).is_relative_to(_nested_data_dir)


def store_data_dir():
    return str(Path(DB_PATH).parent) if _store else DATA_DIR


LOG_DIR = os.path.join(store_data_dir(), "logs")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
BACKUP_DIR = os.path.join(store_data_dir(), "backups")
MANIFEST_DIR = os.path.join(RESOURCE_DIR, "scripts_manifest")
FRONTEND_DIST = os.path.join(RESOURCE_DIR, "frontend", "dist")
FACE_MODEL_PATH = os.path.join(RESOURCE_DIR, "app_data", "models", "face_detection_yunet.onnx")
HOST = "127.0.0.1"
PORT = int(os.environ.get("MS_PORT") or 8642)
MAX_CONCURRENT_TASKS = 1
MAX_QUEUED_TASKS = 1000
MAX_SCAN_CONCURRENCY = 2
DOWNLOAD_TIMEOUT = 120
DOWNLOAD_RETRIES = 2
CACHE_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_DOWNLOAD_DIR = None
MAINTENANCE_ACTIVE = False


def uses_prepared_database(path):
    return bool(_paired and Path(path) == Path(DB_PATH))


def paired_store_configured():
    return bool(_paths.get("preferred_store"))


def require_prepared_store():
    if _store and not _store_matches(*_store[2:]):
        raise OSError("当前媒体库已断开或磁盘不同；请退出后重新启动以选择可用媒体库")
    if _paired and not Path(LIBRARY_ROOT).is_dir():
        raise FileNotFoundError("当前配对的媒体目录不可用")


def ensure_runtime_dirs():
    if _paired:
        require_prepared_store()
        if not Path(DB_PATH).is_file():
            raise FileNotFoundError("当前配对的数据库不可用，未创建空库")
    directories = (DATA_DIR, LOG_DIR, CACHE_DIR) if _paired else (DATA_DIR, LIBRARY_ROOT, LOG_DIR, CACHE_DIR)
    for path in directories:
        os.makedirs(path, exist_ok=True)


def legacy_locations():
    """Discovery only: no copy, writes, or implicit credential import."""
    candidates = [
        ("source", os.path.join(SOURCE_DIR, "app_data"), SOURCE_DIR),
        ("desktop", os.path.join(_default_home(), "app_data"), os.path.join(_default_home(), "library")),
    ]
    return [dict(kind=kind, data_dir=data, library_root=library,
                 database_exists=os.path.isfile(os.path.join(data, "app.db")),
                 library_exists=os.path.isdir(library))
            for kind, data, library in candidates
            if os.path.normcase(os.path.abspath(data)) != os.path.normcase(DATA_DIR)]
