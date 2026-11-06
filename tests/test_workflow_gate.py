"""tools/workflow_gate.py：任务卡核对、暂存区边界，以及经真实 pre-commit 钩子的放行与拦截。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'tools'))
import upload_check  # noqa: E402
import workflow_gate  # noqa: E402
from git_fixture import Repos, git, git_run, hook_env, write  # noqa: E402

MARKER = 'TASK-示例-20260101'
CARD = '.personal/logs/task-cards/2026-01-01_示例.md'
LOG = '.personal/logs/work-logs/2026-01-01_示例.md'
GATES = ['G0 接手', 'G1 探查', 'G2 方案与备份', 'G3 执行', 'G4 验证', 'G5 记录']


def write_task(root, marker=MARKER, unchecked=(), verified='通过', log_text=None, state_text=None):
    lines = ['# 示例任务', f'- 任务标记：{marker}', f'- 工作日志：{LOG}', f'- 验证结果：{verified}']
    lines += [f"- [{' ' if gate[:2] in unchecked else 'x'}] {gate}：已完成" for gate in GATES]
    lines.append('- [ ] G6 提交：待提交')
    write(root, CARD, '\n'.join(lines) + '\n')
    write(root, LOG, log_text if log_text is not None else f'# 工作日志\n\n- 任务标记：{marker}\n\n## 验证\n\n- 验证结果：通过\n')
    write(root, '.personal/current-state.md', state_text if state_text is not None else f'# Current state\n\n- {marker}\n')
    return CARD


class TaskCardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='agent-gate-test-'))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_complete_records_pass(self):
        """防：格式照模板写全了的任务卡被误拦。"""
        self.assertEqual(workflow_gate.check_task(self.root, write_task(self.root)), [])

    def test_unchecked_gate_fails(self):
        """防：G0–G5 有一项没做完就提交。"""
        errors = workflow_gate.check_task(self.root, write_task(self.root, unchecked=('G3',)))
        self.assertEqual(errors, ['任务卡 G3 未勾选'])

    def test_unverified_task_fails(self):
        """防：验证结果不是“通过”也能提交。"""
        self.assertTrue(workflow_gate.check_task(self.root, write_task(self.root, verified='待验证')))

    def test_log_and_state_must_belong_to_this_task(self):
        """防：借用别的任务的工作日志或状态记录蒙混过关。"""
        other = '# 工作日志\n\n- 任务标记：TASK-别的\n\n## 验证\n\n- 验证结果：通过\n'
        self.assertTrue(workflow_gate.check_task(self.root, write_task(self.root, log_text=other)))
        no_check = f'# 工作日志\n\n- 任务标记：{MARKER}\n'
        self.assertTrue(workflow_gate.check_task(self.root, write_task(self.root, log_text=no_check)))
        self.assertTrue(workflow_gate.check_task(self.root, write_task(self.root, state_text='# Current state\n')))

    def test_paths_must_stay_in_their_folders(self):
        """防：任务卡或工作日志指向约定目录以外的文件。"""
        write_task(self.root)
        shutil.copy(self.root / CARD, self.root / '.personal/logs/卡.md')
        self.assertTrue(workflow_gate.check_task(self.root, '.personal/logs/卡.md'))
        with self.assertRaises(ValueError):
            workflow_gate.within(self.root, '.personal/logs/work-logs/../../../outside.md', '.personal/logs/work-logs')


class PathRuleTests(unittest.TestCase):
    def test_private_credential_and_file_library_paths_are_refused(self):
        """防：凭据文件、只留本机的路径、走云盘的原件被暂存提交。"""
        settings = {'local_only': ['.personal/private/', '.device/', '.local/'], 'cloud': {}}
        for rel in ('.env', 'app/.env.local', 'certs/server.pem', 'certs/a.key', 'c.p12', 'c.pfx', 'vault.kdbx',
                    'docs/Passwords.xlsx', '账号密码.md', '.personal/files/合同.pdf', '.personal/private/日记.md',
                    '.device/device.json', '.local/cache.db'):
            with self.subTest(rel=rel):
                self.assertIsNotNone(upload_check.path_problem(rel, settings))
        for rel in ('.personal/logs/work-logs/a.md', '.personal/current-state.md', 'tools/sync.py'):
            with self.subTest(rel=rel):
                self.assertIsNone(upload_check.path_problem(rel, settings))


class StagedGateTests(unittest.TestCase):
    def setUp(self):
        self.repos = Repos()
        self.addCleanup(self.repos.cleanup)
        self.here = self.repos.here

    def commit(self, message, card=None):
        env = hook_env()
        if card:
            env['PROJECT_TASK_CARD'] = card
        return git_run(self.here, 'commit', '-m', message, env=env)

    def stage(self, rel, text):
        write(self.here, rel, text)
        git(self.here, 'add', '-f', '--', rel)

    def test_staged_boundaries(self):
        """防：凭据文件被暂存；origin 不是私有仓时照常提交。"""
        self.stage('.env', 'A=1\n')
        errors, _ = workflow_gate.check_staged(self.here)
        self.assertTrue(any('凭据文件' in e for e in errors))
        git(self.here, 'reset', '-q', '--', '.env')
        self.stage('笔记/a.md', '正常\n')
        self.assertEqual(workflow_gate.check_staged(self.here)[0], [])
        git(self.here, 'remote', 'set-url', 'origin', 'https://github.com/example-user/agent-template.git')
        self.assertTrue(workflow_gate.check_staged(self.here)[0])

    def test_without_task_card_requirement_commit_passes(self):
        """防：没开 require_task_card 时普通提交被任务卡要求拦住。"""
        self.stage('笔记/a.md', '正常\n')
        result = self.commit('普通提交')
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode('utf-8', 'replace'))

    def test_required_task_card_is_checked_through_real_hook(self):
        """防：开了 require_task_card 后钩子没真的核对任务卡，缺项也能提交。"""
        self.repos.set_settings(require_task_card=True)
        self.stage('笔记/a.md', '正常\n')
        count = self.repos.commit_count()
        self.assertNotEqual(self.commit('没有任务卡').returncode, 0)
        write_task(self.here, unchecked=('G4',))
        self.assertNotEqual(self.commit('任务卡缺 G4', CARD).returncode, 0)
        self.assertEqual(self.repos.commit_count(), count)
        write_task(self.here)
        result = self.commit('任务卡齐全', CARD)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode('utf-8', 'replace'))


if __name__ == '__main__':
    unittest.main()
