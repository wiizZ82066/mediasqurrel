"""Offline subscription workspace. The primary database and the old local library stay independent."""
import asyncio
import json
from pathlib import Path
import sqlite3
import time
import uuid

from . import config, db
from .archive import atomic_write_text
from .redaction import redact_text

_TABLES = ('subscriptions', 'subscription_schedules', 'subscription_state', 'subscription_scan_options',
           'subscription_coverage', 'seen_items', 'subscription_items')
# Only these small tables are replaced by the primary subscription configuration.
_MUTABLE = {'subscriptions': 'id', 'subscription_schedules': 'id', 'subscription_scan_options': 'sub_id'}
_runner = None
_stopping = False
_copy_job = None
_status = {'mode': 'primary', 'message': ''}
_seed_key = None
_task_revision = 0


def task_changed():
    global _task_revision
    _task_revision += 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS standby_changes(
 platform TEXT, blogger_id TEXT,item_id TEXT,PRIMARY KEY(platform,blogger_id,item_id));
CREATE TABLE IF NOT EXISTS standby_meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS standby_files(
 task_id TEXT PRIMARY KEY,source_rel TEXT,dest_rel TEXT,checks TEXT NOT NULL);
"""
for _event in ('INSERT', 'UPDATE'):
    _SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS standby_item_{_event} AFTER {_event} ON subscription_items
WHEN EXISTS(SELECT 1 FROM standby_meta WHERE key='used')
BEGIN INSERT OR IGNORE INTO standby_changes VALUES(NEW.platform,NEW.blogger_id,NEW.item_id); END;
"""


def status():
    return dict(_status)


def _attach(conn, name, path):
    conn.execute(f'ATTACH DATABASE ? AS {name}', (Path(path).as_uri() + '?mode=ro',))


def prepare(slot=None):
    """Publish a small consistent standby seed; never copy a task history or traverse media."""
    global _seed_key
    root = config.STANDBY_BASE
    primary = config._primary_pair[0]
    config.require_database(primary)
    if root.resolve() != root or root.is_relative_to(config._primary_pair[1].resolve()):
        raise ValueError('本机暂存区不能位于首选媒体库或链接目录内')
    prefs = primary.parent / 'settings.json'
    try:
        prefs_version = prefs.stat().st_mtime_ns
    except FileNotFoundError:
        prefs_version = None
    slot = slot or config.standby_slot()
    retired = None
    if slot and (slot / 'used').exists():
        if not (slot / 'handback').is_file():
            raise ValueError('暂存批次尚未完成迁回，已保留')
        from .standby_transfer import release_media
        release_media(slot, config._store)
        retired = slot
        slot = None
    # Most service ticks have no subscription changes. This reads just the
    # subscription configuration/cursors, never post history or media files.
    with db.connect(primary) as remote:
        key = (_task_revision, prefs_version, tuple(tuple(map(tuple, remote.execute('SELECT * FROM ' + table)))
                                     for table in ('subscriptions','subscription_schedules','subscription_scan_options')))
    if slot and _seed_key == (slot, key):
        return slot
    fresh = slot is None
    if fresh:
        slot = root / uuid.uuid4().hex
        slot.mkdir(parents=True)
        (slot / 'library').mkdir()
        db.migrate(slot / 'app.db')
    with db.connect(slot / 'app.db', existing=True) as conn:
        if fresh:
            conn.executescript(_SCHEMA)
        _attach(conn, 'src', primary)
        conn.execute('BEGIN')
        if conn.execute('PRAGMA src.user_version').fetchone()[0] != conn.execute('PRAGMA main.user_version').fetchone()[0]:
            raise ValueError('首选库与暂存库版本不一致')
        for table in _TABLES:
            # EXCEPT avoids rewriting unchanged rows and SQLite indexes. No logs,
            # historical scans, thumbnails, browser sessions or media are read.
            conn.execute(f'INSERT OR REPLACE INTO main.{table} SELECT * FROM src.{table} EXCEPT SELECT * FROM main.{table}')
            if table in _MUTABLE:
                pk = _MUTABLE[table]
                conn.execute(f'DELETE FROM main.{table} WHERE {pk} NOT IN (SELECT {pk} FROM src.{table})')
        # A primary run may lose its disk after claiming tomorrow's cursor.
        # Keep its original slot due in the seed until the primary settles it.
        conn.execute("""UPDATE subscription_schedules AS s SET retry_due=(
            SELECT min(r.due_at) FROM src.schedule_runs r,json_each(r.schedule_ids_json) j
            WHERE r.status='running' AND r.sub_id=s.sub_id AND json_extract(j.value,'$.id')=s.id
            AND json_extract(j.value,'$.revision')=s.revision)
            WHERE EXISTS(SELECT 1 FROM src.schedule_runs r,json_each(r.schedule_ids_json) j
              WHERE r.status='running' AND r.sub_id=s.sub_id AND json_extract(j.value,'$.id')=s.id
              AND json_extract(j.value,'$.revision')=s.revision)""")
    if prefs_version is not None:
        from . import settings
        saved = json.dumps({**settings.load(), 'default_download_dir': ''}, ensure_ascii=False, sort_keys=True)
        path = slot / 'settings.json'
        if not path.exists() or path.read_text(encoding='utf-8') != saved:
            atomic_write_text(path, saved)
    if fresh:
        atomic_write_text(root / 'current', slot.name)
        if retired:
            from .standby_transfer import retire_if_empty
            retire_if_empty(retired)
    _seed_key = (slot, key)
    return slot


def begin_local():
    slot = config.standby_slot()
    if not slot or Path(config.DB_PATH) != slot / 'app.db':
        raise ValueError('本机暂存库未就绪')
    atomic_write_text(slot / 'used', slot.name)
    (slot / 'handback').unlink(missing_ok=True)
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO standby_meta VALUES('used','1')")


async def _notice(mode, message):
    global _status
    if _status == {'mode': mode, 'message': message}:
        return
    _status = {'mode': mode, 'message': message}
    print('[standby] ' + message)
    from .task_manager import broadcast
    await broadcast({'type': 'notification', 'level': 'error' if mode == 'attention' else 'info',
                     'title': '订阅存储', 'text': message})


def _busy():
    # Reading the primary database to ask whether it is busy would itself fail
    # when unplugged. These are the actual in-process owners.
    from . import watcher, task_manager, live_service, library_service, settings_api, subscription_batch
    return (watcher.has_active_scans() or live_service.busy() or bool(library_service._scans)
            or bool(task_manager._jobs) or bool(settings_api._work)
            or subscription_batch.snapshot()['status'] == 'running' or bool(task_manager._archive_jobs))


async def _loop():
    global _copy_job
    from . import runtime, task_manager, watcher
    from . import standby_transfer
    next_seed = next_return = 0
    while not _stopping:
        try:
            primary = await asyncio.to_thread(config._preferred_store, config._paths)
            slot = config.standby_slot()
            if config.MAINTENANCE_ACTIVE:
                pass
            elif config.STANDBY_ACTIVE and primary:
                if time.monotonic() >= next_return and not _busy() and not task_manager.update_state()['active']:
                    config.MAINTENANCE_ACTIVE = True
                    try:
                        await _notice('returning', '原媒体盘已接回，正在校验并迁回本次暂存的内容。')
                        _copy_job = asyncio.create_task(asyncio.to_thread(standby_transfer.handback, slot, primary))
                        result = await asyncio.shield(_copy_job)
                        await _notice('returning', f'已迁回 {result} 条下载记录，正在切回原媒体库。')
                        if not _stopping:
                            runtime.restart_handler()
                        return
                    finally:
                        if not (slot / 'handback').is_file():
                            config.MAINTENANCE_ACTIVE = False
            elif config.STANDBY_ACTIVE:
                await _notice('local', '媒体盘离线；订阅按原计划扫描，新下载暂存在本机。')
            elif primary and config._store:
                if watcher.storage_recovery_needed and not _busy():
                    config.MAINTENANCE_ACTIVE = True
                    await _notice('switching', '媒体盘已恢复，正在重新接续中断的订阅和下载。')
                    runtime.restart_handler()
                    return
                if time.monotonic() >= next_seed:
                    _copy_job = asyncio.create_task(asyncio.to_thread(prepare))
                    await asyncio.shield(_copy_job)
                    next_seed = time.monotonic() + 10
                await _notice('primary', '订阅使用首选媒体库，本机暂存已就绪。')
            elif (slot and not primary and config._store) or (not config._store and primary):
                if not _busy():
                    config.MAINTENANCE_ACTIVE = True
                    await _notice('switching', '正在切换订阅存储，网页将自动重连。')
                    runtime.restart_handler()
                    return
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
            next_return = time.monotonic() + 60
            next_seed = time.monotonic() + 30
            await _notice('attention', '存储切换尚未完成，原件保留，将自动重试：' + redact_text(str(error)))
        await asyncio.sleep(5)


async def start():
    global _runner, _stopping
    from . import runtime
    if not config.STANDBY_BASE or runtime.restart_handler is None:
        return
    _stopping = False
    _runner = asyncio.create_task(_loop())


async def stop():
    global _runner, _stopping, _copy_job
    _stopping = True
    if _runner and not _runner.done():
        # A copying thread owns verified files until it finishes. Never release
        # the process locks while that thread is still publishing a batch.
        _runner.cancel()
    if _copy_job:
        await asyncio.gather(_copy_job, return_exceptions=True)
        _copy_job = None
    if _runner:
        await asyncio.gather(_runner, return_exceptions=True)
    _runner = None
