"""Persistent catalog orchestration and low-priority cover analysis."""
import asyncio
import os
from pathlib import Path
import threading
import uuid

from . import catalog, config, db, media_jobs, media_library, thumbs
from .redaction import redact_text
from .security import media_path

_scans = {}
_default_root_id = None
_stopping = False


def default_root_id():
    return _default_root_id


def start():
    global _default_root_id, _stopping
    _stopping = False
    with db.connect() as connection:
        connection.execute("UPDATE media_scans SET status='interrupted',finished_at=?,error=? WHERE status='running'",
                           (db.utc_now(), '程序退出时索引尚未完成，请重新扫描；旧索引已保留'))
    _default_root_id = catalog.register_root(config.LIBRARY_ROOT, label='默认媒体库')['id']
    media_jobs.start()
    start_scan(_default_root_id)


def start_scan(root_id=None, *, force_followup=False):
    if _stopping:
        raise ValueError('媒体库正在退出')
    root_id = root_id or _default_root_id
    if not catalog.get_root(root_id):
        raise ValueError('媒体根目录不存在')
    for scan_id, state in _scans.items():
        if state['root_id'] == root_id and not state['task'].done():
            if force_followup:
                state['dirty'] = True
            return {'id': scan_id, 'root_id': root_id, 'status': 'running'}
    scan_id = uuid.uuid4().hex
    cancel = threading.Event()
    state = {'root_id': root_id, 'cancel': cancel, 'dirty': False}

    async def run():
        try:
            current_id = scan_id
            while True:
                state['dirty'] = False
                result = await asyncio.to_thread(catalog.scan_root, root_id, cancel=cancel, scan_id=current_id)
                if not state['dirty'] or cancel.is_set() or _stopping:
                    return result
                # Downloads can finish after the scanner enumerated their
                # author. Coalesce requests into another pass before releasing
                # this task's completion callbacks (including task_media links).
                current_id = uuid.uuid4().hex
        except Exception as error:
            print('[catalog] ' + redact_text(str(error)))
            raise

    task = asyncio.create_task(run())
    state['task'] = task
    _scans[scan_id] = state
    def done(finished):
        _scans.pop(scan_id, None)
        if not finished.cancelled():
            finished.exception()
    task.add_done_callback(done)
    return {'id': scan_id, 'root_id': root_id, 'status': 'running'}


def cancel_scan(scan_id):
    state = _scans.get(scan_id)
    if not state:
        return False
    state['cancel'].set()
    return True


def list_scans():
    with db.connect() as connection:
        return [dict(row) for row in connection.execute('SELECT * FROM media_scans ORDER BY started_at DESC LIMIT 20')]


async def stop():
    global _stopping
    _stopping = True
    states = list(_scans.values())
    for state in states:
        state['cancel'].set()
    await asyncio.gather(*(state['task'] for state in states), return_exceptions=True)
    _scans.clear()
    await media_jobs.stop()


def root_path(root_id=None):
    if not root_id:
        return config.LIBRARY_ROOT
    root = catalog.get_root(root_id)
    if not root:
        raise ValueError('媒体根目录不存在')
    return root['path']


def _analyze(entry_id, signature):
    if _stopping or config.MAINTENANCE_ACTIVE:
        return
    entry = catalog.get_entry(entry_id)
    if not entry or entry['signature'] != signature or entry.get('availability') != 'present':
        return
    import cv2
    root = root_path(entry['root_id'])
    best, best_score, best_face, best_kind = None, -1, None, None
    candidates = entry.get('cover_candidates', [])
    if len(candidates) > 12:
        candidates = [candidates[index * (len(candidates) - 1) // 11] for index in range(12)]
    for candidate in candidates:
        if _stopping or config.MAINTENANCE_ACTIVE:
            return
        relative, kind = candidate['rel'], candidate['kind']
        try:
            full = media_path(entry['rel_dir'] + '/' + relative, root=root, file_only=True)
        except Exception:
            continue  # A file can disappear between indexing and background work.
        thumbnail = thumbs.get_thumb(full, width=320)
        if not thumbnail:
            continue
        frame = thumbs._imread_unicode(thumbnail)
        if frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharpness = min(cv2.Laplacian(gray, cv2.CV_64F).var(), 1500) / 1500
        ratio, face = 0, None
        detector = media_library._get_face_detector()
        if detector is not None:
            ratio, face = media_library._detect_face(frame, detector)
        score = .3 + min(ratio * 4, 1) * .35 + sharpness * .25 if ratio else sharpness * .36
        if score > best_score:
            best, best_score, best_face, best_kind = relative, score, face, kind
    if best and not _stopping and not config.MAINTENANCE_ACTIVE:
        if not catalog.update_cover(entry_id, best, 'video' if best_kind.startswith('video') else best_kind,
                                    best_face, signature=signature):
            return  # A newer index revision superseded this analysis.
        return {'id': entry_id, 'cover': best, 'cover_type': 'video' if best_kind.startswith('video') else best_kind,
                'cover_face': best_face, 'cover_signature': signature,
                'cover_live_rel': catalog.live_for_cover(entry.get('assets', ()), best)}


def enrich_page(items):
    if media_jobs.jobs is None or config.MAINTENANCE_ACTIVE:
        return
    for entry in items:
        if entry.get('availability') != 'present' or entry.get('cover_signature') == entry.get('signature'):
            continue
        if not entry.get('cover'):
            continue
        try:
            future = media_jobs.jobs.submit(('cover', entry['id'], entry['signature']), _analyze,
                                           entry['id'], entry['signature'], priority=20)
            if not getattr(future, '_catalog_announced', False):
                future._catalog_announced = True
                def announce(result):
                    if not result.cancelled() and result.exception() is None and result.result():
                        from .task_manager import broadcast
                        asyncio.create_task(broadcast({'type': 'library_cover', 'entry': result.result()}))
                future.add_done_callback(announce)
        except media_jobs.BusyError:
            break


async def thumbnail(relative, width=480, root_id=None):
    full = media_path(relative, root=root_path(root_id), file_only=True)
    stat = os.stat(full)
    width = max(120, min(1280, width))
    if config.MAINTENANCE_ACTIVE:
        cached = thumbs.cached_thumb(full, width=width)
        if cached:
            return cached
        raise media_jobs.BusyError('数据维护期间暂停生成缩略图，请稍后重试')
    key = ('thumb', full, stat.st_mtime_ns, stat.st_size, width)
    if media_jobs.jobs is None:
        raise media_jobs.BusyError('图像服务尚未启动')
    future = media_jobs.jobs.submit(key, thumbs.get_thumb, full, relative, width, priority=0)
    return await asyncio.wait_for(asyncio.shield(future), 30)


def task_entry(task):
    if not task:
        return None
    with db.connect() as connection:
        linked = connection.execute('SELECT entry_id FROM task_media WHERE task_id=?', (task['id'],)).fetchone()
    if linked:
        return catalog.get_entry(linked['entry_id'])
    entry = catalog.locate_entry(task['output_dir']) if task.get('output_dir') else None
    if not entry and task.get('output_rel'):
        entry = catalog.find_entry(_default_root_id, task['output_rel'])
    if entry and not config.MAINTENANCE_ACTIVE:
        with db.connect() as connection:
            connection.execute('INSERT OR REPLACE INTO task_media VALUES (?,?,?)', (task['id'], entry['id'], entry['root_id']))
    return entry
