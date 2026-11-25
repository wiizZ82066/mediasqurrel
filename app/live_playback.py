"""Read completed fragments of a growing local recording without holding up its writer."""

from collections import OrderedDict
from contextlib import contextmanager
import asyncio
import os
import re
import sqlite3
import struct
import threading
import time
import uuid

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from . import db, live_recorder, recording_playback as mp4, task_files

router = APIRouter(prefix='/api/live/recordings')
_guard = threading.RLock()
_indexes = OrderedDict()
_TARGET_DURATION = 6  # FFmpeg flushes at a keyframe or after five seconds plus a sample boundary.
_WARM_INTERVAL = 12
_WARM_BUDGET = 0.08
_REQUEST_BUDGET = 0.75


def open_reader(path):
    if os.name != 'nt':
        return path.open('rb', buffering=0)
    import msvcrt
    import pywintypes
    import win32con
    import win32file

    # Read, write and delete sharing lets finalize rename .part even when the viewer is reading.
    try:
        handle = win32file.CreateFile(
            str(path),
            win32con.GENERIC_READ,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_ATTRIBUTE_NORMAL,
            None,
        )
    except pywintypes.error as error:
        raise OSError(error.winerror, '录播文件暂时无法读取') from error
    try:
        fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        handle.Close()
        raise
    handle.Detach()  # fd now owns the native handle.
    return os.fdopen(fd, 'rb', buffering=0)


def target(identity):
    if not re.fullmatch('[a-f0-9]{32}', identity):
        raise HTTPException(404, '录播不存在')
    with db.connect(existing=True) as c:
        row = c.execute(
            """SELECT r.id,r.status,r.output_rel,r.entry_id,e.availability FROM live_recordings r
            LEFT JOIN media_entries e ON e.id=r.entry_id WHERE r.id=? AND r.is_collection=0 AND r.is_aggregate=0""",
            (identity,),
        ).fetchone()
    if not row or (row['entry_id'] and row['availability'] != 'present'):
        raise HTTPException(404, '这段录播已移除或不可用')
    if not row['output_rel']:
        if row['status'] in ('recording', 'finalizing'):
            raise waiting()
        raise HTTPException(404, '没有可播放的录播文件')
    try:
        folder = live_recorder.folder_for(dict(row))
    except ValueError:
        raise HTTPException(403, '录播目录无效') from None
    marker = folder / 'entry.json'
    if marker.resolve() != marker:
        raise HTTPException(403, '录播标识无效')
    manifest = task_files._bounded_json(marker)
    if not manifest and row['status'] in ('recording', 'finalizing'):
        raise waiting()
    if not manifest or manifest.get('platform') != 'douyin' or manifest.get('item_id') != 'live' + identity:
        raise HTTPException(404, '尚未找到这段录播的存档标识')
    for name in ('recording.mp4', 'recording.mp4.part'):
        path = folder / name
        if path.resolve() != path:
            raise HTTPException(403, '录播文件位置无效')
        if path.is_file():
            return path, name == 'recording.mp4' or row['status'] in ('completed', 'stopped', 'interrupted')
    if row['status'] in ('recording', 'finalizing'):
        raise waiting()
    raise HTTPException(404, '没有可播放的录播文件')


def waiting():
    return HTTPException(
        503, '正在等待可播放的视频片段', headers={'Retry-After': '1', 'Cache-Control': 'no-store'}
    )


class GrowingIndex:
    def __init__(self, file_id):
        self.file_id = file_id
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.version = uuid.uuid4().hex
        self.offset = self.observed_size = self.init_length = 0
        self.tracks = self.pending = self.previous_end = None
        self.segments = []
        self.manifest = bytearray(
            (
                '#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXT-X-MEDIA-SEQUENCE:0\n'
                f'#EXT-X-TARGETDURATION:{_TARGET_DURATION}\n#EXT-X-MAP:URI="{self.version}/init"\n'
            ).encode('ascii')
        )

    def extend(self, stream, size, budget):
        """Return whether we reached the writer's current boundary, reading only MP4 metadata."""
        self.observed_size = size
        deadline = time.monotonic() + budget
        while self.offset + 8 <= size:
            start = self.offset
            stream.seek(start)
            header = stream.read(8)
            if len(header) != 8:
                break
            length, kind = struct.unpack('>I4s', header)
            header_size = 8
            if length == 1:
                large = stream.read(8)
                if len(large) != 8:
                    break
                length, header_size = struct.unpack('>Q', large)[0], 16
            if length == 0 or start + length > size:
                break  # FFmpeg has not finished writing this box.
            if length < header_size:
                raise mp4.UnsupportedRecording()
            if kind in (b'moov', b'moof'):
                if length > mp4.MAX_BOX:
                    raise mp4.UnsupportedRecording()
                body = stream.read(length - header_size)
                if len(body) != length - header_size:
                    break
                if kind == b'moov':
                    if self.tracks:
                        raise mp4.UnsupportedRecording()
                    self.tracks = mp4._movie(body)
                    if not self.tracks:
                        raise mp4.UnsupportedRecording()
                else:
                    if not self.tracks or self.pending or len(self.segments) >= mp4.MAX_FRAGMENTS:
                        raise mp4.UnsupportedRecording()
                    fragment = mp4._fragment(body, self.tracks)
                    if not self.segments and (not fragment['independent'] or start > 2 * mp4.MAX_BOX):
                        raise mp4.UnsupportedRecording()
                    if self.previous_end is not None and abs(fragment['start'] - self.previous_end) > 0.1:
                        raise mp4.UnsupportedRecording()
                    self.pending = (start, fragment)
            elif kind == b'mdat':
                if not self.pending:
                    raise mp4.UnsupportedRecording()
                pos, fragment = self.pending
                duration = fragment['end'] - fragment['start']
                if not 0 < duration <= _TARGET_DURATION:
                    raise mp4.UnsupportedRecording()
                if not self.segments:
                    self.init_length = pos
                number = len(self.segments)
                self.segments.append((pos, start + length - pos, duration))
                self.manifest.extend(f'#EXTINF:{duration:.6f},\n{self.version}/{number}\n'.encode('ascii'))
                self.previous_end = fragment['end']
                self.pending = None
            self.offset += length
            if time.monotonic() > deadline and self.offset + 8 <= size:
                return False  # There are more existing box headers to index.
        return True


def file_id(stat):
    return (stat.st_dev, stat.st_ino, getattr(stat, 'st_birthtime_ns', 0))


@contextmanager
def locked_index(path, stream, identity):
    key = (identity, path.parent)
    with _guard:
        stat = os.fstat(stream.fileno())
        index = _indexes.get(key)
        if not index or index.file_id != file_id(stat):
            index = _indexes[key] = GrowingIndex(file_id(stat))
        _indexes.move_to_end(key)
        while len(_indexes) > 6:
            _indexes.popitem(last=False)
    with index.lock:
        size = os.fstat(stream.fileno()).st_size
        if size < index.observed_size:
            index.reset()
        yield index, size


def playlist(identity, *, warm_only=False):
    path, ended = target(identity)
    if warm_only and ended:
        return
    try:
        with open_reader(path) as stream:
            with locked_index(path, stream, identity) as (index, size):
                caught_up = index.extend(stream, size, _WARM_BUDGET if warm_only else _REQUEST_BUDGET)
                if warm_only:
                    return
                if not caught_up:
                    raise HTTPException(503, '正在建立录播播放索引', headers={'Retry-After': '0', 'Cache-Control': 'no-store'})
                if not index.segments:
                    raise waiting() if not ended else HTTPException(422, '这段录播没有完整的视频片段')
                manifest = bytes(index.manifest)
    except (mp4.UnsupportedRecording, struct.error, KeyError, IndexError, StopIteration):
        raise HTTPException(422, '这段视频暂不支持追看，可在录制结束后打开回放') from None
    except OSError:
        raise waiting() from None
    if ended:
        manifest += b'#EXT-X-ENDLIST\n'
    return Response(
        manifest,
        media_type='application/vnd.apple.mpegurl',
        headers={'Cache-Control': 'no-store'},
    )


async def warm_recording(identity, owner, finished):
    """Index newly written headers while they are still in the disk cache. Never enumerate the library."""
    previous = 0
    while not finished.is_set():
        try:
            await asyncio.wait_for(finished.wait(), _WARM_INTERVAL)
            return
        except asyncio.TimeoutError:
            pass
        seconds = owner.get('seconds', 0)
        if seconds <= previous or owner['stop'].is_set():
            continue
        previous = seconds
        try:
            await asyncio.to_thread(playlist, identity, warm_only=True)
        except HTTPException as error:
            if error.status_code in (403, 404, 422):
                return
        except (sqlite3.Error, OSError):
            pass  # A temporarily unavailable recording can be checked on its next progress update.


@router.get('/{identity}/watch/playlist.m3u8')
def watch_playlist(identity: str):
    return playlist(identity)


def part_range(header, size):
    if header is None:
        return 0, size - 1
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header) if len(header) <= 64 else None
    if match:
        left, right = match.groups()
        if left:
            start, end = int(left), min(int(right), size - 1) if right else size - 1
            if start <= end:
                return start, end
        elif right and int(right):
            return max(0, size - int(right)), size - 1
    raise HTTPException(416, '视频片段范围无效', headers={'Content-Range': f'bytes */{size}'})


class FragmentResponse(StreamingResponse):
    def __init__(self, stream, start, length, **kwargs):
        self.file = stream

        async def read():
            left = length
            stream.seek(start)
            while left:
                chunk = await anyio.to_thread.run_sync(stream.read, min(left, 128 * 1024))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

        super().__init__(read(), **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.file.close()


@router.api_route('/{identity}/watch/{version}/{part}', methods=['GET', 'HEAD'])
def watch_fragment(identity: str, version: str, part: str, request: Request):
    path, _ = target(identity)
    try:
        stream = open_reader(path)
    except OSError:
        raise waiting() from None
    try:
        with locked_index(path, stream, identity) as (index, _):
            if version != index.version:
                raise HTTPException(409, '录播来源已改变，请重新打开播放器')
            if part == 'init' and index.init_length:
                start, size = 0, index.init_length
            elif re.fullmatch(r'[0-9]{1,6}', part) and int(part) < len(index.segments):
                start, size, _ = index.segments[int(part)]
            else:
                raise HTTPException(404, '该片段尚未就绪')
        header = request.headers.get('range')
        left, right = part_range(header, size)
        length = right - left + 1
        headers = {
            'Content-Length': str(length),
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'private, max-age=300',
        }
        if header is not None:
            headers['Content-Range'] = f'bytes {left}-{right}/{size}'
        options = dict(status_code=206 if header else 200, media_type='video/mp4', headers=headers)
        if request.method == 'HEAD':
            stream.close()
            return Response(**options)
        return FragmentResponse(stream, start + left, length, **options)
    except BaseException:
        stream.close()
        raise
