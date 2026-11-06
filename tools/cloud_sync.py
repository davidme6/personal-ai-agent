#!/usr/bin/env python3
"""云盘双向同步：用 rclone bisync 让一个本机目录（cloud.local_path，默认收件箱 .personal/inbox/）和云盘远端双向同步。
想让它同步原件资料库，把 cloud.local_path 改成 .personal/files。

规则：
- 先比较差异再动手：plan 只预览；init 不带 --yes 也只预览。
- 只有一边改了：同步到另一边。本机被替换或被删掉的旧文件先挪到 .local/cloud_backups/<时间>/。
- 两边都改了：算冲突，两份都留（文件名带 .conflict1 / .conflict2），不自动选边，由你人工合并。
- 一次要删的文件超过 cloud.max_delete_percent：整次同步停止，不做任何改动。
- local_only 和 .personal/private/ 下的文件永不上传（过滤规则写在 .local/cloud_filters.txt）。

准备：安装 rclone 1.66 或更新版本（https://rclone.org/downloads/），用 `rclone config` 配好远端
（Google Drive、OneDrive、Dropbox、S3、WebDAV 等都行），再在 .personal/agent.json 里填
"cloud": {"remote": "远端名:文件夹"}，例如 gdrive:personal-agent-files。

  python tools/cloud_sync.py init [--yes]        # 首次建立基线；不带 --yes 只预览
  python tools/cloud_sync.py plan                # 建好基线之后：预览这次同步会发生什么，不改任何东西
  python tools/cloud_sync.py run [--scheduled]   # 正式同步；--scheduled 给定时任务用，只写日志
日志：.local/cloud_sync.log
"""
import argparse
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import agent_config

ROOT = Path(__file__).resolve().parent.parent
MIN_VERSION = (1, 66)   # --conflict-resolve / --conflict-loser / --recover 从 1.66 开始才有
FILTERS = '.local/cloud_filters.txt'
STATE = '.local/cloud_sync_state.json'
LOG = '.local/cloud_sync.log'
BACKUPS = '.local/cloud_backups'
JUNK = ('~$*', '*.tmp', '.DS_Store', 'Thumbs.db', 'desktop.ini')   # 锁文件和系统垃圾，同步了只会制造冲突
CONFLICT_NAME = re.compile(r'\.conflict\d+(\.|$)')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
INIT_NOTICE = ('首次建立基线：以两边的并集为基线。只在一边有的文件会复制到另一边；'
               '两边都有、内容不同的同名文件，以本机版本为准（云盘那份会被替换，多数云盘能在历史版本里找回）。\n'
               '建好基线之后，才按“只一边改了就同步、两边都改了两份都留”的规则走。')


class Problem(Exception):
    """配置或环境不满足，同步还没开始。"""


def make_logger(root, echo):
    log = Path(root) / LOG

    def say(text):
        text = str(text)
        if echo:
            print(text)
        log.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
        with open(log, 'a', encoding='utf-8', errors='replace') as f:
            for line in text.splitlines() or ['']:
                f.write(f'{stamp} {line}\n')
    return say


def rel_to(base, target):
    try:
        return Path(target).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return None


def local_dir(root, settings):
    raw = (settings.get('cloud') or {}).get('local_path') or agent_config.PATHS['inbox']
    return (Path(root) / raw).resolve()


def check_local_dir(root, settings, local):
    base = rel_to(root, local)
    if base is None:
        return   # 工作区外的目录：不涉及 local_only
    private = agent_config.PATHS['private']
    if rel_to(local, Path(root) / '.local') is not None:
        raise Problem(f'cloud.local_path（{base}）不能包含 .local/：那里是本机缓存和备份。')
    if agent_config.is_local_only(base, settings) or base == private or base.startswith(private + '/'):
        raise Problem(f'cloud.local_path（{base}）在只留本机的范围里，不能同步到云盘。')


def escape_glob(text):
    return re.sub(r'([\\*?\[\]{}])', r'\\\1', text)


def case_insensitive(glob):
    """rclone 过滤规则区分大小写：把字母换成 [aA] 的写法，让 .ENV、Secret.txt 也能命中。"""
    return ''.join(f'[{c.lower()}{c.upper()}]' if c.isalpha() and c.isascii() else c for c in glob)


def filter_rules(root, settings, local):
    """过滤规则文本。内容必须稳定（不写时间），因为 rclone 发现过滤规则变了会要求重新建基线。"""
    base = rel_to(root, local)
    excluded = []
    for prefix in list(settings.get('local_only') or []) + [agent_config.PATHS['private']]:
        prefix = prefix.replace('\\', '/').strip().strip('/')
        if prefix.startswith('./'):
            prefix = prefix[2:]
        if base is None or not prefix:
            continue
        inside = prefix if base == '.' else (prefix[len(base) + 1:] if prefix.startswith(base + '/') else '')
        if inside and inside not in excluded:
            excluded.append(inside)
    lines = ['# 由 tools/cloud_sync.py 生成，不要手改：local_only 和 .personal/private/ 永不上传']
    for item in excluded:
        lines += [f'- /{escape_glob(item)}', f'- /{escape_glob(item)}/**']
    lines += [f'- {pattern}' for pattern in JUNK]
    lines += [f'- {case_insensitive(glob)}' for glob in agent_config.sync_exclude_globs(settings)]
    return '\n'.join(lines) + '\n'


def rclone_version(exe):
    try:
        proc = subprocess.run([exe, 'version'], capture_output=True, text=True, encoding='utf-8',
                              errors='replace', creationflags=NO_WINDOW, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'rclone v(\d+)\.(\d+)', proc.stdout or '')
    return (int(match.group(1)), int(match.group(2))) if match else None


def preflight(root, settings, say):
    """检查远端、rclone 和本机目录；返回 (rclone 路径, 远端, 本机目录)。"""
    remote = str((settings.get('cloud') or {}).get('remote') or '').strip()
    if not remote:
        raise Problem('cloud.remote 为空：还没有配置云盘远端，不同步。\n'
                      '先用 `rclone config` 配好远端，再在 .personal/agent.json 里填 '
                      '"cloud": {"remote": "远端名:文件夹"}。')
    exe = shutil.which('rclone')
    if not exe:
        raise Problem('没找到 rclone：请从 https://rclone.org/downloads/ 安装 1.66 或更新的版本，'
                      '并确保在命令行里能直接运行 rclone。')
    version = rclone_version(exe)
    if version is None:
        say('提示：读不出 rclone 版本，按 1.66 以上处理；如果下面报参数不认识，请升级 rclone。')
    elif version < MIN_VERSION:
        raise Problem(f'rclone 版本太旧（v{version[0]}.{version[1]}），需要 1.66 以上：'
                      '冲突两份都留、断点恢复这些参数从 1.66 开始才有。请升级：https://rclone.org/downloads/')
    local = local_dir(root, settings)
    check_local_dir(root, settings, local)
    return exe, remote, local


def max_delete(settings):
    value = (settings.get('cloud') or {}).get('max_delete_percent', 10)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = -1
    if not 0 <= value <= 100:
        raise Problem(f'cloud.max_delete_percent 要是 0 到 100 的整数，现在是 {value!r}。')
    return value


def bisync_command(exe, local, remote, filters, percent, backup, dry_run=False, resync=False):
    command = [exe, 'bisync', str(local), remote,
               '--filters-file', str(filters),
               '--conflict-resolve', 'none',       # 两边都改了：不选边
               '--conflict-loser', 'num',          # 两份都留，加编号
               '--conflict-suffix', 'conflict',
               '--suffix-keep-extension',          # 冲突副本保留原扩展名，能直接打开
               '--max-delete', str(percent),       # 删除比例超过就整次停止
               '--resilient', '--recover',         # 小错误下次自动重试，中断后自动恢复
               '--backup-dir1', str(backup),       # 本机被替换 / 删除的文件先挪到这里
               '--verbose']
    if resync:
        command.append('--resync')
    if dry_run:
        command.append('--dry-run')
    return command


def state_key(remote, local, rules):
    return {'remote': remote, 'local_path': str(local),
            'filters_sha256': hashlib.sha256(rules.encode('utf-8')).hexdigest()}


def read_state(root):
    try:
        return json.loads((Path(root) / STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def require_baseline(root, remote, local, rules):
    state, key = read_state(root), state_key(remote, local, rules)
    if not state:
        raise Problem('还没有建立同步基线。先运行 `python tools/cloud_sync.py init` 预览，确认后再加 --yes。')
    if state.get('remote') != key['remote'] or state.get('local_path') != key['local_path']:
        raise Problem(f"远端或本机目录和建基线时不一样（当时：{state.get('local_path')} ⇄ {state.get('remote')}）。"
                      '换了位置要重新建基线：先运行 init 预览差异，确认后再加 --yes。')
    if state.get('filters_sha256') != key['filters_sha256']:
        raise Problem('local_only 设置变了，同步过滤规则跟着变了，rclone 要求重新建基线。'
                      '先运行 init 预览差异，确认后再加 --yes。')


def write_filters(root, rules):
    path = Path(root) / FILTERS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rules, encoding='utf-8', newline='\n')
    return path


def execute(command, say):
    say('执行：' + subprocess.list2cmdline(command))
    proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                          creationflags=NO_WINDOW)
    output = ((proc.stdout or '') + (proc.stderr or '')).rstrip()
    if output:
        say(output)
    return proc.returncode, output


def explain_failure(code, output, percent, say):
    text = output.lower()
    if 'too many deletes' in text:
        say(f'[停止] 这次要删除的文件超过 {percent}%，整次同步已停止，没有做任何改动。'
            '先核实是不是有意删的；确认后临时调高 cloud.max_delete_percent 再运行。')
    elif 'resync' in text:
        say('[停止] rclone 要求重新建立基线。不要直接覆盖：先运行 init 预览差异，确认后再 init --yes。')
    else:
        say(f'[停止] 同步失败（rclone 退出码 {code}），详情见 {LOG}')


def report_conflicts(local, say):
    found = sorted(p for p in Path(local).rglob('*') if p.is_file() and CONFLICT_NAME.search(p.name))
    if found:
        say(f'⚠ 有 {len(found)} 个冲突副本（两边都改过，两份都留着了）。请人工合并，留下要的那份并删掉其余的：')
        for item in found:
            say('   ' + Path(item).relative_to(local).as_posix())
    return found


def prepare(root, settings, say, need_baseline=True):
    exe, remote, local = preflight(root, settings, say)
    percent = max_delete(settings)
    rules = filter_rules(root, settings, local)
    if need_baseline:
        require_baseline(root, remote, local, rules)
        if not local.is_dir():
            raise Problem(f'本机资料目录不存在：{local}。如果是新设备，先运行 init 预览，确认后再加 --yes。')
    filters = write_filters(root, rules)
    backup = Path(root) / BACKUPS / datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    return exe, remote, local, rules, filters, percent, backup


def cmd_plan(root, settings, say):
    exe, remote, local, _, filters, percent, backup = prepare(root, settings, say)
    say(f'预览：{local} ⇄ {remote}（只看差异，不改任何东西）')
    code, output = execute(bisync_command(exe, local, remote, filters, percent, backup, dry_run=True), say)
    if code:
        explain_failure(code, output, percent, say)
    return code


def cmd_init(root, settings, say, yes):
    exe, remote, local, rules, filters, percent, backup = prepare(root, settings, say, need_baseline=False)
    local.mkdir(parents=True, exist_ok=True)
    say(INIT_NOTICE)
    say(f'{local} ⇄ {remote}')
    command = bisync_command(exe, local, remote, filters, percent, backup, dry_run=not yes, resync=True)
    code, output = execute(command, say)
    if code:
        explain_failure(code, output, percent, say)
        return code
    if not yes:
        say('以上只是预览，没有改动任何文件。确认无误后运行：python tools/cloud_sync.py init --yes')
        return 0
    state = dict(state_key(remote, local, rules),
                 initialized_at=datetime.datetime.now().astimezone().isoformat(timespec='seconds'))
    (Path(root) / STATE).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    say('基线已建立。以后用 run 同步，用 plan 预览。')
    return 0


def cmd_run(root, settings, say):
    exe, remote, local, _, filters, percent, backup = prepare(root, settings, say)
    say(f'同步：{local} ⇄ {remote}')
    code, output = execute(bisync_command(exe, local, remote, filters, percent, backup), say)
    if code:
        explain_failure(code, output, percent, say)
        return code
    report_conflicts(local, say)
    say('同步完成。')
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--root', type=Path, default=argparse.SUPPRESS, help='工作区根目录（默认是本脚本的上一级）')
    parser = argparse.ArgumentParser(description='用 rclone bisync 双向同步原件资料库和云盘。', parents=[common])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('plan', parents=[common], help='预览差异，不改任何东西')
    init_parser = sub.add_parser('init', parents=[common], help='首次建立基线（两边并集）')
    init_parser.add_argument('--yes', action='store_true', help='确认执行；不带只预览')
    run_parser = sub.add_parser('run', parents=[common], help='正式双向同步')
    run_parser.add_argument('--scheduled', action='store_true', help='定时任务用：不输出到屏幕，只写日志')
    args = parser.parse_args(argv)
    root = Path(getattr(args, 'root', ROOT)).resolve()
    scheduled = getattr(args, 'scheduled', False)
    say = make_logger(root, echo=not scheduled)
    settings = agent_config.load(root)
    try:
        if args.command == 'plan':
            return cmd_plan(root, settings, say)
        if args.command == 'init':
            return cmd_init(root, settings, say, args.yes)
        return cmd_run(root, settings, say)
    except Problem as exc:
        say(f'[未同步] {exc}')
        return 0 if scheduled else 2


if __name__ == '__main__':
    sys.exit(main())
