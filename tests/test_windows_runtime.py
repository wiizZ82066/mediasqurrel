"""Windows integration uses temporary files and mocked OS effects only."""
from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from app import config, script_registry, windows_api, windows_runtime as windows


class WindowsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = self.stack.enter_context(tempfile.TemporaryDirectory())
        # Windows hosted runners may expose TEMP through an 8.3 alias.
        self.root = Path(root).resolve()
        self.source = self.root / 'Media Squirrel'
        self.source.mkdir()
        (self.source / 'run.py').write_text('# fixture', encoding='utf-8')
        self.python = self.root / 'python.exe'
        self.python.write_bytes(b'fixture')
        self.pythonw = self.root / 'pythonw.exe'
        self.pythonw.write_bytes(b'fixture')
        self.stack.enter_context(patch.object(windows, 'IS_WINDOWS', True))
        # Exercise the mocked Windows branch on Linux CI too; never launch it.
        self.stack.enter_context(patch.object(subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True))
        self.stack.enter_context(patch.object(sys, 'executable', str(self.python)))
        self.stack.enter_context(patch.object(config, 'SOURCE_DIR', str(self.source)))
        self.stack.enter_context(patch.object(config, 'PATHS_FILE', str(self.source / '.mediasquirrel.local.json')))
        self.stack.enter_context(patch.object(config, 'PORT', 8642))
        self.stack.enter_context(patch.dict(windows.os.environ, {name: '' for name in windows.PATH_OVERRIDES}))
        self.registry = MagicMock(HKEY_CURRENT_USER='current-user', REG_SZ=1, KEY_READ=2, KEY_SET_VALUE=4)
        self.values = {'UnrelatedApp': 'untouched'}
        self.registry.QueryValueEx.side_effect = self.query
        self.registry.SetValueEx.side_effect = lambda _key, name, _reserved, _kind, value: self.values.__setitem__(name, value)
        self.registry.DeleteValue.side_effect = lambda _key, name: self.values.pop(name, None)
        self.stack.enter_context(patch.object(windows, '_registry', return_value=self.registry))

    def query(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.registry.REG_SZ

    def test_status_is_read_only_and_default_disabled(self):
        status = windows.startup_status()
        self.assertTrue(status['supported'])
        self.assertTrue(status['available'])
        self.assertFalse(status['enabled'])
        self.registry.SetValueEx.assert_not_called()
        self.registry.CreateKeyEx.assert_not_called()

    def test_startup_current_user_enable_and_disable_only_own_value(self):
        status = windows.set_startup(True)
        self.assertTrue(status['enabled'])
        self.assertTrue(status['matches_current'])
        command = self.values[windows.RUN_NAME]
        self.assertIn('pythonw.exe', command)
        self.assertIn('"' + str(self.source / 'run.py') + '"', command)
        self.assertIn('--no-browser', command)
        self.assertNotIn('--paths-file', command)
        self.assertEqual(self.registry.CreateKeyEx.call_args.args[0], 'current-user')
        self.assertFalse(windows.set_startup(False)['enabled'])
        self.assertEqual(self.values, {'UnrelatedApp': 'untouched'})

    def test_stale_registration_is_detected_and_can_be_updated(self):
        self.values[windows.RUN_NAME] = 'old-launcher'
        self.assertFalse(windows.startup_status()['matches_current'])
        self.assertTrue(windows.set_startup(True)['matches_current'])

    def test_disable_still_works_when_pythonw_is_missing(self):
        self.values[windows.RUN_NAME] = 'old-launcher'
        self.pythonw.unlink()
        status = windows.startup_status()
        self.assertTrue(status['enabled'])
        self.assertFalse(status['available'])
        self.assertFalse(windows.set_startup(False)['enabled'])

    def test_invalid_enable_and_long_command_do_not_write_registry(self):
        for invalid in ('true', 1, None):
            with self.assertRaises(ValueError):
                windows.set_startup(invalid)
        with patch.object(config, 'PATHS_FILE', str(self.root / ('x' * 220 + '.json'))):
            with self.assertRaisesRegex(RuntimeError, '260'):
                windows.set_startup(True)
        self.registry.SetValueEx.assert_not_called()

    def test_temporary_path_override_rejects_enable_but_allows_disable(self):
        self.values[windows.RUN_NAME] = 'old-launcher'
        with patch.dict(windows.os.environ, {'MS_LIBRARY_DIR': str(self.root)}):
            with self.assertRaisesRegex(RuntimeError, '临时路径'):
                windows.set_startup(True)
            self.assertFalse(windows.set_startup(False)['enabled'])
        self.registry.SetValueEx.assert_not_called()

    def test_custom_paths_file_and_port_are_explicit(self):
        # This checks command arguments, not host-specific TEMP path length.
        # A paths file need not exist while constructing a startup command.
        paths = Path(self.root.anchor) / 'ms-paths.json'
        with patch.object(config, 'PATHS_FILE', str(paths)), patch.object(config, 'PORT', 9000):
            command = windows.startup_command()
        self.assertIn('--port 9000', command)
        self.assertIn('--paths-file ' + subprocess.list2cmdline([str(paths)]), command)
        self.assertLessEqual(len(command.encode('utf-16-le')) // 2, 260)

    def test_permission_denied_returns_actionable_status(self):
        self.registry.OpenKey.side_effect = PermissionError('registry denied')
        self.assertIn('registry denied', windows.startup_status()['message'])
        self.assertFalse(windows.startup_status()['available'])
        with self.assertRaises(PermissionError):
            windows.set_startup(False)

    def test_windowless_parent_downloads_use_same_environment_console_python(self):
        with patch.object(sys, 'executable', str(self.pythonw)):
            self.assertEqual(script_registry._launcher_argv(), [str(self.python), '-u'])
        self.python.unlink()
        with self.assertRaisesRegex(RuntimeError, 'python.exe'):
            windows.console_python(self.pythonw)

    def test_background_launch_uses_argument_vector_cwd_and_hidden_child(self):
        with patch.object(windows.subprocess, 'Popen') as launch:
            windows.launch_background(['--background', '--port', '9001'])
        argv = launch.call_args.args[0]
        self.assertEqual(argv[:2], [str(self.pythonw), str(self.source / 'run.py')])
        self.assertIn('--background-child', argv)
        self.assertNotIn('--background', argv)
        self.assertEqual(launch.call_args.kwargs['cwd'], str(self.source))
        self.assertEqual(launch.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)
        self.assertEqual(launch.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_folder_opens_only_allowlisted_existing_directory(self):
        with patch.object(windows.os, 'startfile', create=True) as show, \
                patch.object(config, 'LIBRARY_ROOT', str(self.source)):
            windows.open_directory('library')
            show.assert_called_once_with(str(self.source.resolve()), 'explore')
            for target in ('../', str(self.root), 'https://example.com'):
                with self.assertRaises(ValueError):
                    windows.open_directory(target)
            self.assertEqual(show.call_count, 1)

    def test_settings_body_rejects_extra_fields_and_non_booleans(self):
        with patch.object(windows, 'set_startup') as change:
            for body in ({'enabled': 'true'}, {'enabled': True, 'command': 'anything'}, {}):
                with self.assertRaises(HTTPException) as failure:
                    windows_api.change_startup(body)
                self.assertEqual(failure.exception.status_code, 400)
            change.assert_not_called()
        with patch.object(windows, 'open_directory') as show:
            with self.assertRaises(HTTPException):
                windows_api.open_directory({'kind': 'library', 'path': 'anything'})
            show.assert_not_called()


class LogAndTrayTests(unittest.TestCase):
    def test_logs_redact_across_print_writes_and_bound_overlong_lines(self):
        logger = MagicMock()
        stream = windows.LogStream(logger)
        stream.write('Cookie: ')
        stream.flush()
        stream.write('private-value\n')
        stream.write('token=')
        stream.write('x' * 20000)
        stream.write('tail-must-not-appear\n')
        stream.close()
        text = '\n'.join(call.args[0] for call in logger.info.call_args_list)
        self.assertNotIn('private-value', text)
        self.assertNotIn('tail-must-not-appear', text)
        self.assertNotIn('xxxxxxxx', text)
        self.assertLess(len(text), 17000)

    def test_log_rotation_keeps_task_logs_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            unrelated = directory / 'task-example.log'
            unrelated.write_text('keep task history', encoding='utf-8')
            for number in range(8):
                (directory / f'startup-20200101-{number}.log').write_text('old', encoding='utf-8')
            path, stream, handler = windows.configure_background_logging(directory)
            try:
                handler.maxBytes = 200
                for _ in range(15):
                    stream.write('a short runtime log line\n')
                self.assertTrue(path.is_file())
                self.assertTrue(path.with_name(path.name + '.1').is_file())
                self.assertLessEqual(len(list(directory.glob('startup-*.log'))), 5)
                self.assertEqual(unrelated.read_text(), 'keep task history')
            finally:
                stream.close()
                handler.close()

    def test_busy_tray_exit_confirmation_and_cleanup_indicator(self):
        import run
        server = MagicMock(should_exit=False)
        tray = MagicMock()
        with patch.object(run, '_tray_activity', return_value={'running': 1, 'queued': 2, 'scanning': 1}), \
                patch.object(windows, 'message_box', return_value=False) as confirm:
            run._request_tray_exit(server, tray)
            self.assertFalse(server.should_exit)
            tray.stop.assert_not_called()
            confirm.return_value = True
            run._request_tray_exit(server, tray)
            self.assertTrue(server.should_exit)
            self.assertIn('清理', tray.title)
            tray.stop.assert_not_called()
            tray.update_menu.assert_called_once()

    def test_idle_tray_exit_does_not_ask_and_waits_for_worker(self):
        import run
        server = MagicMock(should_exit=False)
        tray = MagicMock()
        with patch.object(run, '_tray_activity', return_value={'running': 0, 'queued': 0, 'scanning': 0}), \
                patch.object(windows, 'message_box') as confirm:
            run._request_tray_exit(server, tray)
            confirm.assert_not_called()
            self.assertTrue(server.should_exit)
            tray.stop.assert_not_called()
        worker = MagicMock()
        worker.is_alive.return_value = False
        run._watch_backend(worker, tray, threading.Event())
        tray.stop.assert_called_once()

    def test_hidden_tray_failure_requests_normal_backend_shutdown(self):
        import run
        from app import runtime
        worker = MagicMock()
        worker.is_alive.return_value = True
        service = MagicMock(started=True, should_exit=False)
        with patch.object(sys, 'argv', ['run.py', '--no-browser']), \
                patch.object(run, '_BACKGROUND', True), \
                patch.object(runtime, 'probe_server', return_value=None), \
                patch.object(runtime, 'port_in_use', return_value=False), \
                patch.object(runtime, 'runtime_identity', return_value={}), \
                patch.object(runtime, 'ensure_frontend'), \
                patch.object(run.uvicorn, 'Server', return_value=service), \
                patch.object(run.threading, 'Thread', return_value=worker), \
                patch.object(run, '_run_tray', side_effect=RuntimeError('tray missing')):
            with self.assertRaisesRegex(RuntimeError, '后台服务将正常退出'):
                run.main()
        self.assertTrue(service.should_exit)
        worker.join.assert_called_once()

    def test_long_startup_keeps_tray_and_does_not_abort_healthy_worker(self):
        import run
        from app import runtime
        worker = MagicMock()
        worker.is_alive.return_value = True
        service = MagicMock(started=False, should_exit=False)
        def tray(_url, actual, _worker):
            self.assertIs(actual, service)
            self.assertFalse(actual.should_exit)
            self.assertIn('正在启动', run._tray_status(actual))
            # Simulate eventual successful startup, then normal user exit.
            actual.started = True
            actual.should_exit = True
        with patch.object(sys, 'argv', ['run.py', '--no-browser']), \
                patch.object(run, '_BACKGROUND', True), \
                patch.object(runtime, 'probe_server', return_value=None), \
                patch.object(runtime, 'port_in_use', return_value=False), \
                patch.object(runtime, 'runtime_identity', return_value={}), \
                patch.object(runtime, 'ensure_frontend'), \
                patch.object(run.uvicorn, 'Server', return_value=service), \
                patch.object(run.threading, 'Thread', return_value=worker), \
                patch('time.sleep') as wait, \
                patch.object(run, '_run_tray', side_effect=tray) as show:
            run.main()
        self.assertEqual(wait.call_count, 200)
        show.assert_called_once()
        worker.join.assert_called_once()

    def test_tray_status_distinguishes_starting_running_and_stopping(self):
        import run
        service = MagicMock(started=False, should_exit=False)
        with patch.object(run, '_tray_activity', return_value={'running': 1, 'queued': 2, 'scanning': 3}) as activity:
            self.assertIn('正在启动', run._tray_status(service))
            activity.assert_not_called()
            service.started = True
            self.assertEqual(run._tray_status(service), '下载 1 · 排队 2 · 扫描 3')
            service.should_exit = True
            self.assertIn('正在退出', run._tray_status(service))


if __name__ == '__main__':
    unittest.main()
