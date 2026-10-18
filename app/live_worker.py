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


def command(options):
    args = [options['ffmpeg'], '-hide_banner', '-loglevel', 'error', '-n', '-rw_timeout', '20000000']
    if options.get('realtime'):
        args += ['-re']  # Internal synthetic-file test, never an API parameter.
    if options['url'].startswith(('https://', 'http://')):
        args += ['-user_agent', options['ua'], '-referer', 'https://live.douyin.com/',
                 '-protocol_whitelist', 'http,https,tcp,tls,crypto']
    args += ['-i', options['url'], '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
             '-map_metadata', '-1', '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
             '-frag_duration', '5000000', '-f', 'mp4', '-progress', 'pipe:1', '-nostats', options['output']]
    return args


def main():
    proc = None
    lock = None
    journal = None
    chat_thread = None
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
                                stderr=subprocess.DEVNULL, creationflags=CREATE_HIDDEN)
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
        emit({'result': reason[0], 'code': proc.returncode})
    except Exception:
        # No arbitrary exception text: it can contain URLs, filesystem paths or headers.
        emit({'result': 'worker_error', 'code': -1})
    finally:
        stop.set()
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout):
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
