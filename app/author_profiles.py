"""Identity-based author avatars, independent of whether an author is subscribed."""
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
import uuid

from . import avatars, config

_slots = threading.BoundedSemaphore(2)
_pending = set()
_failed_until = {}
_guard = threading.Lock()


def valid_identity(platform, identity):
    pattern = r'[0-9]{1,20}' if platform == 'weibo' else r'MS4wLjAB[A-Za-z0-9_-]{8,248}' if platform == 'douyin' else r'(?!)'
    return isinstance(identity, str) and bool(re.fullmatch(pattern, identity))


def from_metadata(platform, meta):
    identity = meta.get('作者sec_uid') if platform == 'douyin' else meta.get('作者UID')
    if valid_identity(platform, identity):
        return identity
    if platform == 'weibo':
        try:
            url = urlsplit(str(meta.get('原文链接') or ''))
            if url.hostname in ('weibo.com', 'www.weibo.com', 'm.weibo.cn'):
                match = re.match(r'^/(?:u/|profile/)?([0-9]{1,20})(?:/|$)', url.path)
                if match:
                    return match[1]
        except ValueError:
            pass
    return ''


def avatar_url(platform, identity):
    return f'/api/authors/{platform}/{identity}/avatar' if valid_identity(platform, identity) else None


def _cache_path(platform, identity):
    digest = hashlib.sha256(f'{platform}:{identity}'.encode()).hexdigest()
    return Path(config.CACHE_DIR) / 'avatars' / 'authors' / f'{digest}.jpg'


def _douyin_profile(value, identity):
    if str(value.get('sec_uid') or value.get('secUid') or '') != identity:
        return None
    for key in ('avatar_larger', 'avatar_medium', 'avatar_thumb', 'avatarLarger', 'avatarMedium', 'avatarThumb'):
        image = value.get(key)
        candidates = image.get('url_list', image.get('urlList', [])) if isinstance(image, dict) else [image] if isinstance(image, str) else []
        for source in candidates if isinstance(candidates, list) else []:
            if avatars.valid_source(source):
                return {'nickname':str(value.get('nickname') or ''), 'avatar_source':source}
    return None


def fetch_profile(platform, identity):
    if not valid_identity(platform, identity):
        return None
    if platform == 'weibo':
        from .weibo_client import fetch_profile as weibo_profile
        return weibo_profile(identity)
    from . import browser, douyin_auth
    from .douyin_page import find_script_data
    with browser.sync_playwright() as p:
        try:
            owned = p.chromium.launch(channel='chrome', headless=True, timeout=10000)
        except Exception:
            owned = p.chromium.launch(headless=True, timeout=10000)
        try:
            context = browser.stealth_context(owned)
            douyin_auth.attach_cookies(context)
            page = context.new_page()
            page.set_default_timeout(2000)
            page.route('**/*', lambda route: route.abort() if route.request.resource_type in
                       ('media', 'image', 'font') else route.continue_())
            responses = []
            def response(value):
                url = urlsplit(value.url)
                if url.hostname == 'www.douyin.com' and url.path.rstrip('/') == '/aweme/v1/web/user/profile/other' and len(responses) < 4:
                    responses.append(value)
            page.on('response', response)
            deadline = time.monotonic() + 30
            try:
                page.goto(f'https://www.douyin.com/user/{identity}', wait_until='domcontentloaded', timeout=20000)
            except Exception as error:
                if type(error).__name__ != 'TimeoutError':
                    raise
            while time.monotonic() < deadline:
                while responses:
                    value = responses.pop(0)
                    if value.status in (403, 429, 444):
                        return None
                    raw = value.body()
                    if value.status == 200 and len(raw) <= 2 * 1024 * 1024:
                        profile = _douyin_profile(json.loads(raw).get('user') or {}, identity)
                        if profile:
                            return profile
                scripts = page.locator('script').evaluate_all(
                    '(els)=>els.map(e=>e.textContent).filter(t=>t && (t.startsWith("self.__pace_f.push(") || t.startsWith("%7B") || t.startsWith("{")))')
                profile = find_script_data(scripts, lambda value: _douyin_profile(value, identity))
                if profile:
                    return profile
                page.wait_for_timeout(500)
        finally:
            owned.close()
    return None


def get_avatar(platform, identity):
    """At most two profile lookups; list APIs never call this network operation."""
    if not valid_identity(platform, identity):
        return None
    target = _cache_path(platform, identity)
    if target.is_file():
        return str(target)
    key = (platform, identity)
    with _guard:
        if key in _pending or _failed_until.get(key, 0) > time.monotonic() or config.MAINTENANCE_ACTIVE:
            return None
        if not _slots.acquire(blocking=False):
            return None
        _pending.add(key)
    try:
        profile = fetch_profile(platform, identity)
        source = (profile or {}).get('avatar_source')
        if not avatars.valid_source(source):
            return None
        data = avatars._download(source, platform)
        if not data:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.' + uuid.uuid4().hex + '.partial')
        try:
            temporary.write_bytes(data)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return str(target)
    except Exception:
        # Profile responses can contain signed URLs and account state.
        return None
    finally:
        with _guard:
            _pending.discard(key)
            if not target.is_file():
                _failed_until[key] = time.monotonic() + 300
                while len(_failed_until) > 128:
                    _failed_until.pop(next(iter(_failed_until)))
        _slots.release()
