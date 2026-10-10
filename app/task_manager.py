"""Bounded runtime state backed by TaskStore; logs and history are queried apart."""
import asyncio
import datetime as _dt
import os
import re
import signal
import time
import uuid
from typing import Optional

from fastapi import WebSocket

from . import config, script_registry
from .progress import parse_progress
from .redaction import REDACTED, redact_text, redact_value, redact_task_metadata
from .task_store import TaskStore, MAX_LINE_CHARS

TASKS: dict[str, dict] = {}
TASK_ORDER: list[str] = []
WS_CLIENTS: set[WebSocket] = set()
ON_TASK_DONE: list = []
ON_TASK_FINISHED: list = []
_semaphore: Optional[asyncio.Semaphore] = None
_update_locked_until = 0.0
_store: Optional[TaskStore] = None
_jobs: dict[str, asyncio.Task] = {}
_flush_job = None
_stopping = False
_storage_error = None
_ws_queues = {}
_ws_jobs = {}
_archive_jobs = set()
_resume_enabled = True
TERMINAL = {'success', 'failed', 'cancelled', 'interrupted'}


def _now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def update_state(acquire=False):
    global _update_locked_until
    count = _store.active_count() if _store else sum(t['status'] in ('queued', 'running') for t in TASKS.values())
    if acquire and count == 0:
        _update_locked_until = time.monotonic() + 30
    return {'active': count, 'locked': time.monotonic() < _update_locked_until}


def release_update_lock():
    global _update_locked_until
    _update_locked_until = 0.0


def _sem():
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_TASKS)
    return _semaphore


async def _ws_sender(ws, queue):
    try:
        while ws in WS_CLIENTS:
            message = await queue.get()
            await asyncio.wait_for(ws.send_json(message), timeout=0.5)
    except (Exception, asyncio.CancelledError):
        pass
    finally:
        WS_CLIENTS.discard(ws)
        _ws_queues.pop(ws, None)
        _ws_jobs.pop(ws, None)
        try:
            await asyncio.wait_for(ws.close(code=1013), timeout=0.25)
        except Exception:
            pass


async def broadcast(message):
    """Never let a slow tab block process pipes; reconnect recovers from SQLite."""
    message = redact_value(message)
    for ws in list(WS_CLIENTS):
        queue = _ws_queues.get(ws)
        if queue is None:
            queue = _ws_queues[ws] = asyncio.Queue(maxsize=128)
            _ws_jobs[ws] = asyncio.create_task(_ws_sender(ws, queue))
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            WS_CLIENTS.discard(ws)
            _ws_jobs[ws].cancel()


def public_task(task):
    keys = ('id', 'script_id', 'script_name', 'script_icon', 'params', 'status',
            'progress', 'created_at', 'started_at', 'finished_at', 'exit_code',
            'output_dir', 'output_rel', 'parent_task_id', 'attempt', 'error',
            'content_key', 'metadata', 'hidden')
    result = redact_value({key: task.get(key) for key in keys})
    result['last_log_seq'] = task.get('last_log_seq', task.get('next_log_seq', 1) - 1)
    return result


def _remember(task):
    TASKS[task['id']] = task
    if task['id'] not in TASK_ORDER:
        TASK_ORDER.insert(0, task['id'])
    terminal = [tid for tid in TASK_ORDER if TASKS[tid]['status'] in TERMINAL]
    for tid in terminal[100:]:
        TASKS.pop(tid, None)
        TASK_ORDER.remove(tid)


def _schedule(task_id):
    job = asyncio.create_task(_run(task_id))
    _jobs[task_id] = job
    def done(finished):
        _jobs.pop(task_id, None)
        if not finished.cancelled():
            error = finished.exception()
            if error:
                print('[tasks] ' + redact_text(str(error)))
        if TASKS.get(task_id, {}).get('status') in TERMINAL:
            _pump()
    job.add_done_callback(done)


def _capacity():
    return max(1, min(1000, int(getattr(config, 'MAX_QUEUED_TASKS', 1000))))


def _fill_queue():
    if not _store:
        return
    active_ids = [tid for tid, task in TASKS.items() if task['status'] in ('queued', 'running')]
    while len(active_ids) < _capacity():
        batch = _store.active(limit=min(100, _capacity() - len(active_ids)), status='queued', exclude_ids=active_ids)
        if not batch:
            break
        for task in batch:
            task.update(logs=[], proc=None)
            _remember(task)
            active_ids.append(task['id'])


def _pump():
    if _stopping or _storage_error or not _resume_enabled:
        return
    available = max(1, config.MAX_CONCURRENT_TASKS) - len(_jobs)
    if available <= 0:
        return
    try:
        _fill_queue()
        queued = sorted((task for task in TASKS.values() if task['status'] == 'queued' and task['id'] not in _jobs),
                        key=lambda task: (task['created_at'], task['id']))
        for task in queued[:available]:
            _schedule(task['id'])
    except Exception as error:
        _storage_failed(error)


def _save(task):
    if _store:
        _store.save(task)


def _storage_failed(error):
    global _storage_error
    _storage_error = redact_text(str(error))
    print('[tasks] 持久化失败，暂停创建新任务: ' + _storage_error)


def append_log(task_id, line, stream='stdout'):
    task = TASKS[task_id]
    if _store:
        entry = _store.append(task_id, _now(), stream, line)
    else:  # isolated legacy unit tests; production lifespan must initialize storage
        text = redact_text(line.rstrip('\r\n'))
        entry = {'seq': task.get('_log_seq', 0) + 1, 'time': _now(), 'stream': stream,
                 'text': text[:MAX_LINE_CHARS] + ('… [日志行已截断]' if len(text) > MAX_LINE_CHARS else '')}
        task['_log_seq'] = entry['seq']
    task['last_log_seq'] = entry['seq']
    task.setdefault('logs', []).append(entry)
    del task['logs'][:-50]
    return entry


async def initialize(store=None, *, resume_queued=True):
    """Call after schema migration and before the subscription scheduler starts."""
    global _store, _stopping, _storage_error, _semaphore, _flush_job, _resume_enabled
    if _jobs or (_flush_job and not _flush_job.done()):
        raise RuntimeError('任务运行器已经启动')
    _store = store or TaskStore()
    _stopping, _storage_error, _semaphore = False, None, None
    _resume_enabled = resume_queued
    TASKS.clear()
    TASK_ORDER.clear()
    recovered = []
    after_id = ''
    while True:
        batch = _store.active(limit=100, status='running', after_id=after_id)
        if not batch:
            break
        for task in batch:
            task['logs'], task['proc'] = [], None
            task['status'] = 'interrupted'
            task['finished_at'] = _now()
            task['error'] = '应用退出时任务未完成；可重新下载，不支持断点续传'
            task['progress'] = {**task['progress'], 'label': '任务已中断，可重试'}
            _store.save(task)
            entry = _store.append(task['id'], _now(), 'stderr', task['error'])
            task['last_log_seq'] = entry['seq']
            _store.flush()
            try:
                await asyncio.to_thread(_store.archive, task['id'])
            except OSError as error:
                print('[tasks] 中断任务日志待归档: ' + redact_text(str(error)))
            recovered.append(public_task(task))
            del recovered[:-100]
            _remember(task)
        after_id = batch[-1]['id']
    _fill_queue()
    _flush_job = asyncio.create_task(_flush_loop())
    if resume_queued:
        _pump()
    return recovered


async def _flush_loop():
    while True:
        await asyncio.sleep(0.25)
        try:
            _store.flush()
            for task in list(TASKS.values()):
                if task.pop('_progress_dirty', False) and task['status'] == 'running':
                    _save(task)
        except Exception as error:
            _storage_failed(error)
            for task in list(TASKS.values()):
                if task['status'] == 'running':
                    task['_storage_failure'] = _storage_error
                    await _stop_tree(task.get('proc'))
            return


def _output_params(params):
    values = dict(params)
    output = str(values.get('out') or getattr(config, 'DEFAULT_DOWNLOAD_DIR', None) or config.LIBRARY_ROOT).strip()
    resolver = getattr(config, 'resolve_output_dir', None)
    if resolver:
        values['out'] = resolver(output)
    else:
        output = os.path.expanduser(output)
        if not os.path.isabs(output) and '..' in output.replace('\\', '/').split('/'):
            raise ValueError('输出目录不能包含上级目录跳转')
        values['out'] = os.path.abspath(output if os.path.isabs(output) else os.path.join(config.LIBRARY_ROOT, output))
    if os.path.normcase(os.path.realpath(values['out'])) == os.path.normcase(os.path.realpath(config.RESOURCE_DIR)):
        raise ValueError('媒体输出目录必须独立于项目根目录')
    return values


async def create(script_id, params, *, metadata=None, content_key=None, parent_task_id=None, attempt=1):
    if config.MAINTENANCE_ACTIVE:
        raise ValueError('数据维护正在进行，请完成后再创建任务')
    if _stopping:
        raise ValueError('应用正在退出，请稍后重试')
    if _storage_error:
        raise ValueError('任务持久化不可用，请检查磁盘空间并重启应用')
    if time.monotonic() < _update_locked_until:
        raise ValueError('应用正在安装更新，请稍后重试')
    if content_key:
        existing = _store.by_content_key(content_key) if _store else next(
            (t for t in TASKS.values() if t.get('content_key') == content_key and t['status'] in ('queued', 'running')), None)
        if existing:
            return public_task(existing)
    active_count = _store.active_count() if _store else sum(t['status'] in ('queued', 'running') for t in TASKS.values())
    if active_count >= _capacity():
        raise ValueError(f'下载队列已达容量 {_capacity()}，请等待已有任务完成后重试')
    manifest = script_registry.get(script_id)
    if not manifest:
        raise ValueError(f'未知脚本: {script_id}')
    if not isinstance(params, dict):
        raise ValueError('任务参数必须为对象')
    allowed = {p['name'] for p in manifest.get('params', [])}
    params = _output_params({key: value for key, value in params.items() if key in allowed})
    missing = [p['label'] for p in manifest.get('params', [])
               if p.get('required') and not str(params.get(p['name'], '')).strip()]
    if missing:
        raise ValueError('以下必填项为空: ' + '、'.join(missing))
    task = {'id': uuid.uuid4().hex, 'script_id': script_id, 'script_name': manifest['name'],
            'script_icon': manifest.get('icon', ''), 'params': redact_value(params),
            'command': script_registry.build_command(script_id, params), 'status': 'queued',
            'progress': {'label': '等待下载', 'percent': 0}, 'created_at': _now(),
            'started_at': None, 'finished_at': None, 'exit_code': None,
            'output_dir': None, 'output_rel': None, 'logs': [], 'proc': None,
            'parent_task_id': parent_task_id, 'attempt': attempt, 'error': None,
            'metadata': redact_task_metadata(metadata), 'content_key': content_key}
    try:
        _save(task)
    except Exception:
        if content_key and _store:
            existing = _store.by_content_key(content_key)
            if existing:
                return public_task(existing)
        raise
    _remember(task)
    _pump()
    await broadcast({'type': 'task_update', 'task': public_task(task)})
    return public_task(task)


def _clean_dir_segment(seg):
    seg = re.sub(r'\s*\(\d+\s*bytes?\)\s*$', '', seg.strip())
    for stop in ('\\context.md', '/context.md'):
        seg = seg.split(stop)[0]
    base = seg.rsplit('\\', 1)[-1].rsplit('/', 1)[-1]
    if '.' in base and not seg.endswith(('\\', '/', ':')):
        seg = seg.rsplit('\\', 1)[0] if '\\' in seg else seg.rsplit('/', 1)[0]
    return seg


def _detect_output_dir(task, text):
    if task.get('output_dir'):
        return
    for keyword in ('保存目录:', '下载完成:', '官方封面已保存:', 'context.md 已生成:'):
        if keyword in text:
            segment = _clean_dir_segment(text.split(keyword, 1)[1])
            if segment and os.path.isabs(segment):
                task['output_dir'] = segment
                try:
                    rel = os.path.relpath(segment, config.LIBRARY_ROOT)
                    if rel != '..' and not rel.startswith('..' + os.sep):
                        task['output_rel'] = rel.replace('\\', '/')
                except ValueError:
                    pass
                return


async def _stop_tree(proc):
    if not proc or proc.returncode is not None:
        return
    if os.name == 'nt':
        try:
            killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(proc.pid), '/T', '/F',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=0x08000000)
            await asyncio.wait_for(killer.wait(), timeout=5)
        except Exception:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except asyncio.TimeoutError:
        try:
            if os.name != 'nt':
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        await asyncio.wait_for(proc.wait(), timeout=3)


async def _run(task_id):
    task = TASKS.get(task_id)
    if not task:
        return
    async with _sem():
        if task['status'] != 'queued' or _stopping or _storage_error:
            return
        proc = None
        try:
            task['status'] = 'running'
            task['started_at'] = _now()
            task['progress'] = {'label': '正在解析链接与获取内容…', 'percent': None}
            _save(task)
            await broadcast({'type': 'task_update', 'task': public_task(task)})
            if 'command' not in task:
                if REDACTED in str(task['params']):
                    raise ValueError('原链接包含已脱敏参数，请重新输入链接创建任务')
                task['command'] = script_registry.build_command(task['script_id'], _output_params(task['params']))
            entry = append_log(task_id, '[管理器] 启动 ' + task['script_name'])
            await broadcast({'type': 'task_log', 'task_id': task_id, 'log': entry})
            kwargs = {'creationflags': 0x08000000} if os.name == 'nt' else {'start_new_session': True}
            proc = await asyncio.create_subprocess_exec(*task['command'], stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=config.RESOURCE_DIR, env={**os.environ, 'MS_PROGRESS': '1',
                    'MS_DOWNLOAD_TIMEOUT': str(config.DOWNLOAD_TIMEOUT),
                    'MS_DOWNLOAD_RETRIES': str(config.DOWNLOAD_RETRIES)}, **kwargs)
            task['proc'] = proc
            if task.get('_cancelling') or _stopping:
                await _stop_tree(proc)
            # read() avoids StreamReader.readline's long-line limit and bounds buffering.
            pending = b''
            dropping_long_line = False
            while True:
                chunk = await proc.stdout.read(8192)
                eof = not chunk
                if dropping_long_line:
                    if b'\n' not in chunk:
                        if not chunk:
                            break
                        continue
                    chunk = chunk.split(b'\n', 1)[1]
                    dropping_long_line = False
                pending += chunk
                while b'\n' in pending or len(pending) >= 32768 or (eof and pending):
                    if b'\n' in pending:
                        raw, pending = pending.split(b'\n', 1)
                    else:
                        raw, pending = pending[:32768], pending[32768:]
                        if not eof:
                            dropping_long_line = True
                            pending = b''
                            raw += '… [超长日志行已截断]'.encode('utf-8')
                    line = raw.decode('utf-8', errors='replace')
                    progress = parse_progress(line)
                    if progress is not None:
                        task['progress'] = redact_value(progress)
                        task['_progress_dirty'] = True
                        await broadcast({'type': 'task_progress', 'task_id': task_id, 'progress': task['progress']})
                        continue
                    _detect_output_dir(task, line)
                    task['_progress_dirty'] = True
                    entry = append_log(task_id, line)
                    await broadcast({'type': 'task_log', 'task_id': task_id, 'log': entry})
                if eof:
                    break
            task['exit_code'] = await proc.wait()
            if task.get('_storage_failure'):
                raise OSError(task['_storage_failure'])
            task['status'] = ('cancelled' if task.get('_cancelling') else 'interrupted' if _stopping
                              else 'success' if task['exit_code'] == 0 else 'failed')
            if task['status'] == 'failed':
                task['error'] = f"下载脚本退出码 {task['exit_code']}，请查看日志"
        except asyncio.CancelledError:
            await _stop_tree(proc)
            task['status'] = 'cancelled' if task.get('_cancelling') else 'interrupted'
            task['error'] = '应用退出时任务中断；重试将重新下载'
        except Exception as error:
            await _stop_tree(proc)
            task['status'] = 'cancelled' if task.get('_cancelling') else 'failed'
            task['error'] = redact_text(str(error))
            task['exit_code'] = -1
            try:
                entry = append_log(task_id, '[管理器] 执行异常: ' + task['error'], 'stderr')
                await broadcast({'type': 'task_log', 'task_id': task_id, 'log': entry})
            except Exception as storage_error:
                _storage_failed(storage_error)
        finally:
            task['proc'] = None
            task.pop('command', None)
            task['finished_at'] = _now()
            labels = {'success': '下载完成', 'failed': '下载失败，请查看日志',
                      'cancelled': '下载已取消', 'interrupted': '任务已中断，可重试'}
            task['progress'] = {**task['progress'], 'label': labels.get(task['status'], '下载失败')}
            previous_percent = task['progress'].get('percent')
            if task['status'] == 'success':
                task['progress']['percent'] = 100
            try:
                if _store:
                    _store.flush()
                _save(task)
            except Exception as error:
                _storage_failed(error)
                task['status'], task['error'] = 'failed', '持久化失败: ' + _storage_error
                task['progress'].update(label='持久化失败，请检查磁盘', percent=previous_percent)
                try:
                    _save(task)
                except Exception:
                    pass
            # Start completion side effects (including background indexing)
            # before clients react to success. Do not wait for the full scan.
            callbacks = [*ON_TASK_FINISHED, *(ON_TASK_DONE if task['status'] == 'success' else [])]
            for callback in callbacks:
                try:
                    result = callback(public_task(task))
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as error:
                    print('[tasks] 完成回调异常: ' + redact_text(str(error)))
            await broadcast({'type': 'task_update', 'task': public_task(task)})
            if _store and not _storage_error and not _stopping:
                try:
                    archiving = asyncio.create_task(asyncio.to_thread(_store.archive, task_id))
                    _archive_jobs.add(archiving)
                    archiving.add_done_callback(_archive_jobs.discard)
                    await asyncio.shield(archiving)
                except Exception as error:
                    # SQL logs remain authoritative if archiving fails; never delete them.
                    print('[tasks] 日志归档延后（原日志保留在数据库）: ' + redact_text(str(error)))
            _remember(task)


async def cancel(task_id):
    task = TASKS.get(task_id) or (_store.get(task_id) if _store else None)
    if not task or task['status'] not in ('queued', 'running'):
        return False
    task['_cancelling'] = True
    if task['status'] == 'queued':
        task['status'], task['finished_at'] = 'cancelled', _now()
        task['progress'] = {'label': '下载已取消', 'percent': 0}
        _save(task)
        _remember(task)
        await broadcast({'type': 'task_update', 'task': public_task(task)})
        for callback in ON_TASK_FINISHED:
            result = callback(public_task(task))
            if asyncio.iscoroutine(result):
                await result
        _pump()
    else:
        entry = append_log(task_id, '[管理器] 用户取消：终止下载进程树', 'stderr')
        await broadcast({'type': 'task_log', 'task_id': task_id, 'log': entry})
        await _stop_tree(task.get('proc'))
    return True


async def retry(task_id):
    original = get_task(task_id)
    if not original:
        raise ValueError('原任务不存在')
    if original['status'] not in TERMINAL:
        raise ValueError('任务尚未结束')
    if REDACTED in str(original['params']):
        raise ValueError('原链接包含已脱敏参数，请重新输入链接')
    return await create(original['script_id'], original['params'], metadata=original.get('metadata'),
                        content_key=original.get('content_key'), parent_task_id=original['id'],
                        attempt=original.get('attempt', 1) + 1)


def list_page(*, limit=50, cursor=None, status=None, q=None, include_hidden=False):
    if _store:
        page = _store.list_page(limit=limit, cursor=cursor, status=status, q=q, include_hidden=include_hidden)
        page['items'] = [public_task(task) for task in page['items']]
        return page
    tasks = list_tasks()
    if status:
        tasks = [task for task in tasks if task['status'] == status]
    return {'items': tasks[:min(100, limit)], 'next_cursor': None}


def list_tasks():
    return [public_task(TASKS[tid]) for tid in TASK_ORDER if tid in TASKS]


def get_task(task_id):
    task = TASKS.get(task_id) or (_store.get(task_id) if _store else None)
    if not task:
        return None
    result = public_task(task)
    if _store is None:  # backwards-compatible direct unit-test inspection only
        result['logs'] = task.get('logs', [])
    return result


def read_logs(task_id, *, after=0, limit=200, diagnostic=False, tail=False):
    if _store:
        return _store.read_logs(task_id, after=after, limit=limit, diagnostic=diagnostic, tail=tail)
    if tail:
        after = max(0, TASKS.get(task_id, {}).get('last_log_seq', 0) - limit)
    items = [line for line in TASKS.get(task_id, {}).get('logs', []) if line['seq'] > after]
    return {'items': items[:limit], 'next_cursor': items[min(len(items), limit) - 1]['seq'] if items else after,
            'has_more': len(items) > limit}


def iter_subscription_tasks(batch_size=100):
    if _store:
        yield from _store.iter_subscription_tasks(batch_size)
    else:
        yield from (public_task(task) for task in TASKS.values() if task.get('metadata', {}).get('subscription'))


def delete_record(task_id):
    if not _store:
        raise ValueError('任务存储尚未初始化')
    deleted = _store.delete_record(task_id)
    if deleted:
        TASKS.pop(task_id, None)
        if task_id in TASK_ORDER:
            TASK_ORDER.remove(task_id)
    return deleted


def delete_logs(task_id):
    if not _store:
        raise ValueError('任务存储尚未初始化')
    _store.delete_logs(task_id)
    if task_id in TASKS:
        TASKS[task_id]['logs'] = []


def restore_record(task_id):
    if not _store:
        raise ValueError('任务存储尚未初始化')
    return _store.restore_record(task_id)


async def shutdown():
    global _stopping, _flush_job
    _stopping = True
    if _flush_job:
        _flush_job.cancel()
        await asyncio.gather(_flush_job, return_exceptions=True)
        _flush_job = None
    jobs = list(_jobs.values())
    for job in jobs:
        job.cancel()
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)
    _jobs.clear()
    if _archive_jobs:
        await asyncio.gather(*list(_archive_jobs), return_exceptions=True)
    for task in list(TASKS.values()):
        await _stop_tree(task.get('proc'))
    if _store:
        _store.flush()
    senders = list(_ws_jobs.values())
    for sender in senders:
        sender.cancel()
    await asyncio.gather(*senders, return_exceptions=True)
    _ws_jobs.clear()
    _ws_queues.clear()
    WS_CLIENTS.clear()
