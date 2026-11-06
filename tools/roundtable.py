#!/usr/bin/env python3
"""研讨小组：用户、本机 AI、Codex 等在同一个记录文件里轮流发言，用来“一家做、另一家审”。

  python tools/roundtable.py post  --room 议题.md --speaker 用户 --text-file 发言.txt
  python tools/roundtable.py codex --room 议题.md [--timeout 900] [--codex 命令路径]

post ：把一段发言原样追加到记录文件，标题是“## 发言人 `时间`”。记录文件不存在时先新建，
       文件头写明来源，这样它本身就是一份逐轮全文，能过 archive_check.py。
codex：调用本机 Codex 命令行（只读沙箱，不能改文件）读记录文件并发言，把它的原话追加进记录文件并打印。
       Codex 没给出发言（报错、超时、空回复）时不写入，记录文件保持原样。

--room 只写文件名时放在 .personal/logs/chat-exports/roundtable/ 下；带目录的按工作区根目录算，
但必须在 .personal/logs/chat-exports/ 里面，避免把发言追加进别的文件。
主持的人或 AI 负责轮流调用：先 post 议题和材料位置，再让各方发言。
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import agent_config

ROOT = Path(__file__).resolve().parent.parent
ROOM_DIR = 'roundtable'   # 在 chat-exports 下
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
PROMPT = """你是 Codex，正在参加一个研讨小组，成员见记录文件。这不是改造任务。
- 只读：不要修改或创建任何文件，不要归档、提交；你的发言由主持统一写入记录。
- 先读记录文件 {room}（相对工作区根目录）。里面有议题、材料位置和到目前为止的全部发言；需要时再读它列出的材料。
- 然后以 Codex 的身份发言：回应最新的发言，说清你同意什么、不同意什么、理由是什么；可以向其他成员提一个问题。
- 中文，直接说观点，不客套，不复述别人已经说过的话，不超过 500 字。只输出发言正文。"""


def find_codex():
    """PATH 里的 codex 优先；Windows 桌面版自带的命令行（目录名带版本号，取最新的）其次。"""
    found = shutil.which('codex')
    if found:
        return found
    bundled = Path(os.environ.get('LOCALAPPDATA', '')) / 'OpenAI/Codex/bin'
    candidates = sorted(bundled.glob('*/codex.exe'), key=lambda path: path.stat().st_mtime) if os.name == 'nt' else []
    return str(candidates[-1]) if candidates else None


def room_path(root, value):
    """把 --room 换成绝对路径；不在 chat-exports 里就抛 ValueError。"""
    chat = agent_config.path(root, 'chat_exports').resolve()
    value = Path(value)
    room = (chat / ROOM_DIR / value) if value.parent == Path('.') else (root / value)
    room = room.resolve()
    if not room.is_relative_to(chat):
        raise ValueError(f'记录文件必须放在 {agent_config.PATHS["chat_exports"]}/ 下：{value}')
    return room


def append(room, speaker, text):
    """原样追加一段发言；文件不存在就先写文件头。"""
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    created = not room.exists()
    room.parent.mkdir(parents=True, exist_ok=True)
    with open(room, 'a', encoding='utf-8', newline='\n') as stream:
        if created:
            stream.write(f'# 研讨记录：{room.stem}\n\n'
                         '- 来源：tools/roundtable.py 逐条追加的发言原文（研讨小组）\n'
                         f'- 开始时间：{stamp}（本机时间）\n\n---\n')
        stream.write(f'\n## {speaker.strip()} `{stamp}`\n\n{text.strip()}\n')
    return created


def ask_codex(root, room, timeout, command=None):
    """返回 Codex 的发言；没拿到就抛 RuntimeError，记录文件不动。"""
    command = command or [find_codex()]
    if not command[0]:
        raise RuntimeError('本机找不到 Codex 命令行（装好后确认 codex 在 PATH 里，或用 --codex 指定路径）。')
    with tempfile.TemporaryDirectory() as temp:
        answer = Path(temp) / 'answer.txt'
        prompt = PROMPT.format(room=room.relative_to(root).as_posix())
        out = subprocess.run([*command, 'exec', '-s', 'read-only', '-C', str(root), '--color', 'never',
                              '-o', str(answer), '-'], input=prompt.encode('utf-8'), capture_output=True,
                             timeout=timeout, creationflags=NO_WINDOW)
        reply = answer.read_text(encoding='utf-8').strip() if answer.exists() else ''
    if out.returncode != 0 or not reply:
        tail = (out.stderr or out.stdout).decode('utf-8', 'replace').strip()[-800:]
        raise RuntimeError(f'Codex 没有给出发言（退出码 {out.returncode}）：\n{tail}')
    append(room, 'Codex', reply)
    return reply


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='研讨小组：多方在同一个记录文件里轮流发言')
    parser.add_argument('--root', type=Path, default=ROOT, help='工作区根目录')
    sub = parser.add_subparsers(dest='command', required=True)
    post = sub.add_parser('post', help='把一段发言原样追加到记录文件')
    post.add_argument('--speaker', required=True, help='发言人，例如 用户、Claude、Cursor')
    post.add_argument('--text-file', type=Path, required=True, help='发言正文所在的文本文件（UTF-8）')
    codex = sub.add_parser('codex', help='让本机 Codex 只读地读记录并发言')
    codex.add_argument('--timeout', type=int, default=900, help='秒')
    codex.add_argument('--codex', help='Codex 命令行路径（默认在 PATH 里找）')
    for command in (post, codex):
        command.add_argument('--room', required=True, help='记录文件名，或 chat-exports 下的相对路径')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        room = room_path(root, args.room)
    except ValueError as error:
        print(f'[停止] {error}')
        return 1
    if args.command == 'post':
        if append(room, args.speaker, args.text_file.read_text(encoding='utf-8-sig')):
            print(f'[新建] {room.relative_to(root).as_posix()}')
        return 0
    if not room.is_file():
        print(f'[停止] 记录文件还不存在，先用 post 写入议题：{room.relative_to(root).as_posix()}')
        return 1
    try:
        print(ask_codex(root, room, args.timeout, [args.codex] if args.codex else None))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(f'[停止] {error}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
