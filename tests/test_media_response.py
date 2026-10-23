"""A canceled playback request must not keep reading a large source file."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import anyio

from app.media_response import MediaFileResponse


class MediaResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='media-response-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'sample.mp4'
        self.content = bytes(range(256)) * 4096
        self.path.write_bytes(self.content)

    async def request(self, *, method='GET', ranges=None, disconnect=False, fail_send=False):
        messages, opened, reads = [], [], []
        disconnected = asyncio.Event()
        listener_done = asyncio.Event()
        original_open = anyio.open_file

        class TrackedFile:
            def __init__(self, file):
                self.file = file

            async def __aenter__(self):
                await self.file.__aenter__()
                return self

            async def __aexit__(self, *args):
                return await self.file.__aexit__(*args)

            async def read(self, size):
                reads.append(size)
                return await self.file.read(size)

            async def seek(self, position):
                return await self.file.seek(position)

        async def open_tracked(*args, **kwargs):
            file = await original_open(*args, **kwargs)
            opened.append(file)
            return TrackedFile(file)

        async def receive():
            try:
                await disconnected.wait()
                return {'type': 'http.disconnect'}
            finally:
                listener_done.set()

        async def send(message):
            messages.append(message)
            if message['type'] == 'http.response.body' and message.get('body'):
                if fail_send:
                    raise RuntimeError('unexpected transport failure')
                if disconnect:
                    disconnected.set()
                    await asyncio.sleep(0)

        headers = [(b'range', ranges.encode())] if ranges else []
        scope = {'type': 'http', 'method': method, 'headers': headers, 'extensions': {}}
        response = MediaFileResponse(self.path)
        with patch.object(anyio, 'open_file', side_effect=open_tracked):
            try:
                await asyncio.wait_for(response(scope, receive, send), 2)
            finally:
                self.assertTrue(all(file.wrapped.closed for file in opened))
        self.assertTrue(listener_done.is_set())
        return messages, reads

    async def test_disconnected_seek_closes_file_after_one_chunk(self):
        messages, reads = await self.request(ranges='bytes=0-', disconnect=True)
        self.assertLessEqual(len(reads), 1)
        body = b''.join(message.get('body', b'') for message in messages)
        self.assertEqual(body, self.content[:MediaFileResponse.chunk_size])
        self.assertLess(len(body), len(self.content))

    async def test_complete_range_returns_exact_bytes_and_stops_listener(self):
        messages, _ = await self.request(ranges='bytes=17-100016')
        self.assertEqual(messages[0]['status'], 206)
        self.assertEqual(dict(messages[0]['headers'])[b'content-range'],
                         f'bytes 17-100016/{len(self.content)}'.encode())
        self.assertEqual(b''.join(message.get('body', b'') for message in messages),
                         self.content[17:100017])
        self.assertFalse(messages[-1]['more_body'])

    async def test_head_range_has_headers_without_file_reads(self):
        messages, reads = await self.request(method='HEAD', ranges='bytes=0-99')
        self.assertEqual(messages[0]['status'], 206)
        self.assertEqual(dict(messages[0]['headers'])[b'content-length'], b'100')
        self.assertEqual(reads, [])
        self.assertEqual(messages[-1].get('body', b''), b'')

    async def test_multiple_ranges_and_normal_get_preserve_file_response_behavior(self):
        messages, _ = await self.request(ranges='bytes=1-3,8-10')
        self.assertEqual(messages[0]['status'], 206)
        body = b''.join(message.get('body', b'') for message in messages)
        self.assertIn(b'content-range: bytes 1-3/', body.lower())
        self.assertIn(b'content-range: bytes 8-10/', body.lower())
        self.assertIn(self.content[1:4], body)
        self.assertIn(self.content[8:11], body)
        messages, _ = await self.request()
        self.assertEqual(messages[0]['status'], 200)
        self.assertEqual(b''.join(message.get('body', b'') for message in messages), self.content)

    async def test_real_transport_failure_is_not_silenced(self):
        with self.assertRaises(ExceptionGroup) as caught:
            await self.request(fail_send=True)
        self.assertIn('unexpected transport failure', str(caught.exception.exceptions[0]))
