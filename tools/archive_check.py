#!/usr/bin/env python3
"""归档完整性检查：看一份对话归档是不是逐轮全文，而不是摘要或整理稿。

  python tools/archive_check.py check <文件> [<文件>…]   相对路径按工作区根目录算
  python tools/archive_check.py day [YYYY-MM-DD]          默认今天

每份文件查四件事：
  1. 有角色标题，用户和 AI 两方都有。认的是二级标题开头的角色名，前面可以带“[标注]”：
     chat_archive.py 导出的“## 用户 `时间`”“## 助手（Claude Code） `时间`”，
     网页版 AI 手写的“## 用户”“## 助手”“## ChatGPT”“## Claude”“## Gemini”等。
  2. 没有空轮次：每个角色标题下面都有正文。
  3. 轮数对得上：文件头写了“条数：用户 N，助手 M”或“轮次：N”的，要和实际标题数一致（防截断）。
  4. 写了来源：第一轮之前有“来源：”“平台：”或“基座：”，说清是哪个平台、怎么取得的。
标了【原文不可取得】的轮次照常算，结果里另报个数（这样的归档只能算“尽可能完整版本”）。
day：检查 .personal/logs/chat-exports/ 下（含子目录）文件名以这一天开头的 .md / .txt，跳过总索引和单次索引。

只查结构：查不出正文是不是被概括过，也查不出平台没给的消息、没导出的会话。有不合格的返回 1。
"""
import argparse
import datetime
import re
import sys
from pathlib import Path

import agent_config
from chat_archive import ATTACHMENTS, INDEX_NAME, SINGLE

ROOT = Path(__file__).resolve().parent.parent
USER_NAMES = ('用户', 'User', 'Human')
AI_NAMES = ('助手', 'Assistant', 'Agent', 'AI', 'ChatGPT', 'GPT', 'Claude', 'Codex', 'Gemini', 'Copilot',
            'Cursor', 'WorkBuddy', 'DeepSeek', 'Kimi', '通义千问', '通义', 'Qwen', '智谱', 'GLM', '豆包',
            '文心一言', '文心', 'Grok')
ROLE = re.compile(r'^##[ \t]+(?:\[[^\]\n]*\][ \t]*)?(' + '|'.join(map(re.escape, USER_NAMES + AI_NAMES))
                  + r')(?=[ \t：:（(`]|$)[^\n]*$', re.M | re.I)
USERS = {name.casefold() for name in USER_NAMES}
SOURCE = re.compile(r'(?:来源|平台|基座|Source|Platform)\s*[：:]', re.I)
DECLARED_SPLIT = re.compile(r'条数[：:]\s*用户\s*(\d+)\s*[，,]\s*助手\s*(\d+)')
DECLARED_TOTAL = re.compile(r'轮次[：:]\s*(\d+)')
COMMENT = re.compile(r'<!--.*?-->', re.S)
MISSING = '【原文不可取得】'
SCOPE = '只查了结构：查不出正文是不是被概括过，也查不出平台没给的消息、没导出的会话。'


def check_file(path):
    """返回 (是否合格, 一句说明)。"""
    path = Path(path)
    name = path.name
    if not path.is_file():
        return False, f'找不到文件：{path}'
    try:
        text = path.read_text(encoding='utf-8-sig')
    except UnicodeDecodeError:
        return False, f'{name}：不是 UTF-8 文本'
    heads = list(ROLE.finditer(text))
    users = sum(head.group(1).casefold() in USERS for head in heads)
    ais = len(heads) - users
    if not users or not ais:
        return False, f'{name}：没有用户和 AI 两方的角色标题，像摘要或整理稿，不是逐轮全文'
    for number, head in enumerate(heads):
        end = heads[number + 1].start() if number + 1 < len(heads) else len(text)
        if not COMMENT.sub('', text[head.end():end]).strip():
            line = text.count('\n', 0, head.start()) + 1
            return False, f'{name}：第 {line} 行“{head.group().strip()}”下面没有正文'
    preamble = text[:heads[0].start()]
    if not SOURCE.search(preamble):
        return False, f'{name}：第一轮之前没写来源（哪个平台、怎么取得的）'
    found = DECLARED_SPLIT.search(preamble)
    if found and (int(found.group(1)), int(found.group(2))) != (users, ais):
        return False, (f'{name}：文件头写的是用户 {found.group(1)}、助手 {found.group(2)}，'
                       f'实际标题是用户 {users}、AI {ais}，可能被截断或改过')
    found = DECLARED_TOTAL.search(preamble)
    if found and int(found.group(1)) != len(heads):
        return False, f'{name}：文件头写的是 {found.group(1)} 轮，实际有 {len(heads)} 个角色标题，可能被截断或改过'
    missing = text[heads[0].start():].count(MISSING)
    note = f'；{missing} 处标了{MISSING}，算尽可能完整版本' if missing else ''
    return True, f'{name}：用户 {users} 轮，AI {ais} 轮，写了来源{note}'


def day_files(root, date):
    """这一天的归档全文：文件名以日期开头，跳过总索引、单次索引和图片附件目录。"""
    chat = agent_config.path(root, 'chat_exports')
    if not chat.is_dir():
        return []
    return sorted(path for path in chat.rglob(date + '*')
                  if path.is_file() and path.suffix in ('.md', '.txt') and path.name != INDEX_NAME
                  and not path.stem.endswith(SINGLE) and ATTACHMENTS not in path.relative_to(chat).parts)


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='检查对话归档是不是逐轮全文')
    parser.add_argument('--root', type=Path, default=ROOT, help='工作区根目录')
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check', help='检查指定的归档文件')
    check.add_argument('files', nargs='+', type=Path)
    day = sub.add_parser('day', help='检查某一天的全部归档')
    day.add_argument('date', nargs='?', help='YYYY-MM-DD，默认今天')
    args = parser.parse_args(argv)
    root = args.root.resolve()

    if args.command == 'check':
        results = [check_file(path if path.is_absolute() else root / path) for path in args.files]
    else:
        try:
            date = datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
        except ValueError:
            parser.error('日期格式应为 YYYY-MM-DD')
        files = day_files(root, date.isoformat())
        results = [check_file(path) for path in files] or [
            (False, f'{date.isoformat()}：{agent_config.PATHS["chat_exports"]}/ 下没有这一天的归档')]
    for ok, detail in results:
        print(('[通过] ' if ok else '[缺口] ') + detail)
    print(SCOPE)
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == '__main__':
    sys.exit(main())
