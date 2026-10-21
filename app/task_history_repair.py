"""Explicit, resumable task metadata repair. Never imports or rewrites media."""
import json
from pathlib import Path

from fastapi import HTTPException

from . import author_profiles, catalog, db, task_authors, task_files
from .redaction import redact_value
from .task_store import TaskStore


def repair():
    """Caller backs up the database first. Stable task IDs make repeated runs safe."""
    store = TaskStore()
    cursor = None
    result = dict(checked=0, updated=0, unresolved=0, skipped_active=0)
    while True:
        page = store.list_page(limit=100, cursor=cursor, include_hidden=True)
        task_authors.enrich(page['items'])
        for task in page['items']:
            result['checked'] += 1
            if task['status'] in ('queued', 'running'):
                result['skipped_active'] += 1
                continue
            metadata = task.get('metadata') or {}
            saved_content = metadata.get('content') or {}
            content = {key:value for key,value in {
                'author':saved_content.get('author') or task.get('author'),
                'title':saved_content.get('title') or task.get('display_title'),
                'platform':saved_content.get('platform') or task.get('author_platform'),
                'blogger_id':saved_content.get('blogger_id') or task.get('author_id'),
                'published_at':saved_content.get('published_at') or task.get('published_at'),
            }.items() if value}
            if (not content.get('author') or not content.get('title') or not content.get('blogger_id') or
                    (not metadata.get('live') and not content.get('published_at'))):
                try:
                    folder = Path(task_files.output_directory(task))
                    # Read only this verified task's bounded context file.
                    if not (folder / 'context.md').resolve().is_relative_to(folder):
                        raise ValueError('Context file is outside the task output')
                    meta, body = catalog._context(folder)
                    for key, value in {
                        'author':meta.get('作者'), 'title':(meta.get('视频标题') or body)[:500],
                        'blogger_id':author_profiles.from_metadata(content.get('platform'), meta),
                        'published_at':catalog._date_value(meta.get('发布时间')) if not metadata.get('live') else None,
                    }.items():
                        if value and not content.get(key):
                            content[key] = value
                except (HTTPException, OSError, ValueError):
                    pass
            if not content.get('author') or not content.get('title'):
                result['unresolved'] += 1
            content = redact_value(content)
            if content and content != metadata.get('content'):
                # Only replace metadata when it still equals our read snapshot.
                # Queue status, logs, retry links and source files are untouched.
                with db.connect() as connection:
                    current = connection.execute('SELECT metadata_json,status FROM tasks WHERE id=?', (task['id'],)).fetchone()
                    if current and current['status'] not in ('queued','running') and json.loads(current['metadata_json']) == metadata:
                        result['updated'] += connection.execute('UPDATE tasks SET metadata_json=? WHERE id=? AND metadata_json=?',
                            (json.dumps({**metadata, 'content':content}, ensure_ascii=False), task['id'], current['metadata_json'])).rowcount
        cursor = page['next_cursor']
        if not cursor:
            return result
