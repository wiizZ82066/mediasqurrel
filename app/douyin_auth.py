"""抖音登录态管理：cookie 的本地存取与状态查询。

- 登录/验证由用户在可见浏览器中完成一次（拖滑块 + 扫码）
- cookies 导出到 app_data/douyin_cookies.json（仅本地，绝不入库/入代码）
- 消费方（搜索/扫描/下载）统一通过 attach_cookies() 注入
"""
import datetime as _dt
import json
import os
import math
import threading
import time
from urllib.parse import urlsplit

try:
    from . import config as _config
    _DATA = _config.DATA_DIR
except ImportError:  # 脚本独立运行时被 importlib 加载
    from app import config as _config
    _DATA = _config.DATA_DIR

COOKIE_PATH = os.path.join(_DATA, "douyin_cookies.json")
STATUS_PATH = os.path.join(_DATA, "douyin_auth.json")

# Only a nonempty, unexpired session cookie belongs to a saved login.
_LOGIN_COOKIE = "sessionid"
_LOCK = threading.RLock()
_CHECKPOINT_SECONDS = 6 * 3600
_SESSION_KEYS = {'sessionid', 'sessionid_ss', 'sid_guard', 'sid_tt', 'uid_tt', 'uid_tt_ss',
                 'sid_ucp_v1', 'ssid_ucp_v1', 'passport_csrf_token', 'passport_csrf_token_default'}


def _first_party(cookies):
    def allowed(cookie):
        domain = str(cookie.get('domain') or '').lower().lstrip('.')
        return domain == 'douyin.com' or domain.endswith('.douyin.com')
    return [c for c in cookies if isinstance(c, dict) and allowed(c)]


def _revision():
    try:
        stat = os.stat(COOKIE_PATH)
        return f'{stat.st_mtime_ns}:{stat.st_size}'
    except OSError:
        return None


def save_cookies(cookies: list[dict], *, verified=False) -> bool:
    """Atomically replace a saved session only after a successful sign-in."""
    from app.archive import atomic_write_text
    cookies = _first_party(cookies)
    if not has_login_cookie(cookies):
        return False
    with _LOCK:
        atomic_write_text(COOKIE_PATH, json.dumps(cookies, ensure_ascii=False))
        _write_status({'updated_at': _dt.datetime.now().isoformat(timespec='seconds'),
                       'revision': _revision(), 'verified_at': time.time() if verified else 0})
    return True


def load_cookies() -> list[dict]:
    try:
        with open(COOKIE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def restore_session(ctx):
    """Return the exact file generation restored into this browser."""
    with _LOCK:
        revision = _revision()
        cookies = _first_party(load_cookies())
        if revision and cookies:
            try:
                ctx.add_cookies(cookies)
            except Exception:
                # Browser error messages may echo the rejected cookie.
                raise RuntimeError('无法恢复本机抖音登录信息，原文件已保留') from None
            return revision
    return None


def attach_cookies(ctx) -> bool:
    try:
        return restore_session(ctx) is not None
    except RuntimeError:
        return False


def _write_status(value):
    from app.archive import atomic_write_text
    atomic_write_text(STATUS_PATH, json.dumps(value, ensure_ascii=False))


def _status_metadata():
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def auth_status() -> dict:
    with _LOCK:
        saved, cookies = _status_metadata(), load_cookies()
        present = has_login_cookie(cookies)
        limited = present and saved.get('revision') == _revision() and saved.get('profile_limited') is True
        return {"logged_in": present and not limited, "saved_login": present, "profile_limited": limited,
                "cookie_count": len(cookies), "updated_at": saved.get("updated_at", ""), "note": ""}


def account_response_status(data):
    if not isinstance(data, dict):
        return 'unknown'
    user = data.get('user')
    uid = str(user.get('uid') or '') if isinstance(user, dict) else ''
    if data.get('status_code') == 0 and uid.isascii() and uid.isdigit() and int(uid) > 0:
        return 'signed_in'
    reason = str(data.get('status_msg') or '').lower()
    if data.get('status_code') not in (0, None) and any(
            phrase in reason for phrase in ('未登录', '请先登录', '登录已过期', 'not logged in', 'login required')):
        return 'expired'
    return 'unknown'


class AccountObservation:
    """Only a first-party response for the signed-in user can confirm a session."""

    def __init__(self, context):
        self.confirmed = False
        self.signed_out = False
        context.on('response', self._observe)

    def _observe(self, response):
        try:
            url = urlsplit(response.url)
            if (url.scheme != 'https' or url.netloc not in ('www.douyin.com', 'www-hj.douyin.com') or
                    url.path.rstrip('/') != '/aweme/v1/web/user/profile/self' or response.status != 200):
                return
            status = account_response_status(response.json())
            if status == 'signed_in':
                self.confirmed = True
            elif status == 'expired':
                self.signed_out = True
        except Exception:
            # Malformed data or a navigation before the body is ready is not confirmation.
            pass


def checkpoint_due(revision):
    saved = _status_metadata()
    last = saved.get('verified_at')
    return (saved.get('revision') != revision or saved.get('profile_limited') is True or
            not isinstance(last, (int, float)) or not math.isfinite(last) or
            time.time() - last >= _CHECKPOINT_SECONDS)


def record_scan_session(revision, *, guest=False, verified_cookies=None):
    """A late worker must never replace a new QR login or save a guest session."""
    from app.archive import atomic_write_text
    with _LOCK:
        if not revision or revision != _revision():
            return
        saved = _status_metadata()
        if guest:
            if not saved.get('profile_limited') or saved.get('revision') != revision:
                _write_status({**saved, 'revision': revision, 'profile_limited': True})
            return
        if not verified_cookies:
            return
        fresh = _first_party(verified_cookies)
        if not has_login_cookie(fresh):
            return
        def session_keys(cookies):
            return {(c.get('name'), c.get('domain'), c.get('path')): (c.get('value'), c.get('expires'))
                    for c in cookies if c.get('name') in _SESSION_KEYS}
        old = _first_party(load_cookies())
        if not checkpoint_due(revision) and session_keys(old) == session_keys(fresh):
            return
        if fresh != old:
            atomic_write_text(COOKIE_PATH, json.dumps(fresh, ensure_ascii=False))
        _write_status({**saved, 'revision': _revision(), 'profile_limited': False, 'verified_at': time.time()})


def is_logged_in() -> bool:
    return bool(auth_status().get("logged_in"))


def has_login_cookie(cookies: list[dict]) -> bool:
    for cookie in cookies:
        if not isinstance(cookie, dict) or cookie.get('name') != _LOGIN_COOKIE:
            continue
        value = cookie.get('value')
        domain = str(cookie.get('domain') or '').lower().lstrip('.')
        if not isinstance(value, str) or not value or not (domain == 'douyin.com' or domain.endswith('.douyin.com')):
            continue
        try:
            expiry = float(cookie.get('expires', -1))
        except (TypeError, ValueError):
            continue
        if math.isfinite(expiry) and (expiry < 0 or expiry > time.time()):
            return True
    return False


def clear():
    """清除本地登录态。"""
    with _LOCK:
        for p in (COOKIE_PATH, STATUS_PATH):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
