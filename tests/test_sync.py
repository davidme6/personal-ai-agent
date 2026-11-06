"""tools/sync.py：拉取的三方合并与分叉保留、推送的边界和撞车处理。用临时目录里的真 Git 仓库。"""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'tools'))
import sync  # noqa: E402
from git_fixture import TEXT, Repos, git, write  # noqa: E402

RULES, OTHER = '笔记/规则.md', '笔记/别的.md'


class SyncCase(unittest.TestCase):
    def setUp(self):
        self.repos = Repos()
        self.addCleanup(self.repos.cleanup)
        self.here = self.repos.here

    def read(self, rel):
        return self.repos.read(self.here, rel)

    def push(self, files, message='改规则', **kwargs):
        with redirect_stdout(io.StringIO()):
            return sync.push(self.here, message, files, **kwargs)


class PullTests(SyncCase):
    def test_fast_forward_keeps_unrelated_local_edit(self):
        """防：拉取云端新提交时冲掉本机还没提交的其他改动。"""
        write(self.here, OTHER, '本机没提交的改动\n')
        head = self.repos.commit_other(RULES, TEXT + '云端加的\n')
        report = sync.pull(self.here)
        self.assertEqual(len(report['commits']), 1)
        self.assertEqual(self.repos.head(), head)
        self.assertEqual(self.read(RULES), TEXT + '云端加的\n')
        self.assertEqual(self.read(OTHER), '本机没提交的改动\n')
        self.assertEqual(sync.pull(self.here)['commits'], [])

    def test_edits_in_different_places_are_merged_and_backed_up(self):
        """防：两边都改过同一个文件时丢掉其中一边；合并前的本机版本要留备份。"""
        mine = '本机改了第一行\n' + TEXT.split('\n', 1)[1]
        write(self.here, RULES, mine)
        self.repos.commit_other(RULES, TEXT + '云端加的\n')
        report = sync.pull(self.here)
        self.assertEqual((report['merged'], report['forks']), ([RULES], []))
        self.assertEqual(self.read(RULES), mine + '云端加的\n')
        self.assertEqual((self.here / report['backup'] / RULES).read_bytes().decode('utf-8'), mine)

    def test_same_place_conflict_keeps_local_version(self):
        """防：合不上时用云端版本覆盖本机的改动。"""
        write(self.here, RULES, '本机版本\n')
        head = self.repos.commit_other(RULES, '云端版本\n')
        report = sync.pull(self.here)
        self.assertEqual(report['forks'], [(RULES, '本机和云端改了同一处')])
        self.assertEqual(self.read(RULES), '本机版本\n')
        self.assertEqual(self.repos.head(), head)

    def test_cloud_adds_file_that_exists_untracked_locally(self):
        """防：云端新增的同名文件顶掉本机还没跟踪的文件。"""
        write(self.here, '笔记/新.md', '本机的\n')
        self.repos.commit_other('笔记/新.md', '云端的\n')
        report = sync.pull(self.here)
        self.assertEqual([rel for rel, _ in report['forks']], ['笔记/新.md'])
        self.assertEqual(self.read('笔记/新.md'), '本机的\n')

    def test_cloud_delete_does_not_remove_local_edit(self):
        """防：云端删了文件、本机改过它时，拉取把本机的改动一起删掉。"""
        write(self.here, OTHER, '本机改过\n')
        git(self.repos.other, 'rm', '--quiet', '--', OTHER)
        git(self.repos.other, 'commit', '--quiet', '-m', '删掉')
        git(self.repos.other, 'push', '--quiet', 'origin', 'main')
        report = sync.pull(self.here)
        self.assertEqual(report['forks'], [(OTHER, '云端已删除，本机改过')])
        self.assertEqual(self.read(OTHER), '本机改过\n')

    def test_diverged_history_stops(self):
        """防：本机和云端各有对方没有的提交时脚本自己乱合。"""
        write(self.here, OTHER, '本机提交\n')
        git(self.here, 'commit', '--quiet', '-am', '本机提交')
        self.repos.commit_other(RULES, '云端提交\n')
        with self.assertRaises(sync.Stop):
            sync.pull(self.here)
        self.assertEqual(self.read(RULES), TEXT)


class PushTests(SyncCase):
    def test_commits_only_listed_files_and_reads_back(self):
        """防：顺手把没列出的改动也提交上去；推送后不核对云端就报成功。"""
        write(self.here, RULES, '要提交的\n')
        write(self.here, OTHER, '不提交的\n')
        head = self.push([RULES])
        self.assertEqual(self.repos.cloud_head(), head)
        self.assertEqual(self.repos.committed_files(), [RULES])
        self.assertEqual(git(self.here, 'diff', '--name-only'), OTHER)

    def test_pulls_first_when_cloud_is_ahead(self):
        """防：云端先有了新提交时推送失败或冲掉它。"""
        self.repos.commit_other(OTHER, '云端先改了\n')
        write(self.here, RULES, '本机改的\n')
        head = self.push([RULES])
        self.assertEqual(self.repos.cloud_head(), head)
        self.assertEqual(self.read(OTHER), '云端先改了\n')

    def test_refuses_staged_files_that_were_not_listed(self):
        """防：别的窗口暂存的文件被这次提交顺带带走。"""
        write(self.here, RULES, '要提交的\n')
        write(self.here, OTHER, '别人暂存的\n')
        git(self.here, 'add', '--', OTHER)
        count = self.repos.commit_count()
        with self.assertRaises(sync.Stop):
            self.push([RULES])
        self.assertEqual(self.repos.commit_count(), count)

    def test_stops_when_listed_file_conflicts_with_cloud(self):
        """防：要提交的文件和云端改了同一处时，直接提交冲掉云端的改动。"""
        write(self.here, RULES, '本机版本\n')
        head = self.repos.commit_other(RULES, '云端版本\n')
        with self.assertRaises(sync.Stop):
            self.push([RULES])
        self.assertEqual(self.repos.cloud_head(), head)
        self.assertEqual(self.read(RULES), '本机版本\n')

    def test_race_retracts_commit_and_keeps_changes(self):
        """防：拉取之后、推送之前云端又有了新提交时硬推，或者撤回时把本机改动弄丢。"""
        write(self.here, RULES, '本机改的\n')
        before = self.repos.head()
        cloud = {}
        with mock.patch.object(sync, 'trace', lambda root, actor: cloud.setdefault(
                'head', self.repos.commit_other(OTHER, '抢先推的\n'))):
            with self.assertRaises(sync.Stop) as stop:
                self.push([RULES])
        self.assertIn('撤回', str(stop.exception))
        self.assertEqual(self.repos.head(), before)
        self.assertEqual(self.repos.cloud_head(), cloud['head'])
        self.assertEqual(self.read(RULES), '本机改的\n')
        self.assertEqual(self.repos.staged(), [RULES])
        head = self.push([RULES])   # 照提示重跑一次就能推上去
        self.assertEqual(self.repos.cloud_head(), head)
        self.assertEqual(self.read(OTHER), '抢先推的\n')

    def test_refuses_remote_that_is_not_private_remote(self):
        """防：origin 被改成别的仓库（比如公开模板）时把私人数据推过去。"""
        git(self.repos.base, 'init', '--quiet', '--bare', 'public.git')
        git(self.here, 'remote', 'set-url', 'origin', '../public.git')
        write(self.here, RULES, '私人内容\n')
        count = self.repos.commit_count()
        with self.assertRaises(sync.Stop) as stop:
            self.push([RULES])
        self.assertIn('不是设置里的私有仓', str(stop.exception))
        self.assertEqual(self.repos.commit_count(), count)
        self.assertEqual(git(self.repos.base / 'public.git', 'for-each-ref'), '')

    def test_refuses_when_private_remote_is_empty(self):
        """防：还没设置私有仓时就提交推送。"""
        self.repos.set_settings(private_remote='')
        write(self.here, RULES, '改了\n')
        with self.assertRaises(sync.Stop) as stop:
            self.push([RULES])
        self.assertIn('private_remote', str(stop.exception))

    def test_refuses_when_hooks_are_not_enabled(self):
        """防：钩子没启用时提交，门禁和上传检查都不会跑。"""
        git(self.here, 'config', 'core.hooksPath', '.no-hooks')
        write(self.here, RULES, '改了\n')
        with self.assertRaises(sync.Stop) as stop:
            self.push([RULES])
        self.assertIn('install_git_hooks.py', str(stop.exception))

    def test_trace_is_called_and_ledger_is_committed(self):
        """防：留痕脚本的调用参数和约定不一致，或者流水没跟着提交上去。"""
        write(self.here, 'tools/trace_changes.py', (
            'import json, sys\n'
            'from pathlib import Path\n'
            'root = Path(sys.argv[sys.argv.index("--root") + 1])\n'
            'ledger = root / ".personal/logs/trace/test-here/changes_2026-01.jsonl"\n'
            'ledger.parent.mkdir(parents=True, exist_ok=True)\n'
            'ledger.write_text(json.dumps(sys.argv[1:]) + "\\n", encoding="utf-8")\n'))
        write(self.here, RULES, '改了\n')
        self.push([RULES], actor='tester')
        ledger = '.personal/logs/trace/test-here/changes_2026-01.jsonl'
        self.assertEqual(self.repos.committed_files(), sorted([RULES, ledger]))
        self.assertEqual(json.loads(self.read(ledger)),
                         ['scan', '--trigger', 'manual', '--actor', 'tester', '--root', str(self.here)])


if __name__ == '__main__':
    unittest.main()
