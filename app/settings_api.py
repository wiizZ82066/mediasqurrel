"""Local preferences, explicit diagnostics and reviewed maintenance actions."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import threading
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from . import catalog, config, db, library_service, maintenance, scheduling, settings, task_manager, watcher
from .archive import atomic_write_text
from . import media_jobs, path_transition
from .redaction import redact_value, redact_text

router = APIRouter()
_work = {}
_paths_pending = False


@router.get('/api/settings')
def current_settings():
    return {'paths': {'library_root': config.LIBRARY_ROOT, 'data_dir': config.DATA_DIR,
                     'default_download_dir': config.DEFAULT_DOWNLOAD_DIR or config.LIBRARY_ROOT,
                     'legacy_locations': config.legacy_locations()},
            'values': settings.load(), 'retention': {'tasks': 'forever', 'logs': 'compressed_forever'},
            'versions': settings.versions(), 'storage': settings.storage_usage(), 'restart_required': _paths_pending}


@router.patch('/api/settings')
def update_settings(body: dict):
    if config.MAINTENANCE_ACTIVE:
        raise HTTPException(409, '维护期间不能修改设置')
    try:
        values = settings.save(body)
        task_manager._semaphore = None
        return {'values': values}
    except ValueError as error:
        raise HTTPException(400, str(error))


@router.get('/api/settings/diagnostics')
def diagnostics():
    return settings.diagnostics()


@router.post('/api/settings/check-browser')
def browser_check():
    return settings.check_browser()


@router.post('/api/settings/check-download')
async def download_check(body: dict):
    if body.get('confirmed') is not True:
        raise HTTPException(400, '需要明确确认这个测试链接')
    script_id = body.get('script_id')
    text = str(body.get('input') or '').strip()
    if script_id not in ('weibo', 'douyin') or not text:
        raise HTTPException(400, '请选择平台并输入你要检测的链接')
    destination = Path(config.DATA_DIR) / 'diagnostics' / 'downloads' / uuid.uuid4().hex
    params = {'url' if script_id == 'weibo' else 'input': text, 'out': str(destination)}
    try:
        return await task_manager.create(script_id, params, metadata={'diagnostic': True})
    except ValueError as error:
        raise HTTPException(400, str(error))


@router.post('/api/settings/diagnostics-export')
def export_diagnostics():
    # Deliberately exclude media text, subscriber identities, raw paths and task
    # parameters. Detailed logs are never included by this generic export.
    data = {'schema_version': 1, 'created_at': db.utc_now(), 'diagnostics': settings.diagnostics()}
    data = redact_value(data, diagnostic=True)
    identity = uuid.uuid4().hex
    path = Path(config.DATA_DIR) / 'diagnostics' / (identity + '.json')
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    return {'download_url': f'/api/settings/diagnostics-export/{identity}'}


@router.get('/api/settings/diagnostics-export/{identity}')
def diagnostic_file(identity: str):
    if len(identity) != 32 or any(char not in '0123456789abcdef' for char in identity):
        raise HTTPException(404, '导出不存在')
    path = Path(config.DATA_DIR) / 'diagnostics' / (identity + '.json')
    if not path.is_file():
        raise HTTPException(404, '导出不存在')
    return FileResponse(path, media_type='application/json', filename='media-squirrel-diagnostics.json')


@router.get('/api/subs/{sub_id}/schedules')
def schedules(sub_id: int):
    return {'items': scheduling.list_schedules(sub_id)}


@router.post('/api/subs/{sub_id}/schedules')
def add_schedule(sub_id: int, body: dict):
    try:
        return scheduling.save_schedule(sub_id, body)
    except (ValueError, KeyError) as error:
        raise HTTPException(400, str(error))


@router.patch('/api/schedules/{schedule_id}')
def change_schedule(schedule_id: str, body: dict):
    with db.connect() as connection:
        row = connection.execute('SELECT sub_id,plan_json FROM subscription_schedules WHERE id=?', (schedule_id,)).fetchone()
    if not row:
        raise HTTPException(404, '计划不存在')
    try:
        return scheduling.save_schedule(row['sub_id'], {**json.loads(row['plan_json']), **body}, schedule_id=schedule_id)
    except ValueError as error:
        raise HTTPException(400, str(error))


@router.delete('/api/schedules/{schedule_id}')
def delete_schedule(schedule_id: str):
    return {'ok': scheduling.delete_schedule(schedule_id)}


@router.get('/api/subs/{sub_id}/schedule-runs')
def schedule_runs(sub_id: int, limit: int = 20, offset: int = 0):
    return {'items': scheduling.list_runs(sub_id, limit=limit, offset=offset)}


@router.get('/api/subs/{sub_id}/scan-options')
def scan_options(sub_id: int):
    return scheduling.scan_options(sub_id)


@router.patch('/api/subs/{sub_id}/scan-options')
def update_scan_options(sub_id: int, body: dict):
    try:
        return scheduling.save_scan_options(sub_id, body)
    except ValueError as error:
        raise HTTPException(400, str(error))


@router.post('/api/maintenance/plan')
def plan(body: dict):
    if config.MAINTENANCE_ACTIVE:
        raise HTTPException(409, '已有维护任务，请等待完成或取消')
    try:
        kind = body.get('kind')
        if kind == 'index_only':
            return maintenance.plan_index(body['source_path'])
        if kind == 'copy':
            return maintenance.plan_copy(body['root_id'], body['destination'])
        if kind == 'backup':
            return maintenance.plan_backup(body['destination'])
        if kind == 'restore':
            return maintenance.plan_restore(body['backup_dir'], body['destination'])
        if kind == 'upgrade_copy':
            return maintenance.plan_upgrade_copy(body['source_database'], body['destination'])
        raise ValueError('未知维护操作')
    except (KeyError, ValueError, OSError) as error:
        raise HTTPException(400, redact_text(str(error)))


@router.get('/api/maintenance/plans')
def plans():
    return {'items': maintenance.list_plans()}


@router.get('/api/maintenance/plans/{plan_id}')
def get_plan(plan_id: str):
    try:
        return maintenance.get_plan(plan_id)
    except KeyError:
        raise HTTPException(404, '维护计划不存在')


@router.post('/api/maintenance/plans/{plan_id}/execute')
async def execute(plan_id: str, body: dict):
    if _paths_pending:
        raise HTTPException(409, '路径配置待重启，请先退出并重启')
    if config.MAINTENANCE_ACTIVE or watcher.has_active_scans() or task_manager.update_state()['active']:
        raise HTTPException(409, '请先等待下载和扫描结束，再执行数据维护')
    plan = get_plan(plan_id)
    token = str(body.get('confirmation_token') or '')
    if not secrets.compare_digest(token, plan['confirmation_token']):
        raise HTTPException(400, '确认信息不匹配，请重新查看预检结果')
    if plan['status'] == 'complete':
        return plan
    if plan['status'] == 'needs_replan' or plan['summary'].get('conflict_count') or plan['summary'].get('missing_count'):
        raise HTTPException(409, '预检发现冲突、缺失或计划已过期，请重新生成计划')
    config.MAINTENANCE_ACTIVE = True
    cancel = threading.Event()
    async def worker():
        try:
            if task_manager._archive_jobs:
                await asyncio.gather(*list(task_manager._archive_jobs), return_exceptions=True)
            # Finish metadata scans before taking a stable backup or rebind.
            await asyncio.gather(*(state['task'] for state in list(library_service._scans.values())), return_exceptions=True)
            if media_jobs.jobs is not None:
                await media_jobs.jobs.queue.join()
            captured = None
            if plan['kind'] in ('copy', 'restore', 'upgrade_copy'):
                captured = await asyncio.to_thread(path_transition.capture_sources, plan_id)
            result = await asyncio.to_thread(maintenance.execute_plan, plan_id, token, cancel=cancel)
            if captured and result['status'] == 'complete':
                result = await asyncio.to_thread(path_transition.seal_plan, plan_id, captured)
            return result
        except Exception as error:
            with db.connect() as connection:
                connection.execute('UPDATE maintenance_plans SET status=?,error=?,finished_at=? WHERE id=?',
                                   ('failed', redact_text(str(error), diagnostic=True)[:500], db.utc_now(), plan_id))
            return maintenance.get_plan(plan_id)
        finally:
            config.MAINTENANCE_ACTIVE = False
            _work.pop(plan_id, None)
    task = asyncio.create_task(worker())
    _work[plan_id] = (task, cancel)
    task.add_done_callback(lambda result: result.exception() if not result.cancelled() else None)
    return {'id': plan_id, 'status': 'running'}


@router.post('/api/maintenance/plans/{plan_id}/cancel')
def cancel(plan_id: str):
    if plan_id in _work:
        _work[plan_id][1].set()
    maintenance.cancel_plan(plan_id)
    return {'ok': True}


async def stop():
    work = list(_work.values())
    for _task, cancel_event in work:
        cancel_event.set()
    await asyncio.gather(*(task for task, _event in work), return_exceptions=True)


@router.post('/api/settings/paths/apply')
async def apply_paths(body: dict):
    global _paths_pending
    if _paths_pending:
        raise HTTPException(409, '路径切换已准备，请先退出并重启')
    if body.get('confirmed') is not True:
        raise HTTPException(400, '需要确认下次启动使用准备好的新目录')
    if config.MAINTENANCE_ACTIVE or watcher.has_active_scans() or task_manager.update_state()['active']:
        raise HTTPException(409, '请等待当前操作完成后再切换路径')
    data_plan = get_plan(body['data_plan_id']) if body.get('data_plan_id') else None
    library_plan = get_plan(body['library_plan_id']) if body.get('library_plan_id') else None
    if not data_plan and not library_plan:
        raise HTTPException(400, '需要已核验完成的复制或恢复计划')
    for item in (data_plan, library_plan):
        if item and item['status'] != 'complete':
            raise HTTPException(400, '计划尚未完成，不能切换路径')
    if data_plan:
        if data_plan['kind'] not in ('restore', 'upgrade_copy'):
            raise HTTPException(400, '数据目录需要恢复或升级副本')
    if library_plan:
        if library_plan['kind'] != 'copy':
            raise HTTPException(400, '媒体目录需要已校验的复制计划')
    config.MAINTENANCE_ACTIVE = True
    try:
        if task_manager._archive_jobs:
            await asyncio.gather(*list(task_manager._archive_jobs), return_exceptions=True)
        await asyncio.gather(*(state['task'] for state in list(library_service._scans.values())), return_exceptions=True)
        if media_jobs.jobs is not None:
            await media_jobs.jobs.queue.join()
        if task_manager._store is not None:
            await asyncio.to_thread(task_manager._store.flush)
        pending = await asyncio.to_thread(path_transition.prepare_transition, data_plan, library_plan)
        await asyncio.to_thread(path_transition.write_pending, pending)
        _paths_pending = True
        return {'restart_required': True, 'data_dir': pending['data_dir'], 'library_root': pending['library_root'],
                'message': '切换已准备；当前数据库引用保持原样。请从托盘退出，再运行 python run.py，启动校验通过后才应用。'}
    except Exception as error:
        config.MAINTENANCE_ACTIVE = False
        raise HTTPException(400, redact_text(str(error)))
