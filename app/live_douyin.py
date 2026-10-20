"""Bounded Douyin room lookup. Stream URLs and cookies never leave this module's caller.

Room endpoint and quality mappings informed by SplashedWhite/douyin-recorder
(MIT). Attribution: frontend/public/licenses/douyin-recorder-mit.txt.
"""
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from . import douyin_auth

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
REFERER = 'https://live.douyin.com/'


def broadcast_time(room):
    """Only a platform timestamp is a broadcast start; never substitute record time."""
    value = room.get('create_time') or room.get('start_time')
    try:
        value = float(value)
        if value > 1e12:
            value /= 1000
        now = datetime.now(timezone.utc).timestamp()
        if 946684800 <= value <= now + 300:
            return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds')
    except (ValueError, TypeError, OverflowError):
        pass
    return ''


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


def parse_room(payload, quality='best'):
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
    if quality != 'best':
        names = [item[0] for item in qualities]
        if quality not in names:
            raise LiveError('不支持的录制画质')
        qualities = qualities[names.index(quality):]
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
                return {'live': True, 'session_id': session_id, 'stream_url': url, 'quality':sdk_key,
                        'broadcast_started_at': broadcast_time(room),
                        'title': str(room.get('title') or '抖音直播')[:500],
                        'nickname': str(owner.get('nickname') or '')[:200],
                        'owner_id': str(owner.get('sec_uid') or '')}
    raise LiveError('正在直播，但所选或更低画质没有可用直播流；可改选最高可用或稍后重试')


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


def lookup(url, quality='best'):
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
            return parse_room(json.loads(raw), quality)
    except LiveError:
        raise
    except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError):
        # Raw HTTP/JSON exceptions may contain signed URLs or cookies.
        raise LiveError('无法读取直播状态，请检查网络、房间链接和本地登录态') from None


def parse_profile(payload, identity):
    if not isinstance(payload, dict) or payload.get('status_code') != 0:
        raise LiveError('无法读取博主直播状态，请检查抖音登录态或稍后重试')
    user = payload.get('user') or {}
    if user.get('sec_uid') != identity:
        raise LiveError('主页身份无法核实，未开始录制')
    status = user.get('live_status')
    if status == 0:
        return None
    if status != 1:
        raise LiveError('博主直播状态未知，未将其误判为下播')
    room = user.get('room_data') or {}
    if isinstance(room, str):
        try:
            room = json.loads(room)
        except ValueError:
            raise LiveError('博主直播信息格式已变化') from None
    rid = (room.get('owner') or {}).get('web_rid') if isinstance(room, dict) else None
    if not rid:
        raise LiveError('博主已开播，但主页未提供直播入口；可改用直播间链接')
    return room_url(f'https://live.douyin.com/{rid}')


def profile_from_scripts(scripts, identity):
    from .douyin_page import find_script_data
    def normalize(value):
        if value.get('secUid', value.get('sec_uid')) != identity:
            return None
        status = value.get('liveStatus', value.get('live_status'))
        room = value.get('roomData', value.get('room_data'))
        if status is None and isinstance(room, dict):
            if room.get('status') == 2:
                status = 1
            elif room.get('status') in (3,4) or (not room and str(value.get('roomIdStr', value.get('roomId'))) == '0'):
                status = 0
        if status is None:
            return None  # An author in a recommended post is not a profile.
        user = {'sec_uid': identity, 'live_status': status,
                'room_data': room}
        return {'room_url': parse_profile({'status_code': 0, 'user': user}, identity)}
    return find_script_data(scripts, normalize)


def lookup_blogger(identity, quality='best'):
    """Read the normal profile response; never search by an ambiguous nickname."""
    from . import browser
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', str(identity)):
        raise LiveError('订阅博主身份格式无效')
    try:
        with browser.sync_playwright() as p:
            try:
                owned = p.chromium.launch(channel='chrome', headless=True, timeout=10000)
            except Exception:
                owned = p.chromium.launch(headless=True, timeout=10000)
            try:
                context = browser.stealth_context(owned)
                douyin_auth.attach_cookies(context)
                page = context.new_page()
                page.route('**/*', lambda route: route.abort() if route.request.resource_type in
                           ('media', 'image', 'font') else route.continue_())
                responses = []
                def response(value):
                    parsed = urlsplit(value.url)
                    if (parsed.hostname == 'www.douyin.com' and
                            parsed.path.rstrip('/') == '/aweme/v1/web/user/profile/other' and len(responses) < 8):
                        responses.append(value)
                page.on('response', response)
                deadline = time.monotonic() + 30
                try:
                    page.goto(f'https://www.douyin.com/user/{identity}', wait_until='domcontentloaded', timeout=20000)
                except Exception as error:
                    if type(error).__name__ != 'TimeoutError':
                        raise
                found = None
                while time.monotonic() < deadline:
                    if responses:
                        value = responses.pop(0)
                        if value.status in (403, 429, 444):
                            raise LiveError('平台限制了主页检查，已降低频率', limited=True)
                        raw = value.body()
                        if value.status == 200 and len(raw) <= 2*1024*1024:
                            payload = json.loads(raw)
                            if (payload.get('user') or {}).get('sec_uid') == identity:
                                found = {'room_url': parse_profile(payload, identity)}
                    if found is None:
                        scripts = page.locator('script').evaluate_all(
                            '(els)=>els.map(e=>e.textContent).filter(t=>t && (t.startsWith("self.__pace_f.push(") || t.startsWith("%7B") || t.startsWith("{")))')
                        found = profile_from_scripts(scripts, identity)
                    if found is not None:
                        break
                    page.wait_for_timeout(500)
                if found is None:
                    raise LiveError('主页未返回可核实的直播状态，请检查登录或平台验证后重试')
                url = found['room_url']
            finally:
                owned.close()
        if not url:
            return {'live': False}
        info = lookup(url, quality)
        info['room_url'] = url
        return info
    except LiveError:
        raise
    except Exception:
        raise LiveError('博主主页直播检查失败，请检查登录态、网络或改用直播间链接') from None
