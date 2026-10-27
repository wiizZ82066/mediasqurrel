"""Paged library API; index work never runs inside a list request."""
import asyncio
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse

from . import catalog, config, db, library_service as service, media_jobs, task_manager, local_files, task_files
from . import recording_playback
from .security import media_path
from .media_response import MediaFileResponse

router = APIRouter()


@router.get('/api/library/roots')
def roots():
    return {'items': catalog.list_roots(), 'default_root_id': service.default_root_id()}


@router.get('/api/library/entries')
async def entries(root_id: str | None = None, author: str | None = None, q: str = '',
                  date_from: str | None = None, date_to: str | None = None, page: int = 1,
                  page_size: int = 60, status: str | None = None, sort: str = 'desc',
                  platform: str | None = None, media_type: str | None = None, cursor: str | None = None):
    try:
        result = await asyncio.to_thread(catalog.list_entries, root_id=root_id, author=author,
            q=q, date_from=date_from, date_to=date_to, page=page, page_size=min(100, page_size),
            status=status, sort=sort, platform=platform, media_type=media_type, cursor=cursor)
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
async def locate(task_id: str | None = None, rel_dir: str | None = None, root_id: str | None = None):
    if task_id:
        task = await asyncio.to_thread(task_manager.get_task, task_id)
        if not task:
            raise HTTPException(404, '任务记录不存在')
        entry = await asyncio.to_thread(service.task_entry, task)
        if not entry and service.task_indexing(task):
            return JSONResponse({'status': 'indexing', 'message': '下载已完成，正在同步媒体库索引…',
                                 'retry_after': 1}, status_code=202,
                                headers={'Retry-After': '1', 'Cache-Control': 'no-store'})
        if not entry:
            # The scan may commit after the first lookup, then leave _scans
            # before the pending check. Re-read after observing it is finished.
            entry = await asyncio.to_thread(service.task_entry, task)
        if not entry and (task.get('metadata') or {}).get('diagnostic'):
            raise HTTPException(404, '下载检测文件保存在独立目录，不会加入正常媒体库')
        if not entry and task.get('output_dir') and not service.task_root_id(task):
            raise HTTPException(404, '任务输出目录未登记为媒体库；请先在设置中为该目录建立索引')
    elif rel_dir:
        entry = await asyncio.to_thread(catalog.find_entry, root_id or service.default_root_id(), rel_dir)
    else:
        raise HTTPException(400, '需要任务 ID 或条目路径')
    if not entry:
        raise HTTPException(404, '此内容未建立索引；请查看媒体库扫描记录，处理失败或取消后重新扫描')
    return {'entry_id': entry['id'], 'root_id': entry['root_id'], 'author': entry['author'], 'rel_dir': entry['rel_dir']}


@router.get('/api/library/entries/{entry_id}')
async def detail(entry_id: str):
    entry = await asyncio.to_thread(catalog.get_entry, entry_id)
    if not entry:
        raise HTTPException(404, '媒体条目不存在')
    from .live_collections import enrich_entry
    entry = await asyncio.to_thread(enrich_entry, entry)
    from .library_details import enrich
    entry = await asyncio.to_thread(enrich, entry)
    service.enrich_page([entry])
    return entry


def _recording_target(entry_id, relative, expected=None):
    with db.connect() as connection:
        row = connection.execute('''SELECT e.rel_dir,r.path FROM media_entries e
            JOIN media_roots r ON r.id=e.root_id JOIN media_assets a ON a.entry_id=e.id
            WHERE e.id=? AND a.rel_path=? AND a.kind='video' AND e.availability='present'
            AND EXISTS(SELECT 1 FROM live_recordings l WHERE l.entry_id=e.id
                AND l.status IN ('completed','stopped','interrupted') AND l.is_collection=0)''',
            (entry_id, relative)).fetchone()
    if not row:
        raise HTTPException(404, '此条目没有已完成的录播视频')
    try:
        folder = Path(media_path(row['rel_dir'], root=row['path']))
        path = Path(media_path('/'.join((row['rel_dir'], relative)), root=row['path'], file_only=True))
        if not path.is_relative_to(folder) or path.suffix.lower() != '.mp4':
            raise HTTPException(403, '只能读取此条目内的录播 MP4')
        fingerprint = recording_playback.revision(path)
    except (FileNotFoundError, KeyError):
        raise HTTPException(404, '录播文件已移动或删除') from None
    if expected is not None and expected != fingerprint:
        raise HTTPException(409, '录播文件已改变，请重新打开内容')
    return path, fingerprint


async def _playback_index(path):
    try:
        return await asyncio.to_thread(recording_playback.get_index, path)
    except recording_playback.PlaybackBusy:
        raise HTTPException(503, '正在准备另一个录播的播放索引，请稍后重试', headers={'Retry-After': '1'}) from None
    except OSError:
        raise HTTPException(404, '录播文件暂时无法读取') from None


@router.get('/api/library/entries/{entry_id}/playback')
async def playback_info(entry_id: str, rel: str):
    path, fingerprint = await asyncio.to_thread(_recording_target, entry_id, rel)
    index = await _playback_index(path)
    if index is None:
        return {'kind': 'native'}
    if index['revision'] != fingerprint:
        raise HTTPException(409, '录播文件已改变，请重新打开内容')
    url = f'/api/library/entries/{quote(entry_id, safe="")}/playback/playlist.m3u8?'
    return {'kind': 'hls', 'url': url + urlencode({'rel': rel, 'rev': fingerprint}),
            'duration_seconds': index['duration']}


@router.get('/api/library/entries/{entry_id}/playback/playlist.m3u8')
async def playback_playlist(entry_id: str, rel: str, rev: str):
    path, _ = await asyncio.to_thread(_recording_target, entry_id, rel, rev)
    index = await _playback_index(path)
    if index is None or index['revision'] != rev:
        raise HTTPException(409, '播放索引已失效，请重新打开内容')
    media_url = f'/api/library/entries/{quote(entry_id, safe="")}/playback/media?'
    media_url += urlencode({'rel': rel, 'rev': rev})
    return Response(recording_playback.playlist(index, media_url),
                    media_type='application/vnd.apple.mpegurl',
                    headers={'Cache-Control': 'private, max-age=3600'})


@router.api_route('/api/library/entries/{entry_id}/playback/media', methods=['GET', 'HEAD'])
def playback_media(entry_id: str, rel: str, rev: str):
    path, _ = _recording_target(entry_id, rel, rev)
    return MediaFileResponse(path, media_type='video/mp4',
                             headers={'Cache-Control': 'private, max-age=3600'})


@router.post('/api/library/scan')
async def scan(body: dict):
    if config.MAINTENANCE_ACTIVE:
        raise HTTPException(409, '数据维护正在进行，请稍后扫描')
    try:
        return service.start_scan(body.get('root_id'))
    except (KeyError, ValueError) as error:
        raise HTTPException(400, str(error))


def _entry_target(entry, relative=None):
    if relative is not None and (not isinstance(relative, str) or
            relative not in {asset['rel_path'] for asset in entry['assets']}):
        raise HTTPException(403, '只能定位此条目中已索引的媒体文件')
    root = service.root_path(entry['root_id'])
    folder = media_path(entry['rel_dir'], root=root)
    target = media_path('/'.join([entry['rel_dir'], relative]), root=root, file_only=True) if relative is not None else folder
    # A link to another author's directory must not escape this entry either.
    if relative is not None and not Path(target).is_relative_to(Path(folder)):
        raise HTTPException(403, '媒体文件超出此条目目录')
    if not Path(folder).is_dir():
        raise FileNotFoundError('条目目录已移动')
    return target


@router.post('/api/library/entries/{entry_id}/reveal')
def reveal(entry_id: str, body: dict):
    entry = catalog.get_entry(entry_id)
    if not entry:
        raise HTTPException(404, '媒体条目不存在')
    if set(body) - {'rel'}:
        raise HTTPException(400, '仅接受条目内的相对文件路径')
    relative = body.get('rel')
    try:
        target = _entry_target(entry, relative)
        local_files.reveal_path(target, select_file=relative is not None)
    except KeyError:
        raise HTTPException(404, '媒体根目录不存在') from None
    except FileNotFoundError:
        raise HTTPException(404, '文件或目录已移动，请更新媒体索引') from None
    except NotImplementedError as error:
        raise HTTPException(501, str(error)) from None
    except OSError:
        raise HTTPException(503, '无法打开文件资源管理器，请检查目录是否可用后重试') from None
    return {'ok': True}


@router.post('/api/library/entries/{entry_id}/open')
def open_media(entry_id: str, body: dict):
    if set(body) != {'rel', 'mode'} or not isinstance(body.get('rel'), str) or body.get('mode') not in ('default', 'choose'):
        raise HTTPException(400, '需要条目内的媒体路径 rel 和打开方式 default 或 choose')
    entry = catalog.get_entry(entry_id)
    if not entry:
        raise HTTPException(404, '媒体条目不存在')
    try:
        target = _entry_target(entry, body['rel'])
        cancelled = local_files.open_media(target, mode=body['mode'])
    except KeyError:
        raise HTTPException(404, '媒体根目录不存在') from None
    except FileNotFoundError:
        raise HTTPException(404, '媒体文件已移动，请更新媒体索引') from None
    except local_files.OpenWithBusyError as error:
        raise HTTPException(409, str(error)) from None
    except NotImplementedError as error:
        raise HTTPException(501, str(error)) from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    except OSError:
        raise HTTPException(503, '无法调用本机应用；请检查文件是否可用，或使用“选择打开方式”') from None
    return {'ok': True, 'cancelled': bool(cancelled)}


@router.post('/api/tasks/{task_id}/reveal')
def reveal_task(task_id: str):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, '任务记录不存在')
    try:
        entry = service.task_entry(task)
        target = _entry_target(entry) if entry else task_files.output_directory(task)
        local_files.reveal_path(target, select_file=False)
    except KeyError:
        raise HTTPException(404, '媒体根目录不存在，请检查磁盘或重新建立索引') from None
    except FileNotFoundError:
        raise HTTPException(404, '任务产物目录已移动或不存在，请更新媒体索引') from None
    except NotImplementedError as error:
        raise HTTPException(501, str(error)) from None
    except OSError:
        raise HTTPException(503, '无法打开任务产物目录，请检查目录是否可用后重试') from None
    return {'ok': True}


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
    return MediaFileResponse(full, headers={'X-Content-Type-Options': 'nosniff'})


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
