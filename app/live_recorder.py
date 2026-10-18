"""Single-file recording, bounded progress and recoverable archive publication."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import threading
import time

from . import archive, config, live_store
from .live_douyin import UA
from .live_worker import CREATE_HIDDEN
from .runtime import InstanceLock, InstanceBusyError
from .windows_runtime import console_python

MIN_FREE = 2 * 1024 ** 3


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        raise ValueError('录播组件不可用，请运行 python -m pip install -r requirements.txt') from None


def folder_for(record):
    root = Path(config.LIBRARY_ROOT).resolve()
    relative = record.get('output_rel') or ''
    folder = (root / relative).resolve()
    if not relative or not folder.is_relative_to(root) or folder == root:
        raise ValueError('录播目录不存在或超出媒体库范围')
    return folder


def prepare(record):
    stamp = datetime.fromisoformat(record['started_at']).astimezone().strftime('%Y-%m-%d-%H-%M')
    entry = archive.begin_entry(config.LIBRARY_ROOT, record['author'], stamp, 'douyin',
                                'live' + record['id'], record['room_url'])
    relative = Path(entry.folder).relative_to(Path(config.LIBRARY_ROOT).resolve()).as_posix()
    live_store.update_record(record['id'], output_rel=relative)
    record['output_rel'] = relative
    return Path(entry.folder)


def record_stream(record, url, stop, *, min_free=MIN_FREE, realtime=False, chat=True):
    folder = prepare(record)
    options = dict(ffmpeg=ffmpeg_exe(), url=url, output=str(folder / 'recording.mp4.part'),
                   min_free=min_free, ua=UA, realtime=realtime)
    if chat:
        options.update(room_url=record['room_url'], session_id=record['session_id'], started_at=record['started_at'])
    worker = subprocess.Popen([console_python(), '-u', '-m', 'app.live_worker'], cwd=config.SOURCE_DIR,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CREATE_HIDDEN)
    latest = {'bytes': 0, 'seconds': 0}
    result = {'result': 'worker_error', 'code': -1}

    def read():
        for line in iter(worker.stdout.readline, b''):
            try:
                data = json.loads(line)
                if 'result' in data:
                    result.update(data)
                elif 'chat' in data:
                    result['chat'] = data['chat']
                else:
                    latest.update(data)
            except (ValueError, TypeError):
                pass
    reader = threading.Thread(target=read, daemon=True)
    try:
        worker.stdin.write((json.dumps(options) + '\n').encode())
        worker.stdin.flush()
        reader.start()
        sent = False
        deadline = None
        while worker.poll() is None:
            if stop.wait(1) and not sent:
                sent = True
                deadline = time.monotonic() + 85
                worker.stdin.close()  # EOF also covers parent process crash.
            if sent:
                time.sleep(.2)
            live_store.update_record(record['id'], bytes=int(latest['bytes']), duration_seconds=float(latest['seconds']))
            if deadline and time.monotonic() > deadline:
                # The worker owns FFmpeg; don't kill the worker and orphan its child.
                raise RuntimeError('录制进程仍在收尾，请等待完成')
        reader.join(timeout=3)
        result.update(bytes=int(latest['bytes']), seconds=float(latest['seconds']))
        return result
    finally:
        if worker.stdin and not worker.stdin.closed:
            worker.stdin.close()
        # All worker input/network waits and stop timeouts are bounded.
        worker.wait(timeout=85)
        worker.stdout.close()


def playable(path):
    if not path.is_file() or path.stat().st_size < 1024:
        return False
    result = subprocess.run([ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-i', str(path), '-map', '0:v:0', '-frames:v', '1', '-f', 'null', '-'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25, creationflags=CREATE_HIDDEN)
    return result.returncode == 0


def finalize(record, status, reason):
    folder = folder_for(record)
    lock = InstanceLock(folder, filename='.recording.lock').acquire()
    try:
        marker = archive.read_manifest(folder)
        if not marker or marker.get('item_id') != 'live' + record['id']:
            raise ValueError('录播存档标识不匹配，未覆盖标记')
        part, final = folder / 'recording.mp4.part', folder / 'recording.mp4'
        source = final if final.exists() else part
        if not playable(source):
            return False  # Keep the partial file for manual diagnosis/recovery.
        if source == part:
            part.rename(final)
        title = record['title'].replace('\r', ' ').replace('\n', ' ')
        quality = {'origin':'原画','uhd':'超清','hd':'高清','sd':'标清','ld':'流畅'}.get(record.get('quality'),'未记录')
        label = {'completed': '下播后结束', 'stopped': '手动停止', 'interrupted': '录制中断，可能缺少部分内容'}[status]
        body = (f"# 抖音直播录播\n\n- **作者**: {record['author']}\n"
                f"- **发布时间**: {record['started_at']}\n- **视频标题**: {title}\n"
                f"- **来源**: {record['room_url']}\n- **录制状态**: {label}\n"
                f"- **实际画质档位**: {quality}（分辨率、帧率及编码沿用直播源）\n"
                f"- **时间说明**: 使用开始录制时间，非直播真实开始时间\n\n{label}。{reason}\n")
        archive.atomic_write_text(folder / 'context.md', body)
        files = ['context.md', 'recording.mp4']
        for name in ('chat.jsonl', 'chat.jsonl.part', 'chat.meta.json', 'chat.events.jsonl', 'chat.events.jsonl.part'):
            path = folder / name
            if path.is_file() and path.stat().st_size:
                files.append(name)
        archive.ArchiveEntry(folder, marker, False).complete(files)
        live_store.update_record(record['id'], bytes=final.stat().st_size)
        return True
    finally:
        lock.release()


def wait_for_previous(record):
    """A crashed parent's worker may be finishing; never rename its active output."""
    folder = folder_for(record)
    deadline = time.monotonic() + 85
    while True:
        try:
            lock = InstanceLock(folder, filename='.recording.lock').acquire()
            lock.release()
            return
        except InstanceBusyError:
            if time.monotonic() >= deadline:
                raise ValueError('上次录制仍在收尾，未改动文件') from None
            time.sleep(.5)
