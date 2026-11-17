"""Eligibility shared by the profile scanner and single-post downloader."""
from urllib.parse import urlsplit

from .content_text import html_to_text

# Reserved downloader exit codes distinguish an intentional skip from an access
# or extraction failure, including when the downloader runs as a subprocess.
NO_PHOTOS, PLATFORM_ACTIVITY, NOT_AUTHOR = 20, 21, 22
REASONS = {
    NO_PHOTOS: '原帖没有配图或 Live 图',
    PLATFORM_ACTIVITY: '微博平台自动生成的动态',
    NOT_AUTHOR: '非博主原创内容',
}


class WeiboSkipped(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(REASONS[code])


def skip_reason(post, author_id=None):
    user = post['user'] if isinstance(post.get('user'), dict) else {}
    owner = str(user.get('idstr') or user.get('id') or '')
    if post.get('retweeted_status') is not None or (author_id and owner and owner != author_id):
        return NOT_AUTHOR
    if html_to_text(str(post.get('source') or '')).strip() == '会员特权专用':
        return PLATFORM_ACTIVITY
    # Some cards omit the source label but retain the official membership card.
    for link in post.get('url_struct') or ():
        if not isinstance(link, dict):
            continue
        try:
            url = urlsplit(str(link.get('long_url') or ''))
        except ValueError:
            continue
        if (url.hostname == 'new.vip.weibo.cn' and url.path.rstrip('/') == '/growthtask/home'
                and str(link.get('url_title') or '').startswith('今天我升级为VIP')):
            return PLATFORM_ACTIVITY
    if post.get('pic_ids') or post.get('pics') or post.get('pic_infos') or post.get('mix_media_info') or post.get('is_show_mixed'):
        return None
    # Missing fields in a profile summary are inconclusive. The downloader
    # checks the full detail before any archive is opened.
    if str(post.get('pic_num')) == '0':
        return NO_PHOTOS
    return None
