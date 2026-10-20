"""Private recorder subprocess. Parent pipe EOF always stops FFmpeg.

Credentials arrive over stdin, never this worker's command line or logs. The
parent consumes only numeric progress and fixed result codes. No HTTP API.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from .runtime import InstanceLock

CREATE_HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

ERRORS = {
    'http_forbidden': '直播流拒绝访问（403）；将重新获取直播地址，可检查本地登录态',
    'http_missing': '直播流地址已失效（404）；将重新获取直播地址',
    'http_limited': '直播流请求受限（429）；已降低重试频率',
    'http_server': '直播源暂时不可用（5xx）；稍后重新获取直播地址',
    'network': '直播流连接超时或中断；请检查网络，稍后自动重试',
    'invalid_media': '直播源未返回有效视频数据；稍后重新获取直播地址',
    'unsupported_codec': '当前录播组件不支持直播源编码；可尝试较低画质',
    'disk_full': '视频写入失败：磁盘空间不足；已有文件保留',
    'ffmpeg_error': '录制进程异常退出；已有文件保留，稍后重新检查直播源',
    'no_video': '直播连接结束前未收到视频；稍后重新获取直播地址',
}


def error_code(line):
    """Classify bounded stderr fragments; never persist arbitrary URLs/headers."""
    text = line.lower()
    for code, needles in (
        ('http_forbidden', ('403 forbidden', 'http error 403')),
        ('http_missing', ('404 not found', 'http error 404')),
        ('http_limited', ('429 too many', 'http error 429')),
        ('http_server', ('http error 50', '500 internal', '502 bad', '503 service', '504 gateway')),
        ('disk_full', ('no space left', 'disk full')),
        ('unsupported_codec', ('unsupported codec', 'codec not currently supported')),
        ('invalid_media', ('invalid data found', 'could not find codec parameters')),
        ('network', ('timed out', 'connection reset', 'connection refused', 'error number -10054', 'i/o error')),
    ):
        if any(needle in text for needle in needles):
            return code
    return ''


def command(options):
    args = [options['ffmpeg'], '-hide_banner', '-loglevel', 'error', '-n', '-rw_timeout', '20000000']
    if options.get('realtime'):
        args += ['-re']  # Internal synthetic-file test, never an API parameter.
    if options['url'].startswith(('https://', 'http://')):
        args += ['-user_agent', options['ua'], '-referer', 'https://live.douyin.com/',
                 '-protocol_whitelist', 'http,https,tcp,tls,crypto',
                 '-reconnect_on_network_error', '1', '-reconnect_on_http_error', '500,502,503,504',
                 '-reconnect_max_retries', '2', '-reconnect_delay_max', '3',
                 '-reconnect_delay_total_max', '10']
    args += ['-i', options['url'], '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
             '-map_metadata', '-1', '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
             '-frag_duration', '5000000', '-f', 'mp4', '-progress', 'pipe:1', '-nostats', options['output']]
    return args


def main():
    proc = None
    lock = None
    journal = None
    chat_thread = None
    error_reader = None
    diagnostic = ['']
    stop = threading.Event()
    reason = ['source_end']
    stdout_lock = threading.Lock()

    def emit(data):
        try:
            with stdout_lock:
                print(json.dumps(data), flush=True)
        except (BrokenPipeError, OSError):
            reason[0] = 'parent_exit'
            stop.set()

    try:
        options = json.loads(sys.stdin.readline(65536))
        output = Path(options['output'])
        lock = InstanceLock(output.parent, filename='.recording.lock').acquire()
        if shutil.disk_usage(output.parent).free < options['min_free']:
            emit({'result': 'disk_full', 'code': -1})
            return

        def parent_commands():
            # A stop message OR closing/crashing parent ends recording.
            try:
                sys.stdin.readline(128)
            finally:
                reason[0] = 'stopped'
                stop.set()
        threading.Thread(target=parent_commands, daemon=True).start()
        if stop.is_set():
            emit({'result': 'stopped', 'code': -1})
            return
        proc = subprocess.Popen(command(options), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=CREATE_HIDDEN)

        def errors():
            for fragment in iter(lambda: proc.stderr.readline(8192), b''):
                code = error_code(fragment.decode('utf-8', errors='replace'))
                if code and not diagnostic[0]:
                    diagnostic[0] = code
        error_reader = threading.Thread(target=errors, daemon=True)
        error_reader.start()
        if options.get('room_url'):
            from . import live_chat
            journal = live_chat.Journal(output.parent, options['session_id'], options['started_at'])
            journal.event('video_process_started')
            chat_thread = threading.Thread(target=live_chat.capture, args=(options['room_url'], stop, journal), daemon=True)
            chat_thread.start()

        def progress():
            values = {}
            last_anchor = 0
            for line in iter(proc.stdout.readline, b''):
                key, _, value = line.decode('ascii', errors='ignore').strip().partition('=')
                if key in ('out_time_us', 'total_size'):
                    try:
                        values[key] = max(0, int(value))
                    except ValueError:
                        pass
                if key == 'progress':
                    if journal:
                        journal.video_progress(values.get('out_time_us', 0) / 1000000)
                    emit({'seconds': values.get('out_time_us', 0) / 1000000,
                          'bytes': values.get('total_size', 0)})
                    if journal and time.monotonic() - last_anchor >= 10:
                        journal.event('video_progress', video_seconds=values.get('out_time_us', 0) / 1000000)
                        last_anchor = time.monotonic()
        reader = threading.Thread(target=progress, daemon=True)
        reader.start()
        while proc.poll() is None:
            if stop.wait(1):
                break
            if shutil.disk_usage(output.parent).free < options['min_free']:
                reason[0] = 'disk_full'
                break
        if proc.poll() is None:
            try:
                proc.stdin.write(b'q\n')
                proc.stdin.flush()
                proc.wait(timeout=25)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
                proc.wait(timeout=5)
                if reason[0] != 'disk_full':
                    reason[0] = 'stop_timeout'
        reader.join(timeout=3)
        error_reader.join(timeout=3)
        emit({'result': reason[0], 'code': proc.returncode,
              'error_code': (diagnostic[0] or 'ffmpeg_error') if proc.returncode else ''})
    except Exception:
        # No arbitrary exception text: it can contain URLs, filesystem paths or headers.
        emit({'result': 'worker_error', 'code': -1})
    finally:
        stop.set()
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            if error_reader:
                error_reader.join(timeout=3)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
        if chat_thread:
            chat_thread.join(timeout=35)
        if journal:
            journal.close()
            emit({'chat': {'status': journal.stats['status'], 'messages': journal.stats['messages']}})
        if lock:
            lock.release()


if __name__ == '__main__':
    main()
