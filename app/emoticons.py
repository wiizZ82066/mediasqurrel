"""Static, transparent local copies of explicitly mapped Weibo emoticons.

No arbitrary URL is accepted by the public helpers. Network work is bounded,
credential-free and optional: callers keep the original token on any failure.
"""
from collections import OrderedDict
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import io
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
import uuid

from PIL import Image
import requests
from urllib3.exceptions import HTTPError

from . import config

MAPPING_FILE = Path(__file__).with_name('weibo_emoticons.json')
# Each item is an exact hostname and a full-match regular expression for its path.
SOURCE_PATTERNS = (
    ('face.t.sinajs.cn', r'/t4/appstyle/expression/ext/normal/[0-9a-f]{2}/[A-Za-z0-9_-]+\.png'),
    ('face.t.sinajs.cn', r'/t4/appstyle/expression/ext/normal/e7/2023_TheWanderingEarthⅡ_mobile\.png'),
)
MAX_MAPPING_BYTES = 512 * 1024
MAX_NAMES = 2000
MAX_BYTES = 1024 * 1024
MAX_PIXELS = 1_000_000
MAX_SIDE = 256
MAX_SECONDS = 10
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_CACHE_FILES = 256
NEGATIVE_SECONDS = 60
_guard = threading.Lock()
_cache_guard = threading.Lock()
_locks = {}
_downloads = threading.BoundedSemaphore(2)
_negative = OrderedDict()
_cache_name = re.compile(r'[0-9a-f]{64}\.png')


def valid_source(source):
    """Restrict even repository mappings to exact, static official CDN paths."""
    if not isinstance(source, str) or len(source) > 2048 or any(c.isspace() for c in source):
        return False
    try:
        parsed = urlsplit(source)
        return (parsed.scheme == 'https' and parsed.port is None
                and not parsed.username and not parsed.password
                and parsed.netloc == parsed.hostname
                and not parsed.query and not parsed.fragment
                and '%' not in parsed.path and '\\' not in source
                and '..' not in parsed.path.split('/')
                and any(parsed.hostname == host and re.fullmatch(pattern, parsed.path)
                        for host, pattern in SOURCE_PATTERNS))
    except ValueError:
        return False


def _valid_name(name):
    return (isinstance(name, str) and re.fullmatch(r'\[[^\[\]/\\\x00-\x20]{1,48}\]', name)
            and not any(c.isspace() for c in name))


@lru_cache(maxsize=1)
def _mapping():
    try:
        with MAPPING_FILE.open('rb') as stream:
            body = stream.read(MAX_MAPPING_BYTES + 1)
        if len(body) > MAX_MAPPING_BYTES:
            return {}
        value = json.loads(body)
        if not isinstance(value, dict) or len(value) > MAX_NAMES:
            return {}
        return {name: source for name, source in value.items()
                if _valid_name(name) and valid_source(source)}
    except (OSError, ValueError, UnicodeError):
        return {}


def get_names():
    """List supported literal tokens without network access or cache writes."""
    return sorted(_mapping())


@contextmanager
def _key_lock(key, deadline):
    with _guard:
        slot = _locks.setdefault(key, [threading.Lock(), 0])
        slot[1] += 1
    acquired = slot[0].acquire(timeout=max(0, deadline - time.monotonic()))
    try:
        yield acquired
    finally:
        if acquired:
            slot[0].release()
        with _guard:
            slot[1] -= 1
            if not slot[1]:
                _locks.pop(key, None)


def _download(source, deadline):
    if not valid_source(source) or time.monotonic() >= deadline:
        return None
    with requests.Session() as session:
        session.trust_env = False
        with session.get(source, timeout=(3, 2), stream=True, allow_redirects=False,
                         headers={'User-Agent': 'MediaSquirrel/Source',
                                  'Accept-Encoding': 'identity', 'Accept': 'image/*'}) as response:
            if response.status_code != 200 or response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                return None
            length = response.headers.get('Content-Length')
            if length and (not length.isdigit() or int(length) > MAX_BYTES):
                return None
            body = bytearray()
            while time.monotonic() < deadline:
                # read1 returns available bytes instead of waiting to fill an
                # entire chunk; no HTTP compression is accepted or decoded.
                read1 = getattr(response.raw, 'read1', None)
                chunk = (read1(8192, decode_content=False) if read1 is not None
                         else response.raw.read(1, decode_content=False))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    return None
            else:
                return None
    if time.monotonic() >= deadline or not body:
        return None
    with Image.open(io.BytesIO(body)) as original:
        if original.format not in ('PNG', 'GIF', 'WEBP', 'JPEG') or original.width * original.height > MAX_PIXELS:
            return None
        original.seek(0)
        original.load()
        image = original.convert('RGBA')
        image.thumbnail((MAX_SIDE, MAX_SIDE))
        image.info.clear()
        output = io.BytesIO()
        image.save(output, 'PNG')
        data = output.getvalue()
        return data if len(data) <= MAX_BYTES and time.monotonic() < deadline else None


def _bounded_download(source, deadline):
    # A fixed number of daemon workers also bounds caller latency if DNS or an
    # image decoder stalls beyond requests' per-socket timeouts. Late results
    # cannot write the cache, and a stuck worker continues to occupy its slot.
    if not _downloads.acquire(timeout=max(0, deadline - time.monotonic())):
        return None
    done = threading.Event()
    result = []

    def work():
        try:
            if not config.MAINTENANCE_ACTIVE:
                result.append(_download(source, deadline))
        except (requests.RequestException, HTTPError, OSError, ValueError, SyntaxError,
                EOFError, Image.DecompressionBombError):
            pass
        finally:
            _downloads.release()
            done.set()

    worker = threading.Thread(target=work, name='emoticon-cache', daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _downloads.release()
        return None
    return result[0] if done.wait(max(0, deadline - time.monotonic())) and result else None


def _cache_space(root, required):
    if required > MAX_CACHE_BYTES or MAX_CACHE_FILES < 1:
        return False
    files = []
    for path in root.glob('*.png'):
        if _cache_name.fullmatch(path.name) and not path.is_symlink():
            stat = path.stat()
            files.append((stat.st_mtime_ns, path, stat.st_size))
    total = sum(size for _, _, size in files)
    count = len(files)
    for _, path, size in sorted(files):
        if total + required <= MAX_CACHE_BYTES and count < MAX_CACHE_FILES:
            break
        path.unlink(missing_ok=True)
        total -= size
        count -= 1
    return total + required <= MAX_CACHE_BYTES and count < MAX_CACHE_FILES


def get_emoticon(name):
    """Return a cached PNG path, or None so the original text remains visible."""
    if not _valid_name(name):
        return None
    source = _mapping().get(name)
    if not source:
        return None
    key = hashlib.sha256(('static-rgba-v1\0' + source).encode()).hexdigest()
    deadline = time.monotonic() + MAX_SECONDS
    root = Path(config.CACHE_DIR) / 'emoticons'
    target = root / (key + '.png')
    with _key_lock(key, deadline) as acquired:
        if not acquired:
            return None
        try:
            if target.is_file() and not target.is_symlink() and 0 < target.stat().st_size <= MAX_BYTES:
                if not config.MAINTENANCE_ACTIVE:
                    os.utime(target, None)
                return str(target)
            with _guard:
                suppressed = _negative.get(key, 0) > time.monotonic()
            if suppressed or config.MAINTENANCE_ACTIVE:
                return None
            data = _bounded_download(source, deadline)
            if not data or config.MAINTENANCE_ACTIVE or time.monotonic() >= deadline:
                raise ValueError('emoticon unavailable')
            with _cache_guard:
                if config.MAINTENANCE_ACTIVE:
                    return None
                root.mkdir(parents=True, exist_ok=True)
                if not _cache_space(root, len(data)):
                    raise ValueError('emoticon cache full')
                temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.partial')
                try:
                    temporary.write_bytes(data)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            return str(target)
        except (OSError, ValueError):
            # Do not log CDN URLs, HTTP bodies, headers or credentials.
            with _guard:
                _negative[key] = time.monotonic() + NEGATIVE_SECONDS
                _negative.move_to_end(key)
                while len(_negative) > 128:
                    _negative.popitem(last=False)
            return None
