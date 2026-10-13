"""Local-only OS integrations, mounted behind the application's origin guard."""
from fastapi import APIRouter, HTTPException

from . import config, windows_runtime
from .redaction import redact_text

router = APIRouter()


@router.get('/api/settings/startup')
def startup():
    return windows_runtime.startup_status()


@router.patch('/api/settings/startup')
def change_startup(body: dict):
    if config.MAINTENANCE_ACTIVE:
        raise HTTPException(409, '维护期间不能修改自启动设置')
    if set(body) != {'enabled'} or type(body.get('enabled')) is not bool:
        raise HTTPException(400, '仅接受 enabled 布尔值')
    try:
        return windows_runtime.set_startup(body['enabled'])
    except (OSError, RuntimeError, ValueError) as error:
        raise HTTPException(400, redact_text(str(error))) from error


@router.post('/api/settings/open-folder')
def open_directory(body: dict):
    if set(body) != {'kind'} or not isinstance(body.get('kind'), str):
        raise HTTPException(400, '请选择一个目录')
    try:
        windows_runtime.open_directory(body['kind'])
        return {'ok': True}
    except (OSError, RuntimeError, ValueError) as error:
        raise HTTPException(400, redact_text(str(error))) from error
