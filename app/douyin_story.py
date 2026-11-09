"""Targeted daily-video access using the user's existing Douyin session.

Only the explicit desktop story_25_filter selects this transport. Never scan
the following feed, accept a nearby post, or persist the surrounding response.
Protocol reference: Ivan494/douyin-live-recorder (MIT); see bundled attribution.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import time
import uuid
from urllib.parse import urlencode

import requests

from . import config
from .archive import atomic_write_text
from .douyin_auth import has_login_cookie, load_cookies
from .douyin_mobile_sign import get_xgorgon
from .douyin_page import detail_from_scripts
from .platform_errors import PlatformAccessError
from .runtime import InstanceBusyError, InstanceLock

_ENDPOINT = 'https://aweme.snssdk.com/aweme/v1/story/profile/list/'
_UA = ('com.ss.android.ugc.aweme/380700 (Linux; U; Android 15; zh_CN; '
       'SM-A5560; Build/AP3A.240905.015.A2)')
_LIMIT = 8 * 1024 * 1024
_COOKIE_NAMES = {'sessionid', 'sessionid_ss', 'sid_tt', 'uid_tt', 'uid_tt_ss', 'sid_guard'}


def _error(message, reason='invalid_response'):
    return PlatformAccessError(message, reason, platform='douyin')


def _session_header():
    cookies = load_cookies()
    if not has_login_cookie(cookies):
        raise _error('下载日常需要抖音登录，请先在设置中登录后重试', 'login_required')
    values = {}
    for cookie in cookies:
        if not isinstance(cookie, dict) or cookie.get('name') not in _COOKIE_NAMES:
            continue
        domain = str(cookie.get('domain') or '').lower().lstrip('.')
        if domain != 'douyin.com' and not domain.endswith('.douyin.com'):
            continue
        try:
            expiry = float(cookie.get('expires', -1))
        except (TypeError, ValueError):
            continue
        value = cookie.get('value')
        if (not math.isfinite(expiry) or 0 <= expiry <= time.time() or
                not isinstance(value, str) or not value or re.search(r'[\s;,\r\n]', value)):
            continue
        values.setdefault(cookie['name'], value)
    if 'sessionid' not in values:
        raise _error('抖音登录信息无效，请在设置中重新登录后重试', 'login_required')
    return '; '.join(f'{key}={value}' for key, value in values.items())


def _device():
    """Reuse a local protocol identity; no account/token is written here."""
    path = Path(config.DATA_DIR) / 'douyin_mobile_device.json'
    lock = InstanceLock(config.DATA_DIR, filename='.douyin-mobile.lock')
    deadline = time.monotonic() + 3
    try:
        while True:
            try:
                lock.acquire()
                break
            except InstanceBusyError:
                if time.monotonic() >= deadline:
                    raise _error('日常读取正在初始化，请稍后重试', 'unavailable') from None
                time.sleep(.05)
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                if (isinstance(value, dict) and
                        all(re.fullmatch(r'[0-9]{19}', str(value.get(key, '')))
                            for key in ('device_id', 'install_id')) and
                        str(uuid.UUID(value.get('cdid', ''))) == value['cdid']):
                    return {key: str(value[key]) for key in ('device_id', 'install_id', 'cdid')}
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            raise _error('本地日常读取配置损坏，请检查数据目录，未覆盖原配置', 'unavailable')
        value = {key: str(7200000000000000000 + secrets.randbelow(200000000000000000))
                 for key in ('device_id', 'install_id')}
        value['cdid'] = str(uuid.uuid4())
        atomic_write_text(path, json.dumps(value))
        return value
    finally:
        lock.release()


def _params(device, author, offset, size):
    now = time.time()
    return {
        'os': 'android', 'os_api': '35', 'os_version': '15',
        'device_platform': 'android', 'device_type': 'SM-A5560', 'device_brand': 'samsung',
        'host_abi': 'arm64-v8a', 'resolution': '1080*2400', 'dpi': '420',
        'language': 'zh', 'region': 'CN', 'sys_region': 'CN', 'locale': 'zh_CN',
        'mcc_mnc': '46000', 'carrier_region': 'CN', 'timezone_name': 'Asia/Shanghai',
        'timezone_offset': '28800', 'ac': 'wifi', 'channel': 'channel_aweme',
        'aid': '1128', 'app_name': 'aweme', 'version_code': '380700',
        'version_name': '38.7.0', 'update_version_code': '380700',
        'device_id': device['device_id'], 'iid': device['install_id'],
        'install_id': device['install_id'], 'cdid': device['cdid'],
        'openudid': hashlib.md5(device['device_id'].encode()).hexdigest()[:16],
        'ssmix': 'a', 'ts': str(int(now)), '_rticket': str(int(now * 1000)),
        'app_type': 'normal', 'to_uid': author, 'offset': offset, 'story_ttl': '7',
        'active_data_size': str(size), 'month_data_size': '4', 'insert_ids': '', 'delete_ids': '',
    }


def _request(params, cookie, timeout):
    query = urlencode(params)
    stamp = time.time()
    headers = {'User-Agent': _UA, 'Cookie': cookie, 'X-Khronos': str(int(stamp)),
               'X-Gorgon': get_xgorgon(query, stamp, cookie=cookie)}
    try:
        with requests.get(_ENDPOINT + '?' + query, headers=headers, stream=True,
                          timeout=(min(10, timeout), timeout), allow_redirects=False) as response:
            status = response.status_code
            if status in (401, 403, 429):
                reason = {401: 'login_required', 403: 'risk_control', 429: 'rate_limited'}[status]
                raise _error('抖音拒绝读取日常，请检查登录、平台验证或限流状态后重试', reason)
            if status != 200:
                raise _error('抖音日常接口暂不可用，已停止读取，请稍后重试', 'platform_error')
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > _LIMIT:
                    raise _error('日常响应超过读取上限，已停止解析')
            payload = json.loads(body)
    except requests.Timeout:
        raise _error('读取抖音日常超时，请稍后重试', 'timeout') from None
    except requests.RequestException:
        raise _error('读取抖音日常时网络连接失败，请稍后重试', 'network_error') from None
    except (ValueError, RecursionError) as error:
        if isinstance(error, PlatformAccessError):
            raise
        raise _error('抖音日常接口未返回有效数据，请检查登录或平台验证') from None
    if not isinstance(payload, dict):
        raise _error('抖音日常响应格式无效')
    code = payload.get('status_code')
    if str(code) in ('8', '12', '2483'):
        raise _error('抖音登录已失效或无法用于日常读取，请在设置中重新登录', 'login_required')
    if str(code) == '5':
        raise _error('抖音日常接口返回参数错误（状态 5），已停止下载；可尝试在设置中重新登录后重试')
    if code not in (0, '0'):
        raise _error('抖音未允许读取日常，请检查账号可见性或平台验证', 'platform_error')
    return payload


def fetch_story_detail(identity, author_uid, *, timeout=30):
    """Read at most three author pages; only a matching ID AND author may save."""
    if not re.fullmatch(r'[0-9]{1,30}', str(identity or '')):
        raise _error('日常作品 ID 无效')
    if not re.fullmatch(r'[0-9]{1,30}', str(author_uid or '')):
        raise _error('此日常链接缺少作者信息，请粘贴手机抖音的完整分享文案或分享短链后重试', 'unsupported')
    cookie = _session_header()
    device = _device()
    offset, size, visited = '0', 20, set()
    for _ in range(3):
        visited.add((offset, size))
        payload = _request(_params(device, str(author_uid), offset, size), cookie, timeout)
        detail = detail_from_scripts([json.dumps(payload, ensure_ascii=False)], str(identity))
        if detail:
            if str(detail['author'].get('uid')) != str(author_uid) or not detail.get('is_story'):
                raise _error('日常作品或作者信息不匹配，已停止下载')
            return detail
        active = payload.get('active_data') or {}
        if isinstance(active, dict) and active.get('has_more') in (True, 1, '1') and size < 80:
            size *= 2
        elif payload.get('has_more') in (True, 1, '1'):
            following = str(payload.get('next_offset') or '')
            if not re.fullmatch(r'[0-9]{1,30}', following) or (following, size) in visited:
                raise _error('日常列表未能继续加载，未将其他作品当作目标下载')
            offset = following
        else:
            raise _error('当前登录账号未返回这条日常；请确认手机 App 中仍可播放，并检查是否为同一账号', 'content_unavailable')
    raise _error('在本次日常读取范围内未找到目标作品，已停止；请重新复制分享短链后重试', 'coverage_limited')
