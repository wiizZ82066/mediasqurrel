"""Weibo mobile list collection with bounded browser pagination."""
import re
from .. import watcher
from .common import collect_browser, published_at, run_sync_scan

MOBILE_UA = ('Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36')

def _strip_html(text):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', str(text or ''))).strip()

def parse_page(payload, uid):
    if not isinstance(payload, dict) or payload.get('ok') != 1:
        raise ValueError('微博列表未成功返回；请检查登录、平台验证或限流状态')
    data = payload.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('cards'), list):
        raise ValueError('微博列表结构发生变化，未将响应当作空列表')
    cards = []
    for card in data['cards']:
        cards.extend(card.get('card_group') or [card])
    items = []
    for card in cards:
        post = card.get('mblog') or {}
        bid = post.get('bid')
        if not bid or post.get('retweeted_status'):
            continue
        title = card.get('title') or {}
        pinned = bool(card.get('is_top') or post.get('isTop') or post.get('is_top') or
                      '置顶' in str(title.get('text', '') if isinstance(title, dict) else title))
        items.append({'item_id': str(bid), 'script_id': 'weibo',
                      'params': {'url': f'https://weibo.com/{uid}/{bid}'},
                      'title': _strip_html(post.get('text'))[:60] or '（无正文）',
                      'created_at': published_at(post.get('created_at')), 'pinned': pinned,
                      '_avatar_source': str((post.get('user') or {}).get('avatar_hd') or
                                            (post.get('user') or {}).get('profile_image_url') or '').replace('http://', 'https://')})
    more = data.get('has_more', payload.get('has_more'))
    if more is not None:
        more = bool(more)
    elif not data['cards']:
        more = False
    return items, more

def _scan_sync(sub):
    uid = str(sub['blogger_id']).strip()
    if not uid.isdigit():
        raise ValueError('微博订阅需要数字博主ID')
    def context(browser):
        return browser.new_context(user_agent=MOBILE_UA, locale='zh-CN',
                                   viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    return collect_browser(sub, lambda payload: parse_page(payload, uid),
                           lambda p: p.chromium.launch(headless=True, timeout=10000), context,
                           f'https://m.weibo.cn/u/{uid}', rf'containerid=107603{re.escape(uid)}(?:&|$)')

def scan(sub):
    return run_sync_scan(_scan_sync, sub)

watcher.register_scanner('weibo', scan)
