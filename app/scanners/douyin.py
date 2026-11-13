"""Douyin post collection, with pinned-aware overlap and explicit limits."""
import re
from .. import douyin_auth, watcher
from ..browser import stealth_context
from .common import collect_browser, published_at, run_sync_scan
from ..douyin_content import source_url

_POST_URL = r'^https://(?:www|www-hj)\.douyin\.com/aweme/v1/web/aweme/post/?(?:\?|$)'

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
        hashtags = [value.get('cha_name') for value in (post.get('cha_list') or []) if isinstance(value, dict)]
        hashtags += [value.get('hashtag_name') for value in (post.get('text_extra') or []) if isinstance(value, dict)]
        items.append({'item_id': str(vid), 'script_id': 'douyin',
                      'params': {'input': source_url(post)},
                      'title': title[:60] or '（无文案）', 'created_at': published_at(post.get('create_time')),
                      'content_text': str(post.get('desc') or ''), 'hashtags': hashtags,
                      'content_complete': isinstance(post.get('desc'), str),
                      'pinned': bool(post.get('is_top') or post.get('is_pinned')),
                      '_avatar_source': ((((post.get('author') or {}).get('avatar_thumb') or {}).get('url_list') or [''])[0])})
    more = payload.get('has_more')
    return items, bool(more) if more is not None else (False if not items else None)

def page_restriction(payload):
    module = payload.get('not_login_module')
    if isinstance(module, dict) and module.get('guide_login_tip_exist') in (True, 1, '1'):
        return 'guest_preview'


def _scan_sync(sub):
    sec_uid = str(sub['blogger_id']).strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', sec_uid):
        raise ValueError('抖音订阅sec_uid格式无效')
    revision = observation = None
    def context(browser):
        nonlocal revision, observation
        ctx = stealth_context(browser)
        revision = douyin_auth.restore_session(ctx)
        observation = douyin_auth.AccountObservation(ctx)
        return ctx

    def collected(page, result, collector):
        if result['coverage']['reason'] == 'guest_preview':
            douyin_auth.record_scan_session(revision, guest=True)
            return
        if not revision or not result['coverage']['complete']:
            return
        # The account and post responses are independent. Allow a short window
        # for the normal account response only when a checkpoint is due.
        if not observation.confirmed and douyin_auth.checkpoint_due(revision):
            for _ in range(12):
                collector.check()
                if observation.confirmed or collector.remaining() <= 0:
                    break
                try:
                    page.wait_for_timeout(min(250, max(1, int(collector.remaining() * 1000))))
                except Exception:
                    return  # A closed page cannot confirm a session; keep its valid posts.
        if observation.confirmed:
            try:
                cookies = page.context.cookies()
            except Exception:
                return
            douyin_auth.record_scan_session(revision, verified_cookies=cookies)

    def launch(p):
        try:
            return p.chromium.launch(channel='chrome', headless=True, timeout=10000)
        except Exception:
            if sub.get('_cancel_event') and sub['_cancel_event'].is_set():
                raise
            return p.chromium.launch(headless=True, timeout=10000)
    result = collect_browser(sub, parse_page, launch, context, f'https://www.douyin.com/user/{sec_uid}', _POST_URL,
                             page_restriction=page_restriction, on_collected=collected)
    if sub.get('scan_options', {}).get('baseline') and result['coverage']['reason'] == 'guest_preview':
        result['error'] = '抖音仅返回部分公开作品，尚未建立基线'
    return result

def scan(sub):
    return run_sync_scan(_scan_sync, sub)

watcher.register_scanner('douyin', scan)
