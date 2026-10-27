"""Bounded home dashboard reads; no log loading, directory scan or platform request."""
import asyncio
import json

from fastapi import APIRouter

from . import catalog, db, library_service, live_api, live_service, task_authors, task_manager, watcher

router = APIRouter()


def snapshot(active_ids=(), scanning_ids=()):
    running = task_manager.list_page(status='running', limit=3)
    queued = task_manager.list_page(status='queued', limit=3)
    # Counts come from SQLite, not the most recent 50 tasks or the browser cache.
    tasks = task_authors.enrich((running['items'] + queued['items'])[:3])
    tasks = [{key: task.get(key) for key in (
        'id', 'status', 'script_id', 'script_name', 'author', 'avatar_url',
        'display_title', 'progress')} for task in tasks]
    with db.connect() as connection:
        next_scan = connection.execute('''
            WITH upcoming AS (
                SELECT s.sub_id, s.kind, s.plan_json, b.nickname, b.platform,
                    CASE WHEN s.retry_due IS NOT NULL AND
                        (s.next_due IS NULL OR julianday(s.retry_due)<julianday(s.next_due))
                        THEN s.retry_due ELSE s.next_due END due_at
                FROM subscription_schedules s JOIN subscriptions b ON b.id=s.sub_id
                WHERE s.enabled=1 AND b.enabled=1
                    AND NOT EXISTS (SELECT 1 FROM schedule_runs r
                        WHERE r.sub_id=s.sub_id AND r.status='running')
                    AND s.sub_id NOT IN (SELECT value FROM json_each(?)))
            SELECT * FROM upcoming WHERE due_at IS NOT NULL
                ORDER BY julianday(due_at),sub_id LIMIT 1
        ''', (json.dumps(list(scanning_ids)),)).fetchone()
        enabled_subs = connection.execute('SELECT count(*) FROM subscriptions WHERE enabled=1').fetchone()[0]
        enabled_live = connection.execute('SELECT count(*) FROM live_monitors WHERE enabled=1').fetchone()[0]
        selected = connection.execute('''
            SELECT m.*, s.nickname FROM live_monitors m JOIN subscriptions s ON s.id=m.sub_id
            WHERE m.enabled=1 OR m.sub_id IN (SELECT value FROM json_each(?))
            ORDER BY (m.sub_id IN (SELECT value FROM json_each(?))) DESC,
                CASE WHEN m.next_check_at='' THEN 1 ELSE 0 END,m.next_check_at,m.sub_id LIMIT 1
        ''', (json.dumps(list(active_ids)), json.dumps(list(active_ids)))).fetchone()
        record = None
        if selected:
            record = connection.execute('''SELECT * FROM live_recordings WHERE sub_id=? AND is_aggregate=0
                ORDER BY started_at DESC,id DESC LIMIT 1''', (selected['sub_id'],)).fetchone()
    upcoming = None
    if next_scan:
        upcoming = {key: next_scan[key] for key in ('sub_id', 'nickname', 'platform', 'kind', 'due_at')}
        upcoming['timezone'] = json.loads(next_scan['plan_json']).get('timezone', 'Asia/Shanghai')
    return {
        'downloads': {'running': running.get('total', len(running['items'])),
                      'queued': queued.get('total', len(queued['items'])), 'items': tasks},
        'scans': {'enabled': enabled_subs, 'next': upcoming},
        'live': {'enabled': enabled_live},
        'recent': catalog.recent_entries(limit=8),
    }, dict(selected) if selected else None, dict(record) if record else None


@router.get('/api/home')
async def home():
    active_ids = tuple(live_service._active)
    scans = [(identity, state) for identity, state in watcher.SCAN_STATES.items() if state.get('status') == 'running']
    result, monitor, record = await asyncio.to_thread(snapshot, active_ids, tuple(identity for identity, _ in scans))
    # Runtime state is resolved on the event loop, using the same rules as subscriptions.
    result['live']['active'] = live_service.active_count()
    current = live_api.monitor_status(monitor, record) if monitor else None
    result['live']['current'] = ({key: current.get(key) for key in (
        'sub_id', 'nickname', 'enabled', 'runtime_status', 'next_check_at', 'record')}
        if current else None)
    result['scans']['active'] = len(scans)
    result['scans']['progress'] = scans[0][1].get('progress') if scans else None
    result['updated_at'] = db.utc_now()
    library_service.enrich_page(result['recent'])
    return result
