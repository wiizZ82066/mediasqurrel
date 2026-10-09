"""Small local avatar cache. Only approved HTTPS platform CDNs are fetched."""
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
import uuid

from PIL import Image
import requests

from . import config, db

CDN_DOMAINS = ('sinaimg.cn', 'sinaimg.com', 'weibocdn.com', 'douyinpic.com',
               'douyinstatic.com', 'byteimg.com', 'pstatp.com', 'ibyteimg.com')
MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 4_000_000
_guard = threading.Lock()
_locks = {}
_downloads = threading.BoundedSemaphore(2)
_negative = OrderedDict()


def valid_source(source):
    if not isinstance(source, str) or len(source) > 8192:
        return False
    try:
        parsed = urlsplit(source)
        host = (parsed.hostname or '').lower().rstrip('.')
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and not parsed.fragment
                and any(host == domain or host.endswith('.' + domain) for domain in CDN_DOMAINS))
    except ValueError:
        return False


def update_source(sub_id, source):
    """A later successful platform response may refresh an expired signed URL."""
    if not valid_source(source):
        return False
    with db.connect() as conn:
        return bool(conn.execute('UPDATE subscriptions SET avatar_source=? WHERE id=? AND avatar_source<>?',
                                 (source, int(sub_id), source)).rowcount)


@contextmanager
def _sub_lock(sub_id):
    with _guard:
        slot = _locks.setdefault(sub_id, [threading.Lock(), 0])
        slot[1] += 1
    try:
        with slot[0]:
            yield
    finally:
        with _guard:
            slot[1] -= 1
            if not slot[1]:
                _locks.pop(sub_id, None)


def _download(source, platform):
    deadline = time.monotonic() + 15
    with _downloads, requests.Session() as session:
        session.trust_env = False
        referer = 'https://weibo.com/' if platform == 'weibo' else 'https://www.douyin.com/'
        with session.get(source, timeout=(3, 8), stream=True, allow_redirects=False,
                         headers={'Referer': referer, 'User-Agent': 'MediaSquirrel/Source'}) as response:
            if response.status_code != 200:
                return None
            length = response.headers.get('Content-Length')
            if length and (not length.isdigit() or int(length) > MAX_BYTES):
                return None
            body = bytearray()
            for chunk in response.iter_content(8192):
                if time.monotonic() > deadline:
                    return None
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    return None
    with Image.open(io.BytesIO(body)) as original:
        if original.format not in ('JPEG', 'PNG', 'WEBP', 'GIF') or original.width * original.height > MAX_PIXELS:
            return None
        original.load()
        image = original.convert('RGB')
        image.thumbnail((256, 256))
        output = io.BytesIO()
        image.save(output, 'JPEG', quality=85)
        return output.getvalue()


def get_avatar(sub_id):
    sub_id = int(sub_id)
    if sub_id <= 0:
        return None
    with db.connect() as conn:
        row = conn.execute('SELECT avatar_source,platform FROM subscriptions WHERE id=?', (sub_id,)).fetchone()
    if not row or not valid_source(row['avatar_source']):
        return None
    source = row['avatar_source']
    key = hashlib.sha256(source.encode()).hexdigest()
    root = Path(config.CACHE_DIR) / 'avatars'
    target = root / f'{sub_id}-{key}.jpg'
    with _sub_lock(sub_id):
        if target.is_file():
            return str(target)
        older = sorted(root.glob(f'{sub_id}-*.jpg'), key=lambda path: path.stat().st_mtime, reverse=True) if root.exists() else []
        fallback = str(older[0]) if older else None
        with _guard:
            suppressed = _negative.get(key, 0) > time.monotonic()
        if suppressed or getattr(config, 'MAINTENANCE_ACTIVE', False):
            return fallback
        try:
            data = _download(source, row['platform'])
            if not data:
                raise ValueError('avatar unavailable')
            root.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.partial')
            try:
                temporary.write_bytes(data)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            # Avatars are rebuildable cache, retain only the current image per sub.
            for path in older:
                path.unlink(missing_ok=True)
            return str(target)
        except (requests.RequestException, OSError, ValueError, Image.DecompressionBombError):
            # Never put the signed CDN URL, HTTP headers or response text in logs.
            with _guard:
                _negative[key] = time.monotonic() + 60
                _negative.move_to_end(key)
                while len(_negative) > 128:
                    _negative.popitem(last=False)
            return fallback
