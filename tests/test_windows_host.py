"""Source host checks: no service, registry changes or production cache writes."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app import config, windows_host as host


class HostLaunchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(host, 'IS_WINDOWS', True))
        self.stack.enter_context(patch.dict(os.environ, {'MS_BRANDED_HOST': ''}))
        self.stack.enter_context(patch.object(subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True))
        self.build = self.stack.enter_context(patch.object(host, 'ensure_host', return_value=(
            Path('fixture host.exe'), Path('fixture python.exe'), Path('fixture python.dll'))))
        self.launch = self.stack.enter_context(patch.object(subprocess, 'Popen'))

    def test_current_host_does_not_recurse_but_parent_marker_does_not_hide_child(self):
        with patch.dict(os.environ, {'MS_BRANDED_HOST': str(os.getpid())}):
            self.assertFalse(host.relaunch_if_needed(['--background-child']))
        self.build.assert_not_called()
        self.launch.assert_not_called()
        with patch.dict(os.environ, {'MS_BRANDED_HOST': 'old-parent-pid'}):
            self.launch.return_value.wait.return_value = 0
            self.assertTrue(host.relaunch_if_needed(['--background-child']))
        self.launch.assert_called_once()

    def test_arguments_remain_separate_and_background_host_is_hidden(self):
        self.launch.return_value.wait.side_effect = subprocess.TimeoutExpired('fixture', .5)
        args = ['--background-child', '--paths-file', 'fixture path/中文 config.json', '--port', '9000']
        self.assertTrue(host.relaunch_if_needed(args))
        command = self.launch.call_args.args[0]
        self.assertEqual(command[:6], ['fixture host.exe', '--python', 'fixture python.exe',
                                      '--dll', 'fixture python.dll', '--'])
        self.assertEqual(command[6:], [str(Path(config.SOURCE_DIR) / 'run.py'), *args])
        self.assertEqual(self.launch.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)
        self.assertFalse(self.launch.call_args.kwargs.get('shell', False))

    def test_failed_build_or_child_falls_back_to_existing_python(self):
        with patch('builtins.print'):
            self.build.side_effect = RuntimeError('compiler unavailable')
            self.assertFalse(host.relaunch_if_needed([]))
            self.launch.assert_not_called()
            self.build.side_effect = None
            self.launch.return_value.wait.return_value = 126
            self.assertFalse(host.relaunch_if_needed([]))

    def test_console_launcher_does_not_build_a_host(self):
        import run
        with patch.object(run, '_BACKGROUND', False), \
                patch.object(sys, 'argv', ['run.py', '--no-tray', '--no-browser']), \
                patch('app.runtime.runtime_identity', side_effect=RuntimeError('fixture stop')):
            with self.assertRaisesRegex(SystemExit, 'fixture stop'):
                run.main()
        self.build.assert_not_called()

    def test_background_launcher_defers_service_start_to_host(self):
        import run
        with patch.object(run, '_BACKGROUND', True), \
                patch.object(sys, 'argv', ['run.py', '--background-child', '--no-browser']), \
                patch.object(host, 'relaunch_if_needed', return_value=True) as relaunch, \
                patch('app.runtime.coordinate_startup') as coordinate:
            run.main()
        relaunch.assert_called_once_with(['--background-child', '--no-browser'])
        coordinate.assert_not_called()


@unittest.skipUnless(os.name == 'nt', 'Native host requires Windows')
class NativeHostTests(unittest.TestCase):
    def test_native_host_keeps_interpreter_child_pipes_and_repairs_cache(self):
        try:
            host.host_inputs()
        except RuntimeError as error:
            self.skipTest(str(error))
        with tempfile.TemporaryDirectory(prefix='host fixture ') as temporary, \
                patch.object(config, 'CACHE_DIR', temporary):
            executable, python, dll = host.ensure_host()
            result = Path(temporary) / '中文 probe.json'
            script = (
                'import ctypes,json,os,pathlib,subprocess,sys; '
                'buf=ctypes.create_unicode_buffer(32768); '
                'ctypes.windll.kernel32.GetModuleFileNameW(None,buf,len(buf)); '
                'child=subprocess.run([sys.executable,"-X","utf8","-c","print(chr(0x677e))"],'
                'capture_output=True,text=True,encoding="utf-8",check=True,'
                'creationflags=subprocess.CREATE_NO_WINDOW); '
                'pathlib.Path(sys.argv[1]).write_text(json.dumps({'
                '"process":buf.value,"prefix":sys.prefix,"executable":sys.executable,'
                '"marker":os.environ.get("MS_BRANDED_HOST"),"pid":os.getpid(),'
                '"child":child.stdout}),encoding="utf-8")')
            subprocess.run(host.host_command(executable, python, dll, ['-I', '-c', script, str(result)]),
                           check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
            details = json.loads(result.read_text(encoding='utf-8'))
            self.assertEqual(Path(details['process']), executable)
            self.assertEqual(Path(details['executable']).resolve(), Path(sys.executable).resolve())
            self.assertEqual(Path(details['prefix']).resolve(), Path(sys.prefix).resolve())
            self.assertEqual(details['marker'], str(details['pid']))
            self.assertEqual(details['child'].strip(), '松')
            # Reusing a valid cache never invokes the compiler or another probe.
            with patch.object(subprocess, 'run', side_effect=AssertionError('cache was rebuilt')):
                self.assertEqual(host.ensure_host()[0], executable)
            executable.write_bytes(b'broken host cache')
            executable.with_name('verified.json').write_text('{', encoding='utf-8')
            self.assertEqual(host.ensure_host()[0], executable)
            self.assertEqual(executable.read_bytes()[:2], b'MZ')
            self.assertFalse(list(executable.parent.glob('build-*')))


if __name__ == '__main__':
    unittest.main()
