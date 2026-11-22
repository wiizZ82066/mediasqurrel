"""An independent reader can rewind a growing recording while its writer appends and publishes it."""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urljoin

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, db, live_playback, live_service
from app.security import LocalOnlyMiddleware
from test_recording_playback import movie, fragment


class LiveWatchTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        base = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='ms-live-watch-')))
        root = base / 'library'
        self.folder = root / 'sample'
        self.folder.mkdir(parents=True)
        for name, path in [('LIBRARY_ROOT', root), ('DB_PATH', base / 'db' / 'app.db')]:
            stack.enter_context(patch.object(config, name, str(path)))
        stack.enter_context(patch.object(live_playback, '_indexes', live_playback.OrderedDict()))
        db.migrate()
        self.identity = 'a' * 32
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO live_recordings(id,room_url,session_id,author,title,status,started_at,output_rel)
                VALUES (?, '', 'sample', 'Sample', 'Title', 'recording', 'now', 'sample')""",
                (self.identity,),
            )
        self.marker = self.folder / 'entry.json'
        self.marker.write_text(
            json.dumps({'platform': 'douyin', 'item_id': 'live' + self.identity}), encoding='utf-8'
        )
        self.path = self.folder / 'recording.mp4.part'
        self.url = '/api/live/recordings/' + self.identity + '/watch/playlist.m3u8'
        app = FastAPI()
        app.include_router(live_playback.router)
        app.add_middleware(LocalOnlyMiddleware)
        self.client = stack.enter_context(TestClient(app, base_url=f'http://127.0.0.1:{config.PORT}'))

    def test_partial_fragment_waits_then_becomes_readable_without_changing_prior_urls(self):
        one, two = fragment(0), fragment(4000)
        self.path.write_bytes(movie() + one[:-2])
        self.assertEqual(self.client.get(self.url).status_code, 503)
        with self.path.open('ab') as writer:
            writer.write(one[-2:] + two[:-10])
            writer.flush()
            playlist = self.client.get(self.url)
            self.assertEqual(playlist.status_code, 200)
            self.assertEqual(playlist.text.count('#EXTINF:'), 1)
            self.assertNotIn('ENDLIST', playlist.text)
            fragment_name = playlist.text.splitlines()[-1]
            url = urljoin(self.url, fragment_name)
            self.assertEqual(self.client.get(url).content, one)
            self.assertEqual(self.client.get(url, headers={'Range': 'bytes=4-11'}).content, one[4:12])
            head = self.client.head(url, headers={'Range': 'bytes=-6'})
            self.assertEqual(
                (head.status_code, head.content, head.headers['content-length']), (206, b'', '6')
            )
            self.assertEqual(self.client.get(url, headers={'Range': 'bytes=9999-'}).status_code, 416)
            writer.write(two[-10:])
            writer.flush()
            new = self.client.get(self.url).text
            self.assertIn(fragment_name, new)
            self.assertEqual(new.count('#EXTINF:'), 2)
        # Windows must permit rename while the viewer holds an open read handle.
        with live_playback.open_reader(self.path) as viewer:
            self.path.rename(self.folder / 'recording.mp4')
            self.assertEqual(viewer.read(len(movie())), movie())
        self.assertEqual(self.client.get(url).content, one)
        self.assertIn('#EXT-X-ENDLIST', self.client.get(self.url).text)
        self.assertEqual(self.client.get(urljoin(self.url, new.splitlines()[-1])).content, two)

    def test_recording_replaced_or_removed_cannot_serve_cached_segments(self):
        self.path.write_bytes(movie() + fragment(0) + fragment(4000))
        url = urljoin(self.url, self.client.get(self.url).text.splitlines()[-1])
        self.path.write_bytes(movie())
        self.assertEqual(self.client.get(url).status_code, 409)
        self.marker.write_text('{"platform":"douyin","item_id":"unrelated"}', encoding='utf-8')
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get(url).status_code, 404)
        with db.connect() as conn:
            conn.execute("UPDATE live_recordings SET output_rel='../outside' WHERE id=?", (self.identity,))
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.get(self.url.replace(self.identity, 'whatever')).status_code, 404)
        self.assertEqual(
            self.client.get(self.url, headers={'Origin': 'https://example.com'}).status_code, 403
        )

    def test_active_snapshot_shares_state_but_hides_private_recorder_fields(self):
        record = {
            'id': self.identity,
            'sub_id': 3,
            'status': 'recording',
            'author': 'Sample',
            'title': 'Test',
            'started_at': 'now',
            'output_rel': 'private',
            'room_url': 'private',
            'duration_seconds': 0,
        }
        stop = threading.Event()
        owner = {'record': record, 'stop': stop, 'seconds': 12, 'progress_at': 99}
        with (
            patch.dict(live_service._active, {3: owner}, clear=True),
            patch.object(live_service.time, 'monotonic', return_value=101),
        ):
            (item,) = live_service.active_recordings()
            self.assertEqual(
                (item['status'], item['duration_seconds'], item['progress_age']), ('recording', 12, 2)
            )
            self.assertNotIn('output_rel', item)
            self.assertNotIn('room_url', item)
            owner['progress_at'] = 5
            self.assertEqual(live_service.active_recordings()[0]['status'], 'stalled')
            stop.set()
            self.assertEqual(live_service.active_recordings()[0]['status'], 'finalizing')
