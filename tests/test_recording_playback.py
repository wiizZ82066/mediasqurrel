"""Offline fMP4 fixtures: bounded reads, original timestamps and scoped byte ranges."""
from contextlib import ExitStack
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import catalog, config, db, library_api, recording_playback as playback
from app.security import LocalOnlyMiddleware


def box(kind, body=b''):
    return struct.pack('>I4s', len(body) + 8, kind) + body


def words(*values):
    return struct.pack('>' + 'I' * len(values), *values)


def movie():
    track = box(b'tkhd', words(0, 0, 0, 1)) + box(b'mdia',
        box(b'mdhd', words(0, 0, 0, 1000)) + box(b'hdlr', words(0, 0) + b'vide'))
    return box(b'ftyp', b'iso6') + box(b'moov', box(b'trak', track) +
        box(b'mvex', box(b'trex', words(0, 1, 1, 1000, 0, 0x01010000))))


def fragment(start, *, composition=0, sync=True, payload=16):
    header = box(b'tfhd', words(0x020028, 1, 1000, 0x01010000))
    clock = box(b'tfdt', words(0, start))
    table = box(b'trun', words(0x4, 4, 0x02000000 if sync else 0x01010000))
    if composition:
        # Version 1 permits a signed composition offset for B-frames.
        table = box(b'trun', words(0x01000804, 4, 0x02000000 if sync else 0x01010000)
                    + struct.pack('>iiii', composition, composition, composition, composition))
    return box(b'moof', box(b'traf', header + clock + table)) + box(b'mdat', b'x' * payload)


def recording():
    return movie() + b''.join(fragment(start) for start in (0, 4000, 8000))


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='ms-playback-')))
        self.path = self.base / 'recording.mp4'
        self.path.write_bytes(recording())
        self.cache = self.base / 'cache'
        self.stack.enter_context(patch.object(config, 'CACHE_DIR', str(self.cache)))
        playback._memory.clear()
        self.addCleanup(playback._memory.clear)

    def test_index_skips_payload_and_ranges_preserve_every_original_fragment(self):
        original = movie() + b''.join(fragment(start, payload=1024 * 1024) for start in (0, 4000, 8000))
        self.path.write_bytes(original)
        index = playback.get_index(self.path)
        self.assertLess(index['metadata_bytes'], 1024)
        self.assertEqual(index['duration'], 12)
        self.assertEqual(len(index['segments']), 3)
        self.assertEqual(original[:index['init_length']], movie())
        joined = b''.join(original[offset:offset + length] for offset, length, _ in index['segments'])
        self.assertEqual(joined, original[len(movie()):])
        self.assertEqual(self.path.read_bytes(), original)
        manifest = playback.playlist(index, '/api/media?rev=abc&rel=video.mp4')
        self.assertIn('#EXT-X-MAP:URI="/api/media?rev=abc&rel=video.mp4"', manifest)
        self.assertEqual(manifest.count('#EXT-X-BYTERANGE:'), 3)
        self.assertTrue(manifest.endswith('#EXT-X-ENDLIST\n'))

    def test_composition_offsets_keep_final_frames_and_signed_offsets_do_not_wrap(self):
        self.path.write_bytes(movie() + fragment(0) + fragment(4000, composition=120))
        self.assertAlmostEqual(playback.inspect_recording(self.path)['duration'], 8.12)
        self.path.write_bytes(movie() + fragment(0, composition=-120))
        self.assertAlmostEqual(playback.inspect_recording(self.path)['duration'], 4)

    def test_unsupported_truncated_discontinuous_or_non_sync_files_fall_back(self):
        cases = [box(b'ftyp', b'iso6') + box(b'moov'), recording()[:-1],
                 movie() + fragment(0) + fragment(9000), movie() + fragment(0, sync=False),
                 movie() + fragment(0)[:-24], b'\x00\x00', movie() + fragment(500)]
        for value in cases:
            with self.subTest(size=len(value)):
                self.path.write_bytes(value)
                self.assertIsNone(playback.inspect_recording(self.path))
        with patch.object(playback, 'MAX_READ', 20):
            self.path.write_bytes(recording())
            self.assertIsNone(playback.inspect_recording(self.path))

    def test_disk_cache_survives_restart_and_original_changes_invalidate_it(self):
        first = playback.get_index(self.path)
        playback._memory.clear()
        with patch.object(playback, 'inspect_recording', side_effect=AssertionError('should use disk cache')):
            self.assertEqual(playback.get_index(self.path), first)
        self.path.write_bytes(recording() + fragment(12000))
        second = playback.get_index(self.path)
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertEqual(second['duration'], 16)

    def test_corrupt_cache_is_rebuilt_and_full_disk_does_not_block_playback(self):
        index = playback.get_index(self.path)
        target = self.cache / 'playback' / (index['revision'] + '.json')
        index['segments'][0][1] = self.path.stat().st_size + 1
        target.write_text(json.dumps(index))
        playback._memory.clear()
        with patch.object(Path, 'write_text', side_effect=OSError('disk full')):
            rebuilt = playback.get_index(self.path)
        self.assertEqual(rebuilt['duration'], 12)
        self.assertLess(rebuilt['segments'][0][1], self.path.stat().st_size)
        self.assertFalse(list(self.cache.rglob('*.partial')))

    def test_only_one_index_build_and_bounded_memory(self):
        with playback._slot:
            with self.assertRaises(playback.PlaybackBusy): playback.get_index(self.path)
        for number in range(6):
            path = self.base / f'part-{number}.mp4'; path.write_bytes(recording())
            playback.get_index(path)
        self.assertLessEqual(len(playback._memory), 4)


class PlaybackApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='ms-playback-api-')))
        self.root = self.base / 'library'; self.folder = self.root / 'Example' / '2026-10-20'
        self.folder.mkdir(parents=True)
        for name, value in [('DB_PATH', self.base / 'app.db'), ('LIBRARY_ROOT', self.root),
                            ('CACHE_DIR', self.base / 'cache')]:
            self.stack.enter_context(patch.object(config, name, str(value)))
        db.migrate()
        self.path = self.folder / 'recording.mp4'; self.path.write_bytes(recording())
        (self.folder / 'context.md').write_text('- **作者**: Example\n- **来源**: 抖音直播\n', encoding='utf-8')
        root = catalog.register_root(self.root)
        catalog.scan_root(root['id'])
        self.entry = catalog.list_entries()['items'][0]
        with db.connect() as c:
            c.execute('''INSERT INTO live_recordings(id,room_url,session_id,author,title,status,started_at,entry_id)
                VALUES ('record','https://live.douyin.com/123','session','Example','Title','stopped','now',?)''',
                (self.entry['id'],))
        self.url = '/api/library/entries/' + self.entry['id'] + '/playback'
        app = FastAPI(); app.include_router(library_api.router); app.add_middleware(LocalOnlyMiddleware)
        self.client = self.stack.enter_context(TestClient(app, base_url=f'http://127.0.0.1:{config.PORT}'))
        playback._memory.clear(); self.addCleanup(playback._memory.clear)

    def test_manifest_ranges_and_head_are_exact_and_revision_is_enforced(self):
        descriptor = self.client.get(self.url, params={'rel': 'recording.mp4'}).json()
        self.assertEqual(descriptor['kind'], 'hls')
        manifest = self.client.get(descriptor['url'])
        self.assertEqual(manifest.status_code, 200)
        media = next(line for line in manifest.text.splitlines() if line.startswith('/api/'))
        response = self.client.get(media, headers={'Range': 'bytes=4-31'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, self.path.read_bytes()[4:32])
        head = self.client.head(media, headers={'Range': 'bytes=4-31'})
        self.assertEqual(head.status_code, 206); self.assertEqual(head.content, b'')
        self.assertEqual(head.headers['content-length'], '28')
        self.path.write_bytes(recording() + fragment(12000))
        self.assertEqual(self.client.get(media).status_code, 409)
        self.assertEqual(self.client.get(descriptor['url']).status_code, 409)

    def test_only_exact_completed_recording_assets_and_local_origin_are_allowed(self):
        self.assertEqual(self.client.get(self.url, params={'rel':'../recording.mp4'}).status_code, 404)
        self.assertEqual(self.client.get(self.url, params={'rel':'context.md'}).status_code, 404)
        self.assertEqual(self.client.get(self.url, params={'rel':'recording.mp4'},
            headers={'Origin':'https://example.invalid'}).status_code, 403)
        with db.connect() as c: c.execute("UPDATE live_recordings SET status='recording'")
        self.assertEqual(self.client.get(self.url, params={'rel':'recording.mp4'}).status_code, 404)
        with db.connect() as c: c.execute("UPDATE live_recordings SET status='interrupted'")
        self.assertEqual(self.client.get(self.url, params={'rel':'recording.mp4'}).status_code, 200)
        self.path.unlink()
        self.assertEqual(self.client.get(self.url, params={'rel':'recording.mp4'}).status_code, 404)

    def test_busy_is_retryable_and_unsupported_uses_existing_native_player(self):
        with playback._slot:
            response = self.client.get(self.url, params={'rel':'recording.mp4'})
        self.assertEqual(response.status_code, 503); self.assertEqual(response.headers['retry-after'], '1')
        self.path.write_bytes(box(b'ftyp') + box(b'moov'))
        self.assertEqual(self.client.get(self.url, params={'rel':'recording.mp4'}).json(), {'kind':'native'})
