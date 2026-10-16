"""Real concurrent launchers with disposable data and a loopback fixture server."""
from contextlib import ExitStack
import errno
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config, runtime


def fixture_launcher(directory, role, port):
    """Exercise run.main; replace downloads/tray/UI with a controlled server."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import json
    import run

    directory = Path(directory)
    identity = {'application': 'media-squirrel', 'instance_id': 'fixture-data',
                'source_id': 'fixture-source', 'code_signature': 'fixture-code'}

    def wait_for(name):
        deadline = time.monotonic() + 15
        while not (directory / name).exists():
            if time.monotonic() > deadline:
                raise RuntimeError('fixture control timed out')
            time.sleep(.02)

    def build(*, build=True):
        if build:
            (directory / ('built-' + role)).touch()
        return 'current'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = json.dumps(identity).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_args):
            pass

    class Server:
        started = False

        def __init__(self, _config):
            pass

        def run(self):
            # The real lifespan keeps its independent single-writer lock.
            lock = runtime.InstanceLock().acquire()
            try:
                (directory / ('entered-' + role)).touch()
                wait_for('release-start')
                if role == 'crash':
                    os._exit(7)  # Prove OS cleanup, with no Python finally.
                with HTTPServer(('127.0.0.1', port), Handler) as server:
                    server.timeout = .1
                    self.started = True
                    (directory / ('ready-' + role)).touch()
                    while not (directory / 'stop').exists():
                        server.handle_request()
            finally:
                lock.release()

    sys.argv = ['run.py', '--no-tray', '--no-browser', '--no-watchdog', '--port', str(port)]
    with patch.object(runtime, 'runtime_identity', return_value=identity), \
            patch.object(runtime, 'ensure_frontend', side_effect=build), \
            patch.object(run.uvicorn, 'Server', Server):
        run.main()


class StartupCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(config, 'DATA_DIR', str(self.root)))

    def test_busy_launch_waits_for_health_without_acquiring_writer_lock(self):
        owner = runtime.InstanceLock(self.root, filename='.startup.lock').acquire()
        expected = {'application': 'media-squirrel', 'instance_id': 'fixture'}
        try:
            with patch.object(runtime, 'probe_server', side_effect=[None, expected]), \
                    patch.object(runtime.time, 'sleep') as wait:
                with runtime.coordinate_startup(9000, directory=self.root) as existing:
                    self.assertEqual(existing, expected)
                wait.assert_called_once_with(.2)
            self.assertFalse((self.root / '.instance.lock').exists())
        finally:
            owner.release()

    def test_timeout_leaves_owner_lock_held_and_does_not_launch(self):
        owner = runtime.InstanceLock(self.root, filename='.startup.lock').acquire()
        try:
            with patch.object(runtime, 'probe_server', return_value=None):
                with self.assertRaisesRegex(RuntimeError, '未启动第二个服务'):
                    with runtime.coordinate_startup(9000, directory=self.root, timeout=0):
                        self.fail('busy launcher must not enter startup')
            with self.assertRaises(runtime.InstanceBusyError):
                runtime.InstanceLock(self.root, filename='.startup.lock').acquire()
        finally:
            owner.release()

    def test_failed_start_releases_gate_for_retry_and_rechecks_health(self):
        with patch.object(runtime, 'probe_server', return_value=None):
            with self.assertRaisesRegex(ValueError, 'build failed'):
                with runtime.coordinate_startup(9000, directory=self.root):
                    raise ValueError('build failed')
            with runtime.coordinate_startup(9000, directory=self.root) as existing:
                self.assertIsNone(existing)
        expected = {'application': 'media-squirrel'}
        with patch.object(runtime, 'probe_server', side_effect=[None, expected]):
            with runtime.coordinate_startup(9000, directory=self.root) as existing:
                self.assertEqual(existing, expected)

    def test_filesystem_failure_is_not_mistaken_for_busy_startup(self):
        with patch.object(runtime, 'probe_server', return_value=None), \
                patch('builtins.open', side_effect=PermissionError(errno.EACCES, 'fixture denied')), \
                patch.object(runtime.time, 'sleep') as wait:
            with self.assertRaises(PermissionError):
                with runtime.coordinate_startup(9000, directory=self.root):
                    self.fail('unwritable directory must not enter startup')
            wait.assert_not_called()

    def wait_for(self, name, timeout=15):
        deadline = time.monotonic() + timeout
        while not (self.root / name).exists():
            if time.monotonic() > deadline:
                self.fail('fixture did not reach ' + name)
            time.sleep(.02)

    def launch(self, role, port):
        environment = {key: value for key, value in os.environ.items() if not key.startswith('MS_')}
        environment.update(MS_HOME=str(self.root), MS_APP_DATA_DIR=str(self.root),
                           MS_LIBRARY_DIR=str(self.root / 'library'),
                           MS_PATHS_FILE=str(self.root / 'unused.json'),
                           PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        output = self.stack.enter_context(open(self.root / (role + '.txt'), 'w', encoding='utf-8'))
        child = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()),
                                  '--fixture', str(self.root), role, str(port)],
                                 cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        def cleanup():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
        self.stack.callback(cleanup)
        return child

    def wait_for_contention(self, role):
        deadline = time.monotonic() + 15
        while '等待服务就绪' not in (self.root / (role + '.txt')).read_text(encoding='utf-8'):
            if time.monotonic() > deadline:
                self.fail('second launcher did not wait for first')
            time.sleep(.02)

    def free_port(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            return listener.getsockname()[1]

    def test_simultaneous_launches_start_one_server_and_reuse_after_slow_start(self):
        port = self.free_port()
        first = self.launch('first', port)
        self.wait_for('entered-first')
        second = self.launch('second', port)
        try:
            # Wait for explicit contention evidence, not a timing assumption.
            self.wait_for_contention('second')
            self.assertIsNone(second.poll())
            self.assertFalse((self.root / 'built-second').exists())
            (self.root / 'release-start').touch()
            self.wait_for('ready-first')
            self.assertEqual(second.wait(timeout=10), 0)
            output = (self.root / 'second.txt').read_text(encoding='utf-8')
            self.assertIn('已在运行', output)
            self.assertNotIn('Traceback', output)
            self.assertFalse((self.root / 'entered-second').exists())
            self.assertIsNone(first.poll())
        finally:
            (self.root / 'stop').touch()
        self.assertEqual(first.wait(timeout=10), 0)
        self.assertFalse((self.root / 'app.db').exists())

    def test_crashed_owner_releases_both_locks_and_waiting_launcher_recovers(self):
        port = self.free_port()
        first = self.launch('crash', port)
        self.wait_for('entered-crash')
        second = self.launch('recovery', port)
        try:
            self.wait_for_contention('recovery')
            (self.root / 'release-start').touch()
            self.assertEqual(first.wait(timeout=10), 7)
            self.wait_for('ready-recovery')
            self.assertIsNone(second.poll())
            self.assertTrue((self.root / 'built-recovery').exists())
        finally:
            (self.root / 'stop').touch()
        self.assertEqual(second.wait(timeout=10), 0)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--fixture':
        fixture_launcher(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    else:
        unittest.main()
