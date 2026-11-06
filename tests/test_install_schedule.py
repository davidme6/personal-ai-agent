import base64
import contextlib
import io
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import agent_config
import install_schedule

SCRIPTS = ('tools/refresh.py', 'tools/trace_changes.py', 'tools/auto_sync.py', 'tools/feishu_sync.py',
           'tools/cloud_sync.py')


def make_workspace(parent, name, settings=None):
    root = Path(parent) / name
    for script in SCRIPTS:
        (root / script).parent.mkdir(parents=True, exist_ok=True)
        (root / script).write_text('', encoding='utf-8')
    if settings is not None:
        (root / '.personal').mkdir(parents=True, exist_ok=True)
        (root / '.personal/agent.json').write_text(json.dumps(settings), encoding='utf-8')
    return root


def powershell_commands(script):
    """只用 PowerShell 的语法解析器拆出每条命令的参数值，不执行脚本。"""
    payload = base64.b64encode(script.encode('utf-8')).decode('ascii')
    probe = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$text = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$null, [ref]$errors)
$commands = $ast.FindAll({{ param($n) $n -is [System.Management.Automation.Language.CommandAst] }}, $true) | ForEach-Object {{
    ,@($_.CommandElements | ForEach-Object {{
        if ($_ -is [System.Management.Automation.Language.StringConstantExpressionAst]) {{ $_.Value }} else {{ $_.Extent.Text }}
    }})
}}
ConvertTo-Json -Compress -Depth 4 @{{ errors = @($errors | ForEach-Object {{ $_.Message }}); commands = @($commands) }}
"""
    encoded = base64.b64encode(probe.encode('utf-16-le')).decode('ascii')
    proc = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                          capture_output=True, timeout=120)
    return json.loads(proc.stdout.decode('utf-8'))


def value_after(command, flag):
    return command[command.index(flag) + 1]


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='schedule-test-')
        self.addCleanup(self.temp.cleanup)

    def test_default_settings_skip_all_sync_tasks(self):
        """防止没配私有仓 / 飞书 / 云盘时也装上自动推送和同步任务。"""
        root = make_workspace(self.temp.name, 'ws')
        tasks, skipped = install_schedule.plan(root, agent_config.load(root), list(install_schedule.TASKS))
        self.assertEqual([t['name'] for t in tasks], ['refresh', 'trace'])
        self.assertEqual({name for name, _ in skipped}, {'auto-sync', 'feishu-sync', 'cloud-sync'})

    def test_feishu_sync_needs_both_switches(self):
        """防止只开了飞书、没同意自动同步（或反过来）时就装上每天自动同步飞书的任务。"""
        for feishu in ({'enabled': True}, {'auto_sync': True}, {'enabled': True, 'auto_sync': 'true'}):
            root = make_workspace(self.temp.name, 'ws', {'feishu': feishu})
            tasks, _ = install_schedule.plan(root, agent_config.load(root), ['feishu-sync'])
            self.assertEqual(tasks, [], feishu)
        root = make_workspace(self.temp.name, 'ws', {'feishu': {'enabled': True, 'auto_sync': True}})
        tasks, _ = install_schedule.plan(root, agent_config.load(root), ['feishu-sync'])
        self.assertEqual(tasks[0]['times'], ['18:40'])
        self.assertEqual(tasks[0]['args'], ['sync', '--scheduled'])

    def test_configured_remotes_install_all_tasks_with_configured_times(self):
        """防止设置里的时间、间隔没有被用上，装成了写死的时间。"""
        root = make_workspace(self.temp.name, 'ws', {
            'private_remote': 'git@example.com:me/private.git', 'cloud': {'remote': 'gdrive:files'},
            'feishu': {'enabled': True, 'auto_sync': True},
            'schedule': {'refresh_times': ['7:05', '21:30'], 'trace_minutes': 15, 'auto_sync_time': '22:00',
                         'feishu_sync_time': '22:10', 'cloud_sync_time': '22:15'}})
        tasks, skipped = install_schedule.plan(root, agent_config.load(root), list(install_schedule.TASKS))
        self.assertEqual(skipped, [])
        by_name = {t['name']: t for t in tasks}
        self.assertEqual(by_name['refresh']['times'], ['07:05', '21:30'])
        self.assertEqual(by_name['trace']['every'], 15)
        self.assertEqual(by_name['feishu-sync']['times'], ['22:10'])
        self.assertEqual(by_name['cloud-sync']['args'], ['run', '--scheduled'])
        self.assertEqual(install_schedule.cron_schedules(by_name['refresh']), ['5 7 * * *', '30 21 * * *'])
        self.assertEqual(install_schedule.cron_schedules(by_name['trace']), ['*/15 * * * *'])

    def test_bad_settings_are_rejected(self):
        """防止写错的时间或间隔被装成永远不跑、或跑错时间的任务。"""
        root = make_workspace(self.temp.name, 'ws', {'schedule': {'refresh_times': ['25:00']}})
        with self.assertRaises(ValueError):
            install_schedule.plan(root, agent_config.load(root), ['refresh'])
        with self.assertRaises(ValueError):
            install_schedule.cron_schedules({'every': 90})
        with self.assertRaises(ValueError):
            install_schedule.parse_only('refresh,typo')


class WindowsScriptTests(unittest.TestCase):
    root = PureWindowsPath(r"D:\我的 工作区\it's agent")
    python = PureWindowsPath(r'C:\Program Files\Python 3\pythonw.exe')

    def script(self, remove=()):
        tasks = [{'name': 'refresh', 'script': 'tools/refresh.py', 'args': [], 'times': ['09:00', '18:00']},
                 {'name': 'trace', 'script': 'tools/trace_changes.py', 'args': ['scan', '--trigger', 'scheduled'],
                  'every': 10}]
        return install_schedule.windows_script(self.root, tasks, list(remove), self.python)

    def test_task_names_carry_workspace_name_and_hash(self):
        """防止两个工作区（哪怕目录同名）的任务互相覆盖。"""
        other = PureWindowsPath(r"E:\别处\it's agent")
        self.assertNotEqual(install_schedule.windows_task_name(self.root, 'refresh'),
                            install_schedule.windows_task_name(other, 'refresh'))
        self.assertTrue(install_schedule.windows_task_name(self.root, 'refresh').startswith('it-s-agent-'))
        self.assertIn("-TaskPath $folder", self.script())
        self.assertIn(r"$folder = '\PersonalAgent\'", self.script())

    def test_skipped_tasks_are_unregistered_not_registered(self):
        """防止 private_remote 清空后旧的自动推送任务还留在系统里继续跑。"""
        text = self.script(remove=['auto-sync'])
        name = install_schedule.windows_task_name(self.root, 'auto-sync')
        self.assertIn('Unregister-ScheduledTask', text)
        self.assertIn(name, text)
        self.assertNotIn(f"-TaskName '{name}' -Action", text)

    @unittest.skipUnless(shutil.which('powershell.exe'), '需要 Windows PowerShell 的语法解析器')
    def test_paths_with_spaces_chinese_and_quotes_parse_to_exact_values(self):
        """防止路径里的空格、中文、单引号把计划任务的程序或参数拆错（只解析脚本，不执行）。"""
        result = powershell_commands(self.script(remove=['auto-sync']))
        self.assertEqual(result['errors'], [])
        actions = [c for c in result['commands'] if c[0] == 'New-ScheduledTaskAction']
        self.assertEqual(len(actions), 2)
        self.assertEqual(value_after(actions[0], '-Execute'), f'"{self.python}"')
        self.assertEqual(value_after(actions[0], '-Argument'), f'"{self.root}\\tools\\refresh.py"')
        self.assertEqual(value_after(actions[1], '-Argument'),
                         f'"{self.root}\\tools\\trace_changes.py" scan --trigger scheduled')
        self.assertEqual(value_after(actions[0], '-WorkingDirectory'), str(self.root))
        registers = [c for c in result['commands'] if c[0] == 'Register-ScheduledTask']
        self.assertEqual([value_after(c, '-TaskName') for c in registers],
                         [install_schedule.windows_task_name(self.root, n) for n in ('refresh', 'trace')])


class CronTests(unittest.TestCase):
    root = Path('/home/user/my 工作区/it\'s 100%')
    python = Path('/opt/py 3/bin/python3')
    task = {'name': 'trace', 'script': 'tools/trace_changes.py', 'args': ['scan', '--trigger', 'scheduled'],
            'every': 10}

    def test_cron_line_quotes_paths_and_escapes_percent(self):
        """防止路径里的空格、中文、单引号、% 让 cron 命令执行错文件或被截断。"""
        line = install_schedule.cron_lines(self.root, self.task, self.python, '/usr/bin:/bin')[0]
        self.assertTrue(line.startswith('*/10 * * * * '))
        command = line[len('*/10 * * * * '):]
        self.assertNotIn('%', command.replace('\\%', ''))   # 每个 % 都转义了
        words = shlex.split(command.replace('\\%', '%'))
        self.assertEqual(words[:2], ['cd', str(self.root)])
        self.assertIn(str(self.python), words)
        self.assertIn(str(self.root / 'tools/trace_changes.py'), words)
        self.assertEqual(words[words.index(str(self.root / 'tools/trace_changes.py')) + 1:][:3],
                         ['scan', '--trigger', 'scheduled'])

    def test_block_replacement_keeps_other_lines_and_is_idempotent(self):
        """防止重复安装时动了用户自己的 cron 行或别的工作区的区块，或越装越多。"""
        other_start, other_end = install_schedule.cron_markers('ffffffff')
        user = ('MAILTO=me@example.com\n# 我的备份\n0 3 * * * /usr/bin/backup --date=+\\%F\n'
                f'{other_start}\n# task: trace\n*/5 * * * * other\n{other_end}\n')
        tasks = [self.task]
        once = install_schedule.merged_cron(self.root, user, tasks, [], self.python, '/usr/bin')
        twice = install_schedule.merged_cron(self.root, once, tasks, [], self.python, '/usr/bin')
        self.assertEqual(once, twice)
        self.assertTrue(once.startswith(user))
        self.assertEqual(install_schedule.merged_cron(self.root, once, [], ['trace'], self.python, '/usr/bin'),
                         user + '\n')   # 卸载后只剩用户原来的内容（多一个分隔空行）

    def test_only_option_keeps_other_tasks_of_this_workspace(self):
        """防止 --only trace 重装时把本工作区已装的 refresh 等任务一起删掉。"""
        refresh = {'name': 'refresh', 'script': 'tools/refresh.py', 'args': [], 'times': ['09:00']}
        both = install_schedule.merged_cron(self.root, '', [refresh, self.task], [], self.python, '/usr/bin')
        changed = dict(self.task, every=20)
        after = install_schedule.merged_cron(self.root, both, [changed], [], self.python, '/usr/bin')
        sections = install_schedule.read_sections(after, install_schedule.workspace_hash(self.root))
        self.assertEqual(set(sections), {'refresh', 'trace'})
        self.assertTrue(sections['trace'][0].startswith('*/20 '))
        self.assertEqual(sections['refresh'], install_schedule.read_sections(
            both, install_schedule.workspace_hash(self.root))['refresh'])

    def test_unterminated_block_is_not_rewritten(self):
        """防止区块结束标记被人删掉后，安装器把标记之后用户自己的任务当成区块一起删掉。"""
        start, _ = install_schedule.cron_markers(install_schedule.workspace_hash(self.root))
        broken = f'{start}\n*/10 * * * * old\n0 4 * * * /usr/bin/user-job\n'
        with self.assertRaises(ValueError):
            install_schedule.merged_cron(self.root, broken, [self.task], [], self.python, '/usr/bin')


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='schedule-dry-')
        self.addCleanup(self.temp.cleanup)
        self.root = make_workspace(self.temp.name, '工作 区')
        self.calls = []

    def fake_run(self, args, **kwargs):
        self.calls.append(list(args))
        if list(args) == ['crontab', '-l']:
            return subprocess.CompletedProcess(args, 0, stdout='0 3 * * * /usr/bin/backup\n', stderr='')
        raise AssertionError(f'dry-run 不应执行：{args}')

    def run_main(self, windows, *extra):
        out = io.StringIO()
        with mock.patch.object(install_schedule, 'is_windows', return_value=windows), \
                mock.patch.object(install_schedule.subprocess, 'run', side_effect=self.fake_run), \
                mock.patch.object(install_schedule.shutil, 'which', return_value='/usr/bin/crontab'), \
                contextlib.redirect_stdout(out):
            code = install_schedule.main(['--root', str(self.root), '--dry-run', *extra])
        return code, out.getvalue()

    def test_windows_dry_run_changes_nothing(self):
        """防止 --dry-run 真的去注册计划任务。"""
        code, text = self.run_main(True)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertIn('Register-ScheduledTask', text)
        self.assertIn('auto-sync', text)   # 列在跳过里
        self.assertFalse((self.root / '.local').exists())

    def test_cron_dry_run_only_reads_crontab(self):
        """防止 --dry-run 改写用户的 crontab。"""
        code, text = self.run_main(False, '--only', 'trace')
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [['crontab', '-l']])
        self.assertIn('# task: trace', text)
        self.assertNotIn('# task: refresh', text)

    def test_dry_run_installs_feishu_sync_only_when_both_switches_true(self):
        """防止飞书两个开关没都打开时，dry-run 计划里仍出现飞书定时同步。"""
        settings_file = self.root / '.personal/agent.json'
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        name = install_schedule.windows_task_name(self.root, 'feishu-sync')
        settings_file.write_text(json.dumps({'feishu': {'enabled': True, 'auto_sync': False}}), encoding='utf-8')
        _, text = self.run_main(True)
        self.assertNotIn(f"-TaskName '{name}' -Action", text)
        self.assertIn('跳过 feishu-sync', text)
        _, text = self.run_main(False)
        self.assertNotIn('# task: feishu-sync', text)
        settings_file.write_text(json.dumps({'feishu': {'enabled': True, 'auto_sync': True}}), encoding='utf-8')
        _, text = self.run_main(True)
        self.assertIn(f"-TaskName '{name}' -Action", text)
        self.assertIn('feishu_sync.py" sync --scheduled', text)
        _, text = self.run_main(False)
        self.assertIn('# task: feishu-sync', text)


if __name__ == '__main__':
    unittest.main()
