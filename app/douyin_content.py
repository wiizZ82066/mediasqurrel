"""Small, lossless Douyin post helpers shared by downloads and subscriptions."""
import re
from urllib.parse import parse_qs, urlsplit


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


def album_images(post):
    images = post.get('images')
    if not isinstance(images, list) or not images:
        raise ValueError('图集没有返回图片列表，未将空响应当作下载成功')
    result = []
    for index, item in enumerate(images, 1):
        urls = media_urls(item)
        if not urls:
            raise ValueError(f'图集第 {index} 张图片缺少可用地址，未跳过此图片')
        result.append(urls)
    return result
