"""Project completed live attempts into task history, never the download queue."""
from . import db, live_recorder, task_manager
from .task_store import TaskStore

LABELS = {'completed': '下播后结束', 'stopped': '已手动停止', 'interrupted': '录制中断'}
CHAT_LABELS = {'captured': '采集已结束', 'captured_with_gaps': '存在采集缺口',
               'pending': '连接未完成', 'unavailable': '采集不可用', 'interrupted': '采集中断',
               'write_failed': '写入失败', 'connected': '采集中'}


async def sync_record(record):
    if record['status'] not in LABELS:
        return
    store = task_manager._store or TaskStore()
    identity = 'live-' + record['id']
    old = store.get(identity, include_deleted=True)
    live = {key: record[key] for key in ('id', 'sub_id', 'status', 'entry_id', 'quality',
                                        'duration_seconds', 'bytes', 'chat_status', 'chat_messages', 'reason')}
    live.update(is_aggregate=record.get('is_aggregate',0),merged_into=record.get('merged_into'),author=record['author'])
    if old and old.get('metadata', {}).get('live') == live and old.get('error', '') == record['error']:
        return
    parent_id = 'live-' + record['previous_id'] if record['previous_id'] else None
    # Follow durable links, independent of task-backfill order or hidden tasks.
    with db.connect() as connection:
        attempt = connection.execute('''WITH RECURSIVE chain(id,previous_id) AS (
            SELECT id,previous_id FROM live_recordings WHERE id=?
            UNION SELECT r.id,r.previous_id FROM live_recordings r JOIN chain c ON r.id=c.previous_id
        ) SELECT COUNT(*) FROM chain''', (record['id'],)).fetchone()[0]
    state = 'success' if record['entry_id'] and record['status'] != 'interrupted' else 'interrupted'
    label = LABELS[record['status']]
    output = None
    if record['output_rel']:
        output = str(live_recorder.folder_for(record))
    task = dict(id=identity, script_id='douyin_live', script_name='抖音录播 · ' + record['author'],
        script_icon='', status=state, params={'url': record['room_url'], 'title': record['title']},
        progress={'label': label, 'percent': 100 if state == 'success' else None},
        created_at=record['started_at'].replace('+00:00', 'Z'),
        started_at=record['started_at'], finished_at=record['finished_at'],
        exit_code=0 if state == 'success' else None, output_dir=output, output_rel=record['output_rel'],
        parent_task_id=parent_id, attempt=max(1, attempt),
        error=record['error'], content_key=None, metadata={'live': live})
    store.save(task)
    message = (f"[录播] {label}；视频 {record['duration_seconds']:.1f} 秒，"
               f"{record['bytes']} 字节；弹幕 {record['chat_messages']} 条（{CHAT_LABELS.get(record['chat_status'], '状态待确认')}）。"
               + ('已关联媒体库。' if record['entry_id'] else '尚未加入媒体库。')
               + (' ' + record['error'] if record['error'] else ''))
    store.append(identity, record['finished_at'] or db.utc_now(), 'system', message)
    store.flush()
    saved = store.get(identity, include_deleted=True)
    if identity in task_manager.TASKS:
        task_manager.TASKS[identity].update(saved)
    if not saved['hidden']:
        await task_manager.broadcast({'type': 'task_update', 'task': task_manager.public_task(saved)})


async def reconcile():
    """Bounded batches also repair a crash between video completion and task save."""
    after = ''
    while True:
        with db.connect() as connection:
            records = [dict(row) for row in connection.execute('''SELECT r.* FROM live_recordings r
                LEFT JOIN tasks t ON t.id='live-' || r.id
                WHERE r.status IN ('completed','stopped','interrupted') AND r.id>?
                AND (t.id IS NULL OR json_extract(t.metadata_json,'$.live.status') IS NOT r.status
                     OR json_extract(t.metadata_json,'$.live.entry_id') IS NOT r.entry_id
                     OR json_extract(t.metadata_json,'$.live.merged_into') IS NOT r.merged_into
                     OR t.error IS NOT r.error)
                ORDER BY r.id LIMIT 50''', (after,))]
        if not records:
            return
        for record in records:
            await sync_record(record)
        after = records[-1]['id']
