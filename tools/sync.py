#!/usr/bin/env python3
"""一键拉取 / 推送你的私有仓（只管 Git，不碰云盘）。

  python tools/sync.py pull [--actor 名字]
  python tools/sync.py push --message "提交说明" [--task-card 路径] [--actor 名字] 文件...

pull：fetch → 能快进就快进 → 列出新提交。本机改过、云端也改了的文件先备份到
      .local/backups/sync/<时间>/ 再三方合并（git merge-file）；合不上的保留本机版本并列为“分叉”，不覆盖。
push：核对 origin 是 .personal/agent.json 里的 private_remote、钩子已启用 → 先 pull →
      有 tools/trace_changes.py 时做一次留痕扫描 → 只暂存列出的文件和本机留痕流水 →
      提交（钩子跑门禁和上传检查）→ 推送 → 回读云端提交号，和本机一致才算成功。
      推送没成功（比如云端刚好有了新提交）时撤回本次提交，改动保留在暂存区，重跑同一条命令即可。
退出码：0 成功；1 没做成，原因在 [停止] 后面。照提示处理，不要绕开脚本强推。
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_config  # noqa: E402
import upload_check  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REMOTE = 'origin'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class Stop(Exception):
    """没做成，需要人或 AI 处理；消息直接给使用者看。"""


def hook_env(extra=None):
    """让钩子用和本脚本同一个 Python（计划任务里 PATH 可能不全）。"""
    exe = Path(sys.executable)
    if exe.stem.lower() == 'pythonw' and exe.with_name('python' + exe.suffix).exists():
        exe = exe.with_name('python' + exe.suffix)   # 无窗口的 pythonw 没有可用的标准输出
    env = {**os.environ, 'AGENT_PYTHON': exe.as_posix(), 'PYTHONIOENCODING': 'utf-8'}
    env.update(extra or {})
    return env


def run(root, *args, env=None, stdin=None):
    return subprocess.run(['git', '-c', 'core.quotepath=false', *args], cwd=str(root), input=stdin,
                          capture_output=True, env=env, creationflags=NO_WINDOW)


def said(out):
    return (out.stderr + out.stdout).decode('utf-8', 'replace').strip()


def git(root, *args, env=None, stdin=None):
    out = run(root, *args, env=env, stdin=stdin)
    if out.returncode != 0:
        raise Stop(f'git {args[0]} 失败：\n{said(out)}')
    return out.stdout.decode('utf-8', 'replace').strip()


def is_ancestor(root, older, newer):
    return run(root, 'merge-base', '--is-ancestor', older, newer).returncode == 0


def current_branch(root):
    branch = git(root, 'branch', '--show-current')
    if not branch:
        raise Stop('当前不在任何分支上（detached HEAD），脚本不处理。')
    return branch


def remote_has(root, branch):
    """云端有没有这个分支；连不上云端时报停止。"""
    out = run(root, 'ls-remote', '--exit-code', '--heads', REMOTE, branch)
    if out.returncode == 2:
        return False
    if out.returncode != 0:
        raise Stop(f'连不上云端（git ls-remote 失败）：\n{said(out)}')
    return True


def three_way(root, ours, base_spec, theirs):
    """本机版本、共同起点、云端版本三方合并；合得上返回内容，合不上（含二进制）返回 None。"""
    with tempfile.NamedTemporaryFile(delete=False) as temp:
        temp.write(run(root, 'cat-file', 'blob', base_spec).stdout)
    try:
        out = run(root, 'merge-file', '-p', str(ours), temp.name, str(theirs))
    finally:
        os.unlink(temp.name)
    return out.stdout if out.returncode == 0 else None


def pull(root):
    """返回 {'branch', 'new_branch', 'commits', 'ahead', 'merged', 'forks': [(路径, 原因)], 'backup'}。"""
    root = Path(root)
    branch = current_branch(root)
    report = {'branch': branch, 'new_branch': False, 'commits': [], 'ahead': 0,
              'merged': [], 'forks': [], 'backup': None}
    if not remote_has(root, branch):
        report['new_branch'] = True
        return report
    tracking = f'refs/remotes/{REMOTE}/{branch}'
    git(root, 'fetch', '--quiet', REMOTE, f'+refs/heads/{branch}:{tracking}')
    head, remote = git(root, 'rev-parse', 'HEAD'), git(root, 'rev-parse', tracking)
    if head == remote:
        return report
    if is_ancestor(root, remote, head):
        report['ahead'] = int(git(root, 'rev-list', '--count', f'{remote}..{head}'))
        return report
    if not is_ancestor(root, head, remote):
        raise Stop('本机和云端各有对方没有的提交，脚本不自动处理。'
                   f'先看 git log --oneline --graph HEAD {REMOTE}/{branch}，再决定怎么合。')
    report['commits'] = git(root, 'log', '--format=%h %an %s', f'{head}..{remote}').splitlines()
    fields = git(root, 'diff', '--no-renames', '--name-status', '-z', head, remote).split('\0')
    incoming = list(zip(fields[0::2], fields[1::2]))   # [(状态, 路径)]
    dirty = set(git(root, 'diff', '--name-only', '-z', 'HEAD').split('\0'))

    # 云端要改、本机也动过的文件：先备份并让位，快进后再合并或放回
    backup = agent_config.path(root, 'local') / 'backups/sync' / datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    aside = []
    for status, rel in incoming:
        path = root / rel
        if not path.is_file() or (status != 'A' and rel not in dirty):
            continue
        (backup / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup / rel)
        aside.append((status, rel))
        if status == 'A':
            path.unlink()   # 本机已有同名的未跟踪文件
        else:
            git(root, 'checkout', '--quiet', 'HEAD', '--', rel)
    if aside:
        report['backup'] = backup.relative_to(root).as_posix()

    merge = run(root, 'merge', '--ff-only', '--quiet', remote)
    if merge.returncode != 0:
        for _, rel in aside:
            shutil.copy2(backup / rel, root / rel)
        raise Stop(f'快进失败，本机文件已放回原样：\n{said(merge)}')

    for status, rel in aside:
        path, saved = root / rel, backup / rel
        if status == 'D':
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, path)
            report['forks'].append((rel, '云端已删除，本机改过'))
        elif saved.read_bytes() == path.read_bytes():
            continue
        elif status == 'A':
            shutil.copy2(saved, path)
            report['forks'].append((rel, '云端新增了同名文件，内容和本机的不同'))
        else:
            merged = three_way(root, saved, f'{head}:{rel}', path)
            if merged is None:
                shutil.copy2(saved, path)
                report['forks'].append((rel, '本机和云端改了同一处'))
            else:
                path.write_bytes(merged)
                report['merged'].append(rel)
    return report


def show(report):
    count = len(report['commits'])
    if report['new_branch']:
        print(f"[拉取] 云端还没有 {report['branch']} 分支，推送时会新建。")
    elif count:
        print(f'[拉取] 云端有 {count} 个新提交，本机已更新：')
        for line in report['commits'][:20]:
            print('  ' + line)
        if count > 20:
            print(f'  …其余 {count - 20} 个略')
    elif report['ahead']:
        print(f"[拉取] 本机领先云端 {report['ahead']} 个提交，还没推送。")
    else:
        print('[拉取] 已是最新。')
    if report['merged']:
        print(f"[合并] {len(report['merged'])} 个文件本机和云端都改过，已自动合并"
              f"（合并前的本机版本在 {report['backup']}）：")
        for rel in report['merged']:
            print('  ' + rel)
    if report['forks']:
        print(f"[分叉] {len(report['forks'])} 个文件保留了本机版本，没有采用云端版本，需要人定"
              f"（备份在 {report['backup']}）：")
        for rel, why in report['forks']:
            print(f'  {rel}：{why}')


def trace(root, actor):
    """有留痕脚本就扫描一次，让这次的流水跟着本次提交一起上去。"""
    script = Path(root) / 'tools/trace_changes.py'
    if script.exists():
        subprocess.run([sys.executable, str(script), 'scan', '--trigger', 'manual', '--actor', actor or 'sync',
                        '--root', str(root)], cwd=str(root), capture_output=True, creationflags=NO_WINDOW)


def ledger_changes(root):
    """本机留痕流水里有改动、且没被 .gitignore 忽略的文件。"""
    folder = (Path(agent_config.PATHS['trace']) / agent_config.device_name(root)).as_posix()
    out = run(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--', folder)
    if out.returncode != 0:
        raise Stop(f'git status 失败：\n{said(out)}')
    entries = out.stdout.decode('utf-8', 'replace').split('\0')   # 不能 strip：第一项开头的空格是状态位
    return [entry[3:] for entry in entries if len(entry) > 3 and entry[3:].endswith('.jsonl')]


def relative(root, item):
    path = Path(item)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(Path(root).resolve())
        except ValueError:
            raise Stop(f'{item} 不在工作区里。')
    return path.as_posix()


def push(root, message, files, task_card=None, actor=None):
    """返回推送后的提交号。"""
    root = Path(root)
    settings = agent_config.load(root)
    blocks, _ = upload_check.remote_problems(root, settings)
    if blocks:
        raise Stop('\n'.join(blocks))
    missing = upload_check.hooks_problem(root)
    if missing:
        raise Stop(f"本仓库的 {'、'.join(missing)} 钩子没启用，提交前的检查不会跑。先运行：python tools/install_git_hooks.py")
    report = pull(root)
    show(report)
    branch = report['branch']
    files = [relative(root, item) for item in files]
    blocked = [rel for rel, _ in report['forks'] if rel in files]
    if blocked:
        raise Stop('要提交的文件和云端改了同一处，直接提交会冲掉云端的改动。'
                   '请在云端版本上重做这些文件的修改：\n  ' + '\n  '.join(blocked))
    trace(root, actor)
    targets = list(dict.fromkeys(files + ledger_changes(root)))
    staged = [rel for rel in git(root, 'diff', '--cached', '--name-only', '-z').split('\0') if rel]
    extra = [rel for rel in staged if rel not in targets]
    if extra:
        raise Stop('暂存区里有本次没列出的文件。是本次要提交的就列在命令里，不是就先 git reset：\n  '
                   + '\n  '.join(extra))
    for start in range(0, len(targets), 50):
        git(root, 'add', '--', *targets[start:start + 50])
    if run(root, 'diff', '--cached', '--quiet').returncode == 0:
        raise Stop('没有要提交的改动。')
    before = git(root, 'rev-parse', 'HEAD') if run(root, 'rev-parse', '--verify', '-q', 'HEAD').returncode == 0 else None
    env = hook_env({'PROJECT_TASK_CARD': str(task_card)} if task_card else None)
    git(root, 'commit', '--quiet', '-F', '-', env=env, stdin=message.encode('utf-8'))
    pushed = run(root, 'push', '--quiet', REMOTE, f'HEAD:refs/heads/{branch}', env=env)
    if pushed.returncode != 0:
        # 先撤回再报错：不管是撞车、钩子拦截还是断网，都不留下一个没推上去的本机提交。
        fetched = run(root, 'fetch', '--quiet', REMOTE, f'+refs/heads/{branch}:refs/remotes/{REMOTE}/{branch}')
        raced = fetched.returncode == 0 and not is_ancestor(root, f'{REMOTE}/{branch}', 'HEAD')
        if before:
            git(root, 'reset', '--quiet', '--soft', before)
        else:
            git(root, 'update-ref', '-d', 'HEAD')   # 仓库的第一个提交：撤回后回到没有提交的状态
        if raced:
            raise Stop('云端刚好有了新提交，推送被拒。本次提交已撤回，改动还在暂存区；重新运行同一条命令即可。')
        raise Stop(f'推送没成功，本次提交已撤回，改动还在暂存区：\n{said(pushed)}')
    head = git(root, 'rev-parse', 'HEAD')
    remote = git(root, 'ls-remote', REMOTE, f'refs/heads/{branch}').split()
    if not remote or remote[0] != head:
        raise Stop(f"已推送，但回读不一致：本机 {head[:8]}，云端 {(remote[0] if remote else '无')[:8]}。")
    return head


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description='一键拉取 / 推送你的私有仓')
    parser.add_argument('--root', type=Path, default=ROOT)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('pull', 'push'):
        command = sub.add_parser(name)
        command.add_argument('--actor', help='谁在操作，写进留痕流水，如 "Codex:会话号"')
    command.add_argument('-m', '--message', required=True)
    command.add_argument('--task-card', help='本任务的任务卡（设置 require_task_card 为 true 时门禁要用）')
    command.add_argument('files', nargs='+', help='本次要提交的文件，相对工作区根目录')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == 'pull':
            report = pull(root)
            show(report)
            if report['commits']:
                trace(root, args.actor)
        else:
            head = push(root, args.message, args.files, args.task_card, args.actor)
            print(f'[推送] 已提交并推送，云端回读一致：{head[:8]}')
    except Stop as stop:
        print(f'[停止] {stop}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
