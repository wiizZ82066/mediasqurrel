"""抖音登录态管理：cookie 的本地存取与状态查询。

- 登录/验证由用户在可见浏览器中完成一次（拖滑块 + 扫码）
- cookies 导出到 app_data/douyin_cookies.json（仅本地，绝不入库/入代码）
- 消费方（搜索/扫描/下载）统一通过 attach_cookies() 注入
"""
import datetime as _dt
import json
import os

try:
    from . import config as _config
    _BASE = _config.BASE_DIR
except ImportError:  # 脚本独立运行时被 importlib 加载
    _BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COOKIE_PATH = os.path.join(_BASE, "app_data", "douyin_cookies.json")
STATUS_PATH = os.path.join(_BASE, "app_data", "douyin_auth.json")

# 登录态标志 cookie（存在即视为已登录）
_LOGIN_COOKIE = "sessionid"
_DY_DOMAINS = [".douyin.com"]


def save_cookies(cookies: list[dict]) -> bool:
    """保存登录 cookies，返回是否包含登录标志。"""
    os.makedirs(os.path.dirname(COOKIE_PATH), exist_ok=True)
    with open(COOKIE_PATH, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False)
    logged = any(c.get("name") == _LOGIN_COOKIE and c.get("value") for c in cookies)
    _write_status(logged_in=logged, cookie_count=len(cookies))
    return logged


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
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "logged_in": logged_in,
            "cookie_count": cookie_count,
            "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "note": note,
        }, f, ensure_ascii=False)


def auth_status() -> dict:
    """当前登录状态（供 UI 展示）。"""
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"logged_in": False, "cookie_count": 0, "updated_at": "", "note": ""}


def is_logged_in() -> bool:
    return bool(auth_status().get("logged_in"))


def has_login_cookie(cookies: list[dict]) -> bool:
    return any(c.get("name") == _LOGIN_COOKIE and c.get("value") for c in cookies)


def clear():
    """清除本地登录态。"""
    for p in (COOKIE_PATH, STATUS_PATH):
        try:
            os.remove(p)
        except OSError:
            pass
    _write_status(logged_in=False, cookie_count=0, note="已手动清除")
