"""Durable live-monitor settings and append-only recording attempts."""
import uuid

from . import db

SCHEMA = """
CREATE TABLE live_monitors (
    sub_id INTEGER PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
    room_url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    interval_seconds INTEGER NOT NULL DEFAULT 60,
    status TEXT NOT NULL DEFAULT 'disabled',
    checked_at TEXT NOT NULL DEFAULT '',
    next_check_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    failures INTEGER NOT NULL DEFAULT 0,
    completed_session TEXT NOT NULL DEFAULT ''
);
CREATE TABLE live_recordings (
    id TEXT PRIMARY KEY,
    sub_id INTEGER,
    room_url TEXT NOT NULL,
    session_id TEXT NOT NULL,
    previous_id TEXT,
    author TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    output_rel TEXT NOT NULL DEFAULT '',
    entry_id TEXT,
    bytes INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    chat_status TEXT NOT NULL DEFAULT 'pending',
    chat_messages INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX live_records_sub ON live_recordings(sub_id,started_at DESC,id DESC);
CREATE UNIQUE INDEX live_one_active_sub ON live_recordings(sub_id)
    WHERE status IN ('recording','finalizing');
"""


def monitor(sub_id):
    with db.connect() as connection:
        row = connection.execute('SELECT * FROM live_monitors WHERE sub_id=?', (sub_id,)).fetchone()
    return dict(row) if row else None


def monitors():
    with db.connect() as connection:
        return [dict(row) for row in connection.execute('SELECT * FROM live_monitors ORDER BY sub_id')]


def configure(sub_id, room_url, enabled, interval_seconds):
    with db.connect() as connection:
        connection.execute('''INSERT INTO live_monitors(sub_id,room_url,enabled,interval_seconds,status)
            VALUES (?,?,?,?,?) ON CONFLICT(sub_id) DO UPDATE SET room_url=excluded.room_url,
            enabled=excluded.enabled,interval_seconds=excluded.interval_seconds,status=excluded.status,
            next_check_at='',failures=0,error='',completed_session=CASE
                WHEN room_url=excluded.room_url THEN completed_session ELSE '' END''',
            (sub_id, room_url, int(enabled), interval_seconds, 'waiting' if enabled else 'disabled'))
    return monitor(sub_id)


def update_monitor(sub_id, **changes):
    allowed = {'status', 'checked_at', 'next_check_at', 'error', 'failures', 'enabled', 'completed_session'}
    if not changes or not changes.keys() <= allowed:
        raise ValueError('Invalid monitor update')
    with db.connect() as connection:
        connection.execute('UPDATE live_monitors SET ' + ','.join(f'{key}=?' for key in changes) + ' WHERE sub_id=?',
                           (*changes.values(), sub_id))


def recordings(sub_id, *, limit=20, offset=0):
    with db.connect() as connection:
        return [dict(row) for row in connection.execute('''SELECT * FROM live_recordings
            WHERE sub_id=? ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?''',
            (sub_id, min(100, max(1, limit)), max(0, offset)))]


def get_record(identity):
    with db.connect() as connection:
        row = connection.execute('SELECT * FROM live_recordings WHERE id=?', (identity,)).fetchone()
    return dict(row) if row else None


def create_record(sub, room, info):
    identity = uuid.uuid4().hex
    with db.connect() as connection:
        previous = connection.execute('''SELECT id FROM live_recordings WHERE sub_id=? AND session_id=?
            ORDER BY started_at DESC,id DESC LIMIT 1''', (sub['id'], info['session_id'])).fetchone()
        connection.execute('''INSERT INTO live_recordings
            (id,sub_id,room_url,session_id,previous_id,author,title,status,started_at)
            VALUES (?,?,?,?,?,?,?,'recording',?)''',
            (identity, sub['id'], room, info['session_id'], previous['id'] if previous else None,
             sub['nickname'] or info.get('nickname') or '抖音直播', info['title'][:500], db.utc_now()))
    return get_record(identity)


def update_record(identity, **changes):
    allowed = {'status', 'finished_at', 'output_rel', 'entry_id', 'bytes', 'duration_seconds', 'reason', 'error', 'chat_status', 'chat_messages'}
    if not changes or not changes.keys() <= allowed:
        raise ValueError('Invalid recording update')
    with db.connect() as connection:
        connection.execute('UPDATE live_recordings SET ' + ','.join(f'{key}=?' for key in changes) + ' WHERE id=?',
                           (*changes.values(), identity))


def unfinished():
    with db.connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM live_recordings WHERE status IN ('recording','finalizing')")]
