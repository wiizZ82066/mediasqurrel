"""Verified daily live archives. Sources and attempt logs are never deleted.

Each revision is immutable on disk. Only after video verification, chat rewrite
and indexing succeed does a transaction replace the visible daily archive.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from . import archive, catalog, config, db, live_recorder, live_store

SCHEMA = """
ALTER TABLE live_recordings ADD COLUMN broadcast_started_at TEXT NOT NULL DEFAULT '';
ALTER TABLE live_recordings ADD COLUMN is_aggregate INTEGER NOT NULL DEFAULT 0;
ALTER TABLE live_recordings ADD COLUMN merged_into TEXT;
ALTER TABLE media_entries ADD COLUMN superseded_by TEXT;
CREATE INDEX live_record_merged ON live_recordings(merged_into);
CREATE TABLE live_day_merges (
 sub_id INTEGER NOT NULL, day TEXT NOT NULL, record_id TEXT,
 fingerprint TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 retry_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(sub_id,day)
);
UPDATE live_recordings SET error='直播连接提前结束，平台未确认下播；旧记录未保存更具体的连接原因'
 WHERE status='interrupted' AND reason='disconnect' AND error='';
"""
_task = None
_cancel = threading.Event()
ZONE = 'Asia/Shanghai'


def day_of(record):
    return datetime.fromisoformat(record['started_at']).astimezone(ZoneInfo(ZONE)).date().isoformat()


def sources(sub_id, day):
    with db.connect() as c:
        return [dict(r) for r in c.execute("""SELECT * FROM live_recordings
            WHERE sub_id=? AND is_aggregate=0 AND date(started_at,'+8 hours')=?
            ORDER BY started_at,id""", (sub_id, day))]


def fingerprint(records):
    fields = ('id', 'status', 'output_rel', 'bytes', 'duration_seconds', 'chat_messages', 'entry_id')
    return hashlib.sha256(json.dumps([[r[k] for k in fields] for r in records]).encode()).hexdigest()


def _run(args, cancel, *, timeout=3600):
    # Drain diagnostics to a temporary file, not an unbounded in-memory PIPE.
    import tempfile
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([live_recorder.ffmpeg_exe(), '-hide_banner', '-nostdin', *args],
            stdout=subprocess.DEVNULL, stderr=errors, creationflags=live_recorder.CREATE_HIDDEN)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel.wait(.25) or time.monotonic() >= deadline:
                    raise ValueError('合并已中止，原始片段和上次合并结果保留')
            if process.returncode:
                raise ValueError('视频合并或校验失败，原始片段保留，未替换媒体库内容')
            errors.seek(0)
            return errors.read(65536).decode('utf-8', errors='replace')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def probe(path):
    result = subprocess.run([live_recorder.ffmpeg_exe(), '-hide_banner', '-nostdin', '-i', str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30, creationflags=live_recorder.CREATE_HIDDEN)
    text = result.stderr.decode('utf-8', errors='replace')
    match = re.search(r'Duration: (\d+):(\d+):([\d.]+)', text)
    video = re.search(r'Video: (\w+).*?, (\d{2,5}x\d{2,5})(?:[ ,])', text)
    audio = re.search(r'Audio: (\w+).*?, (\d+) Hz, ([^,\r\n]+)', text)
    if not match or not video:
        raise ValueError('无法确认片段时长或编码，未合并原件')
    duration = int(match[1])*3600 + int(match[2])*60 + float(match[3])
    return duration, (video.groups(), audio.groups() if audio else None)


def _json_lines(path):
    if path.is_symlink():
        raise ValueError('弹幕文件包含链接，未读取或替换原件')
    if not path.is_file():
        return
    with path.open(encoding='utf-8') as stream:
        while True:
            line = stream.readline(1024*1024)
            if not line:
                break
            if not line.endswith('\n'):
                raise ValueError('弹幕文件不完整，未替换原件')
            yield json.loads(line)


def build(records, cancel):
    if any(r['status'] in ('recording', 'finalizing') for r in records):
        raise ValueError('当天仍有录制正在进行')
    parts = []
    for record in records:
        if not record['entry_id']:
            continue
        folder = live_recorder.folder_for(record)
        source = folder/'recording.mp4'
        if source.is_symlink() or not source.is_file():
            raise ValueError('录播原件缺失，未替换已有索引')
        duration, codec = probe(source)
        parts.append((record, source, duration, codec))
    if len(parts) < 2:
        return None
    if len({p[3] for p in parts}) != 1:
        raise ValueError('当天片段的编码、尺寸或音轨不同，无法无损合并；原件保留')
    if shutil.disk_usage(config.LIBRARY_ROOT).free < sum(p[1].stat().st_size for p in parts)*1.1 + live_recorder.MIN_FREE:
        raise ValueError('合并空间不足，需保留原件大小及 2 GiB 余量')
    first, last = records[0], records[-1]
    identity = uuid.uuid5(uuid.NAMESPACE_URL, f"live-day:{first['sub_id']}:{fingerprint(records)}").hex
    entry = archive.begin_entry(config.LIBRARY_ROOT, first['author'], day_of(first), 'douyin',
                                'live'+identity, first['room_url'])
    folder = Path(entry.folder)
    receipt = folder/'.merge-result.json'
    if entry.existing_complete and archive.entry_status(folder,verify_files=True)=='complete':
        value = json.loads(receipt.read_text(encoding='utf-8'))
        if value.get('id')!=identity or value.get('output_rel')!=folder.relative_to(Path(config.LIBRARY_ROOT).resolve()).as_posix():
            raise ValueError('合并恢复记录不一致，原件保留')
        return value
    # This exact manifest belongs to this derived revision, never a source.
    # An interrupted rebuild can discard its own generated temporary files.
    for name in ('recording.mp4.part','recording.mp4','chat.jsonl','chat.events.jsonl',
                 'chat.meta.json','context.md','.merge-result.json','.concat.txt'):
        path=folder/name
        if path.is_symlink():
            raise ValueError('合并目录包含文件链接，未改动')
        path.unlink(missing_ok=True)
    video = folder/'recording.mp4.part'
    concat = folder/'.concat.txt'
    try:
        with concat.open('x', encoding='utf-8') as stream:
            for _, path, duration, _ in parts:
                escaped = path.as_posix().replace("'", "'\\''")
                stream.write(f"file '{escaped}'\nduration {duration:.6f}\n")
        _run(['-v','error','-n','-f','concat','-safe','0','-i',str(concat),'-map','0:v:0','-map','0:a:0?',
              '-c','copy','-map_metadata','-1','-movflags','+faststart','-f','mp4',str(video)], cancel)
        measured, _ = probe(video)
        if abs(measured - sum(p[2] for p in parts)) > 2:
            raise ValueError('合并后时长不一致，原件保留，未切换索引')
        _run(['-v','error','-xerror','-i',str(video),'-map','0:v:0','-map','0:a:0?',
              '-threads','2','-fps_mode','passthrough','-enc_time_base','demux','-f','null','-'], cancel)
        segments, offset, messages = [], 0, 0
        with (folder/'chat.jsonl').open('x',encoding='utf-8') as chats, (folder/'chat.events.jsonl').open('x',encoding='utf-8') as events:
            previous = None
            for record, source, duration, _ in parts:
                source_ms = round(duration*1000)
                # Received wall-clock times are retained. Missing video is not
                # replaced with invented frames or fabricated chat positions.
                gap = max(0, (datetime.fromisoformat(record['started_at']) -
                    (datetime.fromisoformat(previous['started_at']) + timedelta(seconds=previous['duration']))).total_seconds()) if previous else 0
                segments.append(dict(record_id=record['id'], output_rel=record['output_rel'],
                    started_at=record['started_at'], duration=duration, offset_ms=offset,
                    gap_before_seconds=round(gap, 3), status=record['status'], reason=record['reason']))
                for row in _json_lines(source.parent/'chat.jsonl'):
                    if row.get('type') != 'chat':
                        continue
                    original = row.get('video_time_ms')
                    position = offset+original if isinstance(original,(int,float)) and 0 <= original <= source_ms else None
                    # Whitelist only text and timing; no sender or message ID.
                    output = {k: row.get(k) for k in ('type','text','platform_created_at','received_at','recording_elapsed_ms')}
                    output.update(schema_version=3, segment_id=record['id'], source_video_time_ms=original,
                        video_time_ms=position, alignment='unmapped' if position is None else 'source_progress_estimate')
                    chats.write(json.dumps(output, ensure_ascii=False)+'\n')
                    messages += 1
                for row in _json_lines(source.parent/'chat.events.jsonl'):
                    original = row.get('video_time_ms')
                    row.update(segment_id=record['id'], source_video_time_ms=original,
                               video_time_ms=offset+original if isinstance(original,(int,float)) and 0<=original<=source_ms else None)
                    events.write(json.dumps(row,ensure_ascii=False)+'\n')
                events.write(json.dumps(dict(type='capture_event',event='segment_boundary',video_time_ms=offset,
                    segment_id=record['id'],gap_before_seconds=round(gap,3)))+'\n')
                offset += source_ms
                previous = {**record, 'duration':duration}
        has_gaps = any(s['gap_before_seconds']>2 or s['status']=='interrupted' for s in segments) or any(r['chat_status']!='captured' for r in records)
        chat_status = 'captured_with_gaps' if has_gaps else 'captured'
        meta = dict(schema_version=3, status=chat_status, messages=messages, segments=segments,
                    excluded_records=[r['id'] for r in records if not r['entry_id']],
                    timezone=ZONE, day=day_of(first), alignment='Concatenated video offsets; real gaps are listed in segments. Null positions have no matching video.')
        archive.atomic_write_text(folder/'chat.meta.json',json.dumps(meta,ensure_ascii=False,indent=2))
        video.rename(folder/'recording.mp4')
        archive.atomic_write_text(folder/'context.md', f"# 抖音直播 · 每日合并\n\n- **作者**: {first['author']}\n"
            f"- **发布时间**: {first['started_at']}\n- **视频标题**: {day_of(first)} 直播录播\n"
            f"- **来源**: {first['room_url']}\n\n合并 {len(parts)} 个片段，原始文件和每次任务记录保留。"
            + ("本日存在录制或弹幕缺口，未补造缺失内容。" if has_gaps else "")
            + "\n弹幕 video_time_ms 对齐合并视频；实际时间保留在 received_at，缺口及分段对应见 chat.meta.json。\n")
        files = [p.name for p in folder.iterdir() if p.is_file() and not p.name.startswith('.') and p.name!='entry.json' and p.stat().st_size]
        result = dict(id=identity,sub_id=first['sub_id'],room_url=first['room_url'],session_id='day:'+day_of(first),
            previous_id=None, author=first['author'],title=f"{day_of(first)} 直播录播（{len(parts)} 段合并）",
            status='completed' if last['status']=='completed' else last['status'],
            started_at=first['started_at'],finished_at=last['finished_at'],
            output_rel=folder.relative_to(Path(config.LIBRARY_ROOT).resolve()).as_posix(),
            entry_id=entry.marker['entry_id'],bytes=(folder/'recording.mp4').stat().st_size,
            duration_seconds=measured,chat_status=chat_status,chat_messages=messages,reason=last['reason'],
            error='存在录制或弹幕缺口；展开片段记录可查看原因' if has_gaps else '',
            quality=first['quality'],broadcast_started_at=first.get('broadcast_started_at',''),is_aggregate=1,merged_into=None)
        archive.atomic_write_text(receipt,json.dumps(result,ensure_ascii=False))
        entry.complete(files)
        return result
    finally:
        concat.unlink(missing_ok=True)


async def merge_day(sub_id, day):
    from . import library_service, live_tasks, task_manager
    records = sources(sub_id, day)
    if len(records)<2:
        return None
    signature = fingerprint(records)
    with db.connect() as c:
        old = c.execute('SELECT * FROM live_day_merges WHERE sub_id=? AND day=?',(sub_id,day)).fetchone()
    if old and old['fingerprint']==signature and old['record_id']:
        return live_store.get_record(old['record_id'])
    result = await asyncio.to_thread(build, records, _cancel)
    if result is None:
        return None
    if _cancel.is_set() or fingerprint(sources(sub_id,day))!=signature:
        raise ValueError('当天录播已发生变化，保留新合并文件，稍后重新处理')
    scan = library_service.start_entry_scan({'id':'live-'+result['id'], 'output_dir':str(live_recorder.folder_for(result))})
    if scan:
        await library_service._scans[scan['id']]['task']
    entry = catalog.locate_entry(str(live_recorder.folder_for(result)))
    if not entry or entry['archive_status']!='complete':
        raise ValueError('合并文件已保留，索引尚未完成')
    result['entry_id'] = entry['id']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('INSERT INTO live_recordings ('+','.join(result)+') VALUES ('+','.join('?' for _ in result)+')',tuple(result.values()))
        ids = [r['id'] for r in records]
        if old and old['record_id']:
            ids.append(old['record_id'])
        for identity in ids:
            c.execute('UPDATE media_entries SET superseded_by=? WHERE id=(SELECT entry_id FROM live_recordings WHERE id=?)',
                      (entry['id'],identity))
            c.execute('UPDATE live_recordings SET merged_into=? WHERE id=?',(result['id'],identity))
        c.execute('''INSERT INTO live_day_merges(sub_id,day,record_id,fingerprint) VALUES(?,?,?,?)
            ON CONFLICT(sub_id,day) DO UPDATE SET record_id=excluded.record_id,fingerprint=excluded.fingerprint,error='',retry_at='' ''',
            (sub_id,day,result['id'],signature))
    for identity in ids:
        await live_tasks.sync_record(live_store.get_record(identity))
    await live_tasks.sync_record(result)
    await task_manager.broadcast({'type':'library_updated'})
    return result


async def _pending(active_subs):
    with db.connect() as c:
        groups=c.execute("""SELECT sub_id,date(started_at,'+8 hours') day,MAX(finished_at) finished
            FROM live_recordings WHERE is_aggregate=0 GROUP BY sub_id,day HAVING COUNT(entry_id)>1""").fetchall()
    for group in groups:
        if _cancel.is_set() or group['sub_id'] in active_subs:
            continue
        if group['finished'] and datetime.fromisoformat(group['finished'])>datetime.now(timezone.utc)-timedelta(seconds=120):
            continue
        records=sources(group['sub_id'],group['day'])
        signature=fingerprint(records)
        with db.connect() as c:
            state=c.execute('SELECT * FROM live_day_merges WHERE sub_id=? AND day=?',(group['sub_id'],group['day'])).fetchone()
        if state and (state['fingerprint']==signature or state['retry_at']>db.utc_now()):
            continue
        try:
            await merge_day(group['sub_id'],group['day'])
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else '每日合并暂未完成，原片段保留'
            with db.connect() as c:
                c.execute('''INSERT INTO live_day_merges(sub_id,day,error,retry_at) VALUES(?,?,?,?)
                    ON CONFLICT(sub_id,day) DO UPDATE SET error=excluded.error,retry_at=excluded.retry_at''',
                    (group['sub_id'],group['day'],message,(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()))


def tick(active_subs):
    global _task
    if _task is None or _task.done():
        _cancel.clear()
        _task=asyncio.create_task(_pending(active_subs))


async def stop():
    global _task
    _cancel.set()
    try:
        if _task and not _task.done():
            await asyncio.gather(_task,return_exceptions=True)
    finally:
        _task=None


def busy():
    return _task is not None and not _task.done()
