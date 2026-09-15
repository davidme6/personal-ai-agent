import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import check_workspace
import init_workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='personal-agent-test-')
        self.root = Path(self.temp.name) / 'workspace with spaces'
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns('.git', '.personal', '.device', '__pycache__'))

    def tearDown(self):
        self.temp.cleanup()

    def test_initialize_is_idempotent_and_valid(self):
        self.assertTrue(init_workspace.initialize(self.root))
        profile = self.root / '.personal/profile.md'
        profile.write_text('keep me', encoding='utf-8')
        self.assertEqual(init_workspace.initialize(self.root), [])
        self.assertEqual(profile.read_text(encoding='utf-8'), 'keep me')
        self.assertEqual(check_workspace.check(self.root), 1)

    def test_registry_cannot_escape_workspace(self):
        with self.assertRaises(ValueError):
            check_workspace.inside(self.root, '../outside.md')


if __name__ == '__main__':
    unittest.main()
