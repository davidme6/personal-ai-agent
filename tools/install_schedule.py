#!/usr/bin/env python3
"""把定时任务装到本机：Windows 用任务计划程序，macOS / Linux 用 crontab。

任务（时间在 .personal/agent.json 的 schedule 里改，改完重新运行本脚本）：
  refresh     每天 schedule.refresh_times 的每个时间点运行 tools/refresh.py
  trace       每 schedule.trace_minutes 分钟运行 tools/trace_changes.py scan --trigger scheduled
  auto-sync   每天 schedule.auto_sync_time 运行 tools/auto_sync.py（private_remote 为空时不装）
  feishu-sync 每天 schedule.feishu_sync_time 运行 tools/feishu_sync.py sync --scheduled
              （feishu.enabled 和 feishu.auto_sync 都为 true 才装）
  cloud-sync  每天 schedule.cloud_sync_time 运行 tools/cloud_sync.py run --scheduled（cloud.remote 为空时不装）

  python tools/install_schedule.py                       # 先打印计划，再安装
  python tools/install_schedule.py --dry-run             # 只打印将执行的命令 / crontab 区块，不改系统
  python tools/install_schedule.py --only refresh,trace  # 只装（或只卸）这几个
  python tools/install_schedule.py --uninstall

用哪个 Python 运行本脚本，定时任务就用哪个 Python（Windows 上换成同目录的 pythonw.exe，运行时不弹窗）。
重复安装会替换本工作区原来的任务，不影响其他工作区，也不动你自己的其他定时任务。
因为条件不满足而跳过的任务（比如 private_remote 为空），以前装过的会被移除，系统里的任务始终和设置一致。
Windows：任务放在任务计划程序的 \\PersonalAgent\\ 文件夹，关机错过的开机后补跑。
macOS / Linux：在 crontab 里写一个带标记的区块；cron 不会补跑关机期间错过的任务，输出追加到 .local/schedule.log。
"""
import argparse
import base64
import datetime
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import agent_config

ROOT = Path(__file__).resolve().parent.parent
TASKS = ('refresh', 'trace', 'auto-sync', 'feishu-sync', 'cloud-sync')
TASK_FOLDER = '\\PersonalAgent\\'
CRON_MARK = 'personal-ai-agent'
TIME_LIMIT_MINUTES = {'refresh': 60, 'trace': 10, 'auto-sync': 30, 'feishu-sync': 180, 'cloud-sync': 180}
DESCRIPTIONS = {
    'refresh': '归档对话、更新索引（tools/refresh.py）',
    'trace': '记录工作区文件变化（tools/trace_changes.py）',
    'auto-sync': '把归档与状态推到私有仓（tools/auto_sync.py）',
    'feishu-sync': '原件资料库和飞书云空间同步（tools/feishu_sync.py）',
    'cloud-sync': '原件资料库和云盘双向同步（tools/cloud_sync.py）',
}


# ---------- 计划 ----------

def parse_time(text):
    match = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', str(text or '').strip())
    if not match:
        raise ValueError(f'schedule 里的时间格式不对：{text!r}（要 HH:MM，例如 09:00）')
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def parse_only(text):
    if not text:
        return list(TASKS)
    names = [name.strip() for name in text.split(',') if name.strip()]
    unknown = [name for name in names if name not in TASKS]
    if unknown:
        raise ValueError(f"--only 里有不认识的任务：{', '.join(unknown)}（可选：{', '.join(TASKS)}）")
    return names


def plan(root, settings, selected):
    """返回 (要装的任务, 跳过的 [(任务名, 原因)])。selected 之外的任务不碰。"""
    schedule = settings.get('schedule') or {}
    cloud = settings.get('cloud') or {}
    feishu = settings.get('feishu') or {}
    specs = {
        'refresh': ('tools/refresh.py', [], schedule.get('refresh_times') or []),
        'trace': ('tools/trace_changes.py', ['scan', '--trigger', 'scheduled'], None),
        'auto-sync': ('tools/auto_sync.py', [], [schedule.get('auto_sync_time')]),
        'feishu-sync': ('tools/feishu_sync.py', ['sync', '--scheduled'], [schedule.get('feishu_sync_time')]),
        'cloud-sync': ('tools/cloud_sync.py', ['run', '--scheduled'], [schedule.get('cloud_sync_time')]),
    }
    tasks, skipped = [], []
    for name in TASKS:
        if name not in selected:
            continue
        script, args, times = specs[name]
        if name == 'auto-sync' and not str(settings.get('private_remote') or '').strip():
            skipped.append((name, 'private_remote 为空，不自动推送'))
            continue
        if name == 'feishu-sync' and not (feishu.get('enabled') is True and feishu.get('auto_sync') is True):
            skipped.append((name, 'feishu.enabled 和 feishu.auto_sync 没有都设为 true'))
            continue
        if name == 'cloud-sync' and not str(cloud.get('remote') or '').strip():
            skipped.append((name, 'cloud.remote 为空，没有配置云盘'))
            continue
        if not (root / script).is_file():
            skipped.append((name, f'{script} 不存在'))
            continue
        task = {'name': name, 'script': script, 'args': args}
        if name == 'trace':
            minutes = int(schedule.get('trace_minutes') or 0)
            if minutes <= 0:
                skipped.append((name, 'schedule.trace_minutes 不是正数'))
                continue
            task['every'] = minutes
        else:
            task['times'] = [parse_time(t) for t in times]
            if not task['times']:
                skipped.append((name, 'schedule.refresh_times 为空'))
                continue
        tasks.append(task)
    return tasks, skipped


def describe(task):
    if 'every' in task:
        return f"每 {task['every']} 分钟"
    return '每天 ' + '、'.join(task['times'])


def workspace_hash(root):
    return hashlib.sha256(os.path.normcase(str(root)).encode('utf-8')).hexdigest()[:8]


def workspace_label(root):
    """工作区目录名 + 路径哈希：同名目录的两个工作区也不会撞名。"""
    name = re.sub(r'[^\w.-]', '-', Path(root).name).strip('-.') or 'workspace'
    return f'{name}-{workspace_hash(root)}'


def interpreter():
    exe = Path(sys.executable or 'python')
    if os.name == 'nt':
        windowless = exe.with_name('pythonw.exe')
        if windowless.is_file():
            return windowless
    return exe


# ---------- Windows：任务计划程序 ----------

def ps_quote(text):
    """PowerShell 单引号字符串：不展开 $ 和反引号，单引号写成两个。"""
    return "'" + str(text).replace("'", "''") + "'"


def windows_task_name(root, name):
    return f'{workspace_label(root)}-{name}'


def windows_script(root, tasks, remove, python):
    """生成注册 / 移除任务的 PowerShell 脚本。remove 是要移除的任务名（不是全名）。"""
    root = Path(root)
    lines = ["$ErrorActionPreference = 'Stop'",
             "$ProgressPreference = 'SilentlyContinue'",   # 否则输出被重定向时会混进进度信息
             '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8',
             f'$folder = {ps_quote(TASK_FOLDER)}']
    for task in tasks:
        full = windows_task_name(root, task['name'])
        # 程序路径和脚本路径加双引号，路径里有空格也不会被拆开；起始目录按惯例不加引号。
        argument = ' '.join([f'"{root / task["script"]}"'] + task['args'])
        if 'every' in task:
            trigger = ('New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) '
                       f"-RepetitionInterval (New-TimeSpan -Minutes {task['every']}) "
                       '-RepetitionDuration (New-TimeSpan -Days 3650)')
            triggers = f'@({trigger})'
        else:
            triggers = '@(' + ', '.join(f'(New-ScheduledTaskTrigger -Daily -At {ps_quote(t)})'
                                        for t in task['times']) + ')'
        lines += [
            f'$action = New-ScheduledTaskAction -Execute {ps_quote(chr(34) + str(python) + chr(34))} '
            f'-Argument {ps_quote(argument)} -WorkingDirectory {ps_quote(root)}',
            f'$triggers = {triggers}',
            '$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries '
            '-StartWhenAvailable -MultipleInstances IgnoreNew '
            f"-ExecutionTimeLimit (New-TimeSpan -Minutes {TIME_LIMIT_MINUTES[task['name']]})",
            f'Register-ScheduledTask -TaskPath $folder -TaskName {ps_quote(full)} -Action $action '
            f"-Trigger $triggers -Settings $settings -Description {ps_quote(DESCRIPTIONS[task['name']])} "
            '-Force | Out-Null',
            f"Write-Output ('已安装：' + {ps_quote(full)})",
        ]
    for name in remove:
        full = windows_task_name(root, name)
        lines += [
            f'$old = Get-ScheduledTask -TaskPath $folder -TaskName {ps_quote(full)} -ErrorAction SilentlyContinue',
            f"if ($old) {{ $old | Unregister-ScheduledTask -Confirm:$false; Write-Output ('已移除：' + {ps_quote(full)}) }}",
        ]
    prefix = workspace_label(root) + '-'
    lines += [
        "Write-Output '本工作区现有的定时任务：'",
        f'$mine = @(Get-ScheduledTask -TaskPath $folder -ErrorAction SilentlyContinue | '
        f'Where-Object {{ $_.TaskName.StartsWith({ps_quote(prefix)}) }})',
        "if ($mine.Count -eq 0) { Write-Output '  （没有）' }",
        "foreach ($t in $mine) { $info = Get-ScheduledTaskInfo -InputObject $t; "
        "Write-Output ('  ' + $t.TaskPath + $t.TaskName + '  下次运行：' + $info.NextRunTime) }",
    ]
    return '\n'.join(lines) + '\n'


def run_powershell(script):
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    proc = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                           '-EncodedCommand', encoded], capture_output=True)
    for stream in (proc.stdout, proc.stderr):
        text = stream.decode('utf-8', 'replace').strip()
        if text:
            print(text)
    return proc.returncode


# ---------- macOS / Linux：crontab ----------

def cron_markers(ws_hash):
    return f'# >>> {CRON_MARK} {ws_hash} >>>', f'# <<< {CRON_MARK} {ws_hash} <<<'


def cron_schedules(task):
    if 'every' in task:
        minutes = task['every']
        if minutes < 60:
            return [f'*/{minutes} * * * *']
        if minutes % 60 == 0 and minutes // 60 < 24:
            return [f'0 */{minutes // 60} * * *']
        if minutes == 1440:
            return ['0 0 * * *']
        raise ValueError(f'schedule.trace_minutes={minutes} 没法写成 cron：要小于 60，或是 60 的整数倍且不超过 1440')
    return [f'{int(t[3:])} {int(t[:2])} * * *' for t in task['times']]


def cron_lines(root, task, python, path_env):
    root = Path(root)
    log = root / '.local' / 'schedule.log'
    words = [shlex.quote(str(python)), shlex.quote(str(root / task['script']))] + [shlex.quote(a) for a in task['args']]
    # cron 环境的 PATH 很短（常找不到 git、rclone），所以带上安装时的 PATH；只作用于这一条命令。
    command = (f'cd {shlex.quote(str(root))} && mkdir -p .local && PATH={shlex.quote(path_env)} {" ".join(words)} '
               f'>> {shlex.quote(str(log))} 2>&1')
    command = command.replace('%', '\\%')   # % 在 crontab 里表示换行，必须转义
    return [f'{spec} {command}' for spec in cron_schedules(task)]


def read_sections(current, ws_hash):
    """取出本工作区区块里已有的各任务行：{任务名: [行, ...]}。"""
    start, end = cron_markers(ws_hash)
    sections, inside, name = {}, False, None
    for line in current.splitlines():
        stripped = line.strip()
        if stripped == start:
            inside, name = True, None
        elif stripped == end:
            inside = False
        elif inside:
            match = re.fullmatch(r'#\s*task:\s*(\S+)', stripped)
            if match:
                name = match.group(1)
                sections[name] = []
            elif name and stripped and not stripped.startswith('#'):
                sections[name].append(line)
    return sections


def build_block(root, ws_hash, sections):
    if not sections:
        return []
    start, end = cron_markers(ws_hash)
    lines = [start, f'# 工作区：{root}（由 tools/install_schedule.py 生成，重新安装会替换本区块）']
    for name in TASKS:
        if name in sections:
            lines.append(f'# task: {name}')
            lines.extend(sections[name])
    lines.append(end)
    return lines


def replace_block(current, ws_hash, block):
    """用 block 替换 crontab 里本工作区的区块（block 为空就删掉区块），其他行原样保留。"""
    start, end = cron_markers(ws_hash)
    kept, inside = [], False
    for line in current.splitlines():
        stripped = line.strip()
        if stripped == start:
            if inside:
                raise ValueError(f'crontab 里有两个连续的开始标记 {start}，请手动修好后再运行。')
            inside = True
        elif stripped == end:
            if not inside:
                raise ValueError(f'crontab 里有结束标记 {end} 但没有开始标记，请手动修好后再运行。')
            inside = False
        elif not inside:
            kept.append(line)
    if inside:
        raise ValueError(f'crontab 里只有开始标记、找不到结束标记 {end}；为免误删你的其他任务，请手动修好后再运行。')
    if block:
        if kept and kept[-1].strip():
            kept.append('')
        kept.extend(block)
    return '\n'.join(kept) + '\n' if kept else ''


def merged_cron(root, current, tasks, remove, python, path_env):
    """返回新的完整 crontab 文本：只替换选中的任务，本工作区其他任务和别的行都不动。"""
    ws_hash = workspace_hash(root)
    sections = read_sections(current, ws_hash)
    for name in list(remove) + [task['name'] for task in tasks]:
        sections.pop(name, None)
    for task in tasks:
        sections[task['name']] = cron_lines(root, task, python, path_env)
    return replace_block(current, ws_hash, build_block(root, ws_hash, sections))


def check_cron_path(root):
    if '\n' in str(root) or '\r' in str(root):
        raise ValueError('工作区路径里有换行符，没法写进 crontab。')


def read_crontab():
    if not shutil.which('crontab'):
        raise RuntimeError('没找到 crontab 命令，没法安装定时任务。')
    proc = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    if proc.returncode == 0:
        return proc.stdout
    if 'no crontab' in (proc.stderr or '').lower():
        return ''
    # 读不出来时绝不能当成空的写回去，否则会清掉用户已有的任务。
    raise RuntimeError(f'读取 crontab 失败：{(proc.stderr or "").strip()}')


def write_crontab(root, current, new_text):
    backup_dir = Path(root) / '.local' / 'schedule'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f'crontab-backup-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt'
    backup.write_text(current, encoding='utf-8')
    print(f'原来的 crontab 已备份到 {backup}')
    proc = subprocess.run(['crontab', '-'], input=new_text, text=True, capture_output=True)
    if proc.returncode != 0:
        print(f'写入 crontab 失败：{(proc.stderr or "").strip()}')
    return proc.returncode


# ---------- 入口 ----------

def is_windows():
    return os.name == 'nt'


def main(argv=None):
    parser = argparse.ArgumentParser(description='把定时任务装到本机（Windows 任务计划程序 / crontab）。')
    parser.add_argument('--root', type=Path, default=ROOT, help='工作区根目录（默认是本脚本的上一级）')
    parser.add_argument('--dry-run', action='store_true', help='只打印将执行的命令或 crontab 区块，不改系统')
    parser.add_argument('--uninstall', action='store_true', help='移除本工作区的定时任务')
    parser.add_argument('--only', help=f"只处理这些任务，逗号分隔（{','.join(TASKS)}）")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        selected = parse_only(args.only)
        if args.uninstall:
            tasks, remove = [], selected
            print('将移除本工作区的定时任务：' + '、'.join(selected))
        else:
            tasks, skipped = plan(root, agent_config.load(root), selected)
            remove = [name for name, _ in skipped]
            print(f'工作区：{root}')
            for task in tasks:
                command = ' '.join([task['script']] + task['args'])
                print(f"  装   {task['name']:<11} {describe(task):<20} {command}")
            for name, reason in skipped:
                print(f'  跳过 {name:<11} {reason}（以前装过的会被移除）')
        python = interpreter()
        if not args.dry_run and not args.uninstall and \
                (agent_config.load(root).get('setup') or {}).get('completed') is not True:
            print('提醒：首次设置还没完成（python tools/setup_wizard.py），定时任务按现在的设置安装。')
        if is_windows():
            script = windows_script(root, tasks, remove, python)
            if args.dry_run:
                print('\n将执行的 PowerShell 脚本：\n' + script)
                print('（--dry-run：只打印，没有改动系统）')
                return 0
            Path(root, '.local').mkdir(parents=True, exist_ok=True)
            return run_powershell(script)
        check_cron_path(root)
        path_env = os.environ.get('PATH', '/usr/bin:/bin')
        current = read_crontab()
        new_text = merged_cron(root, current, tasks, remove, python, path_env)
        ws_hash = workspace_hash(root)
        block = build_block(root, ws_hash, read_sections(new_text, ws_hash))
        if args.dry_run:
            print('\n本工作区在 crontab 里的区块将变成：\n' + ('\n'.join(block) if block else '（没有，区块会被删掉）'))
            print('\n（--dry-run：只打印，没有改动系统）')
            return 0
        if new_text == current:
            print('crontab 没有变化。')
        else:
            Path(root, '.local').mkdir(parents=True, exist_ok=True)
            code = write_crontab(root, current, new_text)
            if code:
                return code
        installed = read_sections(read_crontab(), ws_hash)
        print('本工作区现有的定时任务：' + ('、'.join(name for name in TASKS if name in installed) or '（没有）'))
        if sys.platform == 'darwin' and installed:
            print('提示：macOS 上 cron 访问“文稿”“桌面”等目录需要在系统设置里给 cron 完全磁盘访问权限。')
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f'[停止] {exc}')
        return 2


if __name__ == '__main__':
    sys.exit(main())
