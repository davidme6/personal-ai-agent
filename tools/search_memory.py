#!/usr/bin/env python3
"""关键词 / 日期检索：没装 fastembed、用不了语义召回时的兜底，只用标准库。

  python tools/search_memory.py -k 发布计划
  python tools/search_memory.py -k 发布计划 -d 2026-03-14
  python tools/search_memory.py -d 2026-03-14                 这一天相关的全部记录
  python tools/search_memory.py -k 定时任务 --dir .personal/memory --limit 40

默认范围（都在 .personal/ 下）：当前状态、对话导出、记忆、交接、工作日志、任务卡、知识。
  -k  关键词，不分大小写，按行匹配
  -d  YYYY-MM-DD：只看和这一天有关的文件——路径里带这个日期，或正文里出现这个日期
      （对话导出每轮都带时间，跨天的会话也找得到）；只给 -d 时列出提到这一天的行
  --dir  只搜这些目录或文件（相对工作区根目录，可写多次）
  --limit 每个文件最多显示几行（默认 10）；-q 只列文件
命中只是线索：要引用原话、数字、日期时打开原文核对。
"""
import argparse
import sys
from pathlib import Path

import agent_config

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KEYS = ('state', 'chat_exports', 'memory', 'handoff', 'work_logs', 'task_cards')
KNOWLEDGE = agent_config.PATHS.get('knowledge', '.personal/knowledge')
TEXT_EXT = {'.md', '.txt', '.json', '.jsonl', '.csv', '.yaml', '.yml', '.html', '.htm'}
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', 'secrets', 'attachments'}


def default_targets():
    return [agent_config.PATHS[key] for key in DEFAULT_KEYS] + [KNOWLEDGE]


def text_files(root, targets):
    seen = set()
    for target in targets:
        base = root / target
        if base.is_file():
            candidates = [base]
        elif base.is_dir():
            candidates = sorted(path for path in base.rglob('*') if path.is_file()
                                and not SKIP_DIRS.intersection(path.relative_to(base).parts[:-1]))
        else:
            continue
        for path in candidates:
            if path.suffix.lower() in TEXT_EXT and path not in seen:
                seen.add(path)
                yield path


def search(root, keyword=None, date=None, targets=None):
    """返回 [(相对路径, [(行号, 行)])]，只含有命中的文件。"""
    root = Path(root).resolve()
    needle = keyword.casefold() if keyword else None
    results = []
    for path in text_files(root, targets or default_targets()):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError as error:
            print(f'[跳过] {rel}：{error}', file=sys.stderr)
            continue
        if date and date not in rel and date not in text:
            continue
        lines = text.splitlines()
        if needle:
            hits = [(number, line) for number, line in enumerate(lines, 1) if needle in line.casefold()]
        else:
            hits = [(number, line) for number, line in enumerate(lines, 1) if date in line] or [(0, '（文件名带这个日期）')]
        if hits:
            results.append((rel, hits))
    return results


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='按关键词 / 日期检索记忆与记录')
    parser.add_argument('--root', type=Path, default=ROOT, help='工作区根目录')
    parser.add_argument('-k', '--keyword', help='关键词（和 -d 至少给一个）')
    parser.add_argument('-d', '--date', help='日期 YYYY-MM-DD')
    parser.add_argument('--dir', action='append', default=[], help='只搜这些目录或文件，可写多次')
    parser.add_argument('--limit', type=int, default=10, help='每个文件最多显示几行')
    parser.add_argument('-q', '--quiet', action='store_true', help='只列文件')
    args = parser.parse_args(argv)
    if not args.keyword and not args.date:
        parser.error('至少给 -k 关键词 或 -d 日期')
    root = args.root.resolve()
    targets = args.dir or default_targets()
    if not any((root / target).exists() for target in targets):
        print(f'[停止] 这些位置都不存在：{"、".join(targets)}（新工作区先运行 python tools/init_workspace.py）')
        return 1

    results = search(root, args.keyword, args.date, targets)
    for rel, hits in results:
        if args.quiet:
            print(rel)
            continue
        print(f'\n== {rel}（命中 {len(hits)} 行）==')
        for number, line in hits[:args.limit]:
            print(f'  {number}: {line.strip()[:160]}')
        if len(hits) > args.limit:
            print(f'  ……还有 {len(hits) - args.limit} 行，用 --limit 放宽')
    print(f'\n[完成] 命中 {sum(len(hits) for _, hits in results)} 行 / {len(results)} 个文件；引用前请打开原文核对')
    return 0


if __name__ == '__main__':
    sys.exit(main())
