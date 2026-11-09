"""抖音登录态管理：cookie 的本地存取与状态查询。

- 登录/验证由用户在可见浏览器中完成一次（拖滑块 + 扫码）
- cookies 导出到 app_data/douyin_cookies.json（仅本地，绝不入库/入代码）
- 消费方（搜索/扫描/下载）统一通过 attach_cookies() 注入
"""
import datetime as _dt
import json
import os
import math
import time

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
_DY_DOMAINS = [".douyin.com"]


def save_cookies(cookies: list[dict]) -> bool:
    """Atomically replace a saved session only after a successful sign-in."""
    from app.archive import atomic_write_text
    if not has_login_cookie(cookies):
        return False
    atomic_write_text(COOKIE_PATH, json.dumps(cookies, ensure_ascii=False))
    _write_status(logged_in=True, cookie_count=len(cookies))
    return True


def load_cookies() -> list[dict]:
    try:
        with open(COOKIE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def attach_cookies(ctx) -> bool:
    """向浏览器 context 注入已保存的抖音 cookies（无则跳过）。"""
    cookies = load_cookies()
    if not cookies:
        return False
    try:
        ctx.add_cookies(cookies)
        return True
    except Exception:
        return False


def _write_status(logged_in: bool, cookie_count: int, note: str = ""):
    from app.archive import atomic_write_text
    atomic_write_text(STATUS_PATH, json.dumps({
        "logged_in": logged_in,
        "cookie_count": cookie_count,
        "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "note": note,
    }, ensure_ascii=False))


def auth_status() -> dict:
    """Read current saved credentials, never trust the historical status flag."""
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    cookies = load_cookies()
    return {"logged_in": has_login_cookie(cookies), "cookie_count": len(cookies),
            "updated_at": saved.get("updated_at", ""), "note": ""}


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
    for p in (COOKIE_PATH, STATUS_PATH):
        try:
            os.remove(p)
        except OSError:
            pass
    _write_status(logged_in=False, cookie_count=0, note="已手动清除")
