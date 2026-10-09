"""Douyin post collection, with pinned-aware overlap and explicit limits."""
import re
from .. import watcher
from ..browser import launch_chrome, stealth_context
from .common import collect_browser, published_at, run_sync_scan

def parse_page(payload):
    if not isinstance(payload, dict) or payload.get('status_code') != 0:
        raise ValueError('抖音列表请求失败；请检查登录、平台验证或限流状态')
    if not isinstance(payload.get('aweme_list'), list):
        raise ValueError('抖音列表结构发生变化，未将响应当作空列表')
    items = []
    for post in payload['aweme_list']:
        vid = post.get('aweme_id')
        if not vid:
            continue
        title = re.sub(r'\s+', ' ', str(post.get('desc') or '')).strip()
        items.append({'item_id': str(vid), 'script_id': 'douyin',
                      'params': {'input': f'https://www.douyin.com/video/{vid}'},
                      'title': title[:60] or '（无文案）', 'created_at': published_at(post.get('create_time')),
                      'pinned': bool(post.get('is_top') or post.get('is_pinned')),
                      '_avatar_source': ((((post.get('author') or {}).get('avatar_thumb') or {}).get('url_list') or [''])[0])})
    more = payload.get('has_more')
    return items, bool(more) if more is not None else (False if not items else None)

def _scan_sync(sub):
    sec_uid = str(sub['blogger_id']).strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', sec_uid):
        raise ValueError('抖音订阅sec_uid格式无效')
    def context(browser):
        from ..douyin_auth import attach_cookies
        ctx = stealth_context(browser)
        attach_cookies(ctx)
        return ctx
    def launch(p):
        try:
            return p.chromium.launch(channel='chrome', headless=True, timeout=10000)
        except Exception:
            if sub.get('_cancel_event') and sub['_cancel_event'].is_set():
                raise
            return p.chromium.launch(headless=True, timeout=10000)
    return collect_browser(sub, parse_page, launch, context,
                           f'https://www.douyin.com/user/{sec_uid}', r'aweme/v1/web/aweme/post(?:/|\?)')

def scan(sub):
    return run_sync_scan(_scan_sync, sub)

watcher.register_scanner('douyin', scan)
