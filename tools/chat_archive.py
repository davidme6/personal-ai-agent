#!/usr/bin/env python3
"""对话归档：把本机 AI 编程助手的会话记录转成逐字 Markdown，做只留本机的全量备份，并生成总索引。

  python tools/chat_archive.py export        只导出工作目录在本工作区里的会话
  python tools/chat_archive.py export-all    本机所有项目的会话
  python tools/chat_archive.py export-full   原始记录全量备份（只留本机）
  python tools/chat_archive.py index         重写对话总索引

通用参数放在子命令前面：--root 工作区根目录；--home 从哪个用户目录读会话（默认取环境变量
CHAT_ARCHIVE_HOME，没有就用当前用户目录）。export、export-all、export-full 可加 --force，已是最新的也重做。

来源（设置 archive.sources，可选 claude、codex、workbuddy；豆包客户端另有 doubao_export.py，来源里写 doubao 时由 refresh.py 调用）：
  Claude Code  ~/.claude/projects/<编码后的路径>/*.jsonl
  Codex        ~/.codex/sessions/**/*.jsonl、~/.codex/archived_sessions/**/*.jsonl
  WorkBuddy    ~/.workbuddy/projects/*/*.jsonl（用户发的图片在 ~/.workbuddy/blobs/）

export / export-all
  每个会话一个文件 YYYY-MM-DD_<设备名>_<平台>_<会话号前 8 位>.md（Codex 取前 13 位：它的会话号前 8 位
  是时间，同一分钟开的两个会话会重名）。内容是全部用户消息和助手可见回复，带本机时间，按原始顺序；
  不含工具调用与输出、隐藏推理、客户端塞进来的系统提示；明显的密钥和证件号打码。
  本工作区的会话放 .personal/logs/chat-exports/，别的项目放其下 other-projects/<项目名>/；
  设置 archive.local_only_projects 里的项目只写到 .local/chat-exports-local/<项目名>/，不进 Git。
  用户发的图片存到全文旁边的 attachments/<会话号>/，按出现顺序叫 img01、img02……
  哪张含敏感内容：在那里建一个同名的 imgNN.skip，删掉图片，再加 --force 重导，以后都不会再存它。
  已导出且不比原始记录旧的跳过；原始记录有更新就整份重写，可以反复运行。
  子代理、审批这类内部会话和只有一方发言的会话不导。
export-full
  同一批原始记录（含子代理）逐行照抄，只把大块 base64（图片、加密推理）换成一句占位，
  gzip 后按项目存到 .local/backups/sessions/<项目名>/。没有打码，可能含密钥，所以只留本机。
  范围跟 archive.all_projects 走：为 false 时只备份本工作区的会话。
index
  按 .personal/logs/chat-exports/ 里实际存在的 .md 重写 00_index.md。单次索引（<全文文件名去掉 .md>_INDEX.md，
  和全文同目录）挂在对应全文那一行，不单独成行；找不到全文的单次索引照常单独列出。
"""
import argparse
import base64
import datetime
import gzip
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import agent_config

ROOT = Path(__file__).resolve().parent.parent
OTHERS = 'other-projects'
ATTACHMENTS = 'attachments'
LOCAL_EXPORTS = 'chat-exports-local'   # 在 .local/ 下
FULL_DIR = 'backups/sessions'          # 在 .local/ 下
INDEX_NAME = '00_index.md'
SINGLE = '_INDEX'   # 单次索引：<全文文件名去掉 .md>_INDEX.md，和全文放同一目录
SOURCES = ('claude', 'codex', 'workbuddy')
LABELS = {'Claude': 'Claude Code', 'Codex': 'Codex', 'WorkBuddy': 'WorkBuddy'}

REMINDER = re.compile(r'<system-reminder\b[^>]*>.*?</system-reminder>', re.S)
IMAGE_MENTION = re.compile(r'\[Image: source: [^\]]*\]')
NOT_USER = ('<task-notification>', '<local-command', '<command-name>', '<command-message>')
# Codex 塞在用户消息开头的系统块。只认列出来的标签，认不出的宁可留着，也不误删用户自己的话。
INJECTED = re.compile(
    r'\A(?:\s*(?:</?image\b[^>]*>|# AGENTS\.md instructions.*\Z'
    r'|<(environment_context|codex_internal_context|recommended_plugins|subagent_notification|turn_aborted'
    r'|external_codex_apps_\w+|in-app-browser-context|permissions instructions|user_instructions'
    r'|system-reminder|task-notification|command-name|command-message|command-args|local-command-\w+)'
    r'\b.*?(?:</\1>|\Z)))+', re.S)
SUMMARY = 'This session is being continued from a previous conversation'
WB_QUERY = re.compile(r'<user_query>(.*?)</user_query>', re.S)
DATA_URL = re.compile(r'data:image/([^;,]+);base64,(.*)', re.S)
BLOB = re.compile(rb'[A-Za-z0-9+/]{2000,}={0,2}')   # 图片、加密推理等大块 base64
EXPORTED = re.compile(r'\d{4}-\d{2}-\d{2}_.+_(?:Claude|Codex|WorkBuddy|DoubaoWork|Doubao)_[0-9A-Za-z-]+$')
PLATFORM = re.compile(r'^- (?:平台|基座|来源平台)[：:]\s*(.+)$', re.M)

# 明显的密钥形态和证件号，导出前打码。只是兜底，不等于脱敏。
SECRETS = [
    (re.compile(r'sk-[A-Za-z0-9_\-]{16,}'), 'sk-***'),
    (re.compile(r'gh[pousr]_[A-Za-z0-9]{16,}'), 'gh*_***'),
    (re.compile(r'github_pat_[A-Za-z0-9_]{20,}'), 'github_pat_***'),
    (re.compile(r'xox[abprs]-[A-Za-z0-9-]{10,}'), 'xox*-***'),
    (re.compile(r'AKIA[0-9A-Z]{16}'), 'AKIA***'),
    (re.compile(r'AIza[0-9A-Za-z_\-]{20,}'), 'AIza***'),
    (re.compile(r'GOCSPX-[A-Za-z0-9_\-]{20,}'), 'GOCSPX-***'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S), '***私钥已隐藏***'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'), '***私钥已隐藏***'),
    (re.compile(r'((?:password|passwd|secret|api[_-]?key|token)[\w-]*\s*[=:：]\s*)[!-~]{8,}', re.I), r'\1***'),
    (re.compile(r'(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)'),
     '***证件号已隐藏***'),
]


def default_home():
    return Path(os.environ.get('CHAT_ARCHIVE_HOME') or Path.home())


def local_time(stamp):
    try:
        moment = datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        return moment.astimezone().strftime('%Y-%m-%d %H:%M:%S')
    except (AttributeError, ValueError):
        return ''


def millis_time(stamp):
    try:
        return datetime.datetime.fromtimestamp(int(stamp) / 1000).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, OSError, OverflowError):
        return ''


def mask(text):
    for pattern, masked in SECRETS:
        text = pattern.sub(masked, text)
    return text


def add_turn(turns, role, when, text):
    """助手在同一轮里的多段回复合成一段。"""
    if role == 'assistant' and turns and turns[-1][0] == 'assistant':
        turns[-1] = ('assistant', turns[-1][1], turns[-1][2] + '\n\n' + text)
    else:
        turns.append((role, when, text))


# ---------- 图片 ----------

def image_keeper(images_dir, extract):
    """返回 keep(block)：按出现顺序给用户发的图片编号、存进 images_dir，返回写进正文的那一行。

    extract(block) 返回 (图片内容, 扩展名)，记录里没有原图时返回 (None, '')。"""
    counter = [0]

    def keep(block):
        counter[0] += 1
        name = f'img{counter[0]:02d}'
        if images_dir is None:
            return f'（{name}，未存原图）'
        if (images_dir / f'{name}.skip').exists():
            return f'（{name}：已标记为敏感，不保存）'
        try:
            data, ext = extract(block)
        except (ValueError, OSError):
            data, ext = None, ''
        if not data:
            return f'（{name}，记录里没有原图）'
        ext = re.sub(r'[^a-z0-9]', '', ext.lower().replace('jpeg', 'jpg'))[:5] or 'png'
        target = images_dir / f'{name}.{ext}'
        if not target.exists() or target.read_bytes() != data:
            images_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return f'![{name}](<{ATTACHMENTS}/{images_dir.name}/{target.name}>)'

    return keep


def claude_image(block):
    source = block.get('source') or {}
    if not source.get('data'):
        return None, ''
    return base64.b64decode(source['data']), (source.get('media_type') or 'image/png').split('/')[-1]


def codex_image(block):
    found = DATA_URL.match(block.get('image_url') or '')
    if not found:
        return None, ''
    return base64.b64decode(found.group(2)), found.group(1)


def workbuddy_image(blobs):
    def extract(block):
        blob_id = block.get('blob_id') or ''
        ext = (block.get('mime') or 'image/png').split('/')[-1]
        if blobs is None or not re.fullmatch(r'[0-9a-f]{64}', blob_id):
            return None, ''
        folder = blobs / blob_id[:2]
        for path in (folder / f"{blob_id}.{ext.replace('jpeg', 'jpg')}", folder / f'{blob_id}.{ext}', folder / blob_id):
            if path.is_file():
                return path.read_bytes(), path.suffix[1:] or ext
        return None, ''
    return extract


# ---------- 各平台的会话解析：都返回 [(角色, 本地时间, 文本)] ----------

def read_lines(source):
    # utf-8-sig：有的编辑器和 Windows PowerShell 会在文件头写 BOM，否则第一行解析不了
    for line in source.read_text(encoding='utf-8-sig', errors='replace').splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            yield item


def claude_user_text(content, keep):
    """用户实际输入的文字：去掉系统附带的提示，图片逐张交给 keep 处理。"""
    blocks = content if isinstance(content, list) else [{'type': 'text', 'text': content or ''}]
    blocks = [block for block in blocks if isinstance(block, dict)]
    text = '\n'.join(block.get('text') or '' for block in blocks if block.get('type') == 'text')
    text, mentions = IMAGE_MENTION.subn('', REMINDER.sub('', text))
    text = text.strip()
    if text.startswith(NOT_USER):
        return ''
    notes = [keep(block) for block in blocks if block.get('type') == 'image']
    if not notes and mentions:
        notes = [f'（附图 {mentions} 张，记录里没有原图）']
    return '\n\n'.join([text] + notes).strip()


def claude_turns(source, images_dir=None):
    turns, keep = [], image_keeper(images_dir, claude_image)
    for item in read_lines(source):
        if item.get('isSidechain'):   # 子代理的支线
            continue
        kind, stamp, role, text = item.get('type'), item.get('timestamp'), None, ''
        attachment = item.get('attachment') or {}
        if kind == 'user' and item.get('isCompactSummary'):
            role, text = 'note', '（此处会话上下文被压缩后续接，之前的原文都在上面。）'
        elif kind == 'user' and not item.get('isMeta'):
            role, text = 'user', claude_user_text((item.get('message') or {}).get('content'), keep)
        elif kind == 'attachment' and attachment.get('type') == 'queued_command':
            role, text = 'user', claude_user_text(attachment.get('prompt'), keep)
            stamp = attachment.get('timestamp') or stamp
            if text:
                text = '（助手还在处理上一条时发来）\n\n' + text
        elif kind == 'assistant':
            blocks = (item.get('message') or {}).get('content') or []
            role = 'assistant'
            text = '\n\n'.join(block['text'].strip() for block in blocks
                               if isinstance(block, dict) and block.get('type') == 'text'
                               and (block.get('text') or '').strip())
        if text:
            add_turn(turns, role, local_time(stamp), mask(text))
    return turns


def codex_turns(source, images_dir=None):
    """审批、子代理这类内部会话返回空列表。"""
    turns, keep = [], image_keeper(images_dir, codex_image)
    for item in read_lines(source):
        payload = item.get('payload') if isinstance(item.get('payload'), dict) else {}
        if item.get('type') == 'session_meta' and isinstance(payload.get('source'), dict):
            return []
        if (item.get('type') != 'response_item' or payload.get('type') != 'message'
                or payload.get('role') not in ('user', 'assistant')):
            continue
        blocks = [block for block in payload.get('content') or [] if isinstance(block, dict)]
        parts = [block.get('text') or '' for block in blocks
                 if block.get('type') in ('input_text', 'output_text', 'text')]
        role = payload['role']
        if role == 'user':
            parts = [INJECTED.sub('', part) for part in parts]
            parts = [part for part in parts if part.strip()]
            parts += [keep(block) for block in blocks if block.get('type') == 'input_image']
        text = '\n\n'.join(part.strip() for part in parts if part.strip())
        if role == 'user' and text.startswith(SUMMARY):
            role, text = 'note', '（此处是上下文压缩后的摘要，不是用户原话，未收；原文见全量备份。）'
        if text:
            add_turn(turns, role, local_time(item.get('timestamp')), mask(text))
    return turns


def workbuddy_turns(source, images_dir=None, blobs=None):
    """用户的话包在 <user_query> 里，前面是客户端塞的提示。"""
    turns, keep = [], image_keeper(images_dir, workbuddy_image(blobs))
    for item in read_lines(source):
        if item.get('type') != 'message' or item.get('role') not in ('user', 'assistant'):
            continue
        content = [block for block in item.get('content') or [] if isinstance(block, dict)]
        text = '\n\n'.join(block.get('text') or '' for block in content
                           if block.get('type') in ('input_text', 'output_text', 'text'))
        if item['role'] == 'user':
            asked = WB_QUERY.findall(text)
            text = '\n\n'.join(part.strip() for part in asked) if asked else REMINDER.sub('', text)
            notes = [keep(block) for block in content if block.get('type') == 'image_blob_ref']
            text = '\n\n'.join([text.strip()] + notes).strip()
        elif text.strip() == 'aborted':   # 被打断的那一轮，客户端只记了这一个词
            continue
        stamp = item.get('timestamp')
        when = local_time(stamp) if isinstance(stamp, str) else millis_time(stamp)
        if text.strip():
            add_turn(turns, item['role'], when, mask(text.strip()))
    return turns


# ---------- 找会话、定位置 ----------

def sessions(home, sources=SOURCES, subagents=False):
    """本机的 (平台, 原始记录文件, 会话号)。subagents 时连 Claude 子代理的记录一起给。"""
    wanted = {str(name).lower() for name in sources or ()}
    if 'claude' in wanted:
        for path in sorted((home / '.claude/projects').glob('**/*.jsonl' if subagents else '*/*.jsonl')):
            yield 'Claude', path, path.stem
    if 'codex' in wanted:
        seen = set()
        # 在 Codex 里点过“归档”的会话挪到 archived_sessions，两边可能各有一份
        for folder in ('sessions', 'archived_sessions'):
            for path in sorted((home / '.codex' / folder).rglob('*.jsonl')):
                session = path.stem[-36:]
                if session not in seen:
                    seen.add(session)
                    yield 'Codex', path, session
    if 'workbuddy' in wanted:
        for path in sorted((home / '.workbuddy/projects').glob('*/*.jsonl')):
            yield 'WorkBuddy', path, path.stem


def peek(source):
    """不读完整个文件，取会话的开始时间和工作目录。"""
    start = cwd = ''
    with open(source, encoding='utf-8-sig', errors='replace') as stream:
        for number, line in enumerate(stream):
            if not start:
                found = re.search(r'"timestamp":\s*(?:"([^"]+)"|(\d{12,13}))', line)
                if found:
                    start = local_time(found.group(1)) if found.group(1) else millis_time(found.group(2))
            if not cwd:
                found = re.search(r'"cwd":\s*"((?:[^"\\]|\\.)*)"', line)
                if found:
                    try:
                        cwd = json.loads(f'"{found.group(1)}"')
                    except ValueError:
                        cwd = ''
            if (start and cwd) or number > 200:
                break
    return start, cwd


def in_workspace(root, cwd):
    """会话的工作目录是不是本工作区或它的子目录。"""
    if not cwd or not Path(cwd).is_absolute():
        return False
    try:
        here = Path(cwd).resolve()
    except (OSError, RuntimeError):
        return False
    return here == root or root in here.parents


def project_name(cwd):
    name = re.split(r'[\\/]', cwd.rstrip('\\/'))[-1] if cwd else ''
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip(' .') or 'unknown-project'


def short_id(agent, session):
    return session[:13] if agent == 'Codex' else session[:8]


def write_atomic(target, text, mtime_ns=None):
    """先写临时文件再替换，中途出错不会留下半份；mtime_ns 用来把修改时间对齐到原始记录。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.part')
    temp.write_text(text, encoding='utf-8', newline='\n')
    os.replace(temp, target)
    if mtime_ns is not None:
        os.utime(target, ns=(mtime_ns, mtime_ns))


def render(agent, session, source, cwd, turns, device, short):
    label = LABELS[agent]
    times = [when for _, when, _ in turns if when] or ['未知']
    count = Counter(role for role, _, _ in turns)
    lines = ['# 会话全文（逐字导出）', '',
             f'- 会话 ID：{session}',
             f'- 设备：{device}',
             f'- 平台：{label}',
             f"- 项目目录：{cwd or '未知'}",
             f'- 来源：本机原始会话记录 {source.name}',
             f'- 时间范围：{times[0]} 至 {times[-1]}（本机时间）',
             f"- 条数：用户 {count['user']}，助手 {count['assistant']}",
             '- 完整性：全部用户消息和助手可见回复，按原始顺序逐字导出；不含工具调用与输出、隐藏推理、系统提示；'
             f'明显的密钥已打码；用户发的图片存在 `{ATTACHMENTS}/{short}/`。',
             '', '---', '']
    for role, when, text in turns:
        if role == 'note':
            lines += [text, '']
        else:
            who = '用户' if role == 'user' else f'助手（{label}）'
            lines += [f'## {who} `{when}`', '', text, '']
    return '\n'.join(lines)


# ---------- 子命令 ----------

def export_sessions(root, home=None, settings=None, everything=False, force=False):
    """导出会话；返回 (计数, 各项目导出份数, 提醒)。everything 为 False 时只导本工作区的。"""
    root = Path(root).resolve()
    settings = settings or agent_config.load(root)
    home = Path(home) if home else default_home()
    archive = settings.get('archive') or {}
    chat = agent_config.path(root, 'chat_exports')
    local_dir = agent_config.path(root, 'local') / LOCAL_EXPORTS
    private = {str(name).casefold() for name in archive.get('local_only_projects') or []}
    device = agent_config.device_name(root)
    stats, projects = Counter(), Counter()
    for agent, source, session in sessions(home, archive.get('sources', SOURCES)):
        start, cwd = peek(source)
        if in_workspace(root, cwd):
            project, folder = '本工作区', chat
        elif not everything:
            continue
        else:
            project = project_name(cwd)
            folder = (local_dir if project.casefold() in private else chat / OTHERS) / project
        short = short_id(agent, session)
        made = sorted(folder.glob(f'*_{agent}_{short}.md'))
        target = made[0] if made else folder / f"{start[:10] or 'undated'}_{device}_{agent}_{short}.md"
        stamp = source.stat().st_mtime_ns   # 读之前取，读的过程中原始记录又追加了，下次还会重导
        if not force and target.exists() and target.stat().st_mtime_ns >= stamp:
            stats['已是最新'] += 1
            continue
        images = folder / ATTACHMENTS / short
        if agent == 'WorkBuddy':
            turns = workbuddy_turns(source, images, home / '.workbuddy' / 'blobs')
        elif agent == 'Codex':
            turns = codex_turns(source, images)
        else:
            turns = claude_turns(source, images)
        if not {'user', 'assistant'} <= {role for role, _, _ in turns}:
            stats['不是对话（内部会话或只有一方发言）'] += 1
            continue
        write_atomic(target, render(agent, session, source, cwd, turns, device, short), stamp)
        stats[f'{LABELS[agent]} 导出'] += 1
        projects[project] += 1
    warnings = []
    others = chat / OTHERS
    if private and others.is_dir():   # 先导出过、后来才设成只留本机的项目，旧文件还在会同步的目录里
        for folder in sorted(others.iterdir()):
            if folder.is_dir() and folder.name.casefold() in private:
                warnings.append(f'{folder.relative_to(root).as_posix()}/ 还在 .personal 里，但这个项目已设为只留本机；'
                                f'请手动移到 .local/{LOCAL_EXPORTS}/ 或删掉。')
    return stats, projects, warnings


def export_full(root, home=None, settings=None, force=False):
    """全量备份：原始记录逐行照抄，大块 base64 换成占位，gzip 后按项目存；没变的跳过。"""
    root = Path(root).resolve()
    settings = settings or agent_config.load(root)
    home = Path(home) if home else default_home()
    archive = settings.get('archive') or {}
    everything = bool(archive.get('all_projects'))
    base = agent_config.path(root, 'local') / FULL_DIR
    stats = Counter()
    for agent, source, session in sessions(home, archive.get('sources', SOURCES), subagents=True):
        start, cwd = peek(source)
        mine = in_workspace(root, cwd)
        if not (mine or everything):
            continue
        folder = base / project_name(str(root) if mine else cwd)
        made = sorted(folder.glob(f'*_{agent}_{session}.jsonl.gz'))
        target = made[0] if made else folder / f"{start[:10] or 'undated'}_{agent}_{session}.jsonl.gz"
        stamp = source.stat().st_mtime_ns
        if not force and target.exists() and target.stat().st_mtime_ns >= stamp:
            stats['已是最新'] += 1
            continue
        folder.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + '.part')
        with open(source, 'rb') as reader, gzip.open(temp, 'wb', compresslevel=5) as writer:
            for line in reader:
                slim = BLOB.sub(lambda found: b'[binary %d chars omitted]' % len(found.group()), line)
                writer.write(slim)
                stats['text'] += len(slim)
        os.replace(temp, target)
        os.utime(target, ns=(stamp, stamp))
        stats['新存'] += 1
        stats['raw'] += source.stat().st_size
        stats['packed'] += target.stat().st_size
    return stats


def opening(text):
    """会话里用户说的第一句话，当标题用。"""
    found = re.search(r'## 用户 `[^`]*`\n\n(.*?)(?=\n## (?:用户|助手)|\Z)', text, re.S)
    words = (found.group(1) if found else '').split('## My request for Codex:')[-1]   # Codex 桌面版先列附带文件
    return ' '.join(re.sub(r'\A（[^）]*）', '', words.strip()).split())[:30]


def single_index_for(path, singles):
    """同目录下文件名去掉 _INDEX 后是这份全文开头的，算它的单次索引；有多个取最长的。"""
    found = [item for item in singles if item.parent == path.parent and item.stem[:-len(SINGLE)]
             and path.stem.startswith(item.stem[:-len(SINGLE)])]
    return max(found, key=lambda item: len(item.stem)) if found else None


def build_index(root, settings=None):
    """按实际存在的文件重写总索引，返回收录份数。"""
    root = Path(root).resolve()
    settings = settings or agent_config.load(root)
    chat = agent_config.path(root, 'chat_exports')
    files = [path for path in (sorted(chat.rglob('*.md')) if chat.is_dir() else [])
             if path.name != INDEX_NAME and ATTACHMENTS not in path.relative_to(chat).parts]
    singles = [path for path in files if path.stem.endswith(SINGLE)]
    pairs = {path: single_index_for(path, singles) for path in files if path not in singles}
    attached = {single for single in pairs.values() if single}
    rows = []
    for path in files:
        if path in attached:   # 已挂在全文那一行，不单独成行
            continue
        rel = path.relative_to(chat)
        single = pairs.get(path)
        text = path.read_text(encoding='utf-8-sig', errors='replace')
        day = re.match(r'\d{4}-\d{2}-\d{2}', path.name)
        if agent_config.is_local_only(path.relative_to(root).as_posix(), settings):
            title = '（只留本机）'   # 不把只留本机的对话内容写进会同步的索引
        elif EXPORTED.match(path.stem):
            title = opening(text) or path.stem
        else:
            title = path.stem[11:] if day else path.stem
        if len(rel.parts) > 2 and rel.parts[0] == OTHERS:
            title = f'【{rel.parts[1]}】{title}'
        found = PLATFORM.search('\n'.join(text.splitlines()[:30]))
        platform = found.group(1).strip()[:20] if found else '未标注'
        rows.append((day.group() if day else '—', title, platform, len(re.sub(r'\s', '', text)),
                     f'[打开](<{rel.as_posix()}>)',
                     f'[打开](<{single.relative_to(chat).as_posix()}>)' if single else '无'))
    rows.sort(key=lambda row: (row[0] != '—', row[0], row[1]), reverse=True)   # 没有日期的排最后
    lines = ['# 对话总索引', '',
             '脚本生成，不要手改；重新生成：`python tools/chat_archive.py index`。',
             f'共 {len(rows)} 份，只列生成时本机实际存在的文件。先看本表，有单次索引的先读单次索引，再按需打开全文。', '',
             '| 日期 | 主题 | 平台 | 字数 | 全文 | 单次索引 |', '|---|---|---|---|---|---|']
    lines += ['| ' + ' | '.join(str(cell).replace('|', '／').replace('\n', ' ') for cell in row) + ' |'
              for row in rows]
    write_atomic(chat / INDEX_NAME, '\n'.join(lines) + '\n')
    return len(rows)


def main(argv=None):
    if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='对话归档')
    parser.add_argument('--root', type=Path, default=ROOT, help='工作区根目录')
    parser.add_argument('--home', type=Path, help='从哪个用户目录读会话（默认 CHAT_ARCHIVE_HOME 或当前用户目录）')
    sub = parser.add_subparsers(dest='command', required=True)
    for name, text in (('export', '只导出本工作区的会话'), ('export-all', '导出本机所有项目的会话'),
                       ('export-full', '原始记录全量备份，只留本机'), ('index', '重写对话总索引')):
        command = sub.add_parser(name, help=text)
        if name != 'index':
            command.add_argument('--force', action='store_true', help='已是最新的也重做')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    home = args.home or default_home()
    if args.command == 'index':
        print(f'[总索引] 已重写，共 {build_index(root)} 份。')
    elif args.command == 'export-full':
        stats = export_full(root, home, force=args.force)
        print(f"[全量备份] 新存 {stats['新存']} 个，已是最新 {stats['已是最新']} 个；本次原始 {stats['raw'] / 1e6:,.1f} MB，"
              f"去掉二进制块后 {stats['text'] / 1e6:,.1f} MB，压缩后 {stats['packed'] / 1e6:,.1f} MB，"
              f'存在 .local/{FULL_DIR}/')
    else:
        stats, projects, warnings = export_sessions(root, home, everything=args.command == 'export-all',
                                                    force=args.force)
        print('[会话导出] ' + ('；'.join(f'{key} {value}' for key, value in stats.items()) or '没有找到会话'))
        for project, number in projects.most_common():
            print(f'  {project}: {number} 份')
        for warning in warnings:
            print('[注意] ' + warning)
    return 0


if __name__ == '__main__':
    sys.exit(main())
