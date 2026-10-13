"""Synthetic CLI envelopes and local helper processes; no platform requests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from app import douyin_detail_adapter as adapter
from app.platform_errors import PlatformAccessError


ITEM = '7123456789012345678'


def envelope(**changes):
    result = {'ok': True, 'platform': 'douyin', 'endpoint': 'web', 'resource': 'item',
              'action': 'get', 'account': 'fixture', 'error': None, 'page': None,
              'data': {'aweme_id': ITEM, 'author': {'nickname': 'fixture'},
                       'images': [{'url_list': ['https://fixture.invalid/a.jpg']}],
                       'is_top': True, 'text_extra': [{'hashtag_name': 'fixture'}]}}
    result.update(changes)
    return json.dumps(result, ensure_ascii=False).encode('utf-8')


class DetailAdapterTests(unittest.TestCase):
    def setUp(self):
        windows = patch.object(adapter, 'WINDOWS_ADAPTER', True)
        windows.start()
        self.addCleanup(windows.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.node = self.root / ('node.exe' if os.name == 'nt' else 'node')
        self.node.write_bytes(b'fixture-not-an-executable')
        self.package = self.root / 'catbus'
        self.entry = self.package / 'dist' / 'cli' / 'main.js'
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text('// fixture', encoding='utf-8')
        (self.package / 'package.json').write_text(json.dumps({
            'name': 'catbus-cli', 'bin': {'catbus': 'dist/cli/main.js'},
            'engines': {'node': adapter.SUPPORTED_ENGINES}}), encoding='utf-8')
        self.home = self.root / 'standalone-catbus-home'
        self.home.mkdir()
        self.env = dict(zip(adapter.ENV_KEYS, map(str, (self.node, self.entry, self.home))))

    def assert_reason(self, reason, function, *args, **kwargs):
        with self.assertRaises(PlatformAccessError) as raised:
            function(*args, **kwargs)
        self.assertEqual(reason, raised.exception.reason)
        return raised.exception

    def test_unconfigured_status_never_launches_or_discovers_credentials(self):
        with patch.object(adapter, '_run_bounded') as run:
            status = adapter.configuration_status(env={})
        self.assertFalse(status['configured'])
        self.assertFalse(status['available'])
        run.assert_not_called()

    def test_non_windows_preflight_never_launches_cli_or_reads_paths(self):
        with patch.object(adapter, 'WINDOWS_ADAPTER', False), \
                patch.object(adapter, '_run_bounded') as run, \
                patch.object(adapter, 'Path') as path:
            status = adapter.configuration_status(env=self.env)
            self.assertFalse(status['available'])
            self.assertEqual('unavailable', status['reason'])
            self.assertIn('仅支持 Windows', status['message'])
            error = self.assert_reason('unavailable', adapter.fetch_detail, ITEM, env=self.env)
            self.assertTrue(error.fallback_allowed)
            run.assert_not_called()
            path.assert_not_called()

    def test_readiness_only_runs_node_version_and_exposes_no_private_paths(self):
        with patch.object(adapter, '_run_bounded', return_value=(0, b'v24.15.0\n', b'')) as run:
            status = adapter.configuration_status(env=self.env)
        self.assertTrue(status['available'])
        self.assertIn('尚未校验', status['message'])
        self.assertNotIn(str(self.root), str(status))
        self.assertEqual([str(self.node), '--version'], run.call_args.args[0])
        self.assertEqual(str(self.home), run.call_args.kwargs['env']['CATBUS_HOME'])

    def test_incompatible_node_versions_are_locally_unavailable(self):
        for version in (b'v24.13.0', b'v22.22.1', b'v23.9.0', b'v25.5.0', b'invalid'):
            with self.subTest(version=version), patch.object(adapter, '_run_bounded',
                                                              return_value=(0, version, b'')):
                self.assert_reason('unavailable', adapter.fetch_detail, ITEM, env=self.env)

    def test_supported_node_versions(self):
        for version in (b'v22.22.2', b'v24.15.0', b'v26.0.0', b'v28.1.1'):
            with self.subTest(version=version), patch.object(adapter, '_run_bounded',
                                                              return_value=(0, version, b'')):
                self.assertTrue(adapter.configuration_status(env=self.env)['available'])

    def test_explicit_absolute_paths_and_manifest_are_required(self):
        for key in adapter.ENV_KEYS:
            with self.subTest(key=key), patch.object(adapter, '_run_bounded') as run:
                status = adapter.configuration_status(env={**self.env, key: 'relative'})
                self.assertFalse(status['available'])
                run.assert_not_called()
        (self.package / 'package.json').write_text('{}', encoding='utf-8')
        with patch.object(adapter, '_run_bounded') as run:
            self.assertFalse(adapter.configuration_status(env=self.env)['available'])
            run.assert_not_called()

    def test_fixed_read_only_command_preserves_raw_details(self):
        env = {**self.env, 'NODE_OPTIONS': '--require private.js', 'NODE_PATH': 'private'}
        with patch.object(adapter, '_run_bounded', side_effect=[
                (0, b'v26.0.0', b''), (0, envelope(), b'Cookie: never log this')]) as run:
            detail = adapter.fetch_detail(ITEM, env=env)
        self.assertTrue(detail['is_top'])
        self.assertEqual('fixture', detail['text_extra'][0]['hashtag_name'])
        self.assertEqual([str(self.node), str(self.entry), 'douyin', 'item', 'get', ITEM,
                          '-e', 'web', '-o', 'json', '--raw', '--quiet'], run.call_args.args[0])
        self.assertEqual(str(self.home), run.call_args.kwargs['env']['CATBUS_HOME'])
        self.assertNotIn('NODE_OPTIONS', run.call_args.kwargs['env'])
        self.assertNotIn('NODE_PATH', run.call_args.kwargs['env'])
        self.assertEqual(2, run.call_count)

    def test_unsafe_or_unresolved_input_never_starts_process(self):
        for value in ('--cookie=secret', 'https://v.douyin.com/short/', ITEM + ';whoami', 123, ''):
            with self.subTest(value=value), patch.object(adapter, '_run_bounded') as run:
                self.assert_reason('invalid_response', adapter.fetch_detail, value, env=self.env)
                run.assert_not_called()

    def test_auth_and_risk_exit_codes_never_fallback_even_without_json(self):
        for code, reason in ((3, 'login_required'), (5, 'risk_control')):
            error = self.assert_reason(reason, adapter._parse_response,
                                       b'Cookie: sessionid=secret; token=private', code, ITEM)
            self.assertFalse(error.fallback_allowed)
            self.assertNotIn('secret', str(error))
            self.assertNotIn('private', str(error))

    def test_upstream_error_details_are_never_exposed(self):
        cases = [('AUTH_EXPIRED', 3, 'login_required', False),
                 ('RISK_CONTROL', 5, 'risk_control', False),
                 ('NETWORK', 6, 'network_error', False),
                 ('UPSTREAM', 7, 'platform_error', False),
                 ('ERROR', 1, 'platform_error', False),
                 ('UNSUPPORTED', 2, 'unsupported', True),
                 ('NOT_IMPLEMENTED', 4, 'unsupported', True)]
        for code, exit_code, reason, fallback in cases:
            with self.subTest(code=code):
                raw = envelope(ok=False, data=None, error={'code': code,
                    'message': 'Cookie: sessionid=secret', 'hint': 'Token=private',
                    'detail': {'path': str(self.home)}})
                error = self.assert_reason(reason, adapter._parse_response, raw, exit_code, ITEM)
                self.assertEqual(fallback, error.fallback_allowed)
                self.assertNotIn('secret', str(error))
                self.assertNotIn('private', str(error))
                self.assertNotIn(str(self.home), str(error))

    def test_empty_malformed_wrong_platform_or_item_never_fallback(self):
        for raw in (b'', b'{}', b'[]', b'<html>challenge</html>', envelope(platform='weibo'),
                    envelope(data={'aweme_id': '99999', 'author': {}, 'video': {}}),
                    envelope(data={'id': ITEM}), envelope(data={'aweme_id': ITEM})):
            with self.subTest(raw=raw[:50]):
                error = self.assert_reason('invalid_response', adapter._parse_response, raw, 0, ITEM)
                self.assertFalse(error.fallback_allowed)

    def test_nonzero_exit_with_success_envelope_is_not_success(self):
        self.assert_reason('invalid_response', adapter._parse_response, envelope(), 1, ITEM)

    def test_inconsistent_unsupported_envelope_cannot_enable_fallback(self):
        for raw, code in ((envelope(ok=False, error={'code': 'UNSUPPORTED'}), 0),
                          (envelope(ok=False, error={'code': 'NOT_IMPLEMENTED'}), 1),
                          (envelope(ok=False, platform='weibo', error={'code': 'UNSUPPORTED'}), 2)):
            error = self.assert_reason('invalid_response', adapter._parse_response, raw, code, ITEM)
            self.assertFalse(error.fallback_allowed)

    def test_invalid_timeout_prevents_launch(self):
        for timeout in (True, 0, -1, 121, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), patch.object(adapter, '_run_bounded') as run:
                with self.assertRaises(ValueError):
                    adapter.fetch_detail(ITEM, env=self.env, timeout=timeout)
                run.assert_not_called()


class BoundedProcessTests(unittest.TestCase):
    def run_helper(self, source, **kwargs):
        return adapter._run_bounded([sys.executable, '-c', source], env=dict(os.environ),
                                    timeout=kwargs.pop('timeout', 5), **kwargs)

    def test_drains_stdout_and_stderr_without_deadlock(self):
        code, stdout, stderr = self.run_helper(
            "import sys; sys.stderr.buffer.write(b'e'*40000); sys.stdout.buffer.write(b'o'*40000)")
        self.assertEqual(0, code)
        self.assertEqual(b'o' * 40000, stdout)
        self.assertEqual(b'e' * 40000, stderr)

    def test_output_limits_stop_both_streams(self):
        for stream in ('stdout', 'stderr'):
            with self.subTest(stream=stream):
                self.assertRaises(PlatformAccessError, self.run_helper,
                                  f"import sys,time; sys.{stream}.buffer.write(b'x'*50000); sys.{stream}.flush(); time.sleep(30)",
                                  stdout_limit=1024, stderr_limit=1024)

    def test_timeout_stops_process_without_echoing_output(self):
        started = time.monotonic()
        with self.assertRaises(PlatformAccessError) as raised:
            self.run_helper("import time; print('Token=private',flush=True); time.sleep(30)", timeout=0.15)
        self.assertEqual('timeout', raised.exception.reason)
        self.assertNotIn('private', str(raised.exception))
        self.assertLess(time.monotonic() - started, 8)

    def test_cancel_before_launch_and_while_running(self):
        with patch.object(adapter.subprocess, 'Popen') as popen:
            with self.assertRaises(adapter.AdapterCancelled):
                self.run_helper('pass', cancelled=lambda: True)
            popen.assert_not_called()
        started = time.monotonic()
        with self.assertRaises(adapter.AdapterCancelled):
            self.run_helper('import time; time.sleep(30)', cancelled=lambda: time.monotonic() - started > 0.1)
        self.assertLess(time.monotonic() - started, 8)


if __name__ == '__main__':
    unittest.main()
