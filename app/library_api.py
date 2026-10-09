"""Paged library API; index work never runs inside a list request."""
import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

from . import catalog, config, library_service as service, media_jobs, task_manager
from .security import media_path

router = APIRouter()


@router.get('/api/library/roots')
def roots():
    return {'items': catalog.list_roots(), 'default_root_id': service.default_root_id()}


@router.get('/api/library/entries')
async def entries(root_id: str | None = None, author: str | None = None, q: str = '',
                  date_from: str | None = None, date_to: str | None = None, page: int = 1,
                  page_size: int = 60, status: str | None = None, sort: str = 'desc',
                  platform: str | None = None, media_type: str | None = None):
    try:
        result = await asyncio.to_thread(catalog.list_entries, root_id=root_id, author=author,
            q=q, date_from=date_from, date_to=date_to, page=page, page_size=min(100, page_size),
            status=status, sort=sort, platform=platform, media_type=media_type)
        service.enrich_page(result['items'])
        return result
    except ValueError as error:
        raise HTTPException(400, str(error))


@router.get('/api/library/authors')
def authors(root_id: str | None = None, page: int = 1, page_size: int = 60, q: str = ''):
    return catalog.list_authors(root_id=root_id, page=page, page_size=min(100, page_size), q=q)


@router.get('/api/library/dates')
def dates(root_id: str | None = None, author: str | None = None, q: str = '', group: str = 'month', limit: int = 240):
    try:
        return {'items': catalog.date_groups(root_id=root_id, author=author, q=q, group=group, limit=limit)}
    except ValueError as error:
        raise HTTPException(400, str(error))


@router.get('/api/library/locate')
def locate(task_id: str | None = None, rel_dir: str | None = None, root_id: str | None = None):
    if task_id:
        entry = service.task_entry(task_manager.get_task(task_id))
    elif rel_dir:
        entry = catalog.find_entry(root_id or service.default_root_id(), rel_dir)
    else:
        raise HTTPException(400, '需要任务 ID 或条目路径')
    if not entry:
        raise HTTPException(404, '此内容尚未建立索引，请稍后重试或扫描媒体库')
    return {'entry_id': entry['id'], 'root_id': entry['root_id'], 'author': entry['author'], 'rel_dir': entry['rel_dir']}


@router.get('/api/library/entries/{entry_id}')
async def detail(entry_id: str):
    entry = await asyncio.to_thread(catalog.get_entry, entry_id)
    if not entry:
        raise HTTPException(404, '媒体条目不存在')
    service.enrich_page([entry])
    return entry


@router.post('/api/library/scan')
async def scan(body: dict):
    if config.MAINTENANCE_ACTIVE:
        raise HTTPException(409, '数据维护正在进行，请稍后扫描')
    try:
        return service.start_scan(body.get('root_id'))
    except (KeyError, ValueError) as error:
        raise HTTPException(400, str(error))


@router.get('/api/library/scans')
def scans():
    return {'items': service.list_scans()}


@router.post('/api/library/scans/{scan_id}/cancel')
def cancel(scan_id: str):
    return {'ok': service.cancel_scan(scan_id)}


@router.get('/api/thumb')
async def thumbnail(p: str, w: int = 480, root_id: str | None = None):
    try:
        path = await service.thumbnail(p, w, root_id)
    except (asyncio.TimeoutError, media_jobs.BusyError):
        raise HTTPException(503, '缩略图正在排队，请稍后重试', headers={'Retry-After': '2'})
    except (KeyError, ValueError) as error:
        raise HTTPException(400, str(error))
    if not path:
        raise HTTPException(422, '无法读取此媒体的缩略图，原文件未改变')
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=3600'})


@router.get('/media/roots/{root_id}/{path:path}')
def media(root_id: str, path: str):
    try:
        full = media_path(path, root=service.root_path(root_id), file_only=True)
    except KeyError:
        raise HTTPException(404, '媒体根不存在')
    return FileResponse(full, headers={'X-Content-Type-Options': 'nosniff'})


@router.get('/api/search')
def search(q: str, limit: int = 60):
    return {'q': q, 'results': catalog.list_entries(q=q, page_size=min(100, limit))['items']}


@router.get('/api/library')
def legacy_library(response: Response):
    # Older integrations receive only one bounded page.
    response.headers['X-Library-Page-Limit'] = '60'
    response.headers['Link'] = '</api/library/entries>; rel="successor-version"'
    result = catalog.list_entries(page_size=60)
    groups = {}
    for entry in result['items']:
        group = groups.setdefault(entry['author'], {'name': entry['author'], 'count': 0, 'total_size': 0, 'entries': []})
        group['entries'].append(catalog.get_entry(entry['id']))
        group['count'] += 1
        group['total_size'] += entry['size']
    return list(groups.values())
