"""Local-only subscription live settings; never exposes stream URLs or cookies."""
import asyncio
import json
import sqlite3
import time

from fastapi import APIRouter, HTTPException

from . import config, db, live_douyin, live_recorder, live_service, live_store, scheduling, watcher, live_collections

router = APIRouter()


def _sub(sub_id):
    sub = watcher.get_sub(sub_id)
    if not sub or sub['platform'] != 'douyin':
        raise HTTPException(404, '仅抖音订阅支持自动录播')
    return sub


@router.get('/api/live/monitors')
def monitors():
    latest = live_store.latest_for_monitors()
    return {'items': [monitor_status(item, latest.get(item['sub_id'])) for item in live_store.monitors()],
            'active': live_service.active_count()}

def enrich_records(records):
    for record in records:
        record['chat'] = {'status': record['chat_status'], 'messages': record['chat_messages']}
        record['can_publish'] = False
        if record.get('is_collection'):
            continue  # Its anchor sidecar describes one source, not the collection.
        try:
            folder = live_recorder.folder_for(record)
            record['can_publish'] = record['status'] == 'interrupted' and not record['entry_id'] and any(
                (folder / name).is_file() and (folder / name).stat().st_size >= 1024
                for name in ('recording.mp4', 'recording.mp4.part'))
            path = folder / 'chat.meta.json'
            if not path.is_symlink() and path.stat().st_size <= 16384:
                summary = json.loads(path.read_text(encoding='utf-8'))
                record['chat'] = {key: summary.get(key) for key in
                                  ('status', 'messages', 'dropped_frames', 'parse_errors', 'disconnects')}
                if record['chat_status'] in ('interrupted', 'write_failed', 'unavailable'):
                    record['chat']['status'] = record['chat_status']
        except (ValueError, OSError):
            pass
    return records


def monitor_status(monitor, record=None):
    if not monitor:
        return None
    result = dict(monitor)
    state = monitor['status']
    owner = live_service._active.get(monitor['sub_id'])
    if owner is not None:
        if owner['stop'].is_set() or (record and record['status'] == 'finalizing'):
            state = 'finalizing'
        elif not record or record['duration_seconds'] <= 0:
            state = 'connecting'
        elif time.monotonic() - owner.get('progress_at', time.monotonic()) > 25:
            state = 'stalled'
        else:
            state = 'recording'
    elif monitor['sub_id'] in live_service._checks:
        state = 'checking'
    elif not monitor['enabled']:
        state = 'disabled'
    elif state in ('recording', 'finalizing'):
        state = 'recovering'
    result['runtime_status'] = state
    if record:
        enrich_records([record])
        result['record'] = {key: record[key] for key in (
            'id', 'title', 'status', 'started_at', 'finished_at', 'bytes', 'duration_seconds',
            'quality', 'entry_id', 'chat', 'error')}
    else:
        result['record'] = None
    return result


@router.get('/api/subs/{sub_id}/live')
def status(sub_id: int, offset: int = 0):
    _sub(sub_id)
    records = enrich_records(live_store.recordings(sub_id, limit=20, offset=offset))
    latest = live_store.latest_for_monitors().get(sub_id)
    with db.connect() as c:
        for record in records:
            if record.get('is_collection'):
                record['parts']=[{k:r[k] for k in ('id','status','started_at','finished_at','duration_seconds',
                    'chat_messages','reason','error','entry_id','gap_before_seconds','gap_basis')}
                    for r in live_collections.parts(c, record['id'])]
            elif record.get('is_aggregate'):
                record['parts']=[{k:r[k] for k in ('id','status','started_at','finished_at','duration_seconds','chat_messages','reason','error','entry_id')}
                    for r in c.execute('SELECT * FROM live_recordings WHERE merged_into=? AND is_aggregate=0 ORDER BY started_at',(record['id'],))]
    return {'monitor': monitor_status(live_store.monitor(sub_id), latest), 'records': records,
            'merge_errors':[], 'merging':False}


@router.patch('/api/subs/{sub_id}/live')
async def configure(sub_id: int, body: dict):
    _sub(sub_id)
    if config.MAINTENANCE_ACTIVE or sub_id in live_service._active or sub_id in live_service._checks:
        raise HTTPException(409, '请等待直播检查结束，或先停止当前录制')
    try:
        if not {'enabled','interval_seconds'} <= set(body) or not set(body) <= {'room_url','enabled','interval_seconds','source','interval_enabled','plan','quality'} or type(body['enabled']) is not bool:
            raise ValueError('请填写录播开关与检查计划')
        interval = body['interval_seconds']
        if type(interval) is not int or not 60 <= interval <= 86400:
            raise ValueError('检查间隔需为 1 分钟至 24 小时')
        source = body.get('source', 'room' if body.get('room_url') else 'profile')
        quality = body.get('quality','best')
        if quality not in ('best','origin','uhd','hd','sd','ld'):
            raise ValueError('不支持的录制画质')
        if source not in ('profile','room') or type(body.get('interval_enabled',True)) is not bool:
            raise ValueError('直播检查方式无效')
        url = live_douyin.room_url(body.get('room_url')) if source == 'room' else ''
        plan = scheduling.normalize_plan(body['plan']) if body.get('plan') else None
        if plan and (plan['kind']=='interval' or not plan['enabled']):
            raise ValueError('请选择有效的固定时间计划')
        if not body.get('interval_enabled',True) and not plan:
            raise ValueError('请至少选择间隔检查或固定时间计划')
        if body['enabled']:
            await asyncio.to_thread(live_recorder.ffmpeg_exe)
            if config.MAINTENANCE_ACTIVE or sub_id in live_service._active or sub_id in live_service._checks:
                raise ValueError('当前操作状态已改变，请等待后重试')
            if url and any(m['sub_id'] != sub_id and m['enabled'] and m['room_url'] == url for m in live_store.monitors()):
                raise ValueError('该直播间已在另一订阅中启用录播')
        return live_store.configure(sub_id, url, body['enabled'], interval, source=source,
                                    interval_enabled=body.get('interval_enabled',True),plan=plan,quality=quality)
    except (ValueError, sqlite3.IntegrityError) as error:
        raise HTTPException(400, str(error) if isinstance(error, ValueError) else '直播间配置冲突') from None


@router.post('/api/subs/{sub_id}/live/check')
async def check(sub_id: int):
    _sub(sub_id)
    try:
        return live_service.request_check(sub_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post('/api/subs/{sub_id}/live/stop')
async def stop(sub_id: int):
    _sub(sub_id)
    return live_service.stop_recording(sub_id)


@router.post('/api/live/records/{identity}/publish')
async def publish(identity: str):
    if config.MAINTENANCE_ACTIVE or live_service.busy():
        raise HTTPException(409, '请等待录播或维护完成后再重试入库')
    try:
        return {'published': await live_service.retry_publish(identity)}
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
