"""Shell shortcuts use disposable directories, never the real Startup folder."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app import startup_shortcut as startup


class StartupShortcutTests(unittest.TestCase):
    def test_paths_are_stdin_data_and_read_has_no_write_action(self):
        value = {'target': 'C:\\fixture\\pythonw.exe', 'arguments': '"a $weird` name.py"',
                 'working_directory': 'C:\\fixture with spaces'}
        with patch.object(startup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'null', '')) as launch:
            self.assertIsNone(startup.shortcut('write', value=value))
            arguments = launch.call_args.args[0]
            self.assertNotIn(value['arguments'], ' '.join(arguments))
            self.assertEqual(json.loads(launch.call_args.kwargs['input'])['value'], value)
            self.assertEqual(base64.b64decode(arguments[-1]).decode('utf-16-le'), startup._SCRIPT)
            startup.shortcut()
            self.assertEqual(json.loads(launch.call_args.kwargs['input'])['action'], 'read')
            self.assertTrue(launch.call_args.kwargs['check'])
            self.assertEqual(launch.call_args.kwargs['timeout'], 15)

    def test_failure_or_malformed_result_is_not_reported_as_saved(self):
        for output in ('oops', '{}', '{"target":1}'):
            with self.subTest(output=output), patch.object(startup.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, output, '')):
                with self.assertRaisesRegex(RuntimeError, '启动快捷方式'):
                    startup.shortcut()
        with patch.object(startup.subprocess, 'run', side_effect=subprocess.TimeoutExpired('powershell', 15)):
            with self.assertRaisesRegex(RuntimeError, '启动快捷方式'):
                startup.shortcut()

    @unittest.skipUnless(os.name == 'nt', 'Windows Shell shortcut integration only')
    def test_real_shortcut_round_trip_and_exact_restore_in_temporary_folder(self):
        original_run = subprocess.run
        folder_expression = "$folder = [Environment]::GetFolderPath('Startup')"
        self.assertEqual(startup._SCRIPT.count(folder_expression), 1)
        script = startup._SCRIPT.replace(folder_expression, '$folder = $request.test_folder')
        with tempfile.TemporaryDirectory(prefix='ms-shortcut-test-') as directory:
            root = Path(directory).resolve()
            def isolated_run(arguments, **kwargs):
                # Assert the production-known-folder lookup is absent BEFORE
                # executing any COM operation. The fixture cannot touch login.
                decoded = base64.b64decode(arguments[-1]).decode('utf-16-le')
                self.assertNotIn("GetFolderPath('Startup')", decoded)
                payload = json.loads(kwargs['input'])
                payload['test_folder'] = str(root)
                kwargs['input'] = json.dumps(payload)
                return original_run(arguments, **kwargs)
            with patch.object(startup, '_SCRIPT', script), patch.object(startup.subprocess, 'run', side_effect=isolated_run):
                self.assertIsNone(startup.shortcut())
                self.assertEqual(list(root.iterdir()), [])
                expected = {'target': str(Path(sys.executable).resolve()),
                            'arguments': '"test script.py" --no-browser', 'working_directory': str(root)}
                first = startup.shortcut('write', value=expected)
                self.assertTrue(startup.matches(first, expected))
                first_bytes = (root / 'Media Squirrel.lnk').read_bytes()
                changed = {**expected, 'arguments': 'different.py'}
                self.assertTrue(startup.matches(startup.shortcut('write', value=changed), changed))
                startup.shortcut('restore', content=first['content'])
                self.assertEqual((root / 'Media Squirrel.lnk').read_bytes(), first_bytes)
                self.assertTrue(startup.matches(startup.shortcut(), expected))
                self.assertIsNone(startup.shortcut('remove'))
                self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
