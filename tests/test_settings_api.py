from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, db, scheduling, settings, settings_api, watcher
from app.security import LocalOnlyMiddleware


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.library = self.root / 'library'
        self.library.mkdir()
        for key, value in {'DATA_DIR': str(self.root / 'data'), 'LIBRARY_ROOT': str(self.library),
                'DB_PATH': str(self.root / 'data' / 'app.db'), 'MAINTENANCE_ACTIVE': False}.items():
            self.stack.enter_context(patch.object(config, key, value))
        watcher.init_db()
        app = FastAPI()
        app.add_middleware(LocalOnlyMiddleware)
        app.include_router(settings_api.router)
        self.client = TestClient(app, base_url=f'http://127.0.0.1:{config.PORT}')

    def test_partial_schedule_patch_keeps_calendar_and_uuid(self):
        sub = watcher.add_sub('weibo', '123456', 'fixture', interval_minutes=30)
        response = self.client.post(f"/api/subs/{sub['id']}/schedules", json={
            'kind': 'weekly', 'timezone': 'Asia/Shanghai', 'times': ['09:00', '18:00'], 'weekdays': [1, 4]})
        self.assertEqual(response.status_code, 200, response.text)
        identity = response.json()['id']
        response = self.client.patch('/api/schedules/' + identity, json={'enabled': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['plan']['times'], ['09:00', '18:00'])
        self.assertEqual(response.json()['plan']['weekdays'], [1, 4])
        self.assertFalse(response.json()['enabled'])

    def test_maintenance_freezes_mutations_but_allows_reading(self):
        with patch.object(config, 'MAINTENANCE_ACTIVE', True):
            self.assertEqual(self.client.patch('/api/settings', json={'retries': 1}).status_code, 409)
            self.assertEqual(self.client.delete('/api/schedules/missing').status_code, 409)
            self.assertEqual(self.client.post('/api/settings/check-download', json={}).status_code, 409)
            self.assertEqual(self.client.get('/api/maintenance/plans').status_code, 200)

    def test_real_download_requires_explicit_link_and_isolated_destination(self):
        self.assertEqual(self.client.post('/api/settings/check-download', json={}).status_code, 400)
        create = AsyncMock(return_value={'id': 'diagnostic-task'})
        with patch.object(settings_api.task_manager, 'create', create):
            response = self.client.post('/api/settings/check-download', json={
                'confirmed': True, 'script_id': 'weibo', 'input': 'https://weibo.com/123/fixture'})
        self.assertEqual(response.status_code, 200, response.text)
        args, kwargs = create.call_args
        destination = Path(args[1]['out'])
        self.assertTrue(destination.is_relative_to(Path(config.DATA_DIR) / 'diagnostics' / 'downloads'))
        self.assertFalse(destination.is_relative_to(self.library))
        self.assertEqual(kwargs['metadata'], {'diagnostic': True})
        self.assertFalse(destination.exists())

    def test_preferences_validate_before_persist_and_keep_retention_permanent(self):
        with patch.object(settings, 'apply'):
            response = self.client.patch('/api/settings', json={'retries': 3, 'density': 'compact'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(settings.load()['retries'], 3)
            before = (Path(config.DATA_DIR) / 'settings.json').read_bytes()
            for body in ({'retries': 99}, {'default_download_dir': str(self.root / 'outside')}, {'logs_days': 30}):
                self.assertEqual(self.client.patch('/api/settings', json=body).status_code, 400)
                self.assertEqual((Path(config.DATA_DIR) / 'settings.json').read_bytes(), before)

    def test_diagnostic_export_contains_no_cookie_or_personal_paths(self):
        diagnostic = {'checks': [{'reason': 'Cookie: session=fixture-secret', 'path': str(self.root)}]}
        with patch.object(settings, 'diagnostics', return_value=diagnostic):
            result = self.client.post('/api/settings/diagnostics-export', json={})
        self.assertEqual(result.status_code, 200)
        response = self.client.get(result.json()['download_url'])
        self.assertNotIn('fixture-secret', response.text)
        self.assertNotIn(str(self.root).replace('\\', '\\\\'), response.text)
