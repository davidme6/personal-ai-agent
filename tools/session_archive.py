#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
session_archive.py — 会话结束时的机械归档

由 .claude/settings.json 的 SessionEnd hook 调用，从 stdin 读 hook JSON。
只做**机械**的部分，判断性的（工作日志正文、长期记忆、handoff）仍由 Claude 在会话里写。

做三件事：
  1. 导出 transcript 到 logs/chat-exports/
  2. 在 logs/工作日志/ 生成当天骨架（列出今天改了哪些文件），标「待补」
  3. 追加 RAG 增量索引（缺依赖就跳过，不报错）

铁律：**永远 exit 0**。归档脚本不能把会话搞崩。
错误写到 .local/session_archive.log。

手动跑（调试用）：
    echo '{}' | python tools/session_archive.py
    python tools/session_archive.py --transcript <path>
"""
import json
import os
import re
import socket
import subprocess
import sys
import traceback
from datetime import datetime, date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ERRLOG = os.path.join(ROOT, ".local", "session_archive.log")

# 明显的密钥形态，导出前打码（AGENTS.md：密钥不入日志）
SECRET_PATTERNS = [
    (re.compile(r'(sk-[A-Za-z0-9_\-]{16,})'), 'sk-***REDACTED***'),
    (re.compile(r'(gh[pousr]_[A-Za-z0-9]{16,})'), 'gh*_***REDACTED***'),
    (re.compile(r'(AKIA[0-9A-Z]{16})'), 'AKIA***REDACTED***'),
    (re.compile(r'((?:password|passwd|secret|api[_-]?key|token)\s*[=:]\s*)(\S{8,})',
                re.I), r'\1***REDACTED***'),
]


def log_err(msg):
    try:
        os.makedirs(os.path.dirname(ERRLOG), exist_ok=True)
        with open(ERRLOG, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (datetime.now().isoformat(timespec='seconds'), msg))
    except Exception:
        pass


def scrub(text):
    for pat, repl in SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


# ---------- 1. 导出 transcript ----------

def blocks_to_text(content):
    """assistant 的 content 是块列表；user 的通常是字符串"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ''
    out = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get('type')
        if t == 'text':
            out.append((b.get('text') or '').strip())
        elif t == 'tool_use':
            out.append('`[工具] %s`' % b.get('name', '?'))
        elif t == 'tool_result':
            pass          # 工具输出不入全文，太长且多为中间态
    return '\n\n'.join(x for x in out if x)


def export_transcript(transcript_path, out_dir, short_id):
    if not transcript_path or not os.path.isfile(transcript_path):
        return None, '无 transcript 文件', []

    turns = []
    bad = 0
    with open(transcript_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                bad += 1
                continue
            if d.get('type') not in ('user', 'assistant'):
                continue
            if d.get('isSidechain'):      # 子 Agent 的支线不入主全文
                continue
            m = d.get('message')
            if not isinstance(m, dict):
                continue
            body = blocks_to_text(m.get('content'))
            if not body:
                continue
            turns.append((m.get('role', d['type']), d.get('timestamp', ''), body))

    if not turns:
        return None, '无可导出内容', []

    day = (turns[0][1] or '')[:10] or date.today().isoformat()
    host = re.sub(r'[^\w\-]', '', socket.gethostname())[:20] or 'unknown'
    out_path = os.path.join(out_dir, '%s_%s_Claude_%s.md' % (day, host, short_id))

    lines = [
        '# 会话全文（自动导出）',
        '',
        '- 会话 ID：%s' % short_id,
        '- 来源：%s' % transcript_path,
        '- 设备：%s' % host,
        '- 导出时间：%s' % datetime.now().isoformat(timespec='seconds'),
        '- 轮次：%d%s' % (len(turns), ('（%d 行解析失败）' % bad) if bad else ''),
        '',
        '> 自动导出，只含用户与助手正文；工具调用只留名称，工具输出未收录。',
        '> 密钥形态已打码，但**不等于已脱敏**，本文件在 `logs/`（已 gitignore）内，不要外发。',
        '',
        '---',
        '',
    ]
    for role, ts, body in turns:
        # 标题格式必须能过 tools/archive_gate.py 的 ROLE 正则：
        # ^##\s+(?:\[标注\]\s*)?(用户|Agent|助手|豆包|ChatGPT|Codex)(?=[\s：:]|$)
        # 「Claude」不在它的名单里，所以走 [Claude] 前缀 + 助手 的写法。
        who = '用户' if role == 'user' else '[Claude] 助手'
        lines.append('## %s `%s`' % (who, ts[:19]))   # 半角空格：全角　过不了 ROLE 正则
        lines.append('')
        lines.append(scrub(body))
        lines.append('')

    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return out_path, None, turns


# ---------- 2. 工作日志骨架 ----------

# 本仓 business/ memory/ logs/ handoff/ raw_chat/ 都在 .gitignore 里
# （设计如此：Git 只存程序与协议，数据走文件同步通道）
# 所以不能用 git status 找改动，必须按 mtime 扫盘。
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.local', '.venv', 'venv',
             'raw-materials', 'chat-exports', '个人隐私', 'local-secret',
             '.claude', 'skill'}
MAX_LIST = 200


def changed_today():
    """今天改过的文件。返回 [(是否进Git, 相对路径)]，按时间倒序。"""
    midnight = datetime.combine(date.today(), datetime.min.time()).timestamp()
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('~$')]
        for fn in filenames:
            if fn.startswith('~$') or fn.endswith(('.pyc', '.tmp')):
                continue
            full = os.path.join(dirpath, fn)
            try:
                mt = os.path.getmtime(full)
            except OSError:
                continue
            if mt >= midnight:
                hits.append((mt, os.path.relpath(full, ROOT).replace(os.sep, '/')))
                if len(hits) > MAX_LIST * 3:
                    break
    hits.sort(reverse=True)
    hits = hits[:MAX_LIST]

    # 标注哪些进 Git、哪些只走同步通道
    rels = [p for _, p in hits]
    ignored = set()
    if rels:
        try:
            # 必须走二进制：text=True 在 Windows 会把 \n 写成 \r\n，
            # git 收到带 \r 的路径会原样回显并加引号，比对全落空。
            # core.quotepath=false 另防中文路径被转义成 \350\256\260。
            r = subprocess.run(['git', '-c', 'core.quotepath=false',
                                'check-ignore', '--stdin'], cwd=ROOT,
                               input=chr(10).join(rels).encode('utf-8'),
                               capture_output=True, timeout=30)
            ignored = {x.strip().replace('\\', '/')
                       for x in r.stdout.decode('utf-8', 'replace').splitlines()
                       if x.strip()}
        except Exception:
            log_err('check-ignore: %s' % traceback.format_exc())
    return [(p not in ignored, p) for _, p in hits]


def write_skeleton(log_dir, short_id, changed, export_path):
    today = date.today().isoformat()
    path = os.path.join(log_dir, '%s_待补_%s.md' % (today, short_id))
    if os.path.exists(path):          # 同一会话重复触发就不覆盖
        return path

    lines = [
        '# %s 待补工作日志' % today,
        '',
        '> ⚠️ **这是 SessionEnd hook 自动生成的骨架，不是成品。**',
        '> 正文（范围、决策、验证、未完成）需要 Claude 在会话里按 `workflows/session-end.md` 补写。',
        '> 补完后把文件名里的「待补」去掉，改成 `%s_<主题>.md`。' % today,
        '',
        '- 任务标记：待补',
        '- 日期：%s' % today,
        '- 操作者：Claude / %s' % socket.gethostname(),
        '- 全文：%s' % (os.path.relpath(export_path, ROOT).replace('\\', '/') if export_path else '**导出失败**'),
        '- 验证结果：**待补**',
        '',
        '## 范围与决策',
        '',
        '待补。',
        '',
        '## 今天改动的文件（自动列出，共 %d 个）' % len(changed),
        '',
    ]
    if changed:
        in_git  = [p for ok, p in changed if ok]
        sync_only = [p for ok, p in changed if not ok]
        lines.append('| 归属 | 文件 |')
        lines.append('|---|---|')
        for p in in_git[:60]:
            lines.append('| Git | `%s` |' % p)
        for p in sync_only[:120]:
            lines.append('| **仅同步通道** | `%s` |' % p)
        lines.append('')
        lines.append('> `仅同步通道` = 在 .gitignore 里，**不进 Git**，只存在本机。')
        lines.append('> 这些是业务成果和记忆，丢了找不回来——确认云同步/外置备份已覆盖。')
    else:
        lines.append('今天没有文件改动。')
    lines += ['', '## 验证', '', '待补。', '', '## 未完成', '', '待补。', '']

    os.makedirs(log_dir, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return path


# ---------- 3. handoff 自动状态草稿 ----------

def generate_handoff_draft(turns, changed, short_id, export_path):
    """从已解析的会话内容生成 handoff 状态草稿，追加到最新状态.md。
    纯规则提取（不调LLM），标注「待确认」，Agent 下次会话可在此基础上整理。
    """
    handoff_path = os.path.join(ROOT, 'handoff', '最新状态.md')
    if not os.path.isfile(handoff_path):
        return None, '无 handoff 文件'

    # 提取用户最后 3 条消息（非空、非工具结果）
    user_msgs = [body for role, _, body in turns if role == 'user' and body.strip()][-3:]
    user_brief = []
    for msg in user_msgs:
        first_line = msg.strip().split('\n')[0][:80]
        if first_line:
            user_brief.append('- %s' % first_line)

    # 主要改动文件（前5个非 .gitignore 的）
    main_files = [p for ok, p in changed if ok][:5]
    sync_files = [p for ok, p in changed if not ok][:3]

    now = datetime.now()
    block = [
        '',
        '## %s 自动状态草稿（待Agent确认）' % now.strftime('%Y-%m-%d %H:%M'),
        '',
        '- 会话：%s' % short_id,
        '- 全文：%s' % (os.path.relpath(export_path, ROOT).replace('\\', '/') if export_path else '未导出'),
        '- 本日改动文件：%d 个（进Git %d / 仅同步 %d）' % (
            len(changed),
            sum(1 for ok, _ in changed if ok),
            sum(1 for ok, _ in changed if not ok)),
    ]
    if main_files:
        block.append('- Git改动：' + '、'.join('`%s`' % p for p in main_files))
    if sync_files:
        block.append('- 同步通道：' + '、'.join('`%s`' % p for p in sync_files))
    if user_brief:
        block.append('- 用户最后提及：')
        block.extend(user_brief)
    block.append('')
    block.append('> ⚠️ 此段为 session_archive.py 自动提取的草稿，未经 Agent 整理。')
    block.append('> 下次会话开头，请 Agent 将此段整理为正式状态条目，或直接删除。')
    block.append('')

    # 追加，不覆盖
    with open(handoff_path, 'a', encoding='utf-8') as f:
        f.write('\n'.join(block))
    return handoff_path, None


# ---------- 4. RAG 增量索引 ----------

def reindex():
    script = os.path.join(ROOT, 'tools', 'memory_index.py')
    if not os.path.isfile(script):
        return '跳过（无 memory_index.py）'
    try:
        r = subprocess.run([sys.executable, '-B', script], cwd=ROOT,
                           capture_output=True, text=True, timeout=600)
        if r.returncode == 0 and 'ModuleNotFoundError' not in (r.stderr or ''):
            return '已更新'
        if 'fastembed' in (r.stderr or ''):
            return '跳过（缺 fastembed，装了才能语义召回）'
        return '失败（详见 .local/session_archive.log）'
    except subprocess.TimeoutExpired:
        return '超时'
    except Exception as e:
        log_err('reindex: %r' % e)
        return '失败'


# ---------- main ----------

def main():
    payload = {}
    if '--transcript' in sys.argv:
        payload['transcript_path'] = sys.argv[sys.argv.index('--transcript') + 1]
    else:
        try:
            raw = sys.stdin.read()
            if raw.strip():
                payload = json.loads(raw)
        except Exception as e:
            log_err('stdin: %r' % e)

    session_id = str(payload.get('session_id') or '')
    short_id = (session_id.split('-')[0] or datetime.now().strftime('%H%M%S'))[:8]
    transcript = payload.get('transcript_path')
    reason = payload.get('reason', '')

    notes = []

    export_path, err, turns = None, None, []
    try:
        export_path, err, turns = export_transcript(
            transcript, os.path.join(ROOT, 'logs', 'chat-exports'), short_id)
    except Exception as e:
        err = repr(e)
        log_err('export: %s' % traceback.format_exc())
    notes.append('全文：' + (os.path.basename(export_path) if export_path else ('未导出（%s）' % err)))

    changed = []
    try:
        changed = changed_today()
    except Exception:
        log_err('changed_today: %s' % traceback.format_exc())

    # 自动生成 handoff 状态草稿
    handoff_draft = None
    try:
        if turns or changed:
            handoff_draft, _ = generate_handoff_draft(turns, changed, short_id, export_path)
            if handoff_draft:
                notes.append('handoff草稿：已追加（待Agent确认）')
    except Exception:
        log_err('handoff_draft: %s' % traceback.format_exc())
        notes.append('handoff草稿：失败')

    # 没改动 + 没内容，就是个空会话，不生成骨架
    skeleton = None
    if changed or export_path:
        try:
            skeleton = write_skeleton(
                os.path.join(ROOT, 'logs', '工作日志'), short_id, changed, export_path)
            notes.append('日志骨架：%s（%d 个改动文件）' % (os.path.basename(skeleton), len(changed)))
        except Exception:
            log_err('skeleton: %s' % traceback.format_exc())
            notes.append('日志骨架：失败')
    else:
        notes.append('空会话，跳过骨架')

    notes.append('语义索引：' + reindex())

    msg = '📦 已归档（%s）｜%s' % (reason or 'SessionEnd', '　'.join(notes))
    if skeleton and '待补' in os.path.basename(skeleton):
        msg += '\n⚠️ 工作日志正文仍需补写：' + os.path.relpath(skeleton, ROOT).replace('\\', '/')

    print(json.dumps({'systemMessage': msg}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        log_err('main: %s' % traceback.format_exc())
        sys.exit(0)          # 铁律：归档失败不能影响会话
