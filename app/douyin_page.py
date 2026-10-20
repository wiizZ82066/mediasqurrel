"""Read public, embedded post data without evaluating page scripts.

Some Douyin note pages deliver their detail in streamed page data and never
make the detail XHR. Only the requested post is normalized; no account/session
fields from the surrounding page are returned or persisted.
"""
import json
import re
from urllib.parse import unquote

from .douyin_content import media_urls
from .platform_errors import PlatformAccessError

_LIMIT = 8 * 1024 * 1024
_FLIGHT = 'self.__pace_f.push('


def _urls(value):
    if isinstance(value, str):
        value = [value]
    if isinstance(value, dict):
        value = value.get('url_list') or value.get('urlList')
    return media_urls({'url_list': value})


def _normalize(value, identity):
    if str(value.get('aweme_id') or value.get('awemeId') or '') != identity:
        return None
    author = value.get('author') or value.get('authorInfo')
    if not isinstance(author, dict) or not isinstance(author.get('nickname'), str):
        return None
    status = value.get('status') or {}
    if value.get('pcNeedLogin') is True:
        raise PlatformAccessError('此作品需要登录后查看，请先完成抖音登录', 'login_required', platform='douyin')
    if (value.get('isPrivate') is True or value.get('isFriendLimit') is True or
            isinstance(status, dict) and any(status.get(key) is True for key in ('isDelete', 'isProhibited', 'isReviewing'))):
        raise PlatformAccessError('此作品当前不可公开读取，已停止下载', 'platform_error', platform='douyin')
    result = {'aweme_id': identity, 'aweme_type': value.get('aweme_type', value.get('awemeType')),
              'author': {'nickname': author['nickname'], 'uid': author.get('uid', ''),
                         'sec_uid': author.get('sec_uid', author.get('secUid', ''))},
              'desc': value.get('desc'), 'create_time': value.get('create_time', value.get('createTime'))}
    images = value.get('images')
    if isinstance(images, list):
        result['images'] = []
        for image in images:
            # Preserve empty slots; the downloader must report incomplete data.
            if not isinstance(image, dict):
                result['images'].append({})
                continue
            urls = _urls(image.get('download_url_list', image.get('downloadUrlList')))
            urls += _urls(image.get('url_list', image.get('urlList')))
            result['images'].append({'url_list': list(dict.fromkeys(urls))})
    video = value.get('video')
    if isinstance(video, dict):
        result['video'] = {
            'play_addr_h264': {'url_list': _urls(video.get('play_addr_h264'))},
            'play_addr': {'url_list': _urls(video.get('play_addr', video.get('playAddr')))},
            'origin_cover': {'url_list': _urls(video.get('origin_cover', video.get('originCoverUrlList')))},
            'cover': {'url_list': _urls(video.get('coverUrlList', video.get('cover')))}}
    if not isinstance(result['desc'], str) or not ('video' in result or 'images' in result):
        return None
    return result


def detail_from_scripts(scripts, identity):
    return find_script_data(scripts, lambda item: _normalize(item, identity)) if identity else None


def find_script_data(scripts, normalize):
    """Parse bounded JSON/Flight records; never eval arbitrary script content."""
    if not isinstance(scripts, list):
        return None
    used = visited = 0
    decoder = json.JSONDecoder()
    for text in scripts:
        if not isinstance(text, str):
            continue
        used += len(text)
        if used > _LIMIT:
            raise PlatformAccessError('抖音页面数据过大，已停止解析', 'invalid_response', platform='douyin')
        candidates = []
        text = text.strip()
        try:
            if text.startswith(_FLIGHT):
                frame, end = decoder.raw_decode(text[len(_FLIGHT):])
                tail = text[len(_FLIGHT) + end:].strip()
                if tail not in (')', ');') or not isinstance(frame, list) or len(frame) != 2 or frame[0] != 1 or not isinstance(frame[1], str):
                    continue
                candidates = [re.sub(r'^[0-9a-f]+:', '', line) for line in frame[1].splitlines()]
            else:
                candidates = [unquote(text) if text.startswith('%7B') or text.startswith('%5B') else text]
        except (ValueError, RecursionError):
            continue
        for candidate in candidates:
            if not candidate.startswith(('{', '[')):
                continue
            try:
                parsed = json.loads(candidate)
            except (ValueError, RecursionError):
                continue
            pending = [(parsed, 0)]
            while pending:
                item, depth = pending.pop()
                visited += 1
                if visited > 100000:
                    raise PlatformAccessError('抖音页面结构过于复杂，已停止解析', 'invalid_response', platform='douyin')
                if depth > 24:
                    continue  # Deep unrelated app configuration is not post data.
                if isinstance(item, dict):
                    found = normalize(item)
                    if found is not None:
                        return found
                    pending.extend((value, depth + 1) for value in reversed(list(item.values())) if isinstance(value, (dict, list)))
                elif isinstance(item, list):
                    pending.extend((value, depth + 1) for value in reversed(item) if isinstance(value, (dict, list)))
    return None
