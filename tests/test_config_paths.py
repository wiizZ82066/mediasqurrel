import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RuntimePathTests(unittest.TestCase):
    def probe(self, root, **values):
        env = {key: value for key, value in os.environ.items() if not key.startswith('MS_')}
        env.update(MS_HOME=str(root / 'default'), MS_PATHS_FILE=str(root / 'paths.json'))
        env.update(values)
        code = "import json; from app import config; print(json.dumps([config.DATA_DIR,config.LIBRARY_ROOT]))"
        return subprocess.run([sys.executable, '-B', '-c', code], env=env, text=True, capture_output=True)

    def test_path_selection_never_creates_or_migrates_data_at_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chosen = {'data_dir': str(root / 'chosen-data'), 'library_root': str(root / 'chosen-library')}
            (root / 'paths.json').write_text(json.dumps(chosen), encoding='utf-8')
            result = self.probe(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), list(chosen.values()))
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['paths.json'])
            legacy = root / 'portable'
            result = self.probe(root, MS_DATA_DIR=str(legacy))
            self.assertEqual(json.loads(result.stdout), [str(legacy / 'app_data'), str(legacy / 'library')])
            self.assertFalse(legacy.exists())

    def test_relative_configuration_fails_without_silent_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'paths.json').write_text(json.dumps({'library_root': '../escape'}), encoding='utf-8')
            self.assertNotEqual(self.probe(root).returncode, 0)
            self.assertFalse((root / 'default').exists())


if __name__ == '__main__':
    unittest.main()
