import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import cloud_sync

FAKE_RCLONE = '/fake/bin/rclone'


class FakeRclone:
    """代替真的 rclone：记下每次调用的参数，按需要返回版本号和退出码。"""

    def __init__(self, version='rclone v1.68.2', code=0, output=''):
        self.version, self.code, self.output, self.calls = version, code, output, []

    def __call__(self, args, **kwargs):
        args = list(args)
        if args[1:] == ['version']:
            return subprocess.CompletedProcess(args, 0, stdout=self.version + '\n- os/type: test\n', stderr='')
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.code, stdout='', stderr=self.output)

    def bisync_calls(self):
        return [call for call in self.calls if call[1] == 'bisync']


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cloud-sync-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '工作 区'
        (self.root / '.personal/inbox').mkdir(parents=True)
        self.settings({'cloud': {'remote': 'gdrive:agent-files', 'max_delete_percent': 7}})

    def settings(self, data):
        (self.root / '.personal/agent.json').write_text(json.dumps(data), encoding='utf-8')

    def run_cli(self, *args, fake=None, which=FAKE_RCLONE):
        fake = fake or FakeRclone()
        out = io.StringIO()
        with mock.patch.object(cloud_sync.shutil, 'which', return_value=which), \
                mock.patch.object(cloud_sync.subprocess, 'run', side_effect=fake), \
                contextlib.redirect_stdout(out):
            code = cloud_sync.main([*args, '--root', str(self.root)])
        return code, out.getvalue(), fake

    def init(self):
        code, _, _ = self.run_cli('init', '--yes')
        self.assertEqual(code, 0)

    def test_run_keeps_both_versions_on_conflict_and_limits_deletes(self):
        """防止两边都改的文件被一方静默覆盖，或一次误删大批文件被同步到另一边。"""
        self.init()
        code, _, fake = self.run_cli('run')
        self.assertEqual(code, 0)
        command = fake.bisync_calls()[0]
        self.assertEqual(command[:4], [FAKE_RCLONE, 'bisync', str((self.root / '.personal/inbox').resolve()),
                                       'gdrive:agent-files'])
        for flag, value in (('--conflict-resolve', 'none'), ('--conflict-loser', 'num'),
                            ('--conflict-suffix', 'conflict'), ('--max-delete', '7'),
                            ('--filters-file', str(self.root.resolve() / '.local/cloud_filters.txt'))):
            self.assertEqual(command[command.index(flag) + 1], value, flag)
        for flag in ('--resilient', '--recover', '--backup-dir1'):
            self.assertIn(flag, command)
        for flag in ('--resync', '--dry-run', '--force'):
            self.assertNotIn(flag, command)

    def test_plan_is_dry_run(self):
        """防止“只看看差异”的 plan 真的动了文件。"""
        self.init()
        code, _, fake = self.run_cli('plan')
        self.assertEqual(code, 0)
        self.assertIn('--dry-run', fake.bisync_calls()[0])
        self.assertNotIn('--resync', fake.bisync_calls()[0])

    def test_init_without_yes_only_previews(self):
        """防止首次建基线（会以并集互相复制）在用户确认之前就执行。"""
        code, text, fake = self.run_cli('init')
        self.assertEqual(code, 0)
        self.assertTrue(fake.bisync_calls())
        self.assertTrue(all('--dry-run' in call for call in fake.bisync_calls()))
        self.assertIn('并集', text)
        self.assertFalse((self.root / cloud_sync.STATE).exists())
        code, _, _ = self.run_cli('run')
        self.assertEqual(code, 2)   # 没建基线不能直接同步

    def test_init_with_yes_runs_resync_once(self):
        """防止确认后的首次基线没带 --resync，或者仍是预览、没有真正建立。"""
        code, _, fake = self.run_cli('init', '--yes')
        self.assertEqual(code, 0)
        [call] = fake.bisync_calls()
        self.assertIn('--resync', call)
        self.assertNotIn('--dry-run', call)
        self.assertTrue((self.root / cloud_sync.STATE).exists())

    def test_filters_exclude_private_and_local_only(self):
        """防止 .personal/private/ 和 local_only 里的文件被上传到云盘。"""
        self.settings({'local_only': ['.personal/private/', '.device/', '.local/', '.personal/files/身份 证件/'],
                       'cloud': {'remote': 'gdrive:x', 'local_path': '.personal'}})
        self.init()
        rules = (self.root / cloud_sync.FILTERS).read_text(encoding='utf-8')
        self.assertIn('- /private/**', rules)
        self.assertIn('- /files/身份 证件/**', rules)
        self.assertIn('- ~$*', rules)
        self.assertNotIn('+ ', rules)

    def test_filters_text_is_stable(self):
        """防止过滤规则每次内容不同（比如带时间），让 rclone 每次都要求重新建基线。"""
        self.init()
        first = (self.root / cloud_sync.FILTERS).read_text(encoding='utf-8')
        code, _, _ = self.run_cli('run')
        self.assertEqual(code, 0)
        self.assertEqual((self.root / cloud_sync.FILTERS).read_text(encoding='utf-8'), first)

    def test_changed_local_only_requires_new_baseline(self):
        """防止改了 local_only 后直接同步，被 rclone 中途中止或按旧规则同步。"""
        self.init()
        self.settings({'local_only': ['.personal/private/', '.personal/inbox/new-secret/'],
                       'cloud': {'remote': 'gdrive:agent-files'}})
        code, text, fake = self.run_cli('run')
        self.assertEqual(code, 2)
        self.assertEqual(fake.bisync_calls(), [])
        self.assertIn('init', text)

    def test_local_path_inside_local_only_is_refused(self):
        """防止把 local_only 或 .personal/private/ 整个设成同步目录，导致隐私上云。"""
        self.settings({'cloud': {'remote': 'gdrive:x', 'local_path': '.personal/private/docs'}})
        code, text, fake = self.run_cli('init', '--yes')
        self.assertEqual(code, 2)
        self.assertEqual(fake.bisync_calls(), [])
        self.assertIn('只留本机', text)

    def test_missing_remote_or_rclone_gives_clear_message(self):
        """防止没配远端或没装 rclone 时报一堆看不懂的错误，或定时任务一直显示失败。"""
        self.settings({})
        code, text, _ = self.run_cli('run')
        self.assertEqual(code, 2)
        self.assertIn('cloud.remote', text)
        self.settings({'cloud': {'remote': 'gdrive:x'}})
        code, text, _ = self.run_cli('run', which=None)
        self.assertEqual(code, 2)
        self.assertIn('rclone.org/downloads', text)
        code, text, _ = self.run_cli('run', '--scheduled', which=None)
        self.assertEqual(code, 0)
        self.assertEqual(text, '')
        self.assertIn('没找到 rclone', (self.root / cloud_sync.LOG).read_text(encoding='utf-8'))

    def test_old_rclone_is_refused(self):
        """防止旧版 rclone 不认识冲突参数，或用旧的冲突处理方式覆盖文件。"""
        code, text, fake = self.run_cli('init', '--yes', fake=FakeRclone(version='rclone v1.65.2'))
        self.assertEqual(code, 2)
        self.assertEqual(fake.bisync_calls(), [])
        self.assertIn('1.66', text)

    def test_conflict_copies_are_reported_after_run(self):
        """防止两边都改产生的冲突副本没人发现，长期两份并存。"""
        self.init()
        (self.root / '.personal/inbox/报告.conflict1.docx').write_bytes(b'a')
        (self.root / '.personal/inbox/报告.conflict2.docx').write_bytes(b'b')
        code, text, _ = self.run_cli('run')
        self.assertEqual(code, 0)
        self.assertIn('2 个冲突副本', text)
        self.assertIn('报告.conflict1.docx', text)

    def test_too_many_deletes_is_explained(self):
        """防止删除比例超限被中止后，用户不知道原因而去强行覆盖。"""
        self.init()
        fake = FakeRclone(code=1, output='ERROR : Safety abort: too many deletes (>7%, 5 of 10) on Path1.')
        code, text, _ = self.run_cli('run', fake=fake)
        self.assertEqual(code, 1)
        self.assertIn('超过 7%', text)


if __name__ == '__main__':
    unittest.main()
