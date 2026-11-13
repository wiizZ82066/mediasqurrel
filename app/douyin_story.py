"""Targeted daily content access using the user's existing Douyin session.

Only the explicit desktop story_25_filter selects this transport. Never scan
the following feed, accept a nearby post, or persist the surrounding response.
Protocol reference: Ivan494/douyin-live-recorder (MIT); see bundled attribution.
"""
from contextlib import contextmanager
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
_DETAIL_ENDPOINT = 'https://api.amemv.com/aweme/v1/aweme/detail/'
_REGISTER_ENDPOINT = 'https://api.amemv.com/service/2/device_register/'
_UA = ('com.ss.android.ugc.aweme/380700 (Linux; U; Android 15; zh_CN; '
       'SM-A5560; Build/AP3A.240905.015.A2)')
_LIMIT = 8 * 1024 * 1024
_COOKIE_NAMES = {'sessionid', 'sessionid_ss', 'sid_tt', 'uid_tt', 'uid_tt_ss', 'sid_guard'}


def _error(message, reason='invalid_response'):
    return PlatformAccessError(message, reason, platform='douyin')


def _session_header():
    cookies = load_cookies()
    if not has_login_cookie(cookies):
        raise _error('下载日常需要抖音登录，请先在设置页登录后重试', 'login_required')
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
        raise _error('抖音登录信息无效，请在设置页重新登录后重试', 'login_required')
    return '; '.join(f'{key}={value}' for key, value in values.items())


@contextmanager
def _device_lock():
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
        yield Path(config.DATA_DIR) / 'douyin_mobile_device.json'
    finally:
        lock.release()


def _read_device(path):
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(value, dict):
            registered = value.get('registered') is True
            pattern = r'[1-9][0-9]{5,29}' if registered else r'[0-9]{19}'
            if (all(re.fullmatch(pattern, str(value.get(key, '')))
                    for key in ('device_id', 'install_id')) and
                    str(uuid.UUID(value.get('cdid', ''))) == value['cdid']):
                result = {key: str(value[key]) for key in ('device_id', 'install_id', 'cdid')}
                if not registered:
                    return result
                if re.fullmatch(r'[0-9a-f]{16}', str(value.get('openudid', ''))):
                    return result | {'registered': True, 'openudid': value['openudid']}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    raise _error('本地日常读取配置损坏，请检查数据目录，未覆盖原配置', 'unavailable')


def _device():
    """Reuse registered clients; retain the existing legacy configuration format."""
    with _device_lock() as path:
        previous = _read_device(path)
        if previous:
            return previous
        value = {key: str(7200000000000000000 + secrets.randbelow(200000000000000000))
                 for key in ('device_id', 'install_id')}
        value['cdid'] = str(uuid.uuid4())
        atomic_write_text(path, json.dumps(value))
        return value


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
        'openudid': device.get('openudid') or hashlib.md5(device['device_id'].encode()).hexdigest()[:16],
        'ssmix': 'a', 'ts': str(int(now)), '_rticket': str(int(now * 1000)),
        'app_type': 'normal', 'to_uid': author, 'offset': offset, 'story_ttl': '7',
        'active_data_size': str(size), 'month_data_size': '4', 'insert_ids': '', 'delete_ids': '',
    }


def _request(params, cookie, timeout, *, endpoint=_ENDPOINT):
    query = urlencode(params)
    stamp = time.time()
    headers = {'User-Agent': _UA, 'Cookie': cookie, 'X-Khronos': str(int(stamp)),
               'X-Gorgon': get_xgorgon(query, stamp, cookie=cookie)}
    try:
        with requests.get(endpoint + '?' + query, headers=headers, stream=True,
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
        raise _error('抖音登录已失效或无法用于日常读取，请在设置页重新登录', 'login_required')
    if str(code) == '5':
        raise _error('抖音日常接口返回“参数不合法”（状态 5），已停止下载；此错误不能判定登录失效')
    if code not in (0, '0'):
        raise _error('抖音未允许读取日常，请检查账号可见性或平台验证', 'platform_error')
    return payload


def fetch_story_detail(identity, author_uid, *, timeout=30):
    """Prefer one registered-client item request; legacy lists remain bounded."""
    if not re.fullmatch(r'[0-9]{1,30}', str(identity or '')):
        raise _error('日常作品 ID 无效')
    if not re.fullmatch(r'[0-9]{1,30}', str(author_uid or '')):
        raise _error('此日常链接缺少作者信息，请粘贴手机抖音的完整分享文案或分享短链后重试', 'unsupported')
    cookie = _session_header()
    device = _device()
    if device.get('registered'):
        params = _params(device, str(author_uid), '0', 20)
        for key in ('to_uid', 'offset', 'story_ttl', 'active_data_size', 'month_data_size', 'insert_ids', 'delete_ids'):
            params.pop(key)
        params.update(aweme_id=str(identity), manifest_version_code='380700', is_guest_mode='0', minor_status='0')
        payload = _request(params, cookie, timeout, endpoint=_DETAIL_ENDPOINT)
        raw = payload.get('aweme_detail')
        if not isinstance(raw, dict) or str(raw.get('aweme_id')) != str(identity):
            raise _error('抖音没有返回这条日常详情；请确认内容仍可访问，未下载其他作品', 'content_unavailable')
        detail = detail_from_scripts([json.dumps({'aweme_detail': raw}, ensure_ascii=False)], str(identity))
        if not detail or str(detail['author'].get('uid')) != str(author_uid) or not detail.get('is_story'):
            raise _error('日常作品或作者信息不匹配，已停止下载')
        return detail
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


def register_device(*, timeout=25):
    """Explicit one-time setup, never called by a download or login operation.

    Sends generated client identifiers and Android parameters, without cookies.
    Keep an attempt marker BEFORE the POST: a timeout may still have registered
    a server device, so neither the downloader nor another setup may retry it.
    """
    with _device_lock() as path:
        previous = _read_device(path)
        if previous and previous.get('registered'):
            return previous
        attempt = path.with_name('douyin_mobile_registration.json')
        if attempt.exists():
            raise _error('已有移动客户端登记记录；请先核查上次结果，未重复登记', 'unavailable')
        device = previous or {'device_id': '0', 'install_id': '0', 'cdid': str(uuid.uuid4())}
        params = _params(device, '', '0', 20)
        for key in ('to_uid', 'offset', 'story_ttl', 'active_data_size', 'month_data_size',
                    'insert_ids', 'delete_ids', 'device_id', 'iid', 'install_id'):
            params.pop(key)
        if not previous:
            params['openudid'] = secrets.token_hex(8)
        params.update(manifest_version_code='380700', cpu_support64='true')
        client = {
            'display_name': '抖音', 'aid': 1128, 'channel': 'channel_aweme', 'package': 'com.ss.android.ugc.aweme',
            'app_version': '38.7.0', 'version_code': 380700, 'manifest_version_code': 380700,
            'update_version_code': 380700, 'sdk_version': '3.9.5', 'sdk_target_version': 29,
            'os': 'Android', 'os_version': '15', 'os_api': 35, 'device_model': 'SM-A5560',
            'device_brand': 'samsung', 'device_manufacturer': 'samsung', 'cpu_abi': 'arm64-v8a',
            'release_build': 'AP3A.240905.015.A2', 'density_dpi': 420, 'display_density': 'xhdpi',
            'resolution': '1080x2400', 'language': 'zh', 'timezone': 8, 'region': 'CN',
            'tz_name': 'Asia/Shanghai', 'cdid': device['cdid'], 'openudid': params['openudid'],
            'clientudid': str(uuid.uuid4()), 'google_aid': '', 'req_id': str(uuid.uuid4()),
        }
        stamp = time.time()
        body = json.dumps({'magic_tag': 'ss_app_log', 'header': client, '_gen_time': int(stamp)},
                          ensure_ascii=False, separators=(',', ':'))
        query = urlencode(params)
        headers = {'User-Agent': _UA, 'Content-Type': 'application/json; charset=utf-8', 'sdk-version': '2',
                   'X-Khronos': str(int(stamp)), 'X-Gorgon': get_xgorgon(query, stamp, data=body),
                   'X-SS-STUB': hashlib.md5(body.encode()).hexdigest().upper()}
        atomic_write_text(attempt, json.dumps({'status': 'started', 'started_at': int(stamp)}))
        try:
            with requests.post(_REGISTER_ENDPOINT + '?' + query, data=body.encode(), headers=headers,
                               stream=True, timeout=(min(10, timeout), timeout), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise _error('移动客户端登记未成功；请先核查结果，未自动重试', 'platform_error')
                raw = bytearray()
                for chunk in response.iter_content(16384):
                    raw.extend(chunk)
                    if len(raw) > 524288:
                        raise _error('移动客户端登记响应超过上限；未自动重试')
                payload = json.loads(raw)
        except requests.RequestException:
            raise _error('移动客户端登记结果未确认；请先核查网络和登记记录，未自动重试', 'network_error') from None
        except (ValueError, RecursionError) as error:
            if isinstance(error, PlatformAccessError):
                raise
            raise _error('移动客户端登记未返回有效数据；未自动重试') from None
        if not isinstance(payload, dict):
            raise _error('移动客户端登记响应格式无效；未自动重试')
        registered = {
            'device_id': str(payload.get('device_id_str') or payload.get('device_id') or ''),
            'install_id': str(payload.get('install_id_str') or payload.get('install_id') or payload.get('iid') or ''),
            'cdid': device['cdid'], 'openudid': params['openudid'], 'registered': True,
        }
        if not all(re.fullmatch(r'[1-9][0-9]{5,29}', registered[key]) for key in ('device_id', 'install_id')):
            raise _error('移动客户端登记没有返回有效标识；原配置已保留，未自动重试')
        atomic_write_text(path, json.dumps(registered))
        atomic_write_text(attempt, json.dumps({'status': 'complete', 'started_at': int(stamp)}))
        return registered


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='显式登记日常下载客户端；会向抖音发送生成的设备标识与 Android 参数')
    parser.add_argument('--register-device', action='store_true', required=True,
                        help='仅登记一次；已有有效登记则复用，不发送 Cookie')
    parser.parse_args()
    try:
        register_device()
    except PlatformAccessError as error:
        parser.exit(1, str(error) + '\n')
    print('日常下载客户端已就绪；后续下载复用此登记。')
