"""Resolve one opened archive from durable identities, without scanning or network I/O."""
import json
from urllib.parse import urlencode, quote

from . import author_profiles, db


def enrich(entry):
    platform = entry.get('platform')
    identity = author_profiles.from_metadata(platform, entry.get('meta') or {})
    with db.connect() as connection:
        recording = connection.execute('''SELECT r.id,s.blogger_id FROM live_recordings r
            LEFT JOIN subscriptions s ON s.id=r.sub_id AND s.platform='douyin'
            WHERE r.entry_id=? ORDER BY r.is_aggregate DESC LIMIT 1''', (entry['id'],)).fetchone()
        if not identity and platform == 'douyin' and recording:
            identity = recording['blogger_id'] or ''
        if not identity:
            # Older archives can have a durable task link but no author ID in context.md.
            rows = connection.execute('''SELECT t.script_id,t.metadata_json FROM task_media m
                JOIN tasks t ON t.id=m.task_id WHERE m.entry_id=?
                ORDER BY t.created_at DESC LIMIT 10''', (entry['id'],)).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row['metadata_json'])
                except (TypeError, ValueError):
                    continue
                if not isinstance(metadata, dict):
                    continue
                for key in ('subscription', 'content'):
                    source = metadata.get(key) or {}
                    if not isinstance(source, dict):
                        continue
                    candidate = source.get('blogger_id')
                    if (source.get('platform', row['script_id']) == platform and
                            author_profiles.valid_identity(platform, candidate)):
                        identity = candidate
                        break
                if identity:
                    break
        subscription = connection.execute('''SELECT id,avatar_source FROM subscriptions
            WHERE platform=? AND blogger_id=?''', (platform, identity)).fetchone() if identity else None
    entry['author_id'] = identity if author_profiles.valid_identity(platform, identity) else None
    entry['author_avatar_url'] = (f"/api/subs/{subscription['id']}/avatar"
        if subscription and subscription['avatar_source'] else author_profiles.avatar_url(platform, identity))
    if recording:
        for item in entry.get('gallery', []):
            if item['type'] == 'video' and not item.get('live'):
                source_id = item.get('source_entry_id') or entry['id']
                item['playback_info_url'] = (f'/api/library/entries/{quote(source_id, safe="")}/playback?'
                                             + urlencode({'rel': item['rel']}))
    return entry
