import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, library_api
from app.security import LocalOnlyMiddleware


class LibraryRevealTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / 'Author' / 'date'
        self.folder.mkdir(parents=True)
        self.media = self.folder / 'a,中文.jpg'
        self.media.write_bytes(b'fixture')
        self.entry = {'root_id': 'root', 'rel_dir': 'Author/date',
                      'assets': [{'rel_path': 'a,中文.jpg'}]}
        app = FastAPI()
        app.include_router(library_api.router)
        app.add_middleware(LocalOnlyMiddleware)
        self.client = TestClient(app, base_url=f'http://127.0.0.1:{config.PORT}')
        for name, value in (('catalog.get_entry', self.entry), ('service.root_path', self.root)):
            mock = patch('app.library_api.' + name, return_value=value)
            mock.start(); self.addCleanup(mock.stop)
        mock = patch('app.library_api.local_files.reveal_path')
        self.reveal = mock.start(); self.addCleanup(mock.stop)

    def post(self, relative=None, **kwargs):
        return self.client.post('/api/library/entries/entry/reveal', json={'rel': relative}, **kwargs)

    def test_directory_and_indexed_file_are_resolved_without_shell_arguments(self):
        self.assertEqual(self.post().status_code, 200)
        self.reveal.assert_called_with(str(self.folder), select_file=False)
        self.assertEqual(self.post('a,中文.jpg').status_code, 200)
        self.reveal.assert_called_with(str(self.media), select_file=True)

    def test_arbitrary_files_and_cross_origin_requests_cannot_open_explorer(self):
        for relative in ('../secret.jpg', 'C:/secret.jpg', 'context.md', {}, ['a,中文.jpg']):
            self.assertEqual(self.post(relative).status_code, 403)
        self.assertEqual(self.post(headers={'Origin': 'https://external.invalid'}).status_code, 403)
        self.reveal.assert_not_called()

    def test_missing_media_and_unregistered_entries_return_recoverable_errors(self):
        self.media.unlink()
        self.assertEqual(self.post('a,中文.jpg').status_code, 404)
        self.reveal.assert_not_called()
        with patch('app.library_api.catalog.get_entry', return_value=None):
            self.assertEqual(self.post().status_code, 404)

    def test_corrupt_index_cannot_escape_root_and_os_errors_do_not_expose_paths(self):
        self.entry['rel_dir'] = '../private'
        self.assertEqual(self.post().status_code, 403)
        self.reveal.assert_not_called()
        self.entry['rel_dir'] = 'Author/date'
        self.reveal.side_effect = OSError('private/path/should/not/be/returned')
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private/path', response.text)
