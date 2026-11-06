#!/usr/bin/env python3
"""提交前门禁（只读检查，失败不放行提交）。

  python tools/workflow_gate.py --pre-commit [--task-card 路径]   pre-commit 钩子用
  python tools/workflow_gate.py --task-card 路径                  单独核对一张任务卡

--pre-commit 总是核对暂存区边界：origin 推送地址必须是 .personal/agent.json 里的 private_remote
（https 和 ssh 写法、末尾 .git 视为同一个）；不许暂存只留本机的路径（local_only）、.personal/files/
（原件走云盘）、.env*、*.pem/.key/.p12/.pfx/.kdbx、文件名含 password/密码 的文件；暂存内容含密钥格式就拦
（只报文件名，不输出密钥）。

设置 require_task_card 为 true 时，--pre-commit 还要核对任务卡（--task-card，或环境变量
PROJECT_TASK_CARD；都没给时取 .personal/logs/task-cards/ 下今天唯一的一张）：
  - 任务卡在 .personal/logs/task-cards/ 下，有任务标记，G0–G5 都勾上，验证结果为“通过”；
  - 工作日志在 .personal/logs/work-logs/ 下，含任务标记、“## 验证”标题和“验证结果：通过”；
  - .personal/current-state.md 含任务标记。
门禁只核对记录是否对得上，不代替实际测试。
"""
import argparse
import datetime
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_config  # noqa: E402
import upload_check  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCAN_MAX = 5 * 1024 * 1024


def field(text, name):
    match = re.search(r'^- ' + re.escape(name) + r'[：:]\s*(.+)$', text, re.M)
    return match.group(1).strip().strip('`').strip() if match else ''


def within(root, value, area):
    """value 解析后必须落在 root/area 之内，防止任务卡借用别处的文件。"""
    if not value:
        raise ValueError(f'路径为空，必须位于 {area}')
    path = (Path(root) / value).resolve()
    if not path.is_relative_to((Path(root) / area).resolve()):
        raise ValueError(f'{value} 必须位于 {area}')
    return path


def check_task(root, card):
    root = Path(root).resolve()
    errors = []
    try:
        card = within(root, str(card), agent_config.PATHS['task_cards'])
        text = card.read_text(encoding='utf-8-sig')
        marker = field(text, '任务标记')
        if not marker:
            errors.append('任务卡缺任务标记')
        for gate in range(6):
            if not re.search(r'^- \[[xX]\] G' + str(gate) + r'(?:\s|[：:])', text, re.M):
                errors.append(f'任务卡 G{gate} 未勾选')
        if field(text, '验证结果') != '通过':
            errors.append('任务卡验证结果不是“通过”')
        log = within(root, field(text, '工作日志'), agent_config.PATHS['work_logs']).read_text(encoding='utf-8-sig')
        if not marker or marker not in log:
            errors.append('工作日志不含本任务标记')
        if not re.search(r'^## 验证\s*$', log, re.M) or not re.search(r'验证结果[：:]\s*通过', log):
            errors.append('工作日志缺少“## 验证”或“验证结果：通过”')
        state = agent_config.path(root, 'state').read_text(encoding='utf-8-sig')
        if not marker or marker not in state:
            errors.append('current-state.md 没有记录本任务标记')
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    return errors


def find_card(root, given):
    if given:
        return given
    folder = agent_config.path(root, 'task_cards')
    candidates = sorted(folder.glob(datetime.date.today().isoformat() + '_*.md')) if folder.is_dir() else []
    return candidates[0] if len(candidates) == 1 else None


def check_staged(root, settings=None):
    """返回 (问题列表, 暂存文件数)。"""
    settings = settings or agent_config.load(root)
    try:
        entries, _ = upload_check.staged_entries(root)
    except OSError as exc:
        return [str(exc)], 0
    blobs = upload_check.read_blobs(root, {oid: SCAN_MAX for _, oid in entries})
    errors, _ = upload_check.remote_problems(root, settings)
    for name, oid in entries:
        reason = upload_check.path_problem(name, settings)
        if reason:
            errors.append(f'暂存区包含{reason}：{name}')
            continue
        data = blobs[oid][1]
        if data and b'\x00' not in data[:8192] and upload_check.find_secrets(data.decode('utf-8', 'replace')):
            errors.append('暂存内容疑似包含密钥，停止提交（不输出密钥）：' + name)
    return errors, len(entries)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description='提交前门禁')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--pre-commit', action='store_true', help='pre-commit 钩子用：核对暂存区，必要时核对任务卡')
    parser.add_argument('--task-card', type=Path, default=os.environ.get('PROJECT_TASK_CARD') or None)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    settings = agent_config.load(root)
    errors = []
    need_card = settings.get('require_task_card') or not args.pre_commit
    if need_card:
        card = find_card(root, args.task_card)
        if card is None:
            errors.append('请用 --task-card 或环境变量 PROJECT_TASK_CARD 指定本任务的任务卡；不借用其他任务的记录。')
        else:
            errors += check_task(root, card)
    count = 0
    if args.pre_commit:
        staged_errors, count = check_staged(root, settings)
        errors += staged_errors
    for error in errors:
        print('[未通过] ' + error)
    if errors:
        print('处理完再提交。')
        return 1
    if args.pre_commit:
        done = '任务记录对得上；' if need_card else ''
        print(f'[通过] {done}已核对暂存的 {count} 个文件的边界。')
    else:
        print('[通过] 任务记录对得上。门禁只核对记录，不代替实际测试。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
