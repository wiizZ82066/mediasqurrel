"""Periodically verify saved sessions with their own platform, independently of downloads."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import threading
import time

from . import douyin_auth, weibo_auth

INTERVAL = 30 * 60
RETRY = 5 * 60
PLATFORMS = ('douyin', 'weibo')
_results = {}
_wake = None
_worker = None
_stop = None


def _local(platform):
    if platform == 'weibo':
        path = weibo_auth._path()
        saved = bool(weibo_auth.auth_status().get('saved'))
        limited = False
    else:
        path = Path(douyin_auth.COOKIE_PATH)
        data = douyin_auth.auth_status()
        saved, limited = data['saved_login'], data['profile_limited']
    try:
        stat = path.stat()
        version = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        version = None
    return version, saved, limited


def snapshot():
    items = {}
    for platform in PLATFORMS:
        version, saved, limited = _local(platform)
        prev = _results.get(platform, {})
        current = prev if prev.get('version') == version and saved else {}
        items[platform] = {
            'status': current.get('status', 'pending') if saved else 'signed_out',
            'saved': saved, 'checked_at': current.get('checked_at'), 'limited': limited,
        }
    return {'items': items}


def _verify(platform, stop):
    if platform == 'weibo':
        from .weibo_client import BrowserSession
        with BrowserSession(timeout=22, cancel=stop) as session:
            session.open()
            payload = session.request('/api/config', '账号状态')
            if weibo_auth._confirmed_login(payload):
                return 'signed_in'
            if payload.get('ok') == 1 and (payload.get('data') or {}).get('login') is False:
                return 'expired'
            return 'unknown'
    from .browser import launch_chrome, sync_playwright
    with sync_playwright() as playwright:
        browser = launch_chrome(playwright, headless=True)
        try:
            context = browser.new_context(locale='zh-CN')
            if not douyin_auth.restore_session(context):
                return 'unknown'
            account = douyin_auth.AccountObservation(context)
            page = context.new_page()
            page.route('**/*', lambda route: route.abort() if route.request.resource_type in ('media', 'image', 'font') else route.continue_())
            try:
                page.goto('https://www.douyin.com/user/self', wait_until='domcontentloaded', timeout=10000)
            except Exception:
                if stop.is_set():
                    return 'unknown'
            until = time.monotonic() + 6
            while not stop.is_set() and not account.confirmed and not account.signed_out and time.monotonic() < until:
                page.wait_for_timeout(250)
            if account.confirmed:
                return 'signed_in'
            return 'expired' if account.signed_out else 'unknown'
        finally:
            browser.close()


def changed(platform):
    # Interactive login already verified the account against the platform.
    version, saved, _ = _local(platform)
    if saved:
        _results[platform] = {'status': 'signed_in', 'version': version, 'next': time.monotonic() + INTERVAL,
                              'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    else:
        _results.pop(platform, None)
    if _wake:
        _wake.set()


async def _run(wake, stop):
    while not stop.is_set():
        wake.clear()
        delay = INTERVAL
        for platform in PLATFORMS:
            if stop.is_set():
                return
            version, saved, _ = _local(platform)
            if not saved:
                _results.pop(platform, None)
                continue
            previous = _results.get(platform, {})
            if previous.get('version') == version and previous.get('next', 0) > time.monotonic():
                delay = min(delay, previous['next'] - time.monotonic())
                continue
            try:
                result = await asyncio.to_thread(_verify, platform, stop)
            except Exception:
                result = 'unknown'
            if stop.is_set():
                return
            if _local(platform)[:2] != (version, True) or _results.get(platform, {}) != previous:
                wake.set()
                continue
            seconds = RETRY if result == 'unknown' else INTERVAL
            _results[platform] = {'status': result, 'version': version, 'next': time.monotonic()+seconds,
                                  'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
            delay = min(delay, seconds)
        try:
            await asyncio.wait_for(wake.wait(), timeout=max(1, delay))
        except asyncio.TimeoutError:
            pass


def start():
    global _wake, _worker, _stop
    if _worker is None or _worker.done():
        _wake, _stop = asyncio.Event(), threading.Event()
        _worker = asyncio.create_task(_run(_wake, _stop))


async def stop():
    global _worker, _wake, _stop
    if _worker:
        _stop.set()
        _wake.set()
        await asyncio.gather(_worker, return_exceptions=True)
    _worker = _wake = _stop = None
    _results.clear()
