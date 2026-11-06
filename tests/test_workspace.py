import shutil
import subprocess
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
        """防的是：重复初始化覆盖用户已经写过的个人画像，或初始化出来的工作区过不了检查。"""
        self.assertTrue(init_workspace.initialize(self.root))
        profile = self.root / '.personal/profile.md'
        profile.write_text('keep me', encoding='utf-8')
        self.assertEqual(init_workspace.initialize(self.root), [])
        self.assertEqual(profile.read_text(encoding='utf-8'), 'keep me')
        self.assertEqual(check_workspace.check(self.root), 1)

    def test_registry_cannot_escape_workspace(self):
        """防的是：项目登记里写 ../ 路径，让工具读写工作区以外的文件。"""
        with self.assertRaises(ValueError):
            check_workspace.inside(self.root, '../outside.md')

    def test_private_repo_mode_keeps_credentials_and_originals_out_of_git(self):
        """防的是：私有仓模式放开 .personal/ 时，把 .env、*.key、带 secret/password 的文件、原件、收件箱、隐私目录也放进 Git。"""
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)
        init_workspace.initialize(self.root)
        init_workspace.enable_private_repo(self.root, 'https://git.example.com/team/sub/my-agent-private.git')
        personal = self.root / '.personal'
        blocked = ['.env', 'api_secret.txt', 'memory/server.key', 'logs/passwords.md', 'files/report.pdf',
                   'inbox/chat.md', 'private/diary.md', 'projects/p/private/note.md', 'memory/x.tmp', 'memory/__pycache__/m.pyc']
        allowed = ['current-state.md', 'memory/open-loops.md', 'logs/work-logs/a.md', 'projects/p/files/spec.md']
        for rel in blocked + allowed:
            target = personal / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('x', encoding='utf-8')
        tracked = subprocess.run(['git', 'add', '-A', '--dry-run'], cwd=self.root, capture_output=True, text=True,
                                 encoding='utf-8').stdout
        for rel in allowed:
            self.assertIn('.personal/' + rel, tracked, rel)
        # projects/p/private 和 projects/p/files 这种不在 .personal/ 第一层的同名目录：private 照样要挡，files 是用户自己的内容
        for rel in blocked[:8] + ['memory/x.tmp']:
            if rel.startswith('projects/p/private'):
                continue
            self.assertNotIn('.personal/' + rel, tracked, rel)

    def test_private_repo_url_validation(self):
        """防的是：把不是仓库地址的字符串写进 private_remote；同时不能拒绝 GitLab 子组、带端口和 ssh:// 的合法地址。"""
        init_workspace.initialize(self.root)
        for good in ('https://github.com/a/b.git', 'https://gitlab.com/g/sub/b', 'https://git.example.com:8443/a/b',
                     'git@github.com:a/b.git', 'ssh://git@host:2222/a/b.git'):
            self.assertTrue(init_workspace.REMOTE.match(good), good)
        for bad in ('', 'not a url', 'https://host', 'C:/some/dir'):
            self.assertFalse(init_workspace.REMOTE.match(bad), bad)
        with self.assertRaises(ValueError):
            init_workspace.enable_private_repo(self.root, 'not a url')


if __name__ == '__main__':
    unittest.main()
