"""订阅博主搜索：本地存档作者提取 + 线上博主搜索（微博/抖音）。

- local_authors(): 扫描媒体库存档的 context.md，反查 (作者名, platform, blogger_id)
- search_weibo(kw): m.weibo.cn 搜索页内 fetch 用户搜索 container API
- search_douyin(kw): 打开抖音用户搜索页，拦截搜索接口响应

线上搜索均为 playwright/scrapling 同步阻塞调用，API 层需 to_thread 包装。
"""
import asyncio
import json
import os
import re

from playwright.sync_api import sync_playwright

from . import config

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


# ---------------------------------------------------------------- 本地作者

_WEIBO_UID_RE = re.compile(r"weibo\.com/(\d+)/")
_SKIP_DIRS = {
    ".git", ".ab-profile", "app", "frontend", "scripts_manifest",
    "app_data", "node_modules", "__pycache__", ".venv", ".idea", ".vscode",
    ".npm-cache", ".agent-browser", "src-tauri", "backend-dist", "portable", "build",
}


def local_authors() -> list[dict]:
    """扫描存档，按作者聚合平台身份。

    返回: [{name, platforms: {platform: blogger_id}, entries}]
    """
    root = config.LIBRARY_ROOT
    authors: dict[str, dict] = {}
    try:
        names = os.listdir(root)
    except OSError:
        return []

    for name in names:
        author_path = os.path.join(root, name)
        if not os.path.isdir(author_path) or name in _SKIP_DIRS or name.startswith("."):
            continue
        info = authors.setdefault(name, {"name": name, "platforms": {}, "entries": 0})
        for sub in os.listdir(author_path):
            ctx = os.path.join(author_path, sub, "context.md")
            if not os.path.isfile(ctx):
                continue
            info["entries"] += 1
            try:
                with open(ctx, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(4096)
            except OSError:
                continue
            # 微博: 原文链接 weibo.com/{uid}/xxx
            m = _WEIBO_UID_RE.search(content)
            if m:
                info["platforms"]["weibo"] = m.group(1)
                continue
            # 抖音: 作者sec_uid 字段（新存档）
            m = re.search(r"作者sec_uid\*\*: (\S+)", content)
            if m:
                info["platforms"]["douyin"] = m.group(1)

    return sorted(
        [a for a in authors.values() if a["platforms"] or a["entries"]],
        key=lambda a: a["entries"],
        reverse=True,
    )


# ---------------------------------------------------------------- 微博线上搜索

def _parse_followers(v) -> float:
    """'1109.1万' -> 11091000; '5432' -> 5432; 失败 -> 0（用于排序）。"""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("粉丝：", "").replace("粉丝:", "").strip()
    m = re.match(r"([\d.]+)\s*(万|亿)?", s)
    if not m:
        return 0
    n = float(m.group(1))
    if m.group(2) == "万":
        n *= 10_000
    elif m.group(2) == "亿":
        n *= 100_000_000
    return n


def _fmt_followers(n) -> str:
    n = _parse_followers(n)
    if n <= 0:
        return ""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(int(n))


def _weibo_search_sync(kw: str) -> list[dict]:
    """打开 m.weibo.cn 搜索页，域内 fetch 用户搜索 API。"""
    captured: list = []

    def on_response(resp):
        try:
            if "container/getIndex" in resp.url:
                captured.append(resp.json())
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=MOBILE_UA, locale="zh-CN",
            viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
        )
        page = ctx.new_page()
        page.on("response", on_response)
        try:
            from urllib.parse import quote
            page.goto(
                f"https://m.weibo.cn/search?weibo={quote(kw)}",
                wait_until="domcontentloaded", timeout=60000,
            )
            page.wait_for_timeout(5000)

            # 域内 fetch 用户搜索（containerid=100103type=3 为用户维度）
            result = page.evaluate(
                """async (kw) => {
                    const url = 'https://m.weibo.cn/api/container/getIndex?containerid=' +
                        encodeURIComponent('100103type=3&q=' + kw) + '&page_type=searchall';
                    const r = await fetch(url, { credentials: 'include' });
                    if (!r.ok) return { http_error: r.status };
                    return await r.json();
                }""",
                kw,
            )
        finally:
            browser.close()

    if not isinstance(result, dict) or result.get("ok") != 1:
        return []

    cards = ((result.get("data") or {}).get("cards")) or []
    users = []
    seen = set()
    for card in cards:
        # 用户卡片: card_group 里多个 user，或 card 直接带 user
        cands = []
        if card.get("card_group"):
            cands.extend(card["card_group"])
        if card.get("user"):
            cands.append(card)
        for c in cands:
            u = c.get("user")
            if not u or not u.get("id"):
                continue
            uid = str(u["id"])
            if uid in seen:
                continue
            seen.add(uid)
            raw_followers = u.get("followers_count") or (
                # 兜底：卡片描述 "粉丝：1109.1万"
                str(c.get("desc2") or "").replace("粉丝：", "").replace("粉丝:", "") or None
            )
            users.append({
                "nickname": u.get("screen_name") or "",
                "blogger_id": uid,
                "platform": "weibo",
                "followers": _parse_followers(raw_followers),
                "followers_text": _fmt_followers(raw_followers),
                "verified": bool(u.get("verified")),
                "avatar": (u.get("avatar_hd") or u.get("profile_image_url") or "").replace("http://", "https://"),
                "desc": (c.get("desc1") or u.get("description") or "")[:40],
            })
    return users


# ---------------------------------------------------------------- 抖音线上搜索

# 抖音搜索的持久化浏览器 Profile：完成一次人机验证后，信任态留存，
# 后续无头搜索不再触发验证码。
_DY_PROFILE = os.path.join(config.BASE_DIR, "app_data", "douyin_profile")

_CAPTCHA_SEL = (
    '[class*=captcha], [id*=captcha], [class*=verify], iframe[src*=captcha]'
)


def _dy_open(p, kw: str, headless: bool):
    """打开抖音搜索页（persistent profile），返回 (context, page, hunter)。"""
    from urllib.parse import quote

    from .browser import XHRHunter, get_ua

    ctx = p.chromium.launch_persistent_context(
        _DY_PROFILE,
        channel="chrome",
        headless=headless,
        user_agent=get_ua(),
        locale="zh-CN",
        viewport={"width": 1380, "height": 900},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    hunter = XHRHunter(r"aweme/v1/web/(search|discover)").attach(page)
    page.goto(
        f"https://www.douyin.com/search/{quote(kw)}",
        wait_until="domcontentloaded",
        timeout=60000,
    )
    return ctx, page, hunter


def _parse_dy_users(hunter) -> list[dict]:
    """从搜索响应解析用户：user_list 优先，视频作者聚合补充。"""
    from .browser import XHRHunter  # noqa: F401

    users, seen = [], set()

    def add(u: dict):
        sec = u.get("sec_uid")
        if not sec or sec in seen:
            return
        seen.add(sec)
        users.append({
            "nickname": u.get("nickname") or "",
            "blogger_id": sec,
            "platform": "douyin",
            "followers": u.get("follower_count") or 0,
            "followers_text": _fmt_followers(u.get("follower_count")),
            "verified": bool(u.get("is_verified") or u.get("verified")),
            "avatar": ((u.get("avatar_thumb") or {}).get("url_list") or [""])[0],
        })

    for data in hunter.json_results():
        # 用户搜索响应: data[].user_list[].user_info
        for item in data.get("data") or []:
            if isinstance(item, dict):
                for entry in item.get("user_list") or []:
                    add(entry.get("user_info") or entry.get("user") or {})
        if data.get("user_list"):
            for entry in data["user_list"]:
                add(entry.get("user_info") or entry.get("user") or {})
        # 综合搜索响应: data[].aweme_list[].author（视频作者聚合）
        for item in data.get("data") or []:
            if isinstance(item, dict):
                for aw in item.get("aweme_list") or []:
                    add(aw.get("aweme", {}).get("author") or {})
    return users


def _douyin_search_sync(kw: str) -> list[dict]:
    """无头搜索。检测到人机验证时抛出 CaptchaRequiredError（由 API 层转标记）。"""
    with sync_playwright() as p:
        ctx, page, hunter = _dy_open(p, kw, headless=True)
        try:
            page.wait_for_timeout(9000)
            has_captcha = page.evaluate(
                f"() => !!document.querySelector('{_CAPTCHA_SEL}')"
            )
            if has_captcha:
                raise CaptchaRequiredError()
            page.wait_for_timeout(3000)
        finally:
            ctx.close()
    return _parse_dy_users(hunter)


class CaptchaRequiredError(RuntimeError):
    """抖音要求人机验证（需用户在可见浏览器中完成一次滑块）。"""


def douyin_verify_sync(timeout_s: int = 120) -> dict:
    """有头模式打开搜索页，等待用户完成滑块验证并出现搜索接口。

    验证通过的信任态写入持久化 Profile，之后无头搜索不再触发验证。
    """
    with sync_playwright() as p:
        ctx, page, hunter = _dy_open(p, "抖音", headless=False)
        try:
            import time as _t
            deadline = _t.time() + timeout_s
            while _t.time() < deadline:
                page.wait_for_timeout(1500)
                if hunter.results:
                    return {"ok": True, "detail": "验证成功，已解锁无头搜索"}
            # 超时：若验证码已消失也算部分成功（信任态可能已写入）
            has_captcha = page.evaluate(
                f"() => !!document.querySelector('{_CAPTCHA_SEL}')"
            )
            if not has_captcha:
                return {"ok": True, "detail": "验证码已消失，可重试搜索"}
            return {"ok": False, "detail": f"等待验证超时（{timeout_s}s），请重试"}
        finally:
            ctx.close()


def search_online(platform: str, kw: str) -> list[dict]:
    """线上搜索入口（同步阻塞）。返回按粉丝数降序的前 5。

    抖音遇人机验证时抛 CaptchaRequiredError（调用方转 captcha_required 标记）。
    """
    if platform == "weibo":
        users = _weibo_search_sync(kw)
    elif platform == "douyin":
        users = _douyin_search_sync(kw)
    else:
        return []
    users.sort(key=lambda u: u.get("followers") or 0, reverse=True)
    return users[:5]


async def search_online_async(platform: str, kw: str) -> list[dict]:
    return await asyncio.to_thread(search_online, platform, kw)
