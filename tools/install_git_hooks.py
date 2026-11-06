#!/usr/bin/env python3
"""启用（或停用）本仓库自带的 Git 钩子。

  python tools/install_git_hooks.py              启用：git config core.hooksPath tools/git-hooks
  python tools/install_git_hooks.py --uninstall  停用：去掉 core.hooksPath，回到 git 默认的 .git/hooks

pre-commit 跑门禁（workflow_gate.py）和上传检查（upload_check.py --staged）；
pre-push 只许推到 .personal/agent.json 里的 private_remote。
钩子直接用仓库里的版本，git pull 之后自动是最新，不复制到 .git/hooks。
"""
import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_config  # noqa: E402
import upload_check  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HOOKS_PATH = 'tools/git-hooks'
HOOKS = ('pre-commit', 'pre-push')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def git(root, *args):
    return subprocess.run(['git', *args], cwd=str(root), capture_output=True, text=True,
                          encoding='utf-8', errors='replace', creationflags=NO_WINDOW)


def prepare_hook(path):
    """钩子必须是 LF 换行（Git Bash 读到 CRLF 会报错）且可执行（macOS / Linux 不可执行的钩子会被忽略）。"""
    data = path.read_bytes()
    if b'\r\n' in data:
        path.write_bytes(data.replace(b'\r\n', b'\n'))
        print(f'  已把 {path.name} 改成 LF 换行（.gitattributes 里应有 tools/git-hooks/* text eol=lf）')
    if os.name != 'nt':
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install(root):
    """返回问题列表；空列表表示已启用。"""
    root = Path(root)
    if git(root, 'rev-parse', '--git-dir').returncode != 0:
        return ['这里不是 Git 仓库，请在工作区根目录运行。']
    missing = [name for name in HOOKS if not (root / HOOKS_PATH / name).is_file()]
    if missing:
        return [f"缺少钩子文件：{'、'.join(HOOKS_PATH + '/' + name for name in missing)}"]
    for name in HOOKS:
        prepare_hook(root / HOOKS_PATH / name)
    git(root, 'config', 'core.hooksPath', HOOKS_PATH)
    now = git(root, 'config', '--get', 'core.hooksPath').stdout.strip()
    if now != HOOKS_PATH:
        return [f'设置失败：core.hooksPath = {now}']
    still = upload_check.hooks_problem(root)
    if still:
        return [f"设置后 git 仍找不到 {'、'.join(still)} 钩子。"]
    return []


def uninstall(root):
    root = Path(root)
    now = git(root, 'config', '--get', 'core.hooksPath').stdout.strip()
    if now == HOOKS_PATH:
        git(root, 'config', '--unset', 'core.hooksPath')
        return True
    return False


def remote_notes(root):
    settings = agent_config.load(root)
    want = (settings.get('private_remote') or '').strip()
    origin = upload_check.origin_url(root)
    notes = [f'当前 origin：{upload_check.redact(origin) or "（没有）"}']
    if not want:
        notes.append('⚠ 还没设置 private_remote（.personal/agent.json）：设置之前，所有提交和推送都会被钩子拦下。')
    elif not upload_check.same_remote(origin, want):
        notes.append(f'⚠ origin 不是设置里的私有仓。修正：git remote set-url origin {upload_check.redact(want)}')
    return notes


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description='启用或停用本仓库的 Git 钩子')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--uninstall', action='store_true', help='停用，恢复 git 默认钩子目录')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.uninstall:
        print('已停用本仓库钩子。' if uninstall(root) else '本仓库钩子本来就没启用，没有改动。')
        return 0
    problems = install(root)
    if problems:
        for item in problems:
            print('[失败] ' + item)
        return 1
    print(f'已启用钩子目录 {HOOKS_PATH}（pre-commit、pre-push）。')
    for note in remote_notes(root):
        print(note)
    print('此后 git commit 前自动跑门禁和上传检查；git push 前核对远端必须是设置里的私有仓。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
