"""Weibo profile/list requests inside an ephemeral browser visitor context.

No browser or user data is opened on import. Raw response text and signed avatar
URLs never become error messages; the caller decides when profile data persists.
"""
import html
import json
import re
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .browser import sync_playwright
from .platform_errors import PlatformAccessError

MOBILE_UA = ('Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36')
DESKTOP_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


class WeiboAccessError(PlatformAccessError):
    def __init__(self, message, reason='platform_error', *, http_status=None):
        super().__init__(message, reason, platform='weibo')
        self.http_status = http_status


def check_access_status(code, label, *, source='HTTP'):
    """Only an explicit authentication failure calls for another login."""
    status = code if source == 'HTTP' else None
    if code == 401:
        raise WeiboAccessError(f'微博{label}需要登录（{source} {code}），请在设置中完成微博登录。',
                               'login_required', http_status=status)
    if code in (403, 432):
        raise WeiboAccessError(f'微博拒绝访问{label}（{source} {code}），请稍后再试。', 'platform_limited', http_status=status)
    if code == 429:
        raise WeiboAccessError(f'微博{label}请求被限流（{source} {code}），请稍后再试。', 'rate_limited', http_status=status)
    if source == 'HTTP' and isinstance(code, int) and code >= 400:
        raise WeiboAccessError(f'微博{label}请求失败（HTTP {code}），请稍后再试。', http_status=code)


def clean_text(value, limit=200):
    text = html.unescape(re.sub(r'<[^>]*>', '', str(value or '')))
    return re.sub(r'\s+', ' ', text).strip()[:limit]


def checked_uid(value):
    uid = str(value or '').strip()
    if not re.fullmatch(r'\d{1,20}', uid):
        raise ValueError('微博订阅需要数字博主ID')
    return uid


def check_api_access(payload, label):
    if not isinstance(payload, dict):
        return
    if payload.get('ok') in (-100, '-100') or payload.get('errno') in (100001, '100001', 100005, '100005'):
        raise WeiboAccessError(f'微博{label}要求登录，请在设置中完成微博登录。', 'login_required')
    if payload.get('ok') not in (1, '1'):
        code = str(payload.get('errno'))
        if code in ('401', '403', '432', '429'):
            check_access_status(int(code), label, source='平台错误码')


def checked_payload(payload, label='内容列表'):
    if not isinstance(payload, dict):
        raise WeiboAccessError(f'微博{label}响应格式无效；请检查平台验证')
    check_api_access(payload, label)
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


class _ProfileRequest:
    """Observe the mobile page's own profile request, scoped to one visit and account."""
    def __init__(self, page, uid):
        self.page, self.uid = page, uid
        self.request = None
        self.finished = False
        self.events = (('request', self._started), ('requestfinished', self._ended), ('requestfailed', self._ended))
        for name, callback in self.events:
            page.on(name, callback)

    def _started(self, request):
        if self.request is not None or request.method != 'GET':
            return
        url = urlsplit(request.url)
        if (url.scheme != 'https' or url.netloc not in ('m.weibo.cn', 'm.weibo.cn:443')
                or url.path != '/api/container/getIndex'):
            return
        query = parse_qs(url.query)
        if (query.get('value') == [self.uid] and query.get('type') == ['uid']
                and 'page' not in query and 'since_id' not in query
                and query.get('containerid', ['']) in ([''], ['100505' + self.uid])):
            self.request = request

    def _ended(self, request):
        if request == self.request:
            self.finished = True

    def close(self):
        for name, callback in self.events:
            self.page.remove_listener(name, callback)

    def read(self, client):
        # Older pages may never start a profile request. Allow their normal
        # explicit request once. An observed request owns its result, even on denial.
        for _ in range(8):
            if self.request is not None:
                break
            self.page.wait_for_timeout(client.timeout_ms(125))
        if self.request is None:
            return None
        deadline = time.monotonic() + client.timeout_ms() / 1000
        while not self.finished:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WeiboAccessError('微博博主资料请求超时，请稍后重试。', 'network_error')
            self.page.wait_for_timeout(client.timeout_ms(max(1, min(125, int(remaining * 1000)))))
        client.check()
        response = self.request.response()
        if response is None:
            raise WeiboAccessError('微博博主资料网络请求中断，请稍后重试。', 'network_error')
        check_access_status(response.status, '博主资料')
        if response.status != 200:
            raise WeiboAccessError(f'微博博主资料请求失败（HTTP {response.status}），请稍后再试。')
        length = response.header_value('content-length') or ''
        if length.isdecimal() and int(length) > 4_000_000:
            raise WeiboAccessError('微博博主资料响应过大。')
        body = response.body()
        if len(body) > 4_000_000:
            raise WeiboAccessError('微博博主资料响应过大。')
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            raise WeiboAccessError('微博博主资料未返回有效 JSON，请稍后再试。') from None
        return parse_profile(payload, self.uid)


class BrowserSession:
    def __init__(self, *, cancel=None, timeout=45, headless=True, mobile=True, check=None, use_saved_cookies=True):
        self.cancel = cancel or threading.Event()
        self.deadline = time.monotonic() + max(1, timeout)
        self.headless, self.mobile, self.external_check = headless, mobile, check
        self.use_saved_cookies = use_saved_cookies
        self.manager = self.playwright = self.browser = self.context = self.page = None
        self._profile_request = None

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
            self._profile_request = None
            if self.manager and self.playwright:
                self.manager.__exit__(None, None, None)

    def open(self, uid=None):
        host = 'm.weibo.cn' if self.mobile else 'weibo.com'
        uid = checked_uid(uid) if uid else None
        suffix = f'/u/{uid}' if uid else '/'
        if self._profile_request:
            self._profile_request.close()
        self._profile_request = _ProfileRequest(self.page, uid) if uid and self.mobile else None
        try:
            response = self.page.goto(f'https://{host}{suffix}', wait_until='domcontentloaded', timeout=self.timeout_ms(10000))
        except Exception as error:
            self.check()
            from playwright.sync_api import TimeoutError as NavigationTimeout
            if not isinstance(error, NavigationTimeout):
                raise WeiboAccessError('微博页面网络连接中断，请检查网络后重试', 'network_error') from None
            # A slow DOMContentLoaded does not prove the API is unavailable.
            # Continue within the original budget; request() still validates
            # every response, and the origin gate below forbids off-site fetches.
        else:
            if response is not None:
                check_access_status(response.status, '博主主页' if uid else '首页')
        # Visitor cookies are issued during an automatic cross-domain redirect.
        # Issuing API requests before returning to Weibo can yield HTTP 432/403.
        while (urlsplit(self.page.url).scheme, urlsplit(self.page.url).hostname) != ('https', host):
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
        check_access_status(status, label)
        if status != 200 or result.get('invalid') or not isinstance(result.get('payload'), dict):
            raise WeiboAccessError(f'微博{label}未返回有效JSON（HTTP {status or "未知"}），请检查网络或平台验证')
        return result['payload']

    def profile(self, uid):
        uid = checked_uid(uid)
        if self._profile_request and self._profile_request.uid == uid:
            try:
                observed = self._profile_request.read(self)
            except (WeiboAccessError, TimeoutError):
                raise
            except Exception:
                self.check()
                raise WeiboAccessError('微博博主资料响应读取中断，请稍后重试。', 'network_error') from None
            finally:
                self._profile_request.close()
                self._profile_request = None
            if observed is not None:
                return observed
        query = urlencode({'type': 'uid', 'value': uid})
        return parse_profile(self.request('/api/container/getIndex?' + query, '博主资料'), uid)

    def posts(self, container, page):
        if not re.fullmatch(r'\d{1,64}', str(container)) or not 1 <= int(page) <= 100:
            raise ValueError('微博列表分页参数无效')
        return self.request('/api/container/getIndex?' + urlencode({'containerid': container, 'page': page}))


def fetch_profile(uid, *, cancel=None, check=None):
    with BrowserSession(cancel=cancel, timeout=40, check=check) as client:
        client.open(uid)
        return client.profile(uid)
