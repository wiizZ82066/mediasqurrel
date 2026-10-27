"""Small, rebuildable byte-range playlists for completed fragmented recordings.

Only MP4 box headers are read: mdat payloads are skipped with seek. Neither the
recording nor its timestamps are rewritten. Unrecognized layouts use native MP4.
"""
from collections import OrderedDict
import hashlib
import json
import math
from pathlib import Path
import struct
import threading
import time
import uuid

from . import config

VERSION = 2
MAX_BOX = 4 * 1024 * 1024
MAX_READ = 64 * 1024 * 1024
MAX_FRAGMENTS = 100_000
MAX_INDEX = 8 * 1024 * 1024
MAX_CACHE = 64 * 1024 * 1024
_slot = threading.BoundedSemaphore(1)
_guard = threading.Lock()
_memory = OrderedDict()


class UnsupportedRecording(ValueError):
    pass


class PlaybackBusy(RuntimeError):
    pass


def revision(path, stat=None):
    stat = stat or path.stat()
    value = f'{VERSION}:{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}'
    return hashlib.sha256(value.encode()).hexdigest()


def _u32(data, offset):
    return struct.unpack_from('>I', data, offset)[0]


def _u64(data, offset):
    return struct.unpack_from('>Q', data, offset)[0]


def _boxes(data):
    data = memoryview(data)
    offset = 0
    while offset < len(data):
        size, kind = struct.unpack_from('>I4s', data, offset)
        header = 8
        if size == 1:
            size, header = _u64(data, offset + 8), 16
        if size < header or offset + size > len(data):
            raise UnsupportedRecording('Invalid MP4 box bounds')
        yield kind, data[offset + header:offset + size]
        offset += size


def _movie(data):
    children = list(_boxes(data))
    if not any(kind == b'mvex' for kind, _ in children):
        return None
    tracks, defaults = {}, {}
    for kind, body in children:
        if kind == b'mvex':
            for name, value in _boxes(body):
                if name == b'trex':
                    defaults[_u32(value, 4)] = (_u32(value, 12), _u32(value, 20))
        if kind != b'trak':
            continue
        track = {}
        for name, value in _boxes(body):
            if name == b'tkhd':
                track['id'] = _u32(value, 20 if value[0] == 1 else 12)
            elif name == b'mdia':
                for child, content in _boxes(value):
                    if child == b'mdhd':
                        track['scale'] = _u32(content, 20 if content[0] == 1 else 12)
                    elif child == b'hdlr':
                        track['kind'] = bytes(content[8:12])
        if not track.get('id') or not track.get('scale') or track.get('kind') not in (b'vide', b'soun'):
            raise UnsupportedRecording('Unsupported MP4 track')
        tracks[track['id']] = track
    if sum(track['kind'] == b'vide' for track in tracks.values()) != 1 or len(tracks) > 2:
        raise UnsupportedRecording('Expected one video and optional audio track')
    for identity, track in tracks.items():
        track['duration'], track['flags'] = defaults.get(identity, (0, 0))
    return tracks


def _fragment(data, tracks):
    result = []
    for kind, body in _boxes(data):
        if kind != b'traf':
            continue
        children = list(_boxes(body))
        header = next(value for name, value in children if name == b'tfhd')
        flags, identity = _u32(header, 0) & 0xFFFFFF, _u32(header, 4)
        track = tracks[identity]
        if not flags & 0x020000 or flags & 1:
            raise UnsupportedRecording('Fragment offsets are not relative to moof')
        offset = 8 + (4 if flags & 2 else 0)
        default_duration = _u32(header, offset) if flags & 8 else track['duration']
        offset += (4 if flags & 8 else 0) + (4 if flags & 16 else 0)
        default_flags = _u32(header, offset) if flags & 32 else track['flags']
        clock = next(value for name, value in children if name == b'tfdt')
        start = _u64(clock, 4) if clock[0] == 1 else _u32(clock, 4)
        duration, first_flags, presentation_end = 0, None, start
        for name, value in children:
            if name != b'trun':
                continue
            run_flags, count = _u32(value, 0) & 0xFFFFFF, _u32(value, 4)
            if count > 100_000:
                raise UnsupportedRecording('Too many samples in one fragment')
            offset = 8 + (4 if run_flags & 1 else 0)
            run_first_flags = _u32(value, offset) if run_flags & 4 else None
            offset += 4 if run_flags & 4 else 0
            for sample in range(count):
                sample_duration = _u32(value, offset) if run_flags & 0x100 else default_duration
                offset += (4 if run_flags & 0x100 else 0) + (4 if run_flags & 0x200 else 0)
                sample_flags = _u32(value, offset) if run_flags & 0x400 else default_flags
                offset += 4 if run_flags & 0x400 else 0
                composition = (struct.unpack_from('>i' if value[0] == 1 else '>I', value, offset)[0]
                               if run_flags & 0x800 else 0)
                offset += 4 if run_flags & 0x800 else 0
                if not sample_duration or offset > len(value):
                    raise UnsupportedRecording('Missing sample duration or truncated sample table')
                if first_flags is None:
                    first_flags = run_first_flags if sample == 0 and run_first_flags is not None else sample_flags
                presentation_end = max(presentation_end, start + duration + composition + sample_duration)
                duration += sample_duration
        if not duration:
            raise UnsupportedRecording('Empty fragment track')
        independent = (first_flags is not None and not first_flags & 0x10000
                       and (first_flags >> 24) & 3 == 2)
        result.append({'kind': track['kind'], 'start': start / track['scale'],
                       'end': (start + duration) / track['scale'],
                       'presentation_end': presentation_end / track['scale'], 'independent': independent})
    video = next(item for item in result if item['kind'] == b'vide')
    return {**video, 'last_end': max(item['presentation_end'] for item in result)}


def inspect_recording(path):
    """Bounded, metadata-only scan. Return None for a normal or unsupported MP4."""
    stat = path.stat()
    deadline = time.monotonic() + 8
    read_bytes, count, tracks, init_length, pending = 0, 0, None, None, None
    fragments = []
    try:
        # No read-ahead into the video payload when seeking past mdat.
        with path.open('rb', buffering=0) as stream:
            while stream.tell() < stat.st_size:
                position = stream.tell()
                header = stream.read(8)
                read_bytes += len(header)
                size, kind = struct.unpack('>I4s', header)
                header_size = 8
                if size == 1:
                    size = struct.unpack('>Q', stream.read(8))[0]
                    read_bytes += 8
                    header_size = 16
                if size < header_size or position + size > stat.st_size:
                    raise UnsupportedRecording('Truncated recording')
                count += 1
                if count > MAX_FRAGMENTS * 3 or time.monotonic() > deadline:
                    raise UnsupportedRecording('Recording index budget exceeded')
                if kind in (b'moov', b'moof'):
                    if size > MAX_BOX or read_bytes + size > MAX_READ:
                        raise UnsupportedRecording('Recording metadata exceeds budget')
                    body = stream.read(size - header_size)
                    read_bytes += len(body)
                    if kind == b'moov':
                        if tracks is not None or fragments:
                            raise UnsupportedRecording('Unexpected movie header')
                        tracks = _movie(body)
                        if not tracks:
                            return None
                    else:
                        if not tracks or pending or len(fragments) >= MAX_FRAGMENTS:
                            raise UnsupportedRecording('Unexpected fragment')
                        if init_length is None:
                            init_length = position
                            if init_length > MAX_BOX * 2:
                                raise UnsupportedRecording('Initialization range exceeds budget')
                        pending = {**_fragment(body, tracks), 'offset': position}
                elif kind == b'mdat':
                    if not pending:
                        return None
                    pending['length'] = position + size - pending['offset']
                    fragments.append(pending)
                    pending = None
                stream.seek(position + size)
        if pending or not fragments or not fragments[0]['independent'] or abs(fragments[0]['start']) > .25:
            raise UnsupportedRecording('Incomplete or nonzero-start recording')
        segments, current = [], None
        previous = None
        for fragment in fragments:
            if previous and abs(fragment['start'] - previous['end']) > .1:
                # Do not silently normalize timestamp resets or missing intervals.
                raise UnsupportedRecording('Discontinuous recording clock')
            if current and fragment['independent'] and fragment['start'] - current['start'] >= 4:
                segments.append([current['offset'], current['length'], fragment['start'] - current['start']])
                current = None
            if current is None:
                current = dict(fragment)
            current['length'] = fragment['offset'] + fragment['length'] - current['offset']
            previous = fragment
        last_end = max(fragments[-1]['end'], fragments[-1]['last_end'])
        segments.append([current['offset'], current['length'], last_end - current['start']])
        value = {'version': VERSION, 'revision': revision(path, stat), 'size': stat.st_size,
                 'init_length': init_length, 'segments': segments,
                 'duration': sum(segment[2] for segment in segments), 'metadata_bytes': read_bytes}
        if revision(path) != value['revision'] or not _valid(value, value['revision'], stat.st_size):
            raise UnsupportedRecording('Recording changed during indexing')
        return value
    except (UnsupportedRecording, struct.error, KeyError, IndexError, StopIteration):
        return None


def _valid(value, fingerprint, size):
    if not isinstance(value, dict) or value.get('version') != VERSION or value.get('revision') != fingerprint or value.get('size') != size:
        return False
    if type(value.get('init_length')) is not int or not 0 < value['init_length'] <= min(size, MAX_BOX * 2):
        return False
    segments = value.get('segments')
    if not isinstance(segments, list) or not 0 < len(segments) <= MAX_FRAGMENTS:
        return False
    end = value['init_length']
    for segment in segments:
        if not isinstance(segment, list) or len(segment) != 3:
            return False
        offset, length, duration = segment
        if (type(offset) is not int or type(length) is not int or offset < end or length < 8 or
                offset + length > size or length > 32 * 1024 * 1024 or type(duration) not in (float, int) or
                not math.isfinite(duration) or not 0 < duration <= 120):
            return False
        end = offset + length
    duration = value.get('duration')
    return (type(duration) in (float, int) and math.isfinite(duration) and
            abs(duration - sum(segment[2] for segment in segments)) < .001)


def _prune(directory, keep):
    files = sorted((path for path in directory.glob('*.json') if len(path.stem) == 64
                    and all(c in '0123456789abcdef' for c in path.stem)),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    used = keep.stat().st_size
    count = 1
    for path in files:
        if path == keep:
            continue
        used += path.stat().st_size
        count += 1
        if used > MAX_CACHE or count > 128:
            path.unlink(missing_ok=True)


def get_index(path):
    stat = path.stat()
    fingerprint = revision(path, stat)
    directory = Path(config.CACHE_DIR) / 'playback'
    key = (str(directory), fingerprint)
    with _guard:
        if key in _memory:
            _memory.move_to_end(key)
            return _memory[key]
    if not _slot.acquire(blocking=False):
        raise PlaybackBusy('Another playback index is being prepared')
    try:
        target = directory / (fingerprint + '.json')
        value = None
        try:
            if target.stat().st_size <= MAX_INDEX:
                cached = json.loads(target.read_text(encoding='utf-8'))
                if _valid(cached, fingerprint, stat.st_size):
                    value = cached
        except (OSError, ValueError, TypeError):
            pass
        if value is None:
            value = inspect_recording(path)
            if value is not None:
                payload = json.dumps(value, separators=(',', ':'))
                if len(payload) <= MAX_INDEX:
                    temporary = directory / (fingerprint + '.' + uuid.uuid4().hex + '.partial')
                    try:
                        directory.mkdir(parents=True, exist_ok=True)
                        temporary.write_text(payload, encoding='utf-8')
                        temporary.replace(target)
                        _prune(directory, target)
                    except OSError:
                        pass  # Disk-full cache failures do not prevent playback.
                    finally:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            pass
        with _guard:
            _memory[key] = value
            while len(_memory) > 4:
                _memory.popitem(last=False)
        return value
    finally:
        _slot.release()


def playlist(index, media_url):
    # media_url is built exclusively from a validated entry and relative path.
    lines = ['#EXTM3U', '#EXT-X-VERSION:7', '#EXT-X-PLAYLIST-TYPE:VOD',
             '#EXT-X-MEDIA-SEQUENCE:0', '#EXT-X-INDEPENDENT-SEGMENTS',
             f'#EXT-X-TARGETDURATION:{math.ceil(max(segment[2] for segment in index["segments"]))}',
             f'#EXT-X-MAP:URI="{media_url}",BYTERANGE="{index["init_length"]}@0"']
    for offset, length, duration in index['segments']:
        lines += [f'#EXTINF:{duration:.6f},', f'#EXT-X-BYTERANGE:{length}@{offset}', media_url]
    return '\n'.join([*lines, '#EXT-X-ENDLIST', ''])
