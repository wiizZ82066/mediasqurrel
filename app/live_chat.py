"""Local, append-only public chat capture alongside a recording.

Uses the normal page's WebSocket connection (the page handles signing/ACKs).
Only chat text and timestamps are retained. Message IDs are used in memory for deduplication. Protocol
field numbers cross-checked against opedium/douyin-live-proto's field reference;
the bounded wire decoder below is independently implemented, not generated code.
"""
from collections import OrderedDict
from datetime import datetime, timezone
import gzip
import io
import json
import os
from pathlib import Path
import queue
import threading
import time
from urllib.parse import urlsplit

from . import archive, browser, douyin_auth

MAX_FRAME = 512 * 1024
MAX_PAYLOAD = 4 * 1024 * 1024


def fields(data):
    if not isinstance(data, bytes) or len(data) > MAX_PAYLOAD:
        raise ValueError('invalid payload')
    offset = 0
    result = {}

    def integer():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                raise ValueError('truncated varint')
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise ValueError('oversized varint')

    count = 0
    while offset < len(data):
        count += 1
        if count > 20000:
            raise ValueError('too many fields')
        tag = integer()
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError('invalid tag')
        if wire == 0:
            value = integer()
        elif wire in (1, 2, 5):
            length = integer() if wire == 2 else 8 if wire == 1 else 4
            if offset + length > len(data):
                raise ValueError('truncated field')
            value = data[offset:offset + length]
            offset += length
        else:
            raise ValueError('unsupported wire type')
        result.setdefault(number, []).append(value)
    return result


def one(data, key, default=b''):
    return data.get(key, [default])[0]


def text(data, key):
    value = one(data, key)
    if not isinstance(value, bytes):
        raise ValueError('invalid text')
    return value.decode('utf-8', errors='strict')


def decode_frame(frame):
    if not isinstance(frame, bytes) or len(frame) > MAX_FRAME:
        raise ValueError('oversized frame')
    push = fields(frame)
    payload = one(push, 8)
    if not payload:
        return []
    if text(push, 6) == 'gzip' or payload[:2] == b'\x1f\x8b':
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            payload = stream.read(MAX_PAYLOAD + 1)
    response = fields(payload)
    messages = []
    for envelope in response.get(1, []):
        message = fields(envelope)
        if text(message, 1) != 'WebcastChatMessage':
            continue
        chat = fields(one(message, 2))
        common = fields(one(chat, 1))
        raw_time = one(common, 4, 0)
        created_at = None
        if isinstance(raw_time, int) and raw_time:
            try:
                created_at = datetime.fromtimestamp(raw_time / (1000 if raw_time > 100000000000 else 1), timezone.utc).isoformat(timespec='milliseconds')
            except (OSError, OverflowError, ValueError):
                pass
        messages.append({'type': 'chat', 'message_id': str(one(common, 2, one(message, 3, 0))),
            'text': text(chat, 3),
            'platform_created_at': created_at, 'platform_created_raw': raw_time})
    return messages


class Journal:
    def __init__(self, folder, session_id, started_at):
        self.folder = Path(folder)
        self.session_id = session_id
        self.started_at = started_at
        self.base = time.monotonic() - max(0, time.time() - datetime.fromisoformat(started_at).timestamp())
        self.queue = queue.Queue(maxsize=16)
        self.closing = threading.Event()
        self.failed = threading.Event()
        self.video_clock = None
        self.clock_lock = threading.Lock()
        self.stats = dict(schema_version=2, session_id=session_id, recording_started_at=started_at,
                          messages=0, duplicates=0, dropped_frames=0, parse_errors=0, connections=0,
                          disconnects=0, status='connecting',
                          alignment='video_time_ms estimates output media position at receipt, from FFmpeg progress; network/buffer delay applies')
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def offer(self, value):
        if isinstance(value, bytes) and len(value) > MAX_FRAME:
            self.stats['dropped_frames'] += 1
            return
        now = time.monotonic()
        with self.clock_lock:
            anchor = self.video_clock
        # Do not invent a moving video clock while the input stream is stalled.
        video_ms = round((anchor[1] + min(2, max(0, now-anchor[0]))) * 1000) if anchor else None
        item = (datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                round((now - self.base) * 1000), video_ms, value)
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self.stats['dropped_frames'] += 1

    def event(self, name, **values):
        self.offer({'type': 'capture_event', 'event': name, **values})

    def video_progress(self, seconds):
        with self.clock_lock:
            self.video_clock = (time.monotonic(), max(0, seconds))

    def _write(self):
        seen = OrderedDict()
        last_sync = 0
        try:
            with (self.folder / 'chat.jsonl.part').open('x', encoding='utf-8', newline='\n') as stream, \
                    (self.folder / 'chat.events.jsonl.part').open('x', encoding='utf-8', newline='\n') as events:
                while not self.closing.is_set() or not self.queue.empty():
                    try:
                        received, elapsed, video_ms, value = self.queue.get(timeout=.5)
                    except queue.Empty:
                        value = None
                    if value is not None:
                        try:
                            rows = decode_frame(value) if isinstance(value, bytes) else [value]
                        except (ValueError, OSError, EOFError, TypeError):
                            self.stats['parse_errors'] += 1
                            rows = [{'type': 'capture_event', 'event': 'decode_gap'}]
                        for row in rows:
                            if row['type'] == 'chat':
                                identity = row.pop('message_id')
                                if identity != '0' and identity in seen:
                                    self.stats['duplicates'] += 1
                                    continue
                                if identity != '0':
                                    seen[identity] = True
                                    if len(seen) > 10000:
                                        seen.popitem(last=False)
                                self.stats['messages'] += 1
                            row.pop('platform_created_raw', None)
                            row.update(schema_version=2, received_at=received,
                                       recording_elapsed_ms=elapsed, video_time_ms=video_ms)
                            destination = stream if row['type'] == 'chat' else events
                            destination.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
                    if time.monotonic() - last_sync >= 2:
                        stream.flush()
                        os.fsync(stream.fileno())
                        events.flush()
                        os.fsync(events.fileno())
                        archive.atomic_write_text(self.folder / 'chat.meta.json', json.dumps(self.stats, ensure_ascii=False, indent=2))
                        last_sync = time.monotonic()
                stream.flush()
                os.fsync(stream.fileno())
                events.flush()
                os.fsync(events.fileno())
                self.stats['status'] = 'captured_with_gaps' if (self.stats['disconnects'] or self.stats['parse_errors']
                    or self.stats['dropped_frames'] or not self.stats['connections']) else 'captured'
            (self.folder / 'chat.jsonl.part').rename(self.folder / 'chat.jsonl')
            (self.folder / 'chat.events.jsonl.part').rename(self.folder / 'chat.events.jsonl')
        except OSError:
            self.failed.set()
            self.stats['status'] = 'write_failed'
        finally:
            try:
                archive.atomic_write_text(self.folder / 'chat.meta.json', json.dumps(self.stats, ensure_ascii=False, indent=2))
            except OSError:
                pass

    def close(self):
        self.event('capture_end')
        self.closing.set()
        self.thread.join(timeout=10)


def capture(room_url, stop, journal):
    """Own ephemeral browser; no reused profile, no messages sent by this code."""
    attempts = 0
    while not stop.is_set():
        attempts += 1
        try:
            with browser.sync_playwright() as p:
                owned = browser.launch_chrome(p)
                try:
                    context = browser.stealth_context(owned)
                    douyin_auth.attach_cookies(context)
                    page = context.new_page()
                    # Receive chat without duplicating the live video download.
                    def route(request):
                        url = request.request.url
                        if request.request.resource_type in ('media', 'image', 'font') or '.flv' in url or '.m3u8' in url:
                            request.abort()
                        else:
                            request.continue_()
                    page.route('**/*', route)
                    last_frame = [time.monotonic()]

                    def socket(ws):
                        parsed = urlsplit(ws.url)
                        host = parsed.hostname or ''
                        if not (host == 'douyin.com' or host.endswith('.douyin.com')) or '/webcast/' not in parsed.path:
                            return
                        journal.stats['connections'] += 1
                        if not journal.failed.is_set():
                            journal.stats['status'] = 'connected'
                        journal.event('connected', attempt=attempts)
                        def frame(data):
                            last_frame[0] = time.monotonic()
                            if isinstance(data, bytes):
                                journal.offer(data)
                        def closed(*_args):
                            if not stop.is_set():
                                journal.stats['disconnects'] += 1
                                journal.event('disconnected')
                        ws.on('framereceived', frame)
                        ws.on('close', closed)
                        ws.on('socketerror', lambda *_: journal.event('socket_error'))
                    page.on('websocket', socket)
                    page.goto(room_url, wait_until='domcontentloaded', timeout=30000)
                    while not stop.is_set():
                        page.wait_for_timeout(500)
                        if time.monotonic() - last_frame[0] > 90:
                            journal.event('connection_gap', reason='no_frames_90_seconds')
                            journal.stats['disconnects'] += 1
                            break
                finally:
                    owned.close()
        except Exception:
            journal.event('connection_gap', reason='browser_or_network_error')
            journal.stats['disconnects'] += 1
        if not stop.is_set():
            stop.wait(min(300, 15 * 2 ** min(attempts - 1, 5)))
