"""Only fixture source trees and mocked launchers; never start a real service."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app import config, runtime


class RuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.source = self.temp / 'source'
        (self.source / 'app').mkdir(parents=True)
        (self.source / 'frontend' / 'src').mkdir(parents=True)
        (self.source / 'scripts_manifest').mkdir()
        self.write('run.py', 'entry = 1')
        self.write('app/main.py', 'backend = 1')
        self.write('weibo_downloader.py', 'downloader = 1')
        self.write('scripts_manifest/weibo.json', '{"name":"fixture"}')
        self.write('package.json', '{"version":"1.2.5"}')
        self.write('requirements.txt', 'fixture-dependency')
        self.write('frontend/src/main.js', 'const value = 1')
        self.write('frontend/package.json', '{}')
        self.stack.enter_context(patch.object(config, 'DATA_DIR', str(self.temp / 'data')))

    def write(self, relative, content):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    def test_runtime_signature_tracks_runtime_inputs_but_excludes_private_data_and_tests(self):
        before = runtime.runtime_identity(self.source)
        for filename in ('media/private.jpg', 'app_data/cookies.json', 'data/app.db',
                         'cache/private.py', 'tests/test_runtime.py', 'app/tests/test_private.py',
                         'app/__pycache__/private.py', 'frontend/tests/private.js',
                         'frontend/dist/assets/compiled.js', '.mediasquirrel.local.json'):
            self.write(filename, 'private fixture - must not affect identity')
        self.assertEqual(runtime.runtime_identity(self.source), before)
        for filename in ('app/main.py', 'weibo_downloader.py', 'scripts_manifest/weibo.json',
                         'frontend/src/main.js'):
            with self.subTest(filename=filename):
                current = runtime.runtime_identity(self.source)
                self.write(filename, 'updated fixture')
                after = runtime.runtime_identity(self.source)
                self.assertNotEqual(after['code_signature'], current['code_signature'])
                self.assertEqual(after['instance_id'], before['instance_id'])
                self.assertEqual(after['source_id'], before['source_id'])
        self.assertEqual(set(before), {'instance_id', 'source_id', 'code_signature'})
        self.assertNotIn(str(self.source), json.dumps(before))

    def test_identical_copy_keeps_code_and_data_identity_but_changes_source_identity(self):
        copied = self.temp / 'copied-source'
        shutil.copytree(self.source, copied)
        first, second = runtime.runtime_identity(self.source), runtime.runtime_identity(copied)
        self.assertEqual(first['instance_id'], second['instance_id'])
        self.assertEqual(first['code_signature'], second['code_signature'])
        self.assertNotEqual(first['source_id'], second['source_id'])
        self.assertEqual(runtime.runtime_identity(self.source / 'app' / '..'), first)

    def test_health_keeps_startup_snapshot_after_source_and_version_change(self):
        # Import the actual application under an isolated module name without
        # running lifespan, while all identity inputs point at fixture sources.
        module_path = Path(config.SOURCE_DIR) / 'app' / 'main.py'
        name = 'app._fixture_runtime_main'
        spec = importlib.util.spec_from_file_location(name, module_path)
        module = importlib.util.module_from_spec(spec)
        with patch.object(config, 'SOURCE_DIR', str(self.source)), \
                patch.object(config, 'RESOURCE_DIR', str(self.source)), \
                patch.dict(sys.modules, {name: module}):
            spec.loader.exec_module(module)
            before = module.api_health()
            self.write('app/main.py', 'backend = 2')
            self.write('package.json', '{"version":"2.0.0"}')
            self.write('frontend/src/main.js', 'const value = 2')
            self.assertNotEqual(runtime.runtime_identity()['code_signature'], before['code_signature'])
            with patch.object(runtime, 'runtime_identity', side_effect=AssertionError('health recomputed source')), \
                    patch.object(module, 'runtime_identity', side_effect=AssertionError('health recomputed source')):
                self.assertEqual(module.api_health(), before)
            self.assertEqual(before['version'], '1.2.5')
            before['source_id'] = 'caller mutation'
            self.assertNotEqual(module.api_health()['source_id'], 'caller mutation')
            self.assertNotIn(str(self.source), json.dumps(module.api_health()))


    def test_html_entry_and_spa_fallback_revalidate_without_changing_asset_headers(self):
        from fastapi.testclient import TestClient
        dist = self.source / 'frontend' / 'dist'
        (dist / 'assets').mkdir(parents=True)
        (dist / 'index.html').write_text('<html>fixture current entry</html>', encoding='utf-8')
        (dist / 'assets' / 'fixture.js').write_text('fixture asset', encoding='utf-8')
        name = 'app._fixture_html_cache_main'
        spec = importlib.util.spec_from_file_location(name, Path(config.SOURCE_DIR) / 'app' / 'main.py')
        module = importlib.util.module_from_spec(spec)
        with patch.object(config, 'FRONTEND_DIST', str(dist)), patch.dict(sys.modules, {name: module}):
            spec.loader.exec_module(module)
            client = TestClient(module.app, base_url='http://127.0.0.1:8642')
            try:
                for url in ('/', '/fixture-library-route'):
                    with self.subTest(url=url):
                        response = client.get(url, headers={'Accept': 'text/html'})
                        self.assertEqual(response.status_code, 200)
                        self.assertIn('fixture current entry', response.text)
                        self.assertEqual(response.headers['cache-control'], 'no-cache, max-age=0, must-revalidate')
                asset = client.get('/assets/fixture.js')
                self.assertEqual(asset.status_code, 200)
                self.assertNotEqual(asset.headers.get('cache-control'), 'no-cache, max-age=0, must-revalidate')
            finally:
                client.close()


class LauncherIdentityTests(unittest.TestCase):
    expected = {'instance_id': 'same-data', 'source_id': 'same-source', 'code_signature': 'same-code'}

    def setUp(self):
        import run
        self.launcher = run
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=False))
        self.stack.enter_context(patch.object(config, 'PORT', 8642))
        self.stack.enter_context(patch.object(sys, 'argv', ['run.py', '--no-tray']))
        self.identity = self.stack.enter_context(patch.object(runtime, 'runtime_identity', return_value=dict(self.expected)))
        self.probe = self.stack.enter_context(patch.object(runtime, 'probe_server', return_value=dict(self.expected)))
        self.build = self.stack.enter_context(patch.object(runtime, 'ensure_frontend', return_value='current'))
        self.stack.enter_context(patch.object(runtime, 'port_in_use', return_value=False))
        self.open = self.stack.enter_context(patch.object(run.webbrowser, 'open'))
        self.server = self.stack.enter_context(patch.object(run.uvicorn, 'Server'))
        self.stack.enter_context(patch.object(run.uvicorn, 'Config'))
        self.stack.enter_context(patch('builtins.print'))

    def test_matching_instance_is_reopened_only_after_read_only_build_validation(self):
        self.launcher.main()
        self.build.assert_called_once_with(build=False)
        self.open.assert_called_once_with('http://127.0.0.1:8642')
        self.server.assert_not_called()

    def test_matching_instance_respects_no_browser(self):
        sys.argv.append('--no-browser')
        self.launcher.main()
        self.build.assert_called_once_with(build=False)
        self.open.assert_not_called()
        self.server.assert_not_called()

    def test_stale_unknown_other_source_and_other_data_never_open_or_build(self):
        cases = [({'instance_id': 'same-data'}, '未提供启动代码标识'),
                 ({**self.expected, 'code_signature': 'old-code'}, '旧代码'),
                 ({**self.expected, 'source_id': 'other-source'}, '另一份源码'),
                 ({**self.expected, 'instance_id': 'other-data'}, '另一份数据')]
        for health, message in cases:
            with self.subTest(message=message):
                self.probe.return_value = health
                with self.assertRaisesRegex(SystemExit, message):
                    self.launcher.main()
                self.build.assert_not_called()
                self.open.assert_not_called()
                self.server.assert_not_called()

    def test_matching_code_with_stale_build_requires_normal_exit_without_building(self):
        self.build.return_value = 'stale'
        with self.assertRaisesRegex(SystemExit, '系统托盘'):
            self.launcher.main()
        self.build.assert_called_once_with(build=False)
        self.open.assert_not_called()
        self.server.assert_not_called()

    def test_new_instance_still_builds_before_starting_service(self):
        self.probe.return_value = None
        sys.argv.append('--no-browser')
        order = []
        service = MagicMock(started=True)
        self.build.side_effect = lambda: order.append('build') or 'built'
        self.server.side_effect = lambda *_args, **_kwargs: order.append('server') or service
        self.launcher.main()
        self.assertEqual(order, ['build', 'server'])
        self.build.assert_called_once_with()
        service.run.assert_called_once_with()
        self.open.assert_not_called()

    def test_console_startup_failure_returns_nonzero_for_batch_error_pause(self):
        self.probe.return_value = None
        sys.argv.append('--no-browser')
        self.server.return_value.started = False
        with self.assertRaisesRegex(SystemExit, '本地服务启动失败') as raised:
            self.launcher.main()
        self.assertNotEqual(raised.exception.code, 0)
        self.server.return_value.run.assert_called_once_with()
        self.open.assert_not_called()

    def test_matching_dev_instance_accepts_externally_served_frontend(self):
        self.build.return_value = 'external'
        with patch.dict(os.environ, {'MS_DEV': '1'}):
            self.launcher.main()
        self.build.assert_called_once_with(build=False)
        self.open.assert_called_once_with('http://127.0.0.1:8642')
        self.server.assert_not_called()

    def test_new_dev_reload_instance_keeps_uvicorn_reload_enabled(self):
        self.probe.return_value = None
        self.build.return_value = 'external'
        sys.argv.extend(['--reload', '--no-browser'])
        with patch.dict(os.environ, {'MS_DEV': '1'}), patch.object(self.launcher.uvicorn, 'run') as serve:
            self.launcher.main()
        self.build.assert_called_once_with()
        self.assertTrue(serve.call_args.kwargs['reload'])
        self.assertEqual(serve.call_args.args, ('app.main:app',))
        self.server.assert_not_called()
        self.open.assert_not_called()

    def test_wait_and_open_requires_the_expected_startup_identity(self):
        class InlineThread:
            def __init__(self, *, target, daemon):
                self.target = target
            def start(self):
                self.target()
        cases = [({'instance_id': 'same-data'}, False),
                 ({**self.expected, 'source_id': 'other-source'}, False),
                 ({**self.expected, 'code_signature': 'old-code'}, False),
                 (dict(self.expected), True)]
        with patch.object(self.launcher.threading, 'Thread', InlineThread):
            for health, should_open in cases:
                with self.subTest(health=health):
                    self.open.reset_mock()
                    self.probe.return_value = health
                    self.launcher._wait_and_open('http://127.0.0.1:8642', expected_identity=self.expected)
                    self.assertEqual(self.open.called, should_open)
        self.identity.assert_not_called()
        self.build.assert_not_called()
        self.server.assert_not_called()


if __name__ == '__main__':
    unittest.main()
