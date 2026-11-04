"""Small, lossless Douyin post helpers shared by downloads and subscriptions."""
import re
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit


def is_douyin_url(value):
    try:
        parts = urlsplit(value)
        host = (parts.hostname or '').lower()
        return (parts.scheme in ('http', 'https') and not parts.username and not parts.password
                and parts.port in (None, 80, 443)
                and any(host == domain or host.endswith('.' + domain)
                        for domain in ('douyin.com', 'iesdouyin.com')))
    except (TypeError, ValueError):
        return False


def content_id(url):
    if not is_douyin_url(url):
        return None
    parts = urlsplit(url)
    match = re.search(r'/(?:video|note|slides)/([0-9]{1,30})(?:/|$)', parts.path)
    if match:
        return match.group(1)
    query = parse_qs(parts.query)
    for name in ('modal_id', 'vid', 'item_id'):
        value = (query.get(name) or [''])[0]
        if re.fullmatch(r'[0-9]{1,30}', value):
            return value
    return None


def is_album(post):
    return str(post.get('aweme_type')) == '68' or bool(
        isinstance(post.get('images'), list) and post['images'])


def source_url(post):
    identity = str(post.get('aweme_id') or '')
    if not re.fullmatch(r'[0-9]{1,30}', identity):
        raise ValueError('抖音作品 ID 无效')
    return f"https://www.douyin.com/{'note' if is_album(post) else 'video'}/{identity}"


def media_urls(value):
    """Keep platform order and every usable fallback; never accept local URLs."""
    urls = value.get('url_list') if isinstance(value, dict) else None
    if not isinstance(urls, list):
        return []
    result = []
    for url in urls:
        if not isinstance(url, str):
            continue
        try:
            parts = urlsplit(url)
            valid = (parts.scheme in ('https', 'http') and parts.hostname
                     and not parts.username and not parts.password)
        except ValueError:
            valid = False
        if valid and url not in result:
            result.append(url)
    return result


def _image_field(value):
    if isinstance(value, dict):
        value = value.get('url_list', value.get('urlList'))
    return media_urls({'url_list': value})


def image_source_urls(image):
    """Keep playback/original images ahead of explicitly watermarked downloads.

    Signed CDN URLs are not rewritten: changing a template can invalidate its
    signature. Keep fallback sources separate so the downloader can report them.
    """
    if not isinstance(image, dict):
        return [], []
    preferred, fallback = [], []
    for field in ('origin_image', 'originImage', 'display_image', 'displayImage', 'url_list', 'urlList'):
        for url in _image_field(image.get(field)):
            target = fallback if re.search(r'~tplv-[^/?:]*water', unquote(urlsplit(url).path), re.I) else preferred
            target.append(url)
    for field in ('download_url_list', 'downloadUrlList', 'owner_watermark_image', 'ownerWatermarkImage'):
        fallback.extend(_image_field(image.get(field)))
    preferred = list(dict.fromkeys(preferred))
    return preferred, [url for url in dict.fromkeys(fallback) if url not in preferred]


def clean_play_url(url):
    """Normalize only known public playback endpoints; preserve signed CDN URLs."""
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if (parts.scheme not in ('http', 'https') or parts.username or parts.password
            or not any(host == domain or host.endswith('.' + domain)
                       for domain in ('douyin.com', 'iesdouyin.com', 'snssdk.com'))
            or not re.fullmatch(r'/aweme/v1/play(?:wm)?/?', parts.path)):
        return url
    query = []
    for parameter in parts.query.split('&'):
        key = unquote(parameter.split('=', 1)[0]).lower()
        if key == 'logo_name':
            continue
        query.append('watermark=0' if key == 'watermark' else parameter)
    return urlunsplit(parts._replace(path=parts.path.replace('/playwm', '/play'), query='&'.join(query)))


def video_source_urls(video):
    """Prefer playable streams, never rank the watermarked download by filesize."""
    preferred, fallback = [], []
    addresses = [video.get('play_addr_h264'), video.get('play_addr')]
    for rate in video.get('bit_rate') or []:
        if isinstance(rate, dict) and not rate.get('is_h265'):
            addresses.append(rate.get('play_addr'))
    for address in addresses:
        for url in media_urls(address):
            cleaned = clean_play_url(url)
            preferred.append(cleaned)
            if cleaned != url:
                fallback.append(url)
    # A download address can be watermarked even when it is larger than playback.
    fallback.extend(media_urls(video.get('download_addr')))
    preferred = list(dict.fromkeys(preferred))
    return preferred, [url for url in dict.fromkeys(fallback) if url not in preferred]


def album_images(post):
    images = post.get('images')
    if not isinstance(images, list) or not images:
        raise ValueError('图集没有返回图片列表，未将空响应当作下载成功')
    result = []
    for index, item in enumerate(images, 1):
        preferred, fallback = image_source_urls(item)
        urls = preferred + fallback
        if not urls:
            raise ValueError(f'图集第 {index} 张图片缺少可用地址，未跳过此图片')
        result.append(urls)
    return result
