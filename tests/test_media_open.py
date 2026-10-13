"""Open/reveal endpoints and Win32 calls use isolated files and OS doubles."""
from contextlib import ExitStack
import ctypes
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, library_api, local_files
from app.security import LocalOnlyMiddleware


class MediaOpenTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.library = self.root / 'library'
        self.folder = self.library / 'Author' / '2026-10-13_weibo_123'
        self.folder.mkdir(parents=True)
        self.media = self.folder / 'photo,中文.jpg'
        self.media.write_bytes(b'controlled-fixture')
        self.entry = {'id': 'entry', 'root_id': 'root', 'rel_dir': self.folder.relative_to(self.library).as_posix(),
                      'assets': [{'rel_path': self.media.name}]}
        self.task = {'id': 'task', 'status': 'success', 'script_id': 'weibo', 'params': {'out': str(self.library)},
                     'output_dir': str(self.folder), 'metadata': {}}
        self.stack.enter_context(patch.object(config, 'LIBRARY_ROOT', str(self.library)))
        self.stack.enter_context(patch.object(config, 'MAINTENANCE_ACTIVE', False))
        self.get_entry = self.stack.enter_context(patch.object(library_api.catalog, 'get_entry', return_value=self.entry))
        self.get_task = self.stack.enter_context(patch.object(library_api.task_manager, 'get_task', return_value=self.task))
        self.task_entry = self.stack.enter_context(patch.object(library_api.service, 'task_entry', return_value=self.entry))
        self.stack.enter_context(patch.object(library_api.service, 'root_path', return_value=self.library))
        self.open = self.stack.enter_context(patch.object(local_files, 'open_media', return_value=False))
        self.reveal = self.stack.enter_context(patch.object(local_files, 'reveal_path'))
        app = FastAPI()
        app.include_router(library_api.router)
        app.add_middleware(LocalOnlyMiddleware)
        self.client = TestClient(app, base_url=f'http://127.0.0.1:{config.PORT}')
        self.addCleanup(self.client.close)

    def post_open(self, **body):
        return self.client.post('/api/library/entries/entry/open', json={'rel': self.media.name, 'mode': 'default', **body})

    def write_marker(self, **overrides):
        marker = {'schema_version': 1, 'platform': 'weibo', 'item_id': '123',
                  'entry_id': 'fixture-entry', 'status': 'partial', **overrides}
        (self.folder / 'entry.json').write_text(json.dumps(marker), encoding='utf-8')

    def test_only_indexed_media_is_opened_using_selected_mode(self):
        response = self.post_open()
        self.assertEqual(response.json(), {'ok': True, 'cancelled': False})
        self.open.assert_called_once_with(str(self.media), mode='default')
        self.open.return_value = True
        self.assertEqual(self.post_open(mode='choose').json(), {'ok': True, 'cancelled': True})
        self.open.assert_called_with(str(self.media), mode='choose')

    def test_invalid_mode_body_arbitrary_path_and_scripts_never_launch(self):
        for body in ({'mode': 'execute'}, {'mode': ['choose']}, {'rel': None}, {'path': str(self.media)}):
            self.assertEqual(self.post_open(**body).status_code, 400)
        for path in ('../other.jpg', str(self.media), 'context.md', 'file.jpg.exe', 'photo.jpg:script'):
            self.assertEqual(self.post_open(rel=path).status_code, 403)
        script = self.folder / 'indexed.exe'
        script.write_bytes(b'not-executable-fixture')
        self.entry['assets'].append({'rel_path': script.name})
        self.assertEqual(self.post_open(rel=script.name).status_code, 403)
        self.open.assert_not_called()

    def test_missing_root_file_entry_and_outside_origin_do_not_launch(self):
        self.media.unlink()
        self.assertEqual(self.post_open().status_code, 404)
        self.get_entry.return_value = None
        self.assertEqual(self.post_open().status_code, 404)
        response = self.client.post('/api/library/entries/entry/open',
                                    json={'rel': self.media.name, 'mode': 'choose'},
                                    headers={'Origin': 'https://external.invalid'})
        self.assertEqual(response.status_code, 403)
        self.open.assert_not_called()

    def test_corrupt_index_link_to_sibling_is_rejected(self):
        sibling = self.library / 'Other'
        sibling.mkdir()
        other = sibling / 'outside.jpg'
        other.write_bytes(b'fixture')
        link = self.folder / 'linked.jpg'
        try:
            link.symlink_to(other)
        except OSError:
            self.skipTest('Windows symlink privileges unavailable')
        self.entry['assets'].append({'rel_path': 'linked.jpg'})
        self.assertEqual(self.post_open(rel='linked.jpg').status_code, 403)
        self.open.assert_not_called()

    def test_busy_unsupported_and_shell_failure_have_recoverable_errors(self):
        for failure, code in ((local_files.OpenWithBusyError('已打开'), 409),
                              (NotImplementedError('Windows only'), 501),
                              (OSError('private/path'), 503)):
            self.open.side_effect = failure
            response = self.post_open(mode='choose')
            self.assertEqual(response.status_code, code)
            self.assertNotIn('private/path', response.text)

    def test_task_reveal_prefers_rebound_indexed_entry(self):
        self.task['output_dir'] = str(self.root / 'old-location')
        response = self.client.post('/api/tasks/task/reveal')
        self.assertEqual(response.status_code, 200, response.text)
        self.reveal.assert_called_once_with(str(self.folder), select_file=False)

    def test_task_reveal_accepts_verified_unindexed_and_diagnostic_outputs(self):
        self.task_entry.return_value = None
        self.write_marker()
        response = self.client.post('/api/tasks/task/reveal')
        self.assertEqual(response.status_code, 200, response.text)
        self.reveal.assert_called_with(str(self.folder), select_file=False)
        diagnostic = self.root / 'diagnostics' / 'downloads' / 'fixture'
        target = diagnostic / 'Author' / self.folder.name
        target.mkdir(parents=True)
        (target / 'entry.json').write_text((self.folder / 'entry.json').read_text(), encoding='utf-8')
        self.task.update(params={'out': str(diagnostic)}, output_dir=str(target), metadata={'diagnostic': True})
        self.assertEqual(self.client.post('/api/tasks/task/reveal').status_code, 200)
        self.reveal.assert_called_with(str(target), select_file=False)

    def test_task_reveal_without_output_never_opens_requested_root(self):
        self.task_entry.return_value = None
        self.task.update(status='queued', output_dir=None)
        response = self.client.post('/api/tasks/task/reveal')
        self.assertEqual(response.status_code, 404)
        self.assertIn('尚未', response.json()['detail'])
        self.task['output_dir'] = str(self.library)
        self.assertEqual(self.client.post('/api/tasks/task/reveal').status_code, 403)
        self.reveal.assert_not_called()

    def test_task_reveal_rejects_escape_mismatched_marker_or_empty_folder(self):
        self.task_entry.return_value = None
        self.assertEqual(self.client.post('/api/tasks/task/reveal').status_code, 404)
        self.write_marker(platform='douyin')
        self.assertEqual(self.client.post('/api/tasks/task/reveal').status_code, 404)
        self.write_marker()
        self.task['content_key'] = 'weibo:other-post'
        self.assertEqual(self.client.post('/api/tasks/task/reveal').status_code, 404)
        self.task['output_dir'] = str(self.root)
        self.assertEqual(self.client.post('/api/tasks/task/reveal').status_code, 403)
        self.reveal.assert_not_called()

    def test_task_missing_record_folder_and_cross_origin(self):
        self.task_entry.return_value = None
        self.task['output_dir'] = str(self.folder / 'missing')
        self.assertEqual(self.client.post('/api/tasks/task/reveal').status_code, 404)
        self.get_task.return_value = None
        self.assertEqual(self.client.post('/api/tasks/task/reveal').status_code, 404)
        response = self.client.post('/api/tasks/task/reveal', headers={'Origin': 'https://external.invalid'})
        self.assertEqual(response.status_code, 403)
        self.reveal.assert_not_called()

    def test_legacy_context_and_relative_output_are_supported_without_reading_text(self):
        self.task_entry.return_value = None
        self.task.update(output_dir=None, output_rel=self.folder.relative_to(self.library).as_posix())
        (self.folder / 'context.md').write_text('private text fixture', encoding='utf-8')
        response = self.client.post('/api/tasks/task/reveal')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('private text', response.text)
        self.reveal.assert_called_once_with(str(self.folder), select_file=False)


class NativeOpenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'photo,中文.jpg'
        self.path.write_bytes(b'fixture')
        windows = patch.object(local_files, 'IS_WINDOWS', True)
        windows.start()
        self.addCleanup(windows.stop)

    def test_default_launch_passes_exact_path_as_file_not_command(self):
        with patch.object(local_files.os, 'startfile', create=True) as show:
            self.assertFalse(local_files.open_media(self.path))
        show.assert_called_once_with(str(self.path), 'open')

    def test_helper_rejects_scripts_and_missing_files_before_os_call(self):
        with patch.object(local_files.os, 'startfile', create=True) as show:
            with self.assertRaises(ValueError):
                local_files.open_media(self.path.with_suffix('.cmd'))
            self.path.unlink()
            with self.assertRaises(FileNotFoundError):
                local_files.open_media(self.path)
            show.assert_not_called()

    def test_unsupported_platform_does_not_try_shell_commands(self):
        with patch.object(local_files, 'IS_WINDOWS', False), \
                patch.object(local_files.os, 'startfile', create=True) as show:
            with self.assertRaisesRegex(NotImplementedError, 'Windows'):
                local_files.open_media(self.path)
            show.assert_not_called()

    def test_open_with_releases_concurrency_guard_on_cancel_or_error(self):
        with patch.object(local_files, '_open_with_dialog', return_value=True) as choose:
            self.assertTrue(local_files.open_media(self.path, mode='choose'))
            self.assertTrue(local_files.open_media(self.path, mode='choose'))
            local_files._open_with_lock.acquire()
            try:
                with self.assertRaises(local_files.OpenWithBusyError):
                    local_files.open_media(self.path, mode='choose')
            finally:
                local_files._open_with_lock.release()
            self.assertEqual(choose.call_count, 2)
            choose.side_effect = OSError('fixture')
            with self.assertRaises(OSError):
                local_files.open_media(self.path, mode='choose')
            self.assertFalse(local_files._open_with_lock.locked())

    def test_native_dialog_receives_literal_file_and_single_use_flags_and_balances_com(self):
        for initialized in (0, 1, -2147417850):
            for outcome, cancelled in ((0, False), (-2147023673, True), (-2147467260, True)):
                with self.subTest(initialized=initialized, outcome=outcome):
                    ole, shell = MagicMock(), MagicMock()
                    ole.CoInitializeEx.return_value = initialized
                    def open_dialog(parent, info):
                        self.assertIsNone(parent)
                        self.assertEqual(info._obj.pcszFile, str(self.path))
                        self.assertIsNone(info._obj.pcszClass)
                        self.assertEqual(info._obj.oaifInFlags, 4)
                        return outcome
                    shell.SHOpenWithDialog.side_effect = open_dialog
                    with patch.object(ctypes, 'WinDLL', side_effect=lambda name: ole if name == 'ole32' else shell, create=True):
                        self.assertEqual(local_files._open_with_dialog(self.path), cancelled)
                    self.assertEqual(ole.CoUninitialize.call_count, int(initialized in (0, 1)))

    def test_native_failure_still_balances_com_and_never_launches_on_init_failure(self):
        ole, shell = MagicMock(), MagicMock()
        ole.CoInitializeEx.return_value = 0
        shell.SHOpenWithDialog.return_value = -2147467259
        with patch.object(ctypes, 'WinDLL', side_effect=lambda name: ole if name == 'ole32' else shell, create=True):
            with self.assertRaises(OSError):
                local_files._open_with_dialog(self.path)
            ole.CoUninitialize.assert_called_once()
            shell.SHOpenWithDialog.reset_mock()
            ole.CoInitializeEx.return_value = -2147467259
            with self.assertRaises(OSError):
                local_files._open_with_dialog(self.path)
            shell.SHOpenWithDialog.assert_not_called()


if __name__ == '__main__':
    unittest.main()
