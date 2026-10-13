"""Exact topic matching for subscription downloads; never executes patterns."""
import re
import unicodedata


SCHEMA = """
ALTER TABLE subscriptions ADD COLUMN download_filter_json TEXT NOT NULL DEFAULT '{"tags":[]}';
ALTER TABLE subscription_items ADD COLUMN content_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE subscription_items ADD COLUMN filter_decision_json TEXT NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS subscription_items_history
 ON subscription_items(platform,blogger_id,discovered_at DESC,item_id DESC);
CREATE INDEX IF NOT EXISTS subscription_items_pending_filter
 ON subscription_items(platform,blogger_id,updated_at,item_id) WHERE status='pending_filter';
"""

MAX_TAGS = 20
MAX_TAG_LENGTH = 80
MAX_TEXT_LENGTH = 100000


def _normalize(value):
    return unicodedata.normalize('NFKC', value).strip().strip('#').strip().casefold()


def normalize_rule(value=None):
    if value is None:
        value = {'tags': []}
    if not isinstance(value, dict) or set(value) - {'tags'}:
        raise ValueError('下载规则仅支持 tags 标签列表')
    tags = value.get('tags', [])
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise ValueError('下载标签必须是列表，最多 20 个')
    result = []
    for raw in tags:
        if not isinstance(raw, str) or len(raw) > MAX_TAG_LENGTH + 2:
            raise ValueError('每个下载标签须为不超过 80 字的文本')
        tag = _normalize(raw)
        if not tag or len(tag) > MAX_TAG_LENGTH or '#' in tag or any(unicodedata.category(ch).startswith('C') for ch in tag):
            raise ValueError('下载标签不能为空，也不能包含换行、控制字符或内部 #')
        if tag not in result:
            result.append(tag)
    return {'tags': result}


def extract_tags(text, explicit=()):
    """Weibo #topic# can contain spaces; open Douyin #topic stops at punctuation."""
    text = unicodedata.normalize('NFKC', str(text or ''))[:MAX_TEXT_LENGTH]
    tags = set()
    # Paired topics are only consumed when they end before the next line. For
    # '#one #two', the second marker starts the next open topic instead.
    paired = []
    for match in re.finditer(r'(?=#([^#\r\n]{1,80})#)', text):
        body = match.group(1)
        if body and not body[-1].isspace() and (not paired or match.start() >= paired[-1][1]):
            tags.add(_normalize(body))
            paired.append((match.start(), match.start() + len(body) + 2))
    for start, end in reversed(paired):
        text = text[:start] + ' ' * (end - start) + text[end:]
    for match in re.finditer(r'#([^\W_][\w\u200d-]{0,79})(?=[^\w\u200d-]|$)', text, re.UNICODE):
        tags.add(_normalize(match.group(1)))
    for tag in explicit:
        if isinstance(tag, str) and 0 < len(tag) <= MAX_TAG_LENGTH + 2:
            normalized = _normalize(tag)
            if normalized and '#' not in normalized:
                tags.add(normalized)
    return sorted(tags)


def content_metadata(item):
    text = item.get('content_text')
    known = isinstance(text, str)
    text = text if known else ''
    explicit = item.get('hashtags')
    explicit = explicit[:200] if isinstance(explicit, list) else []
    detail_id = str(item.get('_detail_id') or item.get('item_id') or '')
    return {'tags': extract_tags(text, explicit),
            'detail_id': detail_id if re.fullmatch(r'[A-Za-z0-9]{1,32}', detail_id) else '',
            'complete': known and item.get('content_complete', True) is True and len(text) <= MAX_TEXT_LENGTH}


def evaluate(rule, metadata):
    wanted = normalize_rule(rule)['tags']
    matched = sorted(set(wanted) & set(metadata.get('tags') or []))
    if not wanted:
        result, reason = 'matched', '未设置标签，下载全部新内容'
    elif matched:
        result, reason = 'matched', '命中指定标签'
    elif not metadata.get('complete'):
        result, reason = 'pending_filter', '平台正文不完整，尚不能确认标签；后续扫描取得完整正文后重试'
    else:
        result, reason = 'filtered', '未命中指定标签'
    return {'result': result, 'reason': reason, 'tags': wanted, 'matched': matched}
