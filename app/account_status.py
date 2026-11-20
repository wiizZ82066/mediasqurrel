"""Periodically verify saved sessions with their own platform, independently of downloads."""
import asyncio
from datetime import datetime, timezone
import threading
import time
from typing import NamedTuple
from urllib.parse import urlsplit

from . import douyin_auth, weibo_auth

INTERVAL = 30 * 60
RETRY = 5 * 60
PLATFORMS = ('douyin', 'weibo')
_results = {}
_wakes = {}
_workers = {}
_stop = None


class LocalSession(NamedTuple):
    version: str | None
    saved: bool
    limited: bool = False
    verified_at: float | None = None


class Check(NamedTuple):
    version: str
    status: str
    timestamp: float


def _local(platform):
    if platform == 'douyin':
        version, data, verified = douyin_auth.session_snapshot()
        return LocalSession(version, data['saved_login'], data['profile_limited'], verified)
    try:
        stat = weibo_auth._path().stat()
    except OSError:
        return LocalSession(None, False)
    return LocalSession(f'{stat.st_mtime_ns}:{stat.st_size}', weibo_auth.auth_status()['saved'])


def _current(session, previous, now):
    if not session.saved or not session.version:
        return None
    if previous and previous.version != session.version:
        previous = None
    proof = session.verified_at
    if (not session.limited and isinstance(proof, (int, float)) and now - INTERVAL < proof <= now
            and (previous is None or proof > previous.timestamp)):
        return Check(session.version, 'signed_in', proof)
    return previous


def _remaining(check, now):
    if not check or now < check.timestamp:
        return 0
    ttl = RETRY if check.status == 'unknown' else INTERVAL
    return max(0, ttl - (now - check.timestamp))


def wake(platform):
    # The settings route and subscription browser callbacks also run in threads.
    worker, event = _workers.get(platform), _wakes.get(platform)
    if worker is not None and not worker.done() and event is not None:
        worker.get_loop().call_soon_threadsafe(event.set)


def snapshot():
    items = {}
    for platform in PLATFORMS:
        session = _local(platform)
        now = time.time()
        previous = _results.get(platform)
        current = _current(session, previous, now)
        if current != previous or (session.saved and not _remaining(current, now)):
            wake(platform)
        items[platform] = {
            'status': current.status if current else ('pending' if session.saved else 'signed_out'),
            'saved': session.saved, 'limited': session.limited,
            'checked_at': datetime.fromtimestamp(current.timestamp, timezone.utc).isoformat(timespec='seconds') if current else None,
        }
    return {'items': items}


def _weibo_api_status():
    """Use the browser library's cookie jar without starting Chrome."""
    from .browser import sync_playwright
    from .weibo_client import MOBILE_UA
    url = 'https://m.weibo.cn/api/config'
    with sync_playwright() as playwright:
        context = playwright.request.new_context(
            user_agent=MOBILE_UA, storage_state={'cookies': weibo_auth.load_cookies(), 'origins': []})
        try:
            response = context.get(
                url, timeout=3500, max_redirects=0,
                headers={'Referer': 'https://m.weibo.cn/', 'X-Requested-With': 'XMLHttpRequest'})
            if response.status == 200 and response.url == url:
                return 'signed_in' if weibo_auth._confirmed_login(response.json()) else None
            if response.status in (301, 302, 303, 307, 308):
                return None
            return 'unknown'
        finally:
            context.dispose()


def _verify(platform, stop):
    if stop.is_set():
        return 'unknown'
    if platform == 'weibo':
        from .browser import skip_preview_assets
        from .weibo_client import BrowserSession
        try:
            result = _weibo_api_status()
        except Exception:
            result = None  # The normal browser may use the system proxy or receive JSON after navigation.
        if result is not None:
            return result
        # Some sessions need the normal homepage sign-in redirect to establish
        # the mobile cookie. Only its explicit guest response means expired.
        if stop.is_set():
            return 'unknown'
        with BrowserSession(timeout=8, cancel=stop) as session:
            session.page.route('**/*', skip_preview_assets)
            session.open()
            payload = session.request('/api/config', '账号状态')
            if weibo_auth._confirmed_login(payload):
                return 'signed_in'
            if payload.get('ok') == 1 and (payload.get('data') or {}).get('login') is False:
                return 'expired'
            return 'unknown'
    from .browser import launch_chrome, skip_preview_assets, sync_playwright
    with sync_playwright() as playwright:
        browser = launch_chrome(playwright, headless=True, timeout=3500)
        try:
            context = browser.new_context(locale='zh-CN')
            if not douyin_auth.restore_session(context):
                return 'unknown'
            page = context.new_page()
            page.route('**/*', skip_preview_assets)
            try:
                page.goto('https://www.douyin.com/', wait_until='domcontentloaded', timeout=5000)
            except Exception:
                if stop.is_set():
                    return 'unknown'
            if stop.is_set() or urlsplit(page.url).netloc != 'www.douyin.com':
                return 'unknown'
            # The homepage can render without issuing its own self-profile request.
            # Fetch only from that fixed origin, and return only the verification fields.
            data = page.evaluate("""async () => {
                if (location.origin !== 'https://www.douyin.com') return null;
                const response = await fetch('/aweme/v1/web/user/profile/self/?device_platform=webapp&aid=6383',
                    {credentials:'same-origin', redirect:'error', signal:AbortSignal.timeout(4500)});
                if (!response.ok) return null;
                const {status_code, status_msg, user} = await response.json();
                return {status_code, status_msg, user:{uid:user?.uid}};
            }""")
            return douyin_auth.account_response_status(data)
        finally:
            browser.close()


def changed(platform):
    # Interactive login already verified the account against the platform.
    session = _local(platform)
    if session.saved and session.version:
        _results[platform] = Check(session.version, 'signed_in', time.time())
    else:
        _results.pop(platform, None)
    wake(platform)


async def _run(platform, event, stop):
    from .task_manager import broadcast
    published = None
    while not stop.is_set():
        event.clear()
        session = _local(platform)
        previous = _results.get(platform)
        now = time.time()
        current = _current(session, previous, now)
        if session.saved and session.version and not _remaining(current, now):
            try:
                result = await asyncio.to_thread(_verify, platform, stop)
            except Exception:
                result = 'unknown'
            if stop.is_set():
                return
            latest = _local(platform)
            now = time.time()
            if (not latest.saved or latest.version != session.version or _results.get(platform) is not previous
                    or _current(latest, previous, now) != current):
                # A new login or a newer scan confirmation arrived during the request.
                continue
            session = latest
            current = Check(session.version, result, now)
        if current:
            _results[platform] = current
        else:
            _results.pop(platform, None)
        state = (session.saved, session.limited, current)
        if state != published:
            await broadcast({'type': 'account_status', 'platform': platform})
            published = state
        delay = _remaining(current, time.time()) if current else INTERVAL
        try:
            await asyncio.wait_for(event.wait(), timeout=max(1, delay))
        except asyncio.TimeoutError:
            pass


def start():
    global _stop
    if _stop is None:
        _stop = threading.Event()
    for platform in PLATFORMS:
        if platform not in _workers or _workers[platform].done():
            _wakes[platform] = asyncio.Event()
            _workers[platform] = asyncio.create_task(_run(platform, _wakes[platform], _stop))


async def stop():
    global _stop
    if _stop is not None:
        _stop.set()
        for event in _wakes.values():
            event.set()
        await asyncio.gather(*_workers.values(), return_exceptions=True)
    _workers.clear()
    _wakes.clear()
    _stop = None
    _results.clear()
