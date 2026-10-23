"""Independent, opt-in live monitoring with bounded checks and recorders."""
import asyncio
from datetime import datetime, timedelta, timezone
import shutil
import threading
import time

from . import catalog, config, db, library_service, live_douyin, live_recorder, live_store, live_scheduling, live_tasks, task_manager, watcher, live_merge, live_collections
from .live_worker import ERRORS

MAX_RECORDINGS = 2
MAX_CHECKS = 2
_runner = None
_stopping = False
_checks = {}
_active = {}
_manual_due = {}
_publishing = set()
_recovering = False

REASONS = {
    'offline': '平台已确认下播。', 'manual': '用户停止录制，自动录播已关闭。',
    'shutdown': '程序退出，已保留收到的内容；下次启动重新检查。',
    'disconnect': '直播连接提前结束，平台未确认下播；已有片段保留，将自动重试并归入录播合集。',
    'disk_full': '剩余空间不足 2 GiB，已停止录制并关闭此博主自动录播。',
    'worker_error': '录制未正常结束，请检查网络和录播组件；已有文件保留。',
    'restart': '上次程序异常退出，恢复了已收到的内容，不代表完整直播。',
}


def active_count():
    return len(_active)


def busy():
    return bool(_active or _checks or _publishing or _recovering or live_merge.busy())


def _later(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='milliseconds')


def _failure(sub_id, message, *, limited=False, recording=False):
    monitor = live_store.monitor(sub_id)
    if not monitor:
        return
    failures = monitor['failures'] + 1
    base = 30 if recording else monitor['interval_seconds']
    delay = 1800 if limited else min(900, base * 2 ** min(failures, 5))
    live_store.update_monitor(sub_id, status='error' if monitor['enabled'] else 'disabled', error=message,
                              failures=failures, checked_at=db.utc_now(), next_check_at=_later(delay))


async def _notify(record, status, reason):
    saved = live_store.get_record(record['id'])
    await task_manager.broadcast({'type': 'notification', 'level': 'error' if status == 'interrupted' else 'info',
        'title': '直播录制结果', 'text': f"{record['author']}：{saved['error'] or REASONS[reason]}", 'time': db.utc_now()})


async def _sync_task(record):
    try:
        await live_tasks.sync_record(record)
    except Exception:
        print('[live] 录播任务摘要同步暂未完成，将在后台重试；录播文件已保留')


async def _publish(record, status, reason):
    live_store.update_record(record['id'], status='finalizing', reason=reason)
    try:
        ready = await asyncio.to_thread(live_recorder.finalize, record, status, REASONS[reason])
        if not ready:
            live_store.update_record(record['id'], status='interrupted', reason=reason, finished_at=db.utc_now(),
                error=live_store.get_record(record['id'])['error'] or '未得到可播放的视频；原始文件保留')
            return False
        scan = library_service.start_entry_scan({'id': 'live-' + record['id'],
                                                'output_dir': str(live_recorder.folder_for(record))})
        state = library_service._scans.get(scan['id']) if scan else None
        if state:
            await state['task']
        entry = catalog.locate_entry(str(live_recorder.folder_for(record)))
        if not entry:
            raise ValueError('index pending')
        live_store.update_record(record['id'], status=status, reason=reason, finished_at=db.utc_now(),
                                entry_id=entry['id'], error=record.get('error', '') if status == 'interrupted' else '')
        await task_manager.broadcast({'type': 'library_updated'})
        return True
    except Exception:
        live_store.update_record(record['id'], status='interrupted', reason=reason, finished_at=db.utc_now(),
                                error='收尾或入库未完成，文件保留；可在录播记录中重试入库')
        return False
    finally:
        await _sync_task(live_store.get_record(record['id']))


async def _record(record, info, owner):
    sub_id = record['sub_id']
    status, reason = 'interrupted', 'worker_error'
    result = {}
    def progress(value):
        seconds = max(0, float(value.get('seconds', 0)))
        if seconds > owner.get('seconds', 0):
            owner.update(seconds=seconds, progress_at=time.monotonic())
    try:
        result = await asyncio.to_thread(live_recorder.record_stream, record, info['stream_url'], owner['stop'],
                                         on_progress=progress)
        live_store.update_record(record['id'], bytes=max(0, int(result.get('bytes', 0))),
                                 duration_seconds=max(0, float(result.get('seconds', 0))),
                                 chat_status=(result.get('chat') or {}).get('status', 'unavailable'),
                                 chat_messages=(result.get('chat') or {}).get('messages', 0))
        reason = owner.get('reason') or 'disconnect'
        if result['result'] == 'disk_full':
            reason = 'disk_full'
            live_store.update_monitor(sub_id, enabled=0)
        elif result['result'] in ('worker_error', 'stop_timeout'):
            reason = 'worker_error'
        elif result['code'] != 0:
            reason = 'worker_error'
        elif result['result'] == 'source_end' and not owner.get('reason'):
            try:
                current = await asyncio.to_thread(live_douyin.lookup, record['room_url'])
                if not current['live'] or current['session_id'] != record['session_id']:
                    reason = 'offline'
            except live_douyin.LiveError:
                pass
        status = 'completed' if reason == 'offline' else 'stopped' if reason == 'manual' else 'interrupted'
        if result['code'] != 0 and status in ('completed', 'stopped'):
            status, reason = 'interrupted', 'worker_error'
        code = result.get('error_code')
        if reason == 'disk_full':
            live_store.update_record(record['id'], error=REASONS['disk_full'])
        elif result['code'] != 0:
            live_store.update_record(record['id'], error=ERRORS.get(code, ERRORS['ffmpeg_error']))
        elif not result.get('seconds') and not owner.get('reason'):
            live_store.update_record(record['id'], error=ERRORS['no_video'])
        elif status == 'interrupted':
            live_store.update_record(record['id'], error=ERRORS.get(code) or REASONS[reason])
    except Exception:
        live_store.update_record(record['id'], error=REASONS['worker_error'])
    finally:
        try:
            record = live_store.get_record(record['id'])
            published = await _publish(record, status, reason) if record['output_rel'] else False
            if not record['output_rel']:
                live_store.update_record(record['id'], status='interrupted', finished_at=db.utc_now(),
                                        error=REASONS['worker_error'], reason='worker_error')
                await _sync_task(live_store.get_record(record['id']))
            if status == 'completed' and published:
                live_store.update_monitor(sub_id, completed_session=record['session_id'])
            monitor = live_store.monitor(sub_id)
            if monitor:
                if status == 'interrupted' or not published:
                    saved = live_store.get_record(record['id'])
                    _failure(sub_id, saved['error'] or REASONS[reason], recording=True,
                             limited=result.get('error_code') == 'http_limited')
                else:
                    live_store.update_monitor(sub_id, status='waiting' if monitor['enabled'] else 'disabled',
                        error='', failures=0, **live_scheduling.cursors(monitor))
            await _notify(record, status if published else 'interrupted', reason)
        finally:
            _active.pop(sub_id, None)


async def check(sub_id):
    if _stopping or config.MAINTENANCE_ACTIVE:
        return
    monitor = live_store.monitor(sub_id)
    sub = watcher.get_sub(sub_id)
    if not monitor or not monitor['enabled'] or not sub or sub['platform'] != 'douyin':
        return
    try:
        if sub_id in _active:
            info = await asyncio.to_thread(live_douyin.lookup, _active[sub_id]['room_url'])
        elif monitor['source'] == 'profile':
            info = await asyncio.to_thread(live_douyin.lookup_blogger, sub['blogger_id'], quality=monitor['quality'])
        else:
            info = await asyncio.to_thread(live_douyin.lookup, monitor['room_url'], quality=monitor['quality'])
        # Re-read after network wait: disable/delete/shutdown may have happened.
        latest = live_store.monitor(sub_id)
        if _stopping or config.MAINTENANCE_ACTIVE or not latest or not latest['enabled'] or latest['room_url'] != monitor['room_url']:
            return
        live_store.update_monitor(sub_id, checked_at=db.utc_now(),
                                  **live_scheduling.cursors(monitor, active=sub_id in _active))
        if sub_id in _active:
            live_store.update_monitor(sub_id, status='recording')
            if _active[sub_id].get('seconds', 0) >= 30:
                live_store.update_monitor(sub_id, error='', failures=0)
            if not info['live'] or info['session_id'] != _active[sub_id]['session_id']:
                _active[sub_id]['reason'] = 'offline'
                _active[sub_id]['stop'].set()
            return
        if not info['live']:
            live_store.update_monitor(sub_id, status='offline', error='', failures=0)
            return
        if info.get('owner_id') != sub['blogger_id']:
            raise live_douyin.LiveError('直播间博主身份与此订阅不匹配或无法核实，请检查链接')
        # Douyin can reuse the same room/session ID after a connection outage
        # or a resumed broadcast. A previous completion is historical state,
        # not evidence that the currently confirmed live stream is finished.
        # _active and the database's unique active constraint prevent parallel
        # recordings; manual stop disables the monitor until explicitly enabled.
        if len(_active) >= MAX_RECORDINGS:
            live_store.update_monitor(sub_id, status='capacity', error='已有 2 路录制，稍后重新检查', next_check_at=_later(60))
            return
        if shutil.disk_usage(config.LIBRARY_ROOT).free < live_recorder.MIN_FREE:
            live_store.update_monitor(sub_id, enabled=0)
            raise live_douyin.LiveError(REASONS['disk_full'])
        live_recorder.ffmpeg_exe()
        url = live_douyin.room_url(info.get('room_url') or monitor['room_url'])
        record = live_store.create_record(sub, url, info)
        owner = {'stop': threading.Event(), 'reason': '', 'session_id': info['session_id'], 'room_url':url}
        _active[sub_id] = owner
        live_store.update_monitor(sub_id, status='recording', error='',
                                  **live_scheduling.cursors(live_store.monitor(sub_id), active=True))
        owner['task'] = asyncio.create_task(_record(record, info, owner))
    except live_douyin.LiveError as error:
        _failure(sub_id, str(error), limited=error.limited)
    except Exception:
        _failure(sub_id, '直播检查或启动录制失败，请检查录播组件；稍后重试')


def request_check(sub_id):
    if _stopping or config.MAINTENANCE_ACTIVE:
        raise ValueError('程序正在退出或维护，请稍后检查')
    monitor = live_store.monitor(sub_id)
    if not monitor or not monitor['enabled']:
        raise ValueError('请先开启此博主的自动录播')
    now = datetime.now(timezone.utc).timestamp()
    if sub_id in _checks or now < _manual_due.get(sub_id, 0):
        return {'already_running': True}
    _manual_due[sub_id] = now + 30
    live_store.update_monitor(sub_id, next_check_at=db.utc_now())
    return {'queued': True}


def stop_recording(sub_id, *, disable=True):
    if disable and live_store.monitor(sub_id):
        live_store.update_monitor(sub_id, enabled=0, status='disabled')
    owner = _active.get(sub_id)
    if owner:
        owner['reason'] = 'manual'
        owner['stop'].set()
        live_store.update_monitor(sub_id, status='finalizing')
    return {'stopping': bool(owner)}


async def retry_publish(identity):
    record = live_store.get_record(identity)
    if not record or record['status'] != 'interrupted' or record['sub_id'] in _active or identity in _publishing:
        raise ValueError('此记录暂不支持重试入库')
    _publishing.add(identity)
    try:
        await asyncio.to_thread(live_recorder.wait_for_previous, record)
        return await _publish(record, 'interrupted', record['reason'] if record['reason'] in REASONS else 'restart')
    finally:
        _publishing.discard(identity)


async def recover():
    for record in live_store.unfinished():
        try:
            live_store.update_record(record['id'], chat_status='interrupted')
            await asyncio.to_thread(live_recorder.wait_for_previous, record)
            await _publish(record, 'interrupted', 'restart')
        except Exception:
            live_store.update_record(record['id'], status='interrupted', reason='restart', finished_at=db.utc_now(),
                error='上次录制中断，文件保留；请在录播记录中重试入库')
        if live_store.monitor(record['sub_id']):
            _failure(record['sub_id'], REASONS['restart'])


async def _loop():
    global _recovering
    _recovering = True
    try:
        await recover()
    finally:
        _recovering = False
    next_sync = 0
    while not _stopping:
        if time.monotonic() >= next_sync and not config.MAINTENANCE_ACTIVE:
            try:
                await live_collections.reconcile()
                await live_tasks.reconcile()
            except Exception:
                print('[live] 录播任务摘要同步暂未完成，将在后台重试')
            next_sync = time.monotonic() + 30
        if not config.MAINTENANCE_ACTIVE:
            now = db.utc_now()
            for monitor in live_store.monitors():
                identity = monitor['sub_id']
                if len(_checks) >= MAX_CHECKS:
                    break
                if monitor['enabled'] and identity not in _checks and monitor['next_check_at'] and monitor['next_check_at'] <= now:
                    task = asyncio.create_task(check(identity))
                    _checks[identity] = task
                    task.add_done_callback(lambda _task, key=identity: _checks.pop(key, None))
        await asyncio.sleep(1)


def start():
    global _runner, _stopping, _recovering
    _stopping = False
    _recovering = True
    if _runner is None or _runner.done():
        _runner = asyncio.create_task(_loop())


async def stop():
    global _runner, _stopping
    _stopping = True
    for owner in list(_active.values()):
        owner['reason'] = 'shutdown'
        owner['stop'].set()
    try:
        if _runner:
            await _runner
    finally:
        _runner = None
        await asyncio.gather(*list(_checks.values()), return_exceptions=True)
        await asyncio.gather(*(owner['task'] for owner in list(_active.values())), return_exceptions=True)
        await live_merge.stop()
        _manual_due.clear()
