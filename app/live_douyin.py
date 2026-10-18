"""Bounded Douyin room lookup. Stream URLs and cookies never leave this module's caller.

Room endpoint and quality mappings informed by SplashedWhite/douyin-recorder
(MIT). Attribution: frontend/public/licenses/douyin-recorder-mit.txt.
"""
import json
import re
from urllib.parse import urlsplit

import requests

from . import douyin_auth

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
REFERER = 'https://live.douyin.com/'


class LiveError(ValueError):
    def __init__(self, message, *, limited=False):
        super().__init__(message)
        self.limited = limited


def room_url(value):
    value = str(value or '').strip()
    if not re.fullmatch(r'https://live\.douyin\.com/[0-9]{1,24}/?', value):
        raise ValueError('请输入完整直播间链接：https://live.douyin.com/房间号')
    return value.rstrip('/')


def stream_url(value):
    if not isinstance(value, str) or len(value) > 16384 or any(ord(c) < 33 for c in value):
        return ''
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        # Only platform CDN hosts; no user-provided/local addresses or file protocols.
        trusted = ('douyincdn.com', 'douyincdn.cn', 'douyinvod.com', 'bytecdn.cn', 'bytedance.com')
        if (parsed.scheme in ('http', 'https') and not parsed.username and not parsed.password
                and parsed.port in (None, 80, 443) and any(host == d or host.endswith('.' + d) for d in trusted)):
            return value
    except ValueError:
        pass
    return ''


def parse_room(payload):
    if not isinstance(payload, dict) or payload.get('status_code', 0) not in (0, '0'):
        raise LiveError('平台未返回有效直播状态，请检查登录态后重试')
    data = payload.get('data') or {}
    if not isinstance(data, dict):
        raise LiveError('直播响应格式已变化，未将其误判为下播')
    if data.get('message') == 'room has finished':
        return {'live': False}
    rooms = data.get('data')
    if not isinstance(rooms, list) or not rooms or not isinstance(rooms[0], dict):
        raise LiveError('未找到直播间状态，请确认房间链接与登录态')
    room = rooms[0]
    status = str(room.get('status', ''))
    if status in ('3', '4'):
        return {'live': False}
    if status != '2':
        raise LiveError('平台返回未知直播状态，稍后重试')
    owner = room.get('owner') or data.get('user') or {}
    streams = room.get('stream_url') or {}
    sdk = ((streams.get('live_core_sdk_data') or {}).get('pull_data') or {}).get('stream_data', {})
    if isinstance(sdk, str):
        try:
            sdk = json.loads(sdk)
        except ValueError:
            sdk = {}
    qualities = (('origin', 'ORIGIN'), ('uhd', 'FULL_HD1'), ('hd', 'HD1'), ('sd', 'SD2'), ('ld', 'SD1'))
    for sdk_key, legacy_key in qualities:
        main = ((sdk.get('data') or {}).get(sdk_key) or {}).get('main') or {}
        candidates = [main.get('flv'), main.get('hls'), (streams.get('flv_pull_url') or {}).get(legacy_key),
                      (streams.get('hls_pull_url_map') or {}).get(legacy_key)]
        for candidate in candidates:
            url = stream_url(candidate)
            if url:
                session_id = str(room.get('id_str') or room.get('id') or '')
                if not re.fullmatch(r'[0-9]{1,32}', session_id):
                    raise LiveError('直播场次标识缺失，暂不开始录制')
                return {'live': True, 'session_id': session_id, 'stream_url': url,
                        'title': str(room.get('title') or '抖音直播')[:500],
                        'nickname': str(owner.get('nickname') or '')[:200],
                        'owner_id': str(owner.get('sec_uid') or '')}
    raise LiveError('正在直播，但没有受支持的直播流地址；请稍后重试')


def _read_response(response):
    if response.status_code in (403, 429, 444):
        raise LiveError('平台限制了直播检查，已降低频率；可检查登录态后重试', limited=True)
    if response.status_code != 200:
        raise LiveError('直播检查请求未成功，稍后重试')
    result = bytearray()
    for chunk in response.iter_content(65536):
        result.extend(chunk)
        if len(result) > 2 * 1024 * 1024:
            raise LiveError('直播响应过大，已停止解析')
    return bytes(result)


def lookup(url):
    identity = room_url(url).rsplit('/', 1)[-1]
    try:
        with requests.Session() as session:
            session.headers.update({'User-Agent': UA, 'Referer': REFERER})
            for cookie in douyin_auth.load_cookies():
                domain = str(cookie.get('domain') or '').lstrip('.')
                name, value = cookie.get('name'), cookie.get('value')
                if (domain == 'douyin.com' or domain.endswith('.douyin.com')) and isinstance(name, str) and isinstance(value, str):
                    if not re.search(r'[\x00-\x20;]', name + value):
                        session.cookies.set(name, value, domain=cookie['domain'], path='/')
            # Acquire the public ttwid session cookie without persisting credentials.
            with session.get(REFERER, timeout=(5, 10), stream=True, allow_redirects=False) as response:
                if response.status_code in (403, 429, 444):
                    raise LiveError('平台限制了直播检查，已降低频率', limited=True)
            params = dict(aid='6383', app_name='douyin_web', live_id='1', device_platform='web',
                          language='zh-CN', enter_from='web_live', cookie_enabled='true',
                          browser_language='zh-CN', browser_platform='Win32', browser_name='Chrome',
                          browser_version='120', web_rid=identity)
            with session.get(REFERER + 'webcast/room/web/enter/', params=params, timeout=(5, 15),
                             stream=True, allow_redirects=False) as response:
                raw = _read_response(response)
            return parse_room(json.loads(raw))
    except LiveError:
        raise
    except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError):
        # Raw HTTP/JSON exceptions may contain signed URLs or cookies.
        raise LiveError('无法读取直播状态，请检查网络、房间链接和本地登录态') from None
