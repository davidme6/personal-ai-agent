import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import check_workspace
import init_workspace
import install_schedule
import setup_wizard

# main_library=none 时一轮完整答案（适用问题全部覆盖）
BASIC = {
    'private_remote': '', 'archive_sources': 'claude,codex', 'archive_all_projects': 'no',
    'main_library': 'none', 'inbox_remote': '', 'require_task_card': 'no', 'schedule': 'default',
    'skill_targets': 'none', 'backup': 'none',
}
SYNC_IDS = {'sync_credentials', 'sync_credentials_confirm', 'sync_media', 'sync_exclude_paths',
            'sync_exclude_patterns'}


class WizardTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='setup-wizard-test-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / 'ws'
        self.root.mkdir()
        shutil.copy2(ROOT / 'AGENTS.md', self.root / 'AGENTS.md')
        for name in ('templates', 'config'):
            shutil.copytree(ROOT / name, self.root / name)
        init_workspace.initialize(self.root)
        self.home = base / 'home'
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {'CHAT_ARCHIVE_HOME': str(self.home),
                                           'LOCALAPPDATA': str(self.home / 'AppData' / 'Local')})
        env.start()
        self.addCleanup(env.stop)

    @property
    def settings_file(self):
        return self.root / '.personal/agent.json'

    def settings(self):
        return json.loads(self.settings_file.read_text(encoding='utf-8'))

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup_wizard.main(['--root', str(self.root), *args])
        return code, out.getvalue()

    def pending_ids(self):
        code, text = self.run_main('--questions')
        self.assertEqual(code, 0)
        return [q['id'] for q in json.loads(text)['questions']], json.loads(text)

    def answer(self, answers):
        return setup_wizard.apply_answers(self.root, answers)


class QuestionTests(WizardTestBase):
    def test_follow_up_questions_follow_main_library(self):
        """防的是：没选飞书也追问飞书细节、没选 rclone 也追问 rclone 远端，或者选了之后漏问。"""
        ids, _ = self.pending_ids()
        self.assertIn('main_library', ids)
        self.assertIn('inbox_remote', ids)
        self.assertFalse({'feishu_cli', 'feishu_profile', 'feishu_folder_token', 'feishu_auto_sync',
                          'main_rclone_remote'} & set(ids))
        self.assertEqual(self.answer({'main_library': 'feishu'}), [])
        ids, _ = self.pending_ids()
        self.assertNotIn('main_library', ids)
        self.assertTrue({'feishu_cli', 'feishu_profile', 'feishu_folder_token', 'feishu_auto_sync'} <= set(ids))
        self.assertNotIn('main_rclone_remote', ids)
        setup_wizard.redo(self.root, 'main_library')
        self.assertEqual(self.answer({'main_library': 'rclone'}), [])
        ids, _ = self.pending_ids()
        self.assertIn('main_rclone_remote', ids)
        self.assertNotIn('inbox_remote', ids)
        self.assertFalse({'feishu_cli', 'feishu_auto_sync'} & set(ids))
        self.assertEqual(self.settings()['cloud']['local_path'], '.personal/files')
        self.assertFalse(self.settings()['feishu']['enabled'])

    def test_sync_exclusion_questions_only_with_cloud_and_state_privacy_rule(self):
        """防的是：没用任何云端也问上传排除；或者问的时候没讲清“个人隐私文件夹永远不上传”这条固定规则。"""
        self.assertEqual(self.answer({'main_library': 'none', 'inbox_remote': ''}), [])
        ids, _ = self.pending_ids()
        self.assertFalse(SYNC_IDS & set(ids))
        setup_wizard.redo(self.root, 'inbox_remote')
        self.assertEqual(self.answer({'inbox_remote': 'gdrive:personal-agent-inbox'}), [])
        ids, payload = self.pending_ids()
        self.assertTrue({'sync_credentials', 'sync_media', 'sync_exclude_paths', 'sync_exclude_patterns'} <= set(ids))
        self.assertNotIn('sync_credentials_confirm', ids)
        for question in payload['questions']:
            if question['id'] in SYNC_IDS:
                self.assertIn('个人隐私文件夹永远不上传', question['help'], question['id'])

    def test_detected_only_checks_directories_under_home(self):
        """防的是：检测本机装了哪些 AI 时看错位置（读真实用户目录），或把没装的报成检测到。"""
        (self.home / '.codex').mkdir()
        _, payload = self.pending_ids()
        sources = next(q for q in payload['questions'] if q['id'] == 'archive_sources')
        self.assertEqual(sources['detected'], ['codex'])
        self.assertEqual(sources['default'], 'codex')


class AnswerTests(WizardTestBase):
    def test_one_bad_answer_writes_nothing(self):
        """防的是：一批答案里有一项不合格时，前面合格的已经写进设置，留下一半新一半旧的状态。"""
        before = self.settings_file.read_bytes()
        for bad in ({'require_task_card': 'yes', 'backup': 'maybe'},
                    {'main_library': 'feishu', 'feishu_folder_token': 'bad token/'},
                    {'schedule': 'custom', 'refresh_times': '25:00'},
                    {'feishu_cli': 'x'},                       # 还没选飞书，不适用
                    {'no_such_question': 'x'}):
            errors = self.answer(bad)
            self.assertTrue(errors, bad)
            self.assertEqual(self.settings_file.read_bytes(), before, bad)
        code, text = self.run_main('--answer', 'require_task_card=yes', '--answer', 'trace_minutes=0')
        self.assertEqual(code, 1)
        self.assertIn('trace_minutes', text)
        self.assertEqual(self.settings_file.read_bytes(), before)

    def test_private_remote_validation_and_private_repo_mode(self):
        """防的是：把不是仓库地址的字符串当私有仓写进设置；合法地址却没开启私有仓模式（没生成 .personal/.gitignore）。"""
        self.assertTrue(self.answer({'private_remote': 'not a url'}))
        self.assertEqual(self.settings()['private_remote'], '')
        self.assertFalse((self.root / '.personal/.gitignore').exists())
        url = 'https://git.example.com/<user>/my-agent-private.git'.replace('<user>', 'someone')
        self.assertEqual(self.answer({'private_remote': url}), [])
        self.assertEqual(self.settings()['private_remote'], url)
        self.assertTrue((self.root / '.personal/.gitignore').is_file())
        self.assertTrue(self.answer({'private_remote': 'https://git.example.com/other/repo.git'}))
        self.assertEqual(self.settings()['private_remote'], url)

    def test_completed_only_when_all_applicable_answered_and_recomputed(self):
        """防的是：还有适用问题没答就标完成；或者改了选择、多出新问题后仍显示已完成。"""
        partial = dict(BASIC)
        partial.pop('backup')
        self.assertEqual(self.answer(partial), [])
        self.assertFalse(self.settings()['setup']['completed'])
        self.assertEqual(self.answer({'backup': 'quark'}), [])
        self.assertTrue(self.settings()['setup']['completed'])
        setup_wizard.redo(self.root, 'main_library')
        self.assertFalse(self.settings()['setup']['completed'])
        self.assertEqual(self.answer({'main_library': 'feishu'}), [])
        self.assertFalse(self.settings()['setup']['completed'])   # 飞书追问和上传排除还没答
        self.assertEqual(self.answer({'feishu_cli': 'lark-cli', 'feishu_profile': '', 'feishu_folder_token': '',
                                      'feishu_auto_sync': 'no', 'sync_credentials': 'exclude',
                                      'sync_media': 'no', 'sync_exclude_paths': '',
                                      'sync_exclude_patterns': ''}), [])
        self.assertTrue(self.settings()['setup']['completed'])

    def test_redo_marks_question_pending(self):
        """防的是：想改某一项却没法让它重新出现在待答列表里，或 redo 不认识的编号时静默通过。"""
        self.assertEqual(self.answer(BASIC), [])
        code, _ = self.run_main('--redo', 'backup')
        self.assertEqual(code, 0)
        ids, payload = self.pending_ids()
        self.assertEqual(ids, ['backup'])
        self.assertFalse(payload['completed'])
        code, _ = self.run_main('--redo', 'nope')
        self.assertEqual(code, 2)

    def test_other_settings_keys_are_preserved(self):
        """防的是：向导写设置时把用户自己加的其他键或同一段里的其他字段冲掉。"""
        data = self.settings()
        data['my_key'] = {'keep': 1}
        data['feishu']['extra'] = 'keep'
        data['schedule']['auto_sync_time'] = '20:15'
        self.settings_file.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(self.answer(dict(BASIC, main_library='feishu', feishu_cli='my-cli')), [])
        data = self.settings()
        self.assertEqual(data['my_key'], {'keep': 1})
        self.assertEqual(data['feishu']['extra'], 'keep')
        self.assertEqual(data['feishu']['cli'], 'my-cli')
        self.assertEqual(data['schedule']['auto_sync_time'], '20:15')

    def test_skill_targets_mapping_and_platform_check(self):
        """防的是：Skill 同步目录映射写错；非 Windows 上选了只有 Windows 才有的目录；none 和别的混选。"""
        self.assertEqual(self.answer({'skill_targets': 'claude,codex'}), [])
        self.assertEqual(self.settings()['skills']['targets'],
                         {'claude': '~/.claude/skills', 'codex': '~/.codex/skills'})
        self.assertTrue(self.answer({'skill_targets': 'none,claude'}))
        with mock.patch.object(sys, 'platform', 'linux'):
            errors = self.answer({'skill_targets': 'doubao-work'})
        self.assertTrue(errors)
        with mock.patch.object(sys, 'platform', 'win32'):
            self.assertEqual(self.answer({'skill_targets': 'doubao-work'}), [])
        self.assertIn('%LOCALAPPDATA%', self.settings()['skills']['targets']['doubao-work'])
        self.assertEqual(self.answer({'skill_targets': 'none'}), [])
        self.assertEqual(self.settings()['skills']['targets'], {})

    def test_credentials_upload_needs_second_confirmation(self):
        """防的是：一次手滑选 include 就把密钥类文件放开上传；确认时说 no 却没恢复成不上传。"""
        self.assertEqual(self.answer({'main_library': 'feishu', 'sync_credentials': 'include'}), [])
        self.assertTrue(self.settings()['sync_exclude']['credentials'])
        ids, _ = self.pending_ids()
        self.assertIn('sync_credentials_confirm', ids)
        self.assertEqual(self.answer({'sync_credentials_confirm': 'no'}), [])
        data = self.settings()
        self.assertTrue(data['sync_exclude']['credentials'])
        self.assertEqual(data['setup']['sync_credentials'], 'exclude')
        setup_wizard.redo(self.root, 'sync_credentials')
        self.assertEqual(self.answer({'sync_credentials': 'include', 'sync_credentials_confirm': 'yes'}), [])
        self.assertFalse(self.settings()['sync_exclude']['credentials'])

    def test_exclude_paths_append_to_local_only_without_duplicates(self):
        """防的是：追加本机专用目录时冲掉原有的 local_only，或同一目录写法不同（有无 /、反斜杠）重复出现。"""
        self.assertEqual(self.answer({'main_library': 'feishu',
                                      'sync_exclude_paths': '.personal/files/a, .personal\\files\\a\\, .local',
                                      'sync_exclude_patterns': '*.zip, 合同*', 'sync_media': 'yes'}), [])
        data = self.settings()
        self.assertEqual(data['local_only'], ['.personal/private/', '.device/', '.local/', '.personal/files/a/'])
        self.assertEqual(data['sync_exclude']['patterns'], ['*.zip', '合同*'])
        self.assertTrue(data['sync_exclude']['media'])
        self.assertTrue(self.answer({'sync_exclude_paths': '../outside'}))

    def test_wizard_never_spawns_processes_or_opens_network(self):
        """防的是：向导在用户确认前就去联网、装定时任务或调用外部命令。"""
        boom = mock.Mock(side_effect=AssertionError('不应调用'))
        with mock.patch.object(subprocess, 'run', boom), mock.patch.object(subprocess, 'Popen', boom), \
                mock.patch.object(socket.socket, 'connect', boom), mock.patch.object(os, 'system', boom):
            answers = dict(BASIC, private_remote='git@git.example.com:someone/agent.git', main_library='feishu',
                           skill_targets='claude')
            self.assertEqual(self.answer(answers), [])
            self.run_main('--questions')
            self.run_main('--status')
        boom.assert_not_called()


class InteractiveTests(WizardTestBase):
    def test_full_interactive_flow_with_skip_and_defaults(self):
        """防的是：终端交互里回车不是默认值、跳过的项被当成已答、编号选项映射错。"""
        replies = iter([
            '',          # private_remote：默认留空
            '1,2',       # archive_sources：claude,codex
            'n',         # archive_all_projects
            '3',         # main_library：none
            's',         # inbox_remote：跳过
            'yes',       # require_task_card
            '',          # schedule：默认
            '5',         # skill_targets：none
            '1',         # backup：quark
        ])
        out = io.StringIO()
        with mock.patch('builtins.input', lambda _='': next(replies)), \
                mock.patch.object(sys.stdin, 'isatty', return_value=True), contextlib.redirect_stdout(out):
            code = setup_wizard.main(['--root', str(self.root)])
        self.assertEqual(code, 0)
        data = self.settings()
        self.assertEqual(data['archive']['sources'], ['claude', 'codex'])
        self.assertTrue(data['require_task_card'])
        self.assertEqual(data['setup']['backup'], 'quark')
        self.assertEqual(data['skills']['targets'], {})
        self.assertNotIn('inbox_remote', data['setup']['answered'])
        self.assertIn('private_remote', data['setup']['answered'])
        self.assertFalse(data['setup']['completed'])

    def test_non_terminal_stdin_returns_2_without_waiting(self):
        """防的是：AI 或脚本在非交互环境里调用向导时卡在 input() 上不返回。"""
        with mock.patch.object(sys.stdin, 'isatty', return_value=False), \
                mock.patch('builtins.input', side_effect=AssertionError('不应读输入')):
            code, text = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn('--questions', text)

    def test_missing_settings_file_points_to_init(self):
        """防的是：还没初始化工作区时向导崩溃或自己凭空建一份设置。"""
        self.settings_file.unlink()
        code, text = self.run_main('--questions')
        self.assertEqual(code, 2)
        self.assertIn('init_workspace.py', text)
        self.assertFalse(self.settings_file.exists())


class ReminderTests(WizardTestBase):
    def test_check_workspace_reminds_until_setup_completed(self):
        """防的是：首次设置没做完时检查静默通过，用户以为都配好了；做完了还一直提醒。"""
        (self.root / '.personal/projects/example-project').mkdir(parents=True, exist_ok=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            check_workspace.main(['--root', str(self.root)])
        self.assertIn('首次设置还没完成', out.getvalue())
        self.assertEqual(self.answer(BASIC), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            check_workspace.main(['--root', str(self.root)])
        self.assertIn('Workspace check passed', out.getvalue())
        self.assertNotIn('首次设置还没完成', out.getvalue())

    def test_install_schedule_reminds_but_still_installs(self):
        """防的是：首次设置没完成时安装定时任务不提醒，或者因为提醒反而不装了。"""
        out = io.StringIO()
        with mock.patch.object(install_schedule, 'is_windows', return_value=True), \
                mock.patch.object(install_schedule, 'run_powershell', return_value=0) as run_ps, \
                contextlib.redirect_stdout(out):
            code = install_schedule.main(['--root', str(self.root)])
        self.assertEqual(code, 0)
        run_ps.assert_called_once()
        self.assertIn('首次设置还没完成', out.getvalue())
        out = io.StringIO()
        with mock.patch.object(install_schedule, 'is_windows', return_value=True), contextlib.redirect_stdout(out):
            install_schedule.main(['--root', str(self.root), '--dry-run'])
        self.assertNotIn('首次设置还没完成', out.getvalue())

    def test_init_workspace_points_to_wizard(self):
        """防的是：初始化完不告诉用户下一步要做首次设置，直接就开始用默认组合。"""
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['init_workspace.py', '--root', str(self.root)]), \
                contextlib.redirect_stdout(out):
            init_workspace.main()
        self.assertIn('setup_wizard.py', out.getvalue())


if __name__ == '__main__':
    unittest.main()
