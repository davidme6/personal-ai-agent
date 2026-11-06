#!/usr/bin/env python3
"""每日自动同步：把「归档与状态」类改动提交并推到你的私有仓。给计划任务无窗口调用。

  python tools/auto_sync.py [--root R]

- 设置 private_remote 为空：记一行日志后跳过，返回 0。
- 只提交设置 auto_sync_paths 下有改动的文件；代码和配置留给人工走任务卡提交。
- 拉取和推送都走 tools/sync.py：先拉、三方合并、分叉不覆盖、撞车撤回、推送后回读核对；钩子照常运行。
- 设置 require_task_card 为 true 时，自动生成当天的机械同步任务卡和工作日志，并在 current-state.md
  顶部插一段，让门禁对得上。
- 任何失败：撤销本次暂存（工作区改动原样保留，只撤回本脚本自己生成的那几份记录），
  写 .local/auto_sync.log，尽力弹系统通知。绝不 --no-verify，绝不强推。
"""
import argparse
import base64
import datetime
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_config  # noqa: E402
import sync  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
ACTOR = 'auto_sync'
PULL_ATTEMPTS, PULL_DELAY = 3, 20      # 网络偶发抖动时重试
WRITE_ATTEMPTS, WRITE_DELAY = 5, 0.5   # 文件被编辑器或同步盘短暂占用时重试


def log(root, line):
    path = agent_config.path(root, 'local') / 'auto_sync.log'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass
    try:
        print(line)
    except (OSError, ValueError):   # 无窗口运行时可能没有标准输出
        pass


def notify(title, text):
    """尽力弹系统通知：Windows toast / macOS osascript / Linux notify-send；都没有就算了。"""
    try:
        if os.name == 'nt':
            quote = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731
            script = (
                '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;'
                '$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);'
                "$nodes = $xml.GetElementsByTagName('text');"
                f'$nodes.Item(0).AppendChild($xml.CreateTextNode({quote(title)})) | Out-Null;'
                f'$nodes.Item(1).AppendChild($xml.CreateTextNode({quote(text)})) | Out-Null;'
                '$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);'
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($toast)")
            encoded = base64.b64encode(script.encode('utf-16-le')).decode()
            command = ['powershell', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-EncodedCommand', encoded]
        elif sys.platform == 'darwin' and shutil.which('osascript'):
            escape = lambda s: s.replace('\\', '\\\\').replace('"', '\\"')  # noqa: E731
            command = ['osascript', '-e', f'display notification "{escape(text)}" with title "{escape(title)}"']
        elif shutil.which('notify-send'):
            command = ['notify-send', title, text]
        else:
            return False
        return subprocess.run(command, capture_output=True, timeout=30, creationflags=NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def git(root, *args):
    return subprocess.run(['git', '-c', 'core.quotepath=false', *args], cwd=str(root), capture_output=True,
                          creationflags=NO_WINDOW)


def changed_files(root, paths):
    """auto_sync_paths 下有改动的文件（已跟踪的修改、删除，和没被忽略的新文件）。"""
    existing = [p for p in paths if (Path(root) / p).exists()]
    if not existing:
        return []
    out = git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--', *existing)
    if out.returncode != 0:
        raise sync.Stop('git status 失败：' + out.stderr.decode('utf-8', 'replace').strip())
    entries, names, i = out.stdout.decode('utf-8', 'replace').split('\0'), [], 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[0] in 'RC':
            names.append(entries[i])   # 改名前的旧路径也要提交（记录删除）
            i += 1
        names.append(entry[3:])
    return sorted(set(names))


def ignored_paths(root, paths):
    return [p for p in paths if (Path(root) / p).exists() and git(root, 'check-ignore', '-q', p).returncode == 0]


def update_file(path, transform):
    """读当前内容 → transform → 先写临时文件再替换；被占用失败时重新读、重试几次。

    返回 (写之前的内容，文件原本不存在时为 None, 写入的内容)。
    """
    path = Path(path)
    last = None
    for attempt in range(WRITE_ATTEMPTS):
        if attempt:
            time.sleep(WRITE_DELAY)
        temp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        try:
            old = path.read_text(encoding='utf-8-sig') if path.exists() else None
            new = transform(old or '')
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(new)
            os.replace(temp, path)
            return old, new
        except OSError as exc:
            last = exc
            try:
                temp.unlink()
            except OSError:
                pass
    raise last


def write_records(root, date, marker, changed):
    """生成当天的机械同步任务卡、工作日志，并在 current-state.md 顶部插一段。

    返回 [(相对路径, 写之前的内容, 写入的内容)]；推送没成功时交给 undo_records 撤回，免得留下“通过”的假记录。
    """
    root = Path(root)
    stamp = datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M（UTC%z）')
    count = len(changed)
    preview = '；'.join(changed[:8]) + (f' 等 {count} 项' if count > 8 else '')
    card_rel = f"{agent_config.PATHS['task_cards']}/{date}_auto-sync.md"
    log_rel = f"{agent_config.PATHS['work_logs']}/{date}_auto-sync.md"
    card = f"""# 自动同步（每日无人值守）

- 实际写入时间：{stamp}
- 记录执行者：tools/auto_sync.py（计划任务，自动）
- 来源：git 工作区状态与 tools/sync.py 的结果
- 任务标记：{marker}
- 工作日志：{log_rel}
- 范围：只提交设置 auto_sync_paths 下的归档与状态；代码、配置不在内
- 备份：Git 可回退；拉取合并前的本机版本在 .local/backups/sync/
- 验证结果：通过
- [x] G0 接手：读取设置里的 private_remote 和 auto_sync_paths
- [x] G1 探查：拉取后找到 {count} 个有改动的文件
- [x] G2 方案与备份：只提交归档与状态；分叉、冲突、疑似密钥一律不硬推
- [x] G3 执行：tools/sync.py push 只暂存列出的文件
- [x] G4 验证：推送后回读本机与云端提交号一致；没成功就撤销暂存、撤回本记录并通知
- [x] G5 记录：本任务卡、工作日志、current-state.md
- [ ] G6 提交：由 tools/sync.py push 完成
- 本次改动：{preview}
"""
    worklog = f"""# 工作日志：自动同步

- 实际写入时间：{stamp}
- 记录执行者：tools/auto_sync.py（计划任务，自动）
- 任务标记：{marker}

## 做了什么

- 先 tools/sync.py pull，再把 auto_sync_paths 下的 {count} 个改动提交并推送：{preview}

## 验证

- tools/sync.py 推送后回读本机与云端提交号一致才算成功；钩子照常核对远端、密钥和隐私路径。
- 验证结果：通过
"""
    section = (f'\n## {date} 自动同步（{marker}）\n\n'
               f'- 实际写入时间：{stamp}，记录执行者：tools/auto_sync.py\n'
               f'- 归档与状态 {count} 项提交并推到私有仓（推送没成功时本段会被撤回）。\n')

    def insert(old):
        if marker in old:
            return old
        lines = old.splitlines()
        if lines and lines[0].startswith('#'):
            return lines[0] + '\n' + section + '\n'.join(lines[1:]) + ('\n' if lines[1:] else '')
        return '# Current state\n' + section + ('\n' + old if old else '')

    written = []
    for rel, transform in ((card_rel, lambda _: card), (log_rel, lambda _: worklog),
                           (agent_config.PATHS['state'], insert)):
        written.append((rel, *update_file(root / rel, transform)))
    return written


def undo_records(root, written):
    """撤回自动生成的记录；推送期间被别人改过的文件不动，返回这些文件。"""
    kept = []
    for rel, old, new in written:
        path = Path(root) / rel
        try:
            current = path.read_text(encoding='utf-8-sig') if path.exists() else None
            if current != new:
                kept.append(rel)
            elif old is None:
                path.unlink()
            else:
                update_file(path, lambda _, old=old: old)
        except OSError:
            kept.append(rel)
    return kept


def fail(root, files, title, detail):
    """撤销本次暂存（只动本次列出的文件），工作区改动原样保留。"""
    if files:
        git(root, 'reset', '-q', '--', *files)
    log(root, f'[停止] {detail}')
    notify(title, detail[:200])
    return 1


def run_once(root):
    root = Path(root)
    settings = agent_config.load(root)
    if not (settings.get('private_remote') or '').strip():
        log(root, '[跳过] 未设置私有仓（.personal/agent.json 的 private_remote），不提交、不推送。')
        return 0
    date = time.strftime('%Y-%m-%d')
    marker = f"AUTO-SYNC-{date.replace('-', '')}"

    # 1) 先拉取，拿到别的设备的最新状态。分叉、冲突 sync.py 不会自动处理，交给人。
    error = None
    for attempt in range(PULL_ATTEMPTS):
        if attempt:
            time.sleep(PULL_DELAY)
        try:
            sync.pull(root)
            error = None
            break
        except sync.Stop as stop:
            error = stop
            log(root, f'[重试 {attempt + 1}/{PULL_ATTEMPTS}] 拉取没成功：{str(stop)[:200]}')
    if error:
        return fail(root, [], '自动同步没完成（拉取）', f'拉取 {PULL_ATTEMPTS} 次都没成功：{error}')

    # 2) 只看归档与状态路径。
    paths = settings.get('auto_sync_paths') or []
    ignored = ignored_paths(root, paths)
    if ignored:
        log(root, '[提示] 这些路径被 .gitignore 忽略，不会同步：' + '、'.join(ignored))
    changed = changed_files(root, paths)
    if not changed:
        if ignored:
            notify('自动同步：没有可同步的内容', '归档路径被 .gitignore 忽略，见 .local/auto_sync.log。')
        log(root, '[跳过] 归档与状态路径没有改动。')
        return 0

    # 3) 需要任务卡时生成当天的机械同步记录，记录文件一起提交。
    files, card, written = list(changed), None, []
    if settings.get('require_task_card'):
        written = write_records(root, date, marker, changed)
        card = written[0][0]
        files = list(dict.fromkeys(files + [rel for rel, _, _ in written]))

    # 4) 推送：sync.py 负责先拉、只暂存列出的文件、钩子门禁、撞车撤回、回读核对。
    message = f'自动同步 {date}：归档与状态 {len(changed)} 项'
    try:
        head = sync.push(root, message, files, task_card=card, actor=ACTOR)
    except (sync.Stop, OSError, subprocess.SubprocessError) as stop:
        git(root, 'reset', '-q', '--', *files)
        detail = f'推送没成功（已撤销暂存，工作区改动保留）：{stop}'
        kept = undo_records(root, written)
        if kept:
            detail += '\n这些自动记录在推送期间被改过，没有撤回：' + '、'.join(kept)
        return fail(root, [], '自动同步没完成（推送）', detail)
    log(root, f'[完成] {message}，云端回读一致：{head[:8]}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='每日自动同步归档与状态到私有仓')
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        return run_once(root)
    except Exception as exc:  # 无人值守任务不能静默失败
        log(root, f'[异常] {type(exc).__name__}: {exc}')
        notify('自动同步异常', f'{type(exc).__name__}，见 .local/auto_sync.log。')
        return 1


if __name__ == '__main__':
    sys.exit(main())
