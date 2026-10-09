"""Runtime paths separate from source. Importing this module never writes data.

MS_DATA_DIR retains the legacy portable layout (app_data/ and library/).
New installs use data/ and library/. Local path choices are never tracked.
"""
import json
import os
import sys
from pathlib import Path

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
LOG_DIR = os.path.join(DATA_DIR, "logs")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
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


def ensure_runtime_dirs():
    for path in (DATA_DIR, LIBRARY_ROOT, LOG_DIR, CACHE_DIR):
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
