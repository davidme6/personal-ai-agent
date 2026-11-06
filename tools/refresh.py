#!/usr/bin/env python3
"""归档时的机械更新，一条命令做完；计划任务每天定时也跑它。只动本机文件，不提交、不推送。

  python tools/refresh.py [--root R] [--quiet]

按顺序做：
  1. 会话导出    chat_archive.py export（设置 archive.all_projects 为 true 时用 export-all）
  2. 豆包导出    doubao_export.py（只在设置 archive.sources 里有 doubao 时才跑，默认不跑）
  3. 全量备份    chat_archive.py export-full（只留本机）
  4. 语义索引    memory_index.py（没装 fastembed 就跳过，不算失败）
  5. 对话总索引  chat_archive.py index
  6. 记忆过期检查 .personal/memory/ 下的 .md 按标题分段，段内 updated / last_verified 的最新日期
                距今超过 30 天的列出来，提醒去核实或更新；没有日期标记的段只报个数，
                status: closed / superseded 的段不查。

哪一步失败都接着做后面的。报告每次覆盖写到 .local/refresh_report.txt，另在 .local/refresh.log 追加一行；
有失败时返回 1。用 pythonw 无窗口运行时没有标准输出，结果只看这两个文件。
"""
import argparse
import datetime
import importlib.util
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

import agent_config

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
REPORT = '.local/refresh_report.txt'
LOG = '.local/refresh.log'
STALE_DAYS = 30
STEP_TIMEOUT = 3600   # 一步卡住不能把定时任务永远挂着
MISSING_DEPS = 'Semantic-memory dependencies are missing'   # memory_index.py 缺依赖时的报错
HEADING = re.compile(r'^#{1,4} .+$', re.M)
STAMP = re.compile(r'(?:updated|last_verified)[^\n]*?(\d{4}-\d{2}-\d{2})')
CLOSED = re.compile(r'status[：:]\s*(?:closed|superseded)', re.I)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def semantic_ready():
    return importlib.util.find_spec('fastembed') is not None


def build_steps(root, settings):
    """[(名称, 命令参数或 None, 跳过原因)]。命令参数不含 python 本身；为 None 的步骤跳过。"""
    archive = [str(TOOLS / 'chat_archive.py'), '--root', str(root)]
    export = 'export-all' if (settings.get('archive') or {}).get('all_projects') else 'export'
    sources = {str(name).lower() for name in (settings.get('archive') or {}).get('sources') or ()}
    if semantic_ready():
        semantic = ([str(TOOLS / 'memory_index.py'), '--root', str(root)], '')
    else:
        semantic = (None, '没装 fastembed（要用语义召回时运行 python -m pip install -r requirements-memory.txt）')
    doubao = [('豆包导出', [str(TOOLS / 'doubao_export.py'), '--root', str(root)], '')] if 'doubao' in sources else []
    return [('会话导出', archive + [export], '')] + doubao + [
            ('全量备份', archive + ['export-full'], ''),
            ('语义索引',) + semantic,
            ('对话总索引', archive + ['index'], '')]


def python_executable():
    python = Path(sys.executable)
    if python.name.lower() == 'pythonw.exe' and python.with_name('python.exe').exists():
        python = python.with_name('python.exe')   # pythonw 启动的子进程没有标准输出，换成 python.exe
    return str(python)


def run_step(root, command):
    """跑一步，返回 (是否成功, 输出的最后几行)。"""
    done = subprocess.run([python_executable(), *command], cwd=root, stdin=subprocess.DEVNULL, capture_output=True,
                          encoding='utf-8', errors='replace', timeout=STEP_TIMEOUT,
                          env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, creationflags=NO_WINDOW)
    lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
    return done.returncode == 0, lines[-4:]


def stale_memory(root, today, days=STALE_DAYS):
    """返回 (过期的段 [(文件, 标题, 最新日期, 距今天数)], 没有日期标记的段数)。"""
    stale, undated = [], 0
    folder = agent_config.path(root, 'memory')
    for path in sorted(folder.rglob('*.md')) if folder.is_dir() else []:
        text = path.read_text(encoding='utf-8-sig', errors='replace')   # 文件头有 BOM 时第一个标题也要认得
        marks = list(HEADING.finditer(text))
        for number, mark in enumerate(marks):
            body = text[mark.end():marks[number + 1].start() if number + 1 < len(marks) else len(text)]
            if not body.strip() or CLOSED.search(body):
                continue
            dates = []
            for found in STAMP.findall(body):
                try:
                    dates.append(datetime.date.fromisoformat(found))
                except ValueError:
                    pass
            if not dates:
                undated += 1
            elif (today - max(dates)).days > days:
                stale.append((path.relative_to(root).as_posix(), mark.group().lstrip('# ').strip(),
                              max(dates).isoformat(), (today - max(dates)).days))
    return stale, undated


def refresh(root, steps=None, today=None, runner=run_step):
    """跑完全部步骤，写报告和日志；返回 (报告全文, 失败的步骤名)。"""
    root = Path(root).resolve()
    local = agent_config.path(root, 'local')
    local.mkdir(parents=True, exist_ok=True)
    if steps is None:
        steps = build_steps(root, agent_config.load(root))
    now = datetime.datetime.now()
    report, failed, skipped = [f'机械更新 {now:%Y-%m-%d %H:%M:%S}', ''], [], []
    for name, command, reason in steps:
        if command is None:
            skipped.append(name)
            report.append(f'[{name}] 跳过：{reason}')
            continue
        try:
            ok, tail = runner(root, command)
        except Exception as exc:   # 启动不了、超时都只算这一步失败
            ok, tail = False, [f'{type(exc).__name__}: {exc}']
        if not ok and any(MISSING_DEPS in line for line in tail):
            skipped.append(name)
            report.append(f'[{name}] 跳过：缺少语义索引依赖（python -m pip install -r requirements-memory.txt）')
            continue
        if not ok:
            failed.append(name)
        report += [f"[{name}] {'完成' if ok else '失败'}"] + ['  ' + line for line in tail]
    stale = []
    try:
        stale, undated = stale_memory(root, today or now.date())
        report += ['', f'[记忆过期检查] 超过 {STALE_DAYS} 天没核实的 {len(stale)} 段；没有日期标记的 {undated} 段。']
        report += [f'  {days} 天  {day}  {path}  {title}' for path, title, day, days in stale]
    except OSError as exc:
        failed.append('记忆过期检查')
        report += ['', f'[记忆过期检查] 失败：{exc}']
    summary = f"有 {len(failed)} 步失败：{'、'.join(failed)}" if failed else '全部完成'
    if skipped:
        summary += '；跳过：' + '、'.join(skipped)
    report += ['', '结果：' + summary]
    text = '\n'.join(report) + '\n'
    (root / REPORT).write_text(text, encoding='utf-8', newline='\n')
    with open(root / LOG, 'a', encoding='utf-8', newline='\n') as log:
        log.write(f'{now:%Y-%m-%d %H:%M:%S}  {summary}；记忆过期 {len(stale)} 段\n')
    return text, failed


def main(argv=None):
    parser = argparse.ArgumentParser(description='归档时的机械更新')
    parser.add_argument('--root', type=Path, default=ROOT, help='工作区根目录')
    parser.add_argument('--quiet', action='store_true', help='不打印，只写报告和日志')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        text, failed = refresh(root)
    except Exception:
        # 报告都没写成时，至少在日志里留一行原因；无窗口运行时没有别的地方能看到
        try:
            (root / LOG).parent.mkdir(parents=True, exist_ok=True)
            with open(root / LOG, 'a', encoding='utf-8', newline='\n') as log:
                reason = traceback.format_exc().strip().splitlines()[-1]
                log.write(f'{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  中断：{reason}\n')
        except OSError:
            pass
        return 1
    if not args.quiet and sys.stdout is not None:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        print(text, end='')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
