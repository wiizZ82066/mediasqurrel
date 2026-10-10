"""订阅博主搜索：本地存档作者提取 + 线上博主搜索（微博/抖音）。

- local_authors(): 从持久化索引聚合前200名本地作者，不读取原媒体
- search_weibo(kw): m.weibo.cn 搜索页内 fetch 用户搜索 container API
- search_douyin(kw): 打开抖音用户搜索页，拦截搜索接口响应

线上搜索均为 playwright/scrapling 同步阻塞调用，API 层需 to_thread 包装。
"""
import asyncio
import json
import os
import re
import threading
import time
import weakref
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright

from . import config, db
from .weibo_client import BrowserSession, checked_payload, clean_text, fetch_profile, parse_profile

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


# ---------------------------------------------------------------- 本地作者

_WEIBO_UID_RE = re.compile(r'(?:weibo\.com|m\.weibo\.cn)/(?:u/|profile/)?(\d+)(?:[/?#]|$)')


def local_authors() -> list[dict]:
    """聚合已索引且未缺失的存档，按条目数取最多200个作者。

    返回: [{name, platforms: {platform: blogger_id}, entries}]
    """
    with db.connect() as conn:
        rows = conn.execute("""SELECT author,COUNT(*) AS entries,
            MAX(CASE WHEN json_valid(meta_json) THEN json_extract(meta_json,'$."作者sec_uid"') END) AS douyin_id,
            MAX(CASE WHEN platform='weibo' AND json_valid(meta_json)
                THEN json_extract(meta_json,'$."原文链接"') END) AS weibo_url
            FROM media_entries WHERE availability='present'
            GROUP BY author ORDER BY entries DESC,author LIMIT 200""").fetchall()
    result = []
    for row in rows:
        platforms = {}
        match = _WEIBO_UID_RE.search(str(row['weibo_url'] or ''))
        if match:
            platforms['weibo'] = match[1]
        if row['douyin_id']:
            platforms['douyin'] = str(row['douyin_id'])
        result.append({'name': row['author'], 'platforms': platforms, 'entries': row['entries']})
    return result


_SEARCH_SLOTS = threading.BoundedSemaphore(2)
_PROFILE_SLOT = threading.Lock()
_SEARCH_CONTEXT = threading.local()
_ASYNC_SEARCH = weakref.WeakKeyDictionary()


def _check_search():
    cancel = getattr(_SEARCH_CONTEXT, 'cancel', None)
    if cancel and cancel.is_set():
        raise SearchCancelled('搜索已取消')
    if time.monotonic() >= getattr(_SEARCH_CONTEXT, 'deadline', float('inf')):
        raise TimeoutError('博主搜索超时，请稍后重试')


def _timeout_ms(maximum=10000):
    _check_search()
    return max(1, min(maximum, int((getattr(_SEARCH_CONTEXT, 'deadline', time.monotonic() + 10) - time.monotonic()) * 1000)))


def _wait_search(page, milliseconds):
    remaining = milliseconds
    while remaining > 0:
        _check_search()
        step = min(250, remaining, _timeout_ms())
        page.wait_for_timeout(step)
        remaining -= step
    _check_search()


def _acquire(slot):
    while not slot.acquire(timeout=.1):
        _check_search()
    try:
        _check_search()
    except BaseException:
        slot.release()
        raise


class SearchCancelled(RuntimeError):
    pass


def parse_identity(platform, keyword):
    """Recognize explicit identities, without claiming online verification."""
    value = str(keyword or '').strip()
    if not value or len(value) > 512:
        raise ValueError('请输入1至512个字符的昵称、用户ID或主页链接')
    identity = None
    if '://' in value:
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or url.username or url.password or url.port not in (None, 80, 443):
            raise ValueError('主页链接格式不支持')
        path = unquote(url.path).strip('/')
        if platform == 'weibo' and url.hostname in ('weibo.com', 'www.weibo.com', 'm.weibo.cn'):
            match = re.fullmatch(r'(?:(?:u|profile)/)?(\d{1,20})', path)
            identity = match[1] if match else None
        elif platform == 'douyin' and url.hostname in ('douyin.com', 'www.douyin.com'):
            match = re.fullmatch(r'user/([A-Za-z0-9_-]{8,256})', path)
            identity = match[1] if match else None
        if not identity:
            raise ValueError('请填写所选平台的博主主页链接；作品和短链接不能作为订阅主页')
    elif platform == 'weibo' and re.fullmatch(r'(?i)(?:uid\s*[:：]\s*)?\d{1,20}', value):
        identity = re.sub(r'(?i)^uid\s*[:：]\s*', '', value)
    elif platform == 'douyin':
        match = re.fullmatch(r'(?i)sec_uid\s*[:：]\s*([A-Za-z0-9_-]{8,256})', value)
        identity = match[1] if match else value if re.fullmatch(r'MS4wLjABAAAA[A-Za-z0-9_-]{8,244}', value) else None
    if identity:
        return {'nickname': identity, 'blogger_id': identity, 'platform': platform,
                'homepage': f'https://weibo.com/u/{identity}' if platform == 'weibo' else f'https://www.douyin.com/user/{identity}',
                'followers': 0, 'followers_text': '', 'verified': False, 'avatar': '',
                'direct_identity': True, 'desc': '已识别用户ID；昵称与主页尚未联网核验'}
    return None


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
    """Complete visitor/SSO navigation before requesting the user-search API."""
    from urllib.parse import urlencode
    with BrowserSession(cancel=getattr(_SEARCH_CONTEXT, 'cancel', None), timeout=40,
                        check=_check_search) as client:
        client.open()
        query = urlencode({'containerid': '100103type=3&q=' + kw, 'page_type': 'searchall'})
        result = client.request('/api/container/getIndex?' + query, '用户搜索')
    return _parse_weibo_users(result)


def _parse_weibo_users(result):
    if not isinstance(result, dict) or result.get("ok") != 1:
        raise RuntimeError('微博未返回有效搜索结果，请检查网络或平台登录/验证状态')
    data = checked_payload(result, '用户搜索')
    cards = data.get('cards')
    if not isinstance(cards, list):
        raise RuntimeError('微博用户搜索结构发生变化，未将响应当作空结果')
    users = []
    seen = set()
    for card in cards:
        if not isinstance(card, dict):
            continue
        # 用户卡片: card_group 里多个 user，或 card 直接带 user
        cands = []
        if card.get("card_group"):
            cands.extend(card["card_group"])
        if card.get("user"):
            cands.append(card)
        for c in cands:
            if not isinstance(c, dict):
                continue
            u = c.get("user")
            if not isinstance(u, dict) or not str(u.get("id") or '').isdigit():
                continue
            uid = str(u["id"])
            if uid in seen:
                continue
            seen.add(uid)
            profile = parse_profile({'ok': 1, 'data': {'userInfo': u}}, uid)
            raw_followers = u.get("followers_count") or (
                # 兜底：卡片描述 "粉丝：1109.1万"
                str(c.get("desc2") or "").replace("粉丝：", "").replace("粉丝:", "") or None
            )
            users.append({
                "nickname": profile['nickname'],
                "blogger_id": uid,
                "platform": "weibo",
                "followers": _parse_followers(raw_followers),
                "followers_text": _fmt_followers(raw_followers),
                "verified": bool(u.get("verified")),
                "avatar": profile['avatar_source'],
                "homepage": profile['homepage'],
                "desc": clean_text(c.get("desc1") or u.get("description"), 40),
            })
    return users


# ---------------------------------------------------------------- 抖音线上搜索

# 抖音搜索的持久化浏览器 Profile：完成一次人机验证后，信任态留存，
# 后续无头搜索不再触发验证码。
_DY_PROFILE = os.path.join(config.DATA_DIR, "douyin_profile")

_CAPTCHA_SEL = (
    '[class*=captcha], [id*=captcha], [class*=verify], iframe[src*=captcha]'
)


def _dy_open(p, kw: str, headless: bool, use_cookies: bool = False):
    """打开抖音搜索页（persistent profile），返回 (context, page, hunter)。

    use_cookies=True 时注入本地保存的登录 cookies（搜索等无头消费方）。
    """
    from urllib.parse import quote

    from . import douyin_auth
    from .browser import XHRHunter, get_ua

    common = dict(
        user_agent=get_ua(),
        locale="zh-CN",
        viewport={"width": 1380, "height": 900},
    )
    try:
        # 优先系统 Chrome（指纹真实，利于过风控）
        ctx = p.chromium.launch_persistent_context(
            _DY_PROFILE, channel="chrome", headless=headless, timeout=_timeout_ms(), **common,
        )
    except Exception:
        _check_search()
        # 无系统 Chrome 时退回 Playwright 自带 chromium
        ctx = p.chromium.launch_persistent_context(
            _DY_PROFILE, headless=headless, timeout=_timeout_ms(), **common,
        )
    try:
        _check_search()
        if use_cookies:
            douyin_auth.attach_cookies(ctx)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(2000)
        class SearchHunter(XHRHunter):
            def _on_response(self, response):
                if len(self._hits) < 16:
                    super()._on_response(response)
                    if self._hits and len(self._hits[-1]['body']) > 2 * 1024 * 1024:
                        self._hits[-1]['body'] = ''
        hunter = SearchHunter(r"aweme/v1/web/.*(search|discover)").attach(page)
        page.goto(
            f"https://www.douyin.com/search/{quote(kw)}?type=general",
            wait_until="domcontentloaded", timeout=_timeout_ms(),
        )
        return ctx, page, hunter
    except BaseException:
        ctx.close()
        raise


def _extract_dom_users(page) -> list[dict]:
    """从页面 DOM 提取用户卡片（昵称/粉丝数/sec_uid）。

    无头被拦时数据接口发不出，但真人拖完滑块后页面会渲染结果，
    此时 DOM 里有 a[href*="/user/{sec_uid}"] 链接与卡片文本。
    """
    import time as _t

    deadline = _t.time() + 75  # 等用户拖滑块，最长 75 秒
    rows = []
    while _t.time() < deadline:
        _check_search()
        try:
            rows = page.evaluate(
                """() => {
                    const out = [];
                    const seen = new Set();
                    for (const a of document.querySelectorAll('a[href*="/user/"]')) {
                        const href = a.getAttribute('href') || '';
                        const sec = href.split('/user/')[1]?.split('?')[0] || '';
                        if (!sec || sec === 'self' || seen.has(sec)) continue;
                        seen.add(sec);
                        const card = a.closest('li, [class*=card], [class*=user]');
                        out.push({
                            sec_uid: sec,
                            text: (card?.innerText || a.innerText || '').trim(),
                        });
                    }
                    return out;
                }"""
            )
        except Exception:
            rows = []
        if len(rows) >= 2:
            break
        _wait_search(page, 2000)

    users, seen_uid = [], set()
    bad_nick = {"认证徽章", "登录", "注册", "首页", "粉丝", "关注", "作品", "点赞"}
    for row in rows:
        sec = row["sec_uid"]
        if sec in seen_uid:
            continue
        seen_uid.add(sec)
        lines = [ln.strip() for ln in row["text"].split("\n") if ln.strip()]
        # 昵称 = 第一个非徽章/非数字的行
        nickname = next(
            (ln for ln in lines
             if ln not in bad_nick and not re.match(r"^[\d.,\s万亿]+$", ln)),
            "",
        )
        m = re.search(r"([\d.]+\s*[万亿]?)\s*粉丝", row["text"])
        followers_text = m.group(1).replace(" ", "") if m else ""
        if not nickname:
            continue
        # 简介 = 昵称之后的第一条有效行
        desc = ""
        for i, ln in enumerate(lines):
            if ln == nickname and i + 1 < len(lines) and lines[i + 1] != nickname:
                desc = lines[i + 1]
                break
        users.append({
            "nickname": nickname,
            "blogger_id": sec,
            "platform": "douyin",
            "followers": _parse_followers(followers_text),
            "followers_text": followers_text,
            "verified": "认证" in row["text"],
            "avatar": "",
            "desc": desc[:30],
        })
    return users


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

    def _stream_json_objects(text: str) -> list[dict]:
        """chunked 流响应（多个 JSON 拼接）提取全部完整 JSON 对象。"""
        dec = json.JSONDecoder()
        objs, idx = [], 0
        while idx < len(text):
            c = text.find("{", idx)
            if c < 0:
                break
            try:
                obj, end = dec.raw_decode(text, c)
                objs.append(obj)
                idx = end
            except ValueError:
                idx = c + 1
        return objs

    api_datas: list[dict] = []
    for hit in hunter.results:
        try:
            api_datas.append(json.loads(hit["body"]))
        except ValueError:
            api_datas.extend(_stream_json_objects(hit["body"]))

    for data in api_datas:
        if not isinstance(data, dict):
            continue
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
    """抖音搜索：无头优先，被拦时自动升级可见窗口（用户拖滑块）后 DOM 提取。

    - 未登录 + 被拦 -> 抛 CaptchaRequiredError（前端引导登录）
    - 已登录 + 被拦 -> 弹可见窗口，注入提示条，等待用户拖滑块（最长75s），
      结果渲染后自动抓取并关窗
    """
    from . import douyin_auth

    with sync_playwright() as p:
        # ---- 无头尝试 ----
        ctx, page, hunter = _dy_open(p, kw, headless=True, use_cookies=True)
        try:
            _wait_search(page, 9000)
            has_captcha = page.evaluate(
                f"() => !!document.querySelector('{_CAPTCHA_SEL}')"
            )
            if not has_captcha:
                _wait_search(page, 3000)
                users = _parse_dy_users(hunter)
                if users:
                    return users
        finally:
            ctx.close()

        # ---- 未登录：交给前端引导登录 ----
        if not douyin_auth.is_logged_in():
            raise CaptchaRequiredError()

        # ---- 已登录：可见窗口半自动（用户拖滑块）----
        ctx, page, hunter = _dy_open(p, kw, headless=False, use_cookies=True)
        try:
            page.evaluate(
                """() => {
                    const tip = document.createElement('div');
                    tip.textContent = '🐿️ Media Squirrel：如出现滑块请拖动完成，结果出现后本窗口自动关闭';
                    tip.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;'
                        + 'background:#0071e3;color:#fff;padding:10px 16px;font-size:14px;'
                        + 'font-weight:600;text-align:center;font-family:sans-serif;';
                    document.body.appendChild(tip);
                }"""
            )
            users = _extract_dom_users(page)
            # 接口数据（若已发出）优先补充
            api_users = _parse_dy_users(hunter)
            merged = {u["blogger_id"]: u for u in users}
            for u in api_users:
                merged.setdefault(u["blogger_id"], u)
            return list(merged.values())
        finally:
            ctx.close()


class CaptchaRequiredError(RuntimeError):
    """抖音要求人机验证（需用户在可见浏览器中完成一次滑块）。"""


def _douyin_login(timeout_s: int = 180) -> dict:
    """可见浏览器窗口：用户完成滑块验证 + 扫码登录，导出登录 cookies。

    流程（全程用户可见）:
      1. 打开抖音搜索页（persistent profile）
      2. 如有验证码 -> 用户拖一下滑块
      3. 用户点头像/登录按钮扫码登录
      4. 程序轮询: 验证消失 + 登录 cookie(sessionid) 出现 -> 导出 cookies
    登录态保存于 app_data/douyin_cookies.json（仅本地）。
    """
    import time as _t

    from . import douyin_auth

    with sync_playwright() as p:
        ctx, page, hunter = _dy_open(p, "抖音", headless=False)
        try:
            deadline = _t.time() + timeout_s
            stage = "等待人机验证与登录"
            while _t.time() < deadline:
                _wait_search(page, 2000)
                cookies = ctx.cookies()
                if douyin_auth.has_login_cookie(cookies):
                    logged = douyin_auth.save_cookies(cookies)
                    return {
                        "ok": True,
                        "logged_in": logged,
                        "detail": "登录成功，登录态已保存到本地",
                    }
            # 超时：导出当前 cookies（即使未登录，验证信任态也有价值）
            cookies = ctx.cookies()
            logged = douyin_auth.save_cookies(cookies)
            if logged:
                return {"ok": True, "logged_in": True, "detail": "登录成功"}
            return {
                "ok": False,
                "logged_in": False,
                "detail": f"等待超时（{timeout_s}s）。若已完成滑块，可稍后重试搜索；扫码登录可获得完整体验",
            }
        finally:
            ctx.close()


def douyin_login_sync(timeout_s: int = 180, *, cancel=None) -> dict:
    timeout_s = max(1, min(180, int(timeout_s)))
    _SEARCH_CONTEXT.cancel = cancel
    _SEARCH_CONTEXT.deadline = time.monotonic() + timeout_s + 20
    try:
        _acquire(_PROFILE_SLOT)
        try:
            return _douyin_login(timeout_s)
        finally:
            _PROFILE_SLOT.release()
    finally:
        _SEARCH_CONTEXT.__dict__.clear()


def search_online(platform: str, kw: str, *, cancel=None) -> list[dict]:
    """线上搜索入口（同步阻塞）。返回按粉丝数降序的前 5。

    抖音遇人机验证时抛 CaptchaRequiredError（调用方转 captcha_required 标记）。
    """
    if platform not in ('weibo', 'douyin'):
        raise ValueError('不支持的平台')
    kw = str(kw or '').strip()
    direct = parse_identity(platform, kw)
    if direct and platform != 'weibo':
        return [direct]
    _SEARCH_CONTEXT.cancel = cancel
    _SEARCH_CONTEXT.deadline = time.monotonic() + 90
    try:
        _acquire(_SEARCH_SLOTS)
        try:
            if platform == 'weibo':
                if direct:
                    profile = fetch_profile(direct['blogger_id'], cancel=cancel, check=_check_search)
                    users = [{**direct, 'nickname': profile['nickname'] or direct['nickname'],
                              'avatar': profile['avatar_source'], 'homepage': profile['homepage'],
                              'followers': _parse_followers(profile['followers']),
                              'followers_text': _fmt_followers(profile['followers']),
                              'verified': profile['verified'], 'profile_verified': True, 'desc': profile['desc']}]
                else:
                    users = _weibo_search_sync(kw)
            else:
                _acquire(_PROFILE_SLOT)
                try:
                    users = _douyin_search_sync(kw)
                finally:
                    _PROFILE_SLOT.release()
            _check_search()
            users.sort(key=lambda u: _parse_followers(u.get('followers')), reverse=True)
            return users[:5]
        finally:
            _SEARCH_SLOTS.release()
    finally:
        _SEARCH_CONTEXT.__dict__.clear()


async def search_online_async(platform: str, kw: str) -> list[dict]:
    return await _run_browser_operation(search_online, platform, kw)


async def douyin_login_async(timeout_s=180):
    return await _run_browser_operation(douyin_login_sync, timeout_s)


async def _run_browser_operation(function, *args):
    loop = asyncio.get_running_loop()
    state = _ASYNC_SEARCH.setdefault(loop, {'slots': asyncio.Semaphore(2), 'requests': 0, 'workers': {}, 'tasks': set(), 'closing': False})
    if state['closing']:
        raise RuntimeError('应用正在退出，搜索已停止')
    if state['requests'] >= 16:
        raise RuntimeError('搜索请求较多，请取消旧搜索后再试')
    state['requests'] += 1
    request = asyncio.current_task()
    state['tasks'].add(request)
    try:
        async with state['slots']:
            cancel = threading.Event()
            # Carry exceptions as values: a cancelled shield otherwise reports
            # the thread's later exception as unhandled on Python 3.14.
            def invoke():
                try:
                    return True, function(*args, cancel=cancel)
                except Exception as error:
                    return False, error
            worker = asyncio.create_task(asyncio.to_thread(invoke))
            state['workers'][worker] = cancel
            try:
                ok, result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancel.set()
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        cancel.set()
                raise
            finally:
                state['workers'].pop(worker, None)
            if not ok:
                raise result
            return result
    finally:
        state['requests'] -= 1
        state['tasks'].discard(request)


async def stop_searches():
    """Await browser cleanup before the backend loop closes."""
    state = _ASYNC_SEARCH.get(asyncio.get_running_loop())
    if not state:
        return
    state['closing'] = True
    for cancel in list(state['workers'].values()):
        cancel.set()
    requests = set(state['tasks']) - {asyncio.current_task()}
    for task in requests:
        task.cancel()
    if requests:
        await asyncio.gather(*requests, return_exceptions=True)
