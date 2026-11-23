"""Build a recording receipt from archived metadata, without opening any media or chat files."""
from datetime import datetime, timedelta, timezone

from .live_tasks import CHAT_LABELS
from .mail_media import clean_line
from .redaction import redact_text


ARCHIVED = """SELECT r.id, r.author, r.title, r.status, r.started_at, r.finished_at,
        r.duration_seconds, r.chat_messages, r.chat_status
    FROM live_recordings r JOIN media_entries e ON e.id=r.entry_id
    WHERE r.status IN ('completed','stopped') AND r.is_aggregate=0 AND r.is_collection=0
      AND e.availability='present' AND julianday(r.finished_at) >= julianday(?)"""


def _local_time(value):
    try:
        timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if timestamp.tzinfo is not None:
            return timestamp.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, AttributeError):
        pass
    return '时间未记录'


def receipt(record):
    author = clean_line(redact_text(record['author'] or '', diagnostic=True), 100) or '抖音博主'
    title = clean_line(redact_text(record['title'] or '', diagnostic=True), 100) or '直播录播'
    caption = f'{author} · {title}'
    minutes, seconds = divmod(max(0, int(record['duration_seconds'] or 0)), 60)
    hours, minutes = divmod(minutes, 60)
    result = '已正常下播' if record['status'] == 'completed' else '已手动停止'
    chat = CHAT_LABELS.get(record['chat_status'], '状态待确认')
    body = '\n'.join((
        caption, '', f'{result}，录像已保存到媒体库。',
        f"录制开始（北京时间）  {_local_time(record['started_at'])}",
        f"归档完成（北京时间）  {_local_time(record['finished_at'])}",
        f'本段录像时长  {hours}:{minutes:02d}:{seconds:02d}',
        f"已保存弹幕  {record['chat_messages'] or 0} 条（{chat}）", '',
        '在电脑上打开 Media Squirrel，在「任务」中选择「播放回放」。',
    ))
    return caption, body
