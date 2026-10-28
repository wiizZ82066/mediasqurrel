"""Explicit lightweight login and website checks; no downloads or profile writes."""
from concurrent.futures import ThreadPoolExecutor
import threading
from urllib.parse import urljoin, urlsplit

import requests

from . import douyin_auth

_LOCK = threading.Lock()
_SITES = (('douyin', '抖音', 'https://www.douyin.com/'),
          ('weibo', '微博', 'https://weibo.com/'))


def _allowed_url(url, platform):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        suffix = platform + '.com'
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and (host == suffix or host.endswith('.' + suffix)))
    except ValueError:
        return False


def _website(platform, label, url):
    result = {'id': platform + '_website', 'label': label + '网页访问',
              'status': 'failed', 'reason': '网页暂时无法连接', 'suggestion': '检查网络后重试'}
    try:
        with requests.Session() as session:
            # Do not obtain account credentials from .netrc or attach saved cookies.
            session.auth = lambda request: request
            for _ in range(4):
                with session.get(url, timeout=(5, 8), stream=True, allow_redirects=False,
                                 headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html'}) as response:
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        target = urljoin(url, response.headers.get('Location', ''))
                        if target == url or not _allowed_url(target, platform):
                            result.update(status='warning', reason='网页跳转需要手动确认', suggestion='在浏览器中打开平台网页检查')
                            return result
                        url = target
                        continue
                    if 200 <= status < 300:
                        result.update(status='passed', reason=f'网页可连接（HTTP {status}）', suggestion='')
                    elif status in (401, 403, 429):
                        result.update(status='warning', reason=f'平台要求验证或限制访问（HTTP {status}）', suggestion='在浏览器中完成验证，或稍后重试')
                    else:
                        result['reason'] = f'网页返回异常状态（HTTP {status}）'
                    return result
            result.update(status='warning', reason='网页跳转次数过多', suggestion='在浏览器中打开平台网页检查')
    except requests.Timeout:
        result['reason'] = '网页连接超时'
    except requests.RequestException:
        # Exception text can include proxy credentials or private host details.
        result['reason'] = '网页连接失败'
    return result


def check_sites():
    if not _LOCK.acquire(blocking=False):
        raise RuntimeError('检测正在进行，请稍候')
    try:
        logged_in = douyin_auth.auth_status()['logged_in']
        login = {'id': 'douyin_login', 'label': '抖音登录',
                 'status': 'passed' if logged_in else 'warning',
                 'reason': '已登录' if logged_in else '未登录',
                 'note': '根据本机保存且未过期的登录信息判断',
                 'suggestion': '' if logged_in else '在订阅页点击登录抖音'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            websites = list(pool.map(lambda item: _website(*item), _SITES))
        return {'checks': [login, *websites]}
    finally:
        _LOCK.release()
