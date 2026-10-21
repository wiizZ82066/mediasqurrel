"""Task card details from existing records; never scan media files or fetch avatars."""
import json
import os
from pathlib import Path

from . import author_profiles, config, db
from .redaction import redact_text


def _name(value):
    return redact_text(value.strip()) if isinstance(value, str) else ''


def enrich(tasks):
    if not tasks:
        return tasks
    references = []
    for task in tasks:
        metadata = task.get('metadata') or {}
        sub, live = metadata.get('subscription') or {}, metadata.get('live') or {}
        references.append(dict(id=task['id'], sub_id=sub.get('sub_id'), platform=sub.get('platform'),
            blogger_id=sub.get('blogger_id'), item_id=sub.get('item_id'),
            live_id=live.get('id'), entry_id=live.get('entry_id')))
    with db.connect() as connection:
        rows = connection.execute("""
            WITH refs AS (
              SELECT json_extract(value,'$.id') id, json_extract(value,'$.sub_id') sub_id,
                json_extract(value,'$.platform') platform, json_extract(value,'$.blogger_id') blogger_id,
                json_extract(value,'$.item_id') item_id,
                json_extract(value,'$.live_id') live_id, json_extract(value,'$.entry_id') entry_id
              FROM json_each(?))
            SELECT r.id, e.author linked_author, e.text_preview, e.meta_json, e.platform entry_platform,
                e.sort_at, e.date_source, l.author live_author, l.title live_title,
                l.broadcast_started_at, l.started_at recording_started_at,
                s.nickname, s.blogger_id, s.id sub_id, s.avatar_source,
                ls.id live_sub_id, ls.blogger_id live_blogger_id, ls.avatar_source live_avatar_source, i.title item_title
            FROM refs r LEFT JOIN task_media tm ON tm.task_id=r.id
            LEFT JOIN media_entries e ON e.id=COALESCE(tm.entry_id,r.entry_id)
            LEFT JOIN live_recordings l ON l.id=r.live_id
            LEFT JOIN subscriptions s ON s.id=r.sub_id AND s.platform=r.platform AND s.blogger_id=r.blogger_id
            LEFT JOIN subscriptions ls ON ls.id=l.sub_id AND ls.platform='douyin'
            LEFT JOIN subscription_items i ON i.platform=r.platform AND i.blogger_id=r.blogger_id AND i.item_id=r.item_id
            """, (json.dumps(references),)).fetchall()
        details = {row['id']: row for row in rows}
        roots = None
        subscriptions = None
        for task in tasks:
            metadata = task.get('metadata') or {}
            sub, live = metadata.get('subscription') or {}, metadata.get('live') or {}
            content = metadata.get('content') or {}
            row = details[task['id']]
            published = ((row['sort_at'] if row['date_source'] == 'published' else None) or
                         sub.get('published_at') or content.get('published_at')) if not live else None
            entry_meta = json.loads(row['meta_json']) if row['meta_json'] else {}
            platform = row['entry_platform'] or sub.get('platform') or content.get('platform') or ('douyin' if live else task.get('script_id'))
            identity = (row['blogger_id'] or row['live_blogger_id'] or
                        author_profiles.from_metadata(platform, entry_meta) or content.get('blogger_id') or '')
            name = ((_name(row['nickname']) if row['nickname'] != row['blogger_id'] else '') or
                    _name(row['live_author']) or _name(row['linked_author']) or
                    _name(sub.get('author')) or _name(live.get('author')) or _name(content.get('author')))
            title = (_name(row['live_title']) or _name(row['item_title']) or _name(row['text_preview']) or
                     _name(sub.get('title')) or _name((task.get('params') or {}).get('title')) or _name(content.get('title')))
            avatar_id = row['sub_id'] if row['avatar_source'] else row['live_sub_id'] if row['live_avatar_source'] else None
            if (not name or not title or (not published and not live)) and not metadata.get('diagnostic'):
                # Legacy tasks may have no task_media link. Match an exact
                # indexed directory inside a registered root, not its basename.
                output = task.get('output_dir')
                if not output and task.get('output_rel'):
                    output = os.path.join(config.LIBRARY_ROOT, task['output_rel'])
                if isinstance(output, str) and os.path.isabs(output):
                    path = Path(output).resolve()
                    if roots is None:
                        roots = list(connection.execute('SELECT id,path FROM media_roots'))
                    for root in roots:
                        base = Path(root['path']).resolve()
                        if path != base and path.is_relative_to(base):
                            relative = path.relative_to(base).as_posix()
                            entry = connection.execute('SELECT author,text_preview,meta_json,platform,sort_at,date_source FROM media_entries WHERE root_id=? AND rel_dir=?',
                                                       (root['id'], relative)).fetchone()
                            if entry:
                                name = name or _name(entry['author'])
                                title = title or _name(entry['text_preview'])
                                platform = entry['platform'] or platform
                                identity = identity or author_profiles.from_metadata(platform, json.loads(entry['meta_json']))
                                if not live and entry['date_source'] == 'published':
                                    published = published or entry['sort_at']
                                break
            if not avatar_id and author_profiles.valid_identity(platform, identity):
                if subscriptions is None:
                    subscriptions = {(r['platform'], r['blogger_id']): dict(r) for r in connection.execute(
                        'SELECT id,platform,blogger_id,nickname,avatar_source FROM subscriptions')}
                matched = subscriptions.get((platform, identity))
                if matched:
                    avatar_id = matched['id'] if matched['avatar_source'] else None
                    name = _name(matched['nickname']) or name
            task['author'] = name or None
            task['author_platform'] = platform
            task['author_id'] = identity if author_profiles.valid_identity(platform, identity) else None
            task['author_avatar_url'] = (f'/api/subs/{avatar_id}/avatar' if avatar_id else
                                         author_profiles.avatar_url(platform, identity))
            task['display_title'] = title[:500] or None
            task['published_at'] = published or None
            task['broadcast_started_at'] = row['broadcast_started_at'] or None
            task['recording_started_at'] = row['recording_started_at'] or None
    return tasks
