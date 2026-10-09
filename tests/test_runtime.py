"""Source-launch preflight tests use local fixtures and mocked build/probes."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from app import config, runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def frontend(self):
        root = self.root / 'frontend'
        (root / 'src').mkdir(parents=True)
        (root / 'src' / 'main.js').write_text('first source', encoding='utf-8')
        (root / 'index.html').write_text('source entry', encoding='utf-8')
        (root / 'package.json').write_text('{}', encoding='utf-8')
        return root

    def test_instance_lock_conflict_and_release(self):
        first = runtime.InstanceLock(self.root / 'data').acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, '正在运行'):
                runtime.InstanceLock(self.root / 'data').acquire()
            independent = runtime.InstanceLock(self.root / 'other').acquire()
            independent.release()
        finally:
            first.release()
        reopened = runtime.InstanceLock(self.root / 'data').acquire()
        reopened.release()

    def test_same_data_path_has_stable_identity(self):
        with patch.object(config, 'DATA_DIR', str(self.root / 'data')):
            first = runtime.instance_id()
        with patch.object(config, 'DATA_DIR', str(self.root / 'nested' / '..' / 'data')):
            second = runtime.instance_id()
        self.assertEqual(first, second)

    def test_frontend_build_only_when_source_changes_without_installing(self):
        root = self.frontend()
        (root / 'node_modules' / 'vite').mkdir(parents=True)
        def build(*_args, **_kwargs):
            (root / 'dist' / 'assets').mkdir(parents=True, exist_ok=True)
            (root / 'dist' / 'index.html').write_text('<script src="/assets/main.js"></script>', encoding='utf-8')
            (root / 'dist' / 'assets' / 'main.js').write_text('main bundle', encoding='utf-8')
            (root / 'dist' / 'assets' / 'lazy.js').write_text('lazy bundle', encoding='utf-8')
        with patch.dict('os.environ', {'MS_DEV': '0'}), patch.object(runtime.shutil, 'which', return_value='npm-fixture'), \
                patch.object(runtime.subprocess, 'run', side_effect=build) as command:
            self.assertEqual(runtime.ensure_frontend(frontend=root, build=False), 'stale')
            self.assertEqual(runtime.ensure_frontend(frontend=root), 'built')
            self.assertEqual(runtime.ensure_frontend(frontend=root), 'current')
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0], ['npm-fixture', 'run', 'build'])
            (root / 'dist' / 'assets' / 'lazy.js').unlink()
            self.assertEqual(runtime.ensure_frontend(frontend=root, build=False), 'stale')
            self.assertEqual(runtime.ensure_frontend(frontend=root), 'built')
            (root / 'dist' / 'assets' / 'main.js').write_text('corrupt content', encoding='utf-8')
            self.assertEqual(runtime.ensure_frontend(frontend=root, build=False), 'stale')
            (root / 'src' / 'main.js').write_text('changed source', encoding='utf-8')
            self.assertEqual(runtime.ensure_frontend(frontend=root, build=False), 'stale')

    def test_build_missing_referenced_asset_never_gets_valid_stamp(self):
        root = self.frontend()
        (root / 'node_modules' / 'vite').mkdir(parents=True)
        (root / 'dist' / 'assets').mkdir(parents=True)
        (root / 'dist' / 'index.html').write_text('<script src="/assets/missing.js"></script>', encoding='utf-8')
        with patch.dict('os.environ', {'MS_DEV': '0'}), patch.object(runtime.shutil, 'which', return_value='npm-fixture'), \
                patch.object(runtime.subprocess, 'run'):
            with self.assertRaisesRegex(RuntimeError, '不完整'):
                runtime.ensure_frontend(frontend=root)
        self.assertFalse((root / 'dist' / '.source-hash').exists())

    def test_backend_exit_stops_tray_but_cancelled_monitor_does_not(self):
        from run import _watch_backend
        backend, tray = MagicMock(), MagicMock()
        backend.is_alive.side_effect = [True, False]
        _watch_backend(backend, tray, threading.Event())
        tray.stop.assert_called_once()
        stopped = threading.Event()
        stopped.set()
        tray.reset_mock()
        _watch_backend(backend, tray, stopped)
        tray.stop.assert_not_called()

    def test_missing_frontend_dependencies_are_actionable_without_install(self):
        root = self.frontend()
        with patch.dict('os.environ', {'MS_DEV': '0'}), patch.object(runtime.shutil, 'which', return_value=None), \
                patch.object(runtime.subprocess, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'npm ci'):
                runtime.ensure_frontend(frontend=root)
            command.assert_not_called()

    def test_health_probe_checks_application_and_ignores_environment_proxies(self):
        response = MagicMock()
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(runtime.urllib.request, 'build_opener', return_value=opener) as factory:
            response.read.return_value = json.dumps({'status': 'ok'}).encode()
            self.assertIsNone(runtime.probe_server(8642))
            response.read.return_value = json.dumps({'application': 'media-squirrel', 'instance_id': 'fixture'}).encode()
            self.assertEqual(runtime.probe_server(8642)['instance_id'], 'fixture')
            self.assertEqual(factory.call_args.args[0].proxies, {})
            response.read.assert_called_with(8192)

    def test_port_conflict_is_read_only(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertTrue(runtime.port_in_use(port))
        self.assertFalse(runtime.port_in_use(port))


if __name__ == '__main__':
    unittest.main()
