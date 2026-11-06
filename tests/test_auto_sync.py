"""tools/auto_sync.py：没设置私有仓就跳过、只推归档与状态、门禁记录、失败时保留工作区。"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'tools'))
import auto_sync  # noqa: E402
from git_fixture import Repos, git, write  # noqa: E402

WORK_LOG = '.personal/logs/work-logs/2026-01-01_笔记.md'
SECRET = 'sk-' + 'A1b2C3d4' * 4   # 运行时拼出来，源码里不出现完整的假密钥


class AutoSyncTests(unittest.TestCase):
    def setUp(self):
        self.repos = Repos()
        self.addCleanup(self.repos.cleanup)
        self.here = self.repos.here
        patches = (mock.patch.object(auto_sync, 'notify', return_value=True),
                   mock.patch.object(auto_sync, 'PULL_DELAY', 0),
                   mock.patch.object(auto_sync, 'WRITE_DELAY', 0))
        self.notify = patches[0].start()
        for item in patches[1:]:
            item.start()
        for item in patches:
            self.addCleanup(item.stop)

    def run_sync(self):
        with redirect_stdout(io.StringIO()):
            return auto_sync.run_once(self.here)

    def log_text(self):
        path = self.here / '.local/auto_sync.log'
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def test_skips_when_private_remote_is_not_set(self):
        """防：用户还没设置私有仓时，计划任务把东西推到 origin（可能是公开模板）。"""
        self.repos.set_settings(private_remote='')
        write(self.here, WORK_LOG, '今天的日志\n')
        count = self.repos.commit_count()
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(self.repos.commit_count(), count)
        self.assertIn('未设置私有仓', self.log_text())
        self.notify.assert_not_called()

    def test_commits_only_archive_and_state_paths(self):
        """防：无人值守任务把代码、普通文件也一起提交推送。"""
        write(self.here, WORK_LOG, '今天的日志\n')
        write(self.here, '.personal/memory/偏好.md', '喜欢简短\n')
        write(self.here, 'tools/新工具.py', 'print(1)\n')
        write(self.here, '笔记/规则.md', '本机改了\n')
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(self.repos.committed_files(), sorted([WORK_LOG, '.personal/memory/偏好.md']))
        self.assertEqual(self.repos.cloud_head(), self.repos.head())
        status = git(self.here, 'status', '--porcelain', '--untracked-files=all')
        self.assertIn('tools/新工具.py', status)
        self.assertIn('笔记/规则.md', status)

    def test_generates_records_that_pass_the_task_card_gate(self):
        """防：开了 require_task_card 后自动同步永远被门禁拦下。"""
        self.repos.set_settings(require_task_card=True)
        write(self.here, WORK_LOG, '今天的日志\n')
        self.assertEqual(self.run_sync(), 0, self.log_text())
        committed = self.repos.committed_files()
        self.assertIn('.personal/current-state.md', committed)
        self.assertTrue(any(rel.startswith('.personal/logs/task-cards/') for rel in committed))
        self.assertIn('AUTO-SYNC-', self.repos.read(self.here, '.personal/current-state.md'))
        self.assertEqual(self.repos.cloud_head(), self.repos.head())

    def test_failure_keeps_worktree_and_withdraws_generated_records(self):
        """防：门禁拦下后强推、丢掉工作区改动，或留下写着“通过”的假任务卡。"""
        self.repos.set_settings(require_task_card=True)
        text = f'不小心贴进来的 key: {SECRET}\n'
        write(self.here, WORK_LOG, text)
        count = self.repos.commit_count()
        self.assertEqual(self.run_sync(), 1)
        self.assertEqual(self.repos.commit_count(), count)
        self.assertEqual(self.repos.read(self.here, WORK_LOG), text)
        self.assertEqual(self.repos.staged(), [])
        self.assertFalse((self.here / '.personal/current-state.md').exists())
        self.assertFalse(list((self.here / '.personal/logs/task-cards').glob('*.md')))
        self.assertIn('[停止]', self.log_text())
        self.notify.assert_called_once()

    def test_ignored_archive_paths_are_reported(self):
        """防：.personal/ 还被 .gitignore 忽略时，每天“没有改动”地静默跳过，用户以为同步过了。"""
        write(self.here, '.gitignore', '.personal/\n.device/\n.local/\n')
        write(self.here, WORK_LOG, '今天的日志\n')
        count = self.repos.commit_count()
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(self.repos.commit_count(), count)
        self.assertIn('.gitignore', self.log_text())
        self.notify.assert_called_once()


class UpdateFileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='agent-auto-sync-test-'))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_retries_when_file_is_busy_and_rereads_before_writing(self):
        """防：文件被占用（Errno 22）时直接失败；重试时用旧内容盖掉别人刚写进去的内容。"""
        target = self.root / 'current-state.md'
        target.write_text('# 状态\n', encoding='utf-8')
        real_replace, calls = os.replace, []

        def busy_once(src, dst):
            calls.append(dst)
            if len(calls) == 1:
                Path(dst).write_text('# 状态\n别人刚写的\n', encoding='utf-8')
                raise OSError(22, 'Invalid argument')
            return real_replace(src, dst)

        with mock.patch.object(auto_sync, 'WRITE_DELAY', 0), mock.patch.object(auto_sync.os, 'replace', busy_once):
            auto_sync.update_file(target, lambda old: old + '自动同步一段\n')
        self.assertEqual(len(calls), 2)
        self.assertEqual(target.read_text(encoding='utf-8'), '# 状态\n别人刚写的\n自动同步一段\n')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['current-state.md'])


if __name__ == '__main__':
    unittest.main()
