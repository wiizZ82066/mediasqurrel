"""Local-only subscription live settings; never exposes stream URLs or cookies."""
import asyncio
import json
import sqlite3

from fastapi import APIRouter, HTTPException

from . import config, live_douyin, live_recorder, live_service, live_store, watcher

router = APIRouter()


def _sub(sub_id):
    sub = watcher.get_sub(sub_id)
    if not sub or sub['platform'] != 'douyin':
        raise HTTPException(404, '仅抖音订阅支持自动录播')
    return sub


@router.get('/api/live/monitors')
def monitors():
    return {'items': live_store.monitors(), 'active': live_service.active_count()}


@router.get('/api/subs/{sub_id}/live')
def status(sub_id: int, offset: int = 0):
    _sub(sub_id)
    records = live_store.recordings(sub_id, limit=20, offset=offset)
    for record in records:
        record['chat'] = {'status': record['chat_status'], 'messages': record['chat_messages']}
        try:
            path = live_recorder.folder_for(record) / 'chat.meta.json'
            if not path.is_symlink() and path.stat().st_size <= 16384:
                summary = json.loads(path.read_text(encoding='utf-8'))
                record['chat'] = {key: summary.get(key) for key in
                                  ('status', 'messages', 'dropped_frames', 'parse_errors', 'disconnects')}
                if record['chat_status'] in ('interrupted', 'write_failed', 'unavailable'):
                    record['chat']['status'] = record['chat_status']
        except (ValueError, OSError):
            pass
    return {'monitor': live_store.monitor(sub_id), 'records': records}


@router.patch('/api/subs/{sub_id}/live')
async def configure(sub_id: int, body: dict):
    _sub(sub_id)
    if config.MAINTENANCE_ACTIVE or sub_id in live_service._active or sub_id in live_service._checks:
        raise HTTPException(409, '请等待直播检查结束，或先停止当前录制')
    try:
        if set(body) != {'room_url', 'enabled', 'interval_seconds'} or type(body['enabled']) is not bool:
            raise ValueError('请填写直播链接、录播开关与检查间隔')
        interval = body['interval_seconds']
        if type(interval) is not int or not 60 <= interval <= 3600:
            raise ValueError('检查间隔需为 60–3600 秒')
        url = live_douyin.room_url(body['room_url'])
        if body['enabled']:
            await asyncio.to_thread(live_recorder.ffmpeg_exe)
            if config.MAINTENANCE_ACTIVE or sub_id in live_service._active or sub_id in live_service._checks:
                raise ValueError('当前操作状态已改变，请等待后重试')
            if any(m['sub_id'] != sub_id and m['enabled'] and m['room_url'] == url for m in live_store.monitors()):
                raise ValueError('该直播间已在另一订阅中启用录播')
        return live_store.configure(sub_id, url, body['enabled'], interval)
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
