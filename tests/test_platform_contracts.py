"""Platform capability and access-error contracts use only synthetic fixtures."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import config, script_registry
from app.platform_errors import PlatformAccessError
from app.weibo_client import WeiboAccessError


class PlatformErrorTests(unittest.TestCase):
    def test_auth_challenge_unknown_and_rate_limit_never_switch_transport(self):
        for reason in ('login_required', 'risk_control', 'rate_limited', 'invalid_response',
                       'platform_error', 'new_unrecognized_failure'):
            with self.subTest(reason=reason):
                error = PlatformAccessError('safe message', reason, platform='douyin')
                self.assertFalse(error.fallback_allowed)
                self.assertFalse(error.retryable)
                self.assertEqual(error.reason, reason)

    def test_network_retry_is_distinct_from_fallback_and_missing_adapter(self):
        for reason in ('network_error', 'timeout'):
            error = PlatformAccessError('network failed', reason)
            self.assertTrue(error.retryable)
            self.assertFalse(error.fallback_allowed)
        for reason in ('unavailable', 'unsupported'):
            error = PlatformAccessError('adapter unavailable', reason)
            self.assertTrue(error.fallback_allowed)
            self.assertFalse(error.retryable)

    def test_weibo_preserves_existing_type_reason_and_redacts_public_message(self):
        error = WeiboAccessError('Cookie: fixture-private\nrequest failed', 'login_required')
        self.assertIsInstance(error, ValueError)
        self.assertIsInstance(error, PlatformAccessError)
        self.assertEqual(error.platform, 'weibo')
        self.assertEqual(error.reason, 'login_required')
        self.assertNotIn('fixture-private', str(error))
        self.assertIn('request failed', str(error))


class ManifestCapabilityTests(unittest.TestCase):
    def tearDown(self):
        script_registry.reload()

    def test_legacy_manifest_still_builds_without_advertising_extra_features(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'fixture.py'
            script.write_text('# synthetic fixture', encoding='utf-8')
            (root / 'fixture.json').write_text(json.dumps({'id': 'fixture', 'script': str(script),
                'params': [{'name': 'input', 'positional': True}]}), encoding='utf-8')
            with patch.object(config, 'MANIFEST_DIR', str(root)), \
                    patch.object(script_registry, '_launcher_argv', return_value=['fixture-python']):
                script_registry.reload()
                item = script_registry.get('fixture')
                self.assertEqual(item['capabilities'], {'download': True, 'search': False,
                    'subscription': False, 'media_types': [], 'detail_transports': []})
                self.assertEqual(script_registry.build_command('fixture', {'input': 'two words'}),
                                 ['fixture-python', str(script), 'two words'])

    def test_builtin_capabilities_are_declared_and_callers_cannot_mutate_cache(self):
        script_registry.reload()
        first = script_registry.get('douyin')
        self.assertTrue(first['capabilities']['subscription'])
        self.assertEqual(first['capabilities']['media_types'], ['image', 'video'])
        first['capabilities']['media_types'].clear()
        first['params'][0]['name'] = 'changed'
        second = next(item for item in script_registry.all_scripts() if item['id'] == 'douyin')
        self.assertEqual(second['capabilities']['media_types'], ['image', 'video'])
        self.assertEqual(second['params'][0]['name'], 'input')
        second['capabilities']['media_types'].clear()
        self.assertEqual(script_registry.get('douyin')['capabilities']['media_types'], ['image', 'video'])
        self.assertEqual(script_registry.get('weibo')['capabilities']['media_types'], ['image', 'live'])

    def test_malformed_capability_values_do_not_advertise_unsupported_behavior(self):
        actual = script_registry._capabilities({'capabilities': {
            'search': 'yes', 'subscription': 1, 'media_types': ['image', 'image', {}, 'executable'],
            'detail_transports': ['browser', 'unknown', None]}})
        self.assertFalse(actual['search'])
        self.assertFalse(actual['subscription'])
        self.assertEqual(actual['media_types'], ['image'])
        self.assertEqual(actual['detail_transports'], ['browser'])


if __name__ == '__main__':
    unittest.main()
