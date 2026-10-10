"""Explicit Weibo login; importing or checking status never opens a browser."""
import datetime as dt
import json
from pathlib import Path
import time
import threading
from urllib.parse import urljoin, urlsplit

from . import config
from .archive import atomic_write_text

_LOGIN_LOCK = threading.Lock()


def _path():
    return Path(config.DATA_DIR) / 'weibo_cookies.json'


def _allowed_cookie(cookie):
    if (not isinstance(cookie, dict) or not isinstance(cookie.get('name'), str)
            or not isinstance(cookie.get('value'), str) or not cookie['name'] or not cookie['value']):
        return False
    domain = str(cookie.get('domain') or '').lower().lstrip('.')
    if not any(domain == suffix or domain.endswith('.' + suffix) for suffix in ('weibo.com', 'weibo.cn', 'sina.com.cn')):
        return False
    try:
        expiry = float(cookie.get('expires', -1))
        return expiry < 0 or expiry > time.time()
    except (TypeError, ValueError):
        return False


def load_cookies():
    try:
        value = json.loads(_path().read_text(encoding='utf-8'))
        cookies = value.get('cookies') if isinstance(value, dict) and value.get('verified') is True else None
        return [cookie for cookie in cookies if _allowed_cookie(cookie)] if isinstance(cookies, list) else []
    except (OSError, ValueError, TypeError):
        return []


def auth_status():
    cookies = load_cookies()
    saved = any(cookie['name'] == 'SUB' for cookie in cookies)
    return {'logged_in': saved, 'saved': saved, 'cookie_count': len(cookies),
            'message': '已保存用户主动登录的信息；平台有效性以实际扫描为准' if saved else '未保存微博登录信息；公开内容可先尝试扫描'}


def attach_cookies(context):
    cookies = load_cookies()
    if cookies:
        try:
            context.add_cookies(cookies)
        except Exception:
            raise ValueError('已保存的微博登录信息无法加载，请重新登录后重试') from None
    return bool(cookies)


def _confirmed_login(payload):
    if not isinstance(payload, dict) or payload.get('ok') != 1:
        return False
    data = payload.get('data')
    # /api/config exposes a boolean login flag. Its anonymous payload has no
    # uid/user fields; SUB also exists for visitors and is never proof by itself.
    return isinstance(data, dict) and data.get('login') is True


def _login_url(payload):
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    for field in ('passport_login_url', 'loginUrl'):
        value = data.get(field)
        if not isinstance(value, str) or len(value) > 4096:
            continue
        url = urljoin('https://m.weibo.cn/', value)
        try:
            parsed = urlsplit(url)
            if (parsed.scheme == 'https' and not parsed.username and not parsed.password and parsed.port in (None, 443)
                    and parsed.hostname in ('passport.weibo.cn', 'passport.weibo.com', 'login.sina.com.cn', 'm.weibo.cn', 'weibo.com')):
                return url
        except ValueError:
            continue
    return None


def _login_browser(timeout_s=180, *, cancel=None):
    from .weibo_client import BrowserSession, WeiboAccessError
    timeout_s = max(15, min(300, int(timeout_s)))
    with BrowserSession(cancel=cancel, timeout=timeout_s, headless=False, mobile=True, use_saved_cookies=False) as client:
        client.open()
        prompted = False
        while True:
            client.check()
            # While the user enters credentials on Weibo's own login origin,
            # wait for its normal redirect rather than querying the wrong host.
            if urlsplit(client.page.url).hostname != 'm.weibo.cn':
                client.page.wait_for_timeout(min(250, client.timeout_ms()))
                continue
            payload = None
            try:
                payload = client.request('/api/config', '登录状态')
                verified = _confirmed_login(payload)
            except WeiboAccessError:
                verified = False
            if verified:
                cookies = [cookie for cookie in client.context.cookies() if _allowed_cookie(cookie)]
                if not any(cookie['name'] == 'SUB' for cookie in cookies):
                    raise RuntimeError('微博未返回可保存的登录信息，请重试')
                value = {'version': 1, 'verified': True, 'saved_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'cookies': cookies}
                atomic_write_text(_path(), json.dumps(value, ensure_ascii=False))
                return {'ok': True, **auth_status()}
            if not prompted and (login_url := _login_url(payload)):
                prompted = True
                try:
                    client.page.goto(login_url, wait_until='domcontentloaded', timeout=client.timeout_ms())
                except Exception:
                    raise RuntimeError('微博登录页面未能加载，请关闭窗口后重试') from None
            for _ in range(8):
                client.check()
                client.page.wait_for_timeout(min(250, client.timeout_ms()))


def _login_sync(timeout_s=180, *, cancel=None):
    if not _LOGIN_LOCK.acquire(blocking=False):
        raise RuntimeError('已有微博登录窗口，请先完成或关闭该窗口')
    try:
        return _login_browser(timeout_s, cancel=cancel)
    finally:
        _LOGIN_LOCK.release()


async def login_async(timeout_s=180):
    from .sub_search import _run_browser_operation
    return await _run_browser_operation(_login_sync, timeout_s)
