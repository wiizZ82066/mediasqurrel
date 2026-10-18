"""One bounded manual refresh batch, independent of scheduled due times."""
import asyncio
from collections import deque

from . import config, db, watcher
from .redaction import redact_text

_task = None
_revision = 0
_state = {'status': 'idle', 'total': 0, 'completed': 0, 'failed': 0,
          'skipped': 0, 'paused': 0, 'queued_ids': [], 'running_ids': []}


def snapshot():
    return {**_state, 'queued_ids': list(_state['queued_ids']), 'running_ids': list(_state['running_ids'])}


async def publish():
    global _revision
    _revision += 1
    _state['revision'] = _revision
    from .task_manager import broadcast
    await broadcast({'type': 'sub_batch', 'batch': snapshot()})


def start(dispatch):
    global _task, _state, _revision
    if config.MAINTENANCE_ACTIVE:
        raise ValueError('数据维护期间不能刷新订阅')
    if _task and not _task.done():
        return {**snapshot(), 'already_running': True}
    subs = watcher.list_subs()
    pending = deque(sub['id'] for sub in subs if sub['enabled'])
    _revision += 1
    _state = {'revision': _revision, 'status': 'running' if pending else 'success', 'started_at': db.utc_now(),
              'total': len(pending), 'completed': 0, 'failed': 0, 'skipped': 0,
              'paused': len(subs) - len(pending), 'queued_ids': list(pending), 'running_ids': []}
    if pending:
        _task = asyncio.create_task(_run(pending, dispatch))
    return snapshot()


async def _run(pending, dispatch):
    async def worker():
        while pending:
            identity = pending.popleft()
            _state['queued_ids'].remove(identity)
            _state['running_ids'].append(identity)
            await publish()
            try:
                sub = watcher.get_sub(identity)
                if not sub or not sub['enabled']:
                    _state['skipped'] += 1
                    continue
                result = await watcher.scan_sub(sub)
                if result.get('already_running'):
                    _state['skipped'] += 1
                elif result.get('error'):
                    _state['failed'] += 1
                elif result.get('new_items'):
                    await dispatch(result['new_items'])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _state['failed'] += 1
                _state['error'] = redact_text(str(error))
            finally:
                _state['running_ids'].remove(identity)
                _state['completed'] += 1
                await publish()
    workers = [asyncio.create_task(worker()) for _ in range(min(len(pending), max(1, min(4, config.MAX_SCAN_CONCURRENCY))))]
    try:
        await asyncio.gather(*workers)
        _state['status'] = 'partial' if _state['failed'] else 'success'
    except asyncio.CancelledError:
        _state['status'] = 'interrupted'
        for job in workers:
            job.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    finally:
        _state['finished_at'] = db.utc_now()
        _state['queued_ids'] = []
        await publish()


async def stop():
    global _task
    if _task and not _task.done():
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    _task = None
