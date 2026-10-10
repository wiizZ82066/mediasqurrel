"""Weibo profile/list requests inside an ephemeral browser visitor context.

No browser or user data is opened on import. Raw response text and signed avatar
URLs never become error messages; the caller decides when profile data persists.
"""
import html
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

from .browser import sync_playwright

MOBILE_UA = ('Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36')
DESKTOP_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


class WeiboAccessError(ValueError):
    def __init__(self, message, reason='platform_error'):
        super().__init__(message)
        self.reason = reason


def clean_text(value, limit=200):
    text = html.unescape(re.sub(r'<[^>]*>', '', str(value or '')))
    return re.sub(r'\s+', ' ', text).strip()[:limit]


def checked_uid(value):
    uid = str(value or '').strip()
    if not re.fullmatch(r'\d{1,20}', uid):
        raise ValueError('微博订阅需要数字博主ID')
    return uid


def checked_payload(payload, label='内容列表'):
    if not isinstance(payload, dict):
        raise WeiboAccessError(f'微博{label}响应格式无效；请检查平台验证')
    if payload.get('ok') in (-100, '-100') or payload.get('errno') in (100001, '100001', 100005, '100005'):
        raise WeiboAccessError(f'微博{label}需要登录或平台验证，请使用“微博登录”后重试', 'login_required')
    if payload.get('ok') not in (1, '1'):
        raise WeiboAccessError(f'微博{label}未成功返回；请检查登录、平台验证或限流状态')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise WeiboAccessError(f'微博{label}结构发生变化，未将响应当作空列表')
    return data


def parse_profile(payload, uid):
    uid = checked_uid(uid)
    data = checked_payload(payload, '博主资料')
    user = data.get('userInfo') or data.get('user')
    if not isinstance(user, dict) or str(user.get('idstr') or user.get('id') or '') != uid:
        raise WeiboAccessError('微博返回的博主资料与请求ID不一致')
    nickname = clean_text(user.get('screen_name'), 120)
    avatar = str(user.get('avatar_hd') or user.get('avatar_large') or user.get('profile_image_url') or '')
    if avatar.startswith('http://'):
        avatar = 'https://' + avatar[7:]
    from .avatars import valid_source
    if not valid_source(avatar):
        avatar = ''
    container = ''
    tabs = (data.get('tabsInfo') or {}).get('tabs') or []
    for tab in tabs:
        if isinstance(tab, dict) and tab.get('tab_type') == 'weibo':
            value = str(tab.get('containerid') or '')
            if re.fullmatch(r'\d{1,64}', value):
                container = value
                break
    return {'blogger_id': uid, 'nickname': nickname, 'avatar_source': avatar,
            'homepage': f'https://weibo.com/u/{uid}', 'container_id': container,
            'followers': user.get('followers_count') or 0, 'verified': bool(user.get('verified')),
            'desc': clean_text(user.get('description'), 80)}


class BrowserSession:
    def __init__(self, *, cancel=None, timeout=45, headless=True, mobile=True, check=None, use_saved_cookies=True):
        self.cancel = cancel or threading.Event()
        self.deadline = time.monotonic() + max(1, timeout)
        self.headless, self.mobile, self.external_check = headless, mobile, check
        self.use_saved_cookies = use_saved_cookies
        self.manager = self.playwright = self.browser = self.context = self.page = None

    def check(self):
        if self.external_check:
            self.external_check()
        if self.cancel.is_set():
            from .scanners.common import ScanCancelled
            raise ScanCancelled('微博操作已取消')
        if time.monotonic() >= self.deadline:
            raise TimeoutError('微博操作超时，请检查网络或平台验证后重试')

    def timeout_ms(self, maximum=8000):
        self.check()
        return max(1, min(maximum, int((self.deadline - time.monotonic()) * 1000)))

    def __enter__(self):
        try:
            self.check()
            self.manager = sync_playwright()
            self.playwright = self.manager.__enter__()
            self.browser = self.playwright.chromium.launch(headless=self.headless, timeout=self.timeout_ms(10000))
            options = {'user_agent': MOBILE_UA if self.mobile else DESKTOP_UA, 'locale': 'zh-CN'}
            if self.mobile:
                options.update(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
            self.context = self.browser.new_context(**options)
            from .weibo_auth import attach_cookies
            if self.use_saved_cookies:
                attach_cookies(self.context)
            self.page = self.context.new_page()
            self.page.set_default_timeout(2000)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.manager and self.playwright:
                self.manager.__exit__(None, None, None)

    def open(self, uid=None):
        host = 'm.weibo.cn' if self.mobile else 'weibo.com'
        suffix = f'/u/{checked_uid(uid)}' if uid else '/'
        self.page.goto(f'https://{host}{suffix}', wait_until='domcontentloaded', timeout=self.timeout_ms(10000))
        # Visitor cookies are issued during an automatic cross-domain redirect.
        # Issuing API requests before returning to Weibo can yield HTTP 432/403.
        while urlsplit(self.page.url).hostname != host:
            self.check()
            self.page.wait_for_timeout(min(250, self.timeout_ms()))
        self.check()

    def request(self, path, label='内容列表'):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('微博请求必须使用固定站内路径')
        self.check()
        try:
            result = self.page.evaluate("""async ({path, timeout}) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeout);
                try {
                    const response = await fetch(path, {credentials:'include',
                        headers:{'X-Requested-With':'XMLHttpRequest'}, signal:controller.signal});
                    if (!response.ok) return {status:response.status};
                    const text = await response.text();
                    if (text.length > 4000000) return {status:response.status, invalid:true};
                    try {return {status:response.status, payload:JSON.parse(text)}}
                    catch {return {status:response.status, invalid:true}}
                } catch {
                    return {transport_error:controller.signal.aborted ? 'timeout' : 'network'};
                } finally { clearTimeout(timer); }
            }""", {'path': path, 'timeout': self.timeout_ms()})
        except Exception:
            # Cancellation and the scan budget take precedence over a transport
            # error; never expose Playwright call logs or signed URLs to callers.
            self.check()
            raise WeiboAccessError(f'微博{label}网络请求中断，请稍后重试', 'network_error') from None
        self.check()
        transport_error = result.get('transport_error') if isinstance(result, dict) else None
        if transport_error:
            message = '请求超时' if transport_error == 'timeout' else '网络请求失败'
            raise WeiboAccessError(f'微博{label}{message}，请稍后重试', 'network_error')
        status = result.get('status') if isinstance(result, dict) else None
        if status in (401, 403, 432):
            raise WeiboAccessError(f'微博{label}受限（HTTP {status}），请使用“微博登录”完成登录或验证后重试', 'login_required')
        if status == 429:
            raise WeiboAccessError('微博请求被限流，请降低扫描频率并稍后重试', 'rate_limited')
        if status != 200 or result.get('invalid') or not isinstance(result.get('payload'), dict):
            raise WeiboAccessError(f'微博{label}未返回有效JSON（HTTP {status or "未知"}），请检查网络或平台验证')
        return result['payload']

    def profile(self, uid):
        query = urlencode({'type': 'uid', 'value': checked_uid(uid)})
        return parse_profile(self.request('/api/container/getIndex?' + query, '博主资料'), uid)

    def posts(self, container, page):
        if not re.fullmatch(r'\d{1,64}', str(container)) or not 1 <= int(page) <= 100:
            raise ValueError('微博列表分页参数无效')
        return self.request('/api/container/getIndex?' + urlencode({'containerid': container, 'page': page}))


def fetch_profile(uid, *, cancel=None, check=None):
    with BrowserSession(cancel=cancel, timeout=40, check=check) as client:
        client.open(uid)
        return client.profile(uid)
