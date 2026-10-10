"""Weibo profile + explicit bounded pagination in a browser visitor context."""
import re

from .. import watcher
from ..redaction import redact_text
from ..weibo_client import (BrowserSession, MOBILE_UA, WeiboAccessError, checked_payload,
                            checked_uid, clean_text)
from .common import Collector, ScanCancelled, published_at, run_sync_scan


def _cards(values):
    for card in values:
        if not isinstance(card, dict):
            continue
        yield card
        nested = card.get('card_group')
        if isinstance(nested, list):
            yield from _cards(nested)


def parse_page(payload, uid):
    uid = checked_uid(uid)
    data = checked_payload(payload)
    if not isinstance(data.get('cards'), list):
        raise ValueError('微博列表结构发生变化，未将响应当作空列表')
    items = []
    for card in _cards(data['cards']):
        post = card.get('mblog')
        if not isinstance(post, dict) or post.get('retweeted_status'):
            continue
        user = post.get('user')
        if not isinstance(user, dict) or str(user.get('idstr') or user.get('id') or '') != uid:
            continue  # Recommended posts and promoted cards are not this author.
        bid = str(post.get('bid') or post.get('mblogid') or '')
        if not re.fullmatch(r'[A-Za-z0-9]{1,32}', bid):
            continue
        title = card.get('title') or post.get('title') or {}
        pinned = bool(card.get('is_top') or post.get('isTop') or post.get('is_top') or
                      '置顶' in str(title.get('text', '') if isinstance(title, dict) else title))
        items.append({'item_id': bid, 'script_id': 'weibo',
                      'params': {'url': f'https://weibo.com/{uid}/{bid}'},
                      'title': clean_text(post.get('text'), 60) or '（无正文）',
                      'created_at': published_at(post.get('created_at')), 'pinned': pinned})
    more = data.get('has_more', payload.get('has_more'))
    if more is not None:
        more = more not in (False, 0, '0', '')
    elif not data['cards']:
        more = False
    else:
        next_page = (data.get('cardlistInfo') or {}).get('page')
        more = True if str(next_page or '').isdigit() and int(next_page) > 0 else None
    return items, more


def _scan_sync(sub):
    uid = checked_uid(sub['blogger_id'])
    collector = Collector(sub, lambda payload: parse_page(payload, uid))
    profile, result = None, None
    try:
        with BrowserSession(cancel=collector.cancel, timeout=collector.options['timeout_seconds'],
                            check=collector.check) as client:
            client.open(uid)
            profile = client.profile(uid)
            container = profile.get('container_id')
            if not container:
                raise WeiboAccessError('微博资料没有可识别的作品标签，未将响应当作空列表')
            page, requested = 1, set()
            while not collector.reason and collector.remaining() > 0:
                collector.check()
                if page in requested:
                    collector.reason = 'pagination_stalled'
                    break
                requested.add(page)
                try:
                    payload = client.posts(container, page)
                    data = checked_payload(payload)
                except (WeiboAccessError, TimeoutError) as error:
                    if not collector.valid or collector.options.get('baseline'):
                        raise
                    collector.complete = False
                    collector.reason = 'time_limit' if isinstance(error, TimeoutError) else error.reason
                    result = collector.result()
                    result['coverage']['detail'] = redact_text(str(error))
                    break
                previous = collector.pages
                collector.accept(payload)
                collector.check()
                if collector.reason:
                    break
                if collector.pages == previous:
                    collector.reason = 'pagination_stalled'
                    break
                next_page = (data.get('cardlistInfo') or {}).get('page')
                page = int(next_page) if str(next_page or '').isdigit() and int(next_page) > page else page + 1
            if result is None:
                result = collector.result()
    except ScanCancelled:
        raise
    except Exception as error:
        # A readable profile is independent of list access. The watcher may
        # refresh it, but must never create a baseline from an error response.
        return {'items': [], 'profile': profile, 'error': redact_text(str(error)),
                'coverage': {'complete': False, 'reason': getattr(error, 'reason', 'platform_error'),
                             'pages': collector.pages, 'items': len(collector.items)}}
    result['profile'] = profile
    result['avatar_source'] = profile.get('avatar_source') if profile else None
    return result


def scan(sub):
    return run_sync_scan(_scan_sync, sub)


watcher.register_scanner('weibo', scan)
