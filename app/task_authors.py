"""Task card details from existing records; never scan media files or fetch avatars."""
import json
import os
from pathlib import Path

from . import config, db
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
            SELECT r.id, e.author linked_author, e.text_preview, l.author live_author, l.title live_title,
                s.nickname, s.blogger_id, s.id sub_id, s.avatar_source,
                ls.id live_sub_id, ls.avatar_source live_avatar_source, i.title item_title
            FROM refs r LEFT JOIN task_media tm ON tm.task_id=r.id
            LEFT JOIN media_entries e ON e.id=COALESCE(tm.entry_id,r.entry_id)
            LEFT JOIN live_recordings l ON l.id=r.live_id
            LEFT JOIN subscriptions s ON s.id=r.sub_id AND s.platform=r.platform AND s.blogger_id=r.blogger_id
            LEFT JOIN subscriptions ls ON ls.id=l.sub_id AND ls.platform='douyin'
            LEFT JOIN subscription_items i ON i.platform=r.platform AND i.blogger_id=r.blogger_id AND i.item_id=r.item_id
            """, (json.dumps(references),)).fetchall()
        details = {row['id']: row for row in rows}
        roots = None
        for task in tasks:
            metadata = task.get('metadata') or {}
            sub, live = metadata.get('subscription') or {}, metadata.get('live') or {}
            row = details[task['id']]
            name = ((_name(row['nickname']) if row['nickname'] != row['blogger_id'] else '') or
                    _name(row['live_author']) or _name(row['linked_author']) or
                    _name(sub.get('author')) or _name(live.get('author')))
            title = (_name(row['live_title']) or _name(row['item_title']) or _name(row['text_preview']) or
                     _name(sub.get('title')) or _name((task.get('params') or {}).get('title')))
            avatar_id = row['sub_id'] if row['avatar_source'] else row['live_sub_id'] if row['live_avatar_source'] else None
            if (not name or not title) and not metadata.get('diagnostic'):
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
                            entry = connection.execute('SELECT author,text_preview FROM media_entries WHERE root_id=? AND rel_dir=?',
                                                       (root['id'], relative)).fetchone()
                            if entry:
                                name = name or _name(entry['author'])
                                title = title or _name(entry['text_preview'])
                                break
            task['author'] = name or None
            task['author_avatar_url'] = f'/api/subs/{avatar_id}/avatar' if avatar_id else None
            task['display_title'] = title[:500] or None
    return tasks
