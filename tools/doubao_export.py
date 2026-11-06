#!/usr/bin/env python3
"""豆包 / 豆包Work 桌面客户端的会话逐字导出：把客户端留在本机的运行轨迹 trajectory.jsonl 转成 Markdown。

  python tools/doubao_export.py [--root R] [--home 用户目录] [--force] [--no-images]

在设置 archive.sources 里加上 doubao 后，tools/refresh.py 每次会顺带跑它；默认不跑。

会话在哪（两个客户端都导）：
  豆包Work  <本机应用数据>/DoubaoWork/User Data/Default/.doubaowork/agent_mode/workspace/.sessions
  豆包      <本机应用数据>/Doubao/User Data/Default/.doubao/agent_mode/workspace/.sessions
每个会话是 <会话号>/agents/<agent_id>/system/trajectory.jsonl（UTF-8 明文，一行一条消息），同目录的
assignment.md 记着每条用户消息的时间。<本机应用数据> 在 Windows 上是 %LOCALAPPDATA%；给了 --home 或
环境变量 CHAT_ARCHIVE_HOME 时改用 <该目录>/AppData/Local，任何系统都这样找（测试靠它指到假目录）。
其他系统两样都没给时，提示一句，什么都不做。

输出和 chat_archive.py 一致：每个会话一个 .personal/logs/chat-exports/YYYY-MM-DD_<设备名>_<DoubaoWork|Doubao>_<会话号>.md，
日期取第一条用户消息的时间，没有就取轨迹的修改日期。会话号用完整的：豆包的会话号开头是时间，前 8 位经常重复。
已导出且不比轨迹旧的跳过，轨迹有更新就整份重写，可以反复运行；--force 已是最新的也重写。
内容是全部用户消息和轨迹里的助手可见回复，按原始顺序；不含工具调用与输出、系统提示；明显的密钥和证件号打码。

轨迹不是完整对话：客户端只把一部分助手回复写进轨迹。后面找不到助手回复的用户消息，在正文标【原文不可取得】，
文件头写明缺几条；完整对话只能在客户端里看，这里补不出来。没有用户消息的轨迹不导。
一个会话有多条轨迹时只导主代理（assignment.md 里写着 Role: main 的，都没写就取最大的），子代理属于内部过程。
重写会丢掉旧文件里有、新轨迹里已经没有的内容时，先把旧文件备份到 .local/backups/doubao-exports/。

图片：轨迹里只有图片链接（image_link_list），按出现顺序下载到全文旁边的 attachments/<会话号>/，叫 img01、img02……
已经下载过的不再联网；下载不到的（链接过期、不是图片、超过 20 MB）在正文注明。不想联网加 --no-images。
哪张含敏感内容：在那里建一个同名的 imgNN.skip，删掉图片，再加 --force 重导，以后都不会再存它。
"""
import argparse
import datetime
import http.client
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import agent_config
import chat_archive

ROOT = Path(__file__).resolve().parent.parent
# (文件名里的 Agent 名, 文档里显示的平台名, 客户端数据目录里的隐藏目录名)
APPS = (('DoubaoWork', '豆包Work', '.doubaowork'), ('Doubao', '豆包', '.doubao'))
BACKUPS = 'backups/doubao-exports'   # 在 .local/ 下
GAP = '> 【原文不可取得】本机轨迹里，这条用户消息之后没有助手的文字回复。'
IMAGES_HEADING = '## 附件图片'
MAX_IMAGE = 20 * 1024 * 1024
TIMEOUT = 15
MAIN_ROLE = re.compile(r'^- Role: main\s*$', re.M)
ASKED = re.compile(r'^## \[([\d\-T:.+Z]+)\]\s*需求\s*\n(.*?)(?=\n## \[|\Z)', re.S | re.M)
TURN = re.compile(r'^## (?:用户 `[^`\n]*`|助手（[^）\n]*）)[ \t]*$', re.M)
# 客户端塞在用户消息开头的系统块。只认列出来的标签，认不出的宁可留着，也不误删用户自己的话。
INJECTED = re.compile(
    r'\A(?:\s*(?:<(system-reminder|task-notification|local-command[\w-]*)\b[^>]*>.*?</\1>'
    r'|<\w+ instructions\b[^>]*>\s*</\w+>))+\s*', re.S)


def client_dirs(home=None, windows=None):
    """本机存在的 [(Agent 名, 平台名, 会话目录)]；不知道去哪找时返回 None。"""
    if home is not None:
        base = Path(home) / 'AppData' / 'Local'
    elif (os.name == 'nt') if windows is None else windows:
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    else:
        return None
    found = []
    for agent, label, hidden in APPS:
        folder = base / agent / 'User Data' / 'Default' / hidden / 'agent_mode' / 'workspace' / '.sessions'
        if folder.is_dir():
            found.append((agent, label, folder))
    return found


def main_trajectory(session_dir):
    """一个会话的主代理轨迹；会话里没有轨迹时返回 None。"""
    found = sorted(session_dir.glob('agents/*/system/trajectory.jsonl'))
    if len(found) <= 1:
        return found[0] if found else None
    for path in found:
        note = path.parent / 'assignment.md'
        if note.is_file() and MAIN_ROLE.search(note.read_text(encoding='utf-8-sig', errors='replace')):
            return path
    return max(found, key=lambda path: path.stat().st_size)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):   # 万一是分块的写法，只取文字块
        return '\n\n'.join(block['text'] for block in content
                           if isinstance(block, dict) and isinstance(block.get('text'), str)
                           and block.get('type') in (None, 'text'))
    return ''


def read_trajectory(source):
    """返回 ([(角色, 时间, 文本)], [图片链接])，按原始顺序；时间先留空。"""
    turns, links = [], []
    for item in chat_archive.read_lines(source):
        listed = item.get('image_link_list')
        if isinstance(listed, list):
            links += [link for link in listed if isinstance(link, str) and link.startswith(('http://', 'https://'))]
        role = item.get('role')
        if role not in ('user', 'assistant'):   # tool 是工具调用的输出，不收
            continue
        text = text_of(item.get('content'))
        if role == 'user':
            text = INJECTED.sub('', text)
        if text.strip():
            chat_archive.add_turn(turns, role, '', chat_archive.mask(text.strip()))
    return turns, links


def local_time(stamp):
    # 客户端写的是纳秒精度；Python 3.10 及以前只认 6 位小数
    stamp = re.sub(r'\.(\d+)', lambda found: '.' + (found.group(1) + '000000')[:6], stamp, count=1)
    return chat_archive.local_time(stamp)


def asked_times(note):
    """assignment.md 里每条用户消息的本机时间，按出现顺序。"""
    if not note.is_file():
        return []
    text = note.read_text(encoding='utf-8-sig', errors='replace')
    return [local_time(stamp) for stamp, body in ASKED.findall(text) if body.strip()]


def unanswered(turns):
    """后面没有助手文字回复的用户消息的下标。"""
    return {index for index, (role, _, _) in enumerate(turns)
            if role == 'user' and (index + 1 == len(turns) or turns[index + 1][0] == 'user')}


def download(url, limit=MAX_IMAGE):
    """返回 (图片内容, 扩展名)；拿不到返回 (None, '')。"""
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            kind = (response.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            if kind and not kind.startswith('image/') and kind != 'application/octet-stream':
                return None, ''   # 链接过期时常返回一个网页，别当图片存
            data = response.read(limit + 1)
    except (OSError, ValueError, http.client.HTTPException):
        return None, ''
    if not data or len(data) > limit:
        return None, ''
    ext = kind.split('/')[-1] if kind.startswith('image/') else Path(urllib.parse.urlsplit(url).path).suffix[1:]
    return data, ext or 'jpg'


def link_image(images_dir):
    """给 chat_archive.image_keeper 用的取图函数：已经下载过的直接读本地，链接过期也不丢。"""
    def extract(block):
        saved = [path for path in sorted(images_dir.glob(block['name'] + '.*')) if path.suffix != '.skip']
        if saved:
            return saved[0].read_bytes(), saved[0].suffix[1:]
        return download(block['url'])
    return extract


def image_lines(links, images_dir):
    if images_dir is None:
        keep = chat_archive.image_keeper(None, None)
    else:
        keep = chat_archive.image_keeper(images_dir, link_image(images_dir))
    return [keep({'name': f'img{number:02d}', 'url': link}) for number, link in enumerate(links, 1)]


def render(label, session, source, turns, device, timing, pictures, short, downloaded):
    count = Counter(role for role, _, _ in turns)
    gaps = unanswered(turns)
    times = [when for role, when, _ in turns if when]
    modified = datetime.datetime.fromtimestamp(source.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
    span = f'{times[0]} 至 {times[-1]}（本机时间）' if times else f'不详（{timing}）；轨迹最后修改于 {modified}'
    if gaps:
        whole = (f"- 完整性：尽可能完整版本。用户消息全部在；助手回复只有本机轨迹里存下的 {count['assistant']} 段，"
                 f'另有 {len(gaps)} 条用户消息之后轨迹里没有助手的文字回复，已在正文标【原文不可取得】，'
                 '完整对话只能在客户端里看。')
    else:
        whole = '- 完整性：全部用户消息和本机轨迹里的助手可见回复，按原始顺序逐字导出。'
    whole += '不含工具调用与输出、系统提示；明显的密钥已打码；'
    if not pictures:
        whole += '轨迹里没有图片链接。'
    elif downloaded:
        whole += f'轨迹里的 {len(pictures)} 张图片按链接下载到 `{chat_archive.ATTACHMENTS}/{short}/`，下载不到的已注明。'
    else:
        whole += f'轨迹里有 {len(pictures)} 张图片链接，这次没有下载。'
    lines = ['# 会话全文（逐字导出）', '',
             f'- 会话 ID：{session}',
             f'- 设备：{device}',
             f'- 平台：{label}',
             f'- 来源：本机运行轨迹 agents/{source.parent.parent.name}/system/trajectory.jsonl',
             f'- 时间范围：{span}',
             f"- 条数：用户 {count['user']}，助手 {count['assistant']}",
             f'- 缺助手回复：{len(gaps)} 条',
             whole, '', '---', '']
    for index, (role, when, text) in enumerate(turns):
        heading = f"用户 `{when or '时间不详'}`" if role == 'user' else f'助手（{label}）'
        lines += [f'## {heading}', '', text, '']
        if index in gaps:
            lines += [GAP, '']
    if pictures:
        lines += [IMAGES_HEADING, '']
        for line in pictures:
            lines += [line, '']
    return '\n'.join(lines)


def lost_content(old, new):
    """旧导出里某一轮的正文，在新导出里找不到了。"""
    body = old.split('\n---\n', 1)[-1].split('\n' + IMAGES_HEADING, 1)[0]
    for chunk in TURN.split(body)[1:]:
        chunk = chunk.replace(GAP, '').strip()
        if chunk and chunk not in new:
            return True
    return False


def export_session(root, chat, device, agent, label, session_dir, force, images):
    """导出一个会话，返回 (状态, 备份下来的旧文件或 None)。"""
    source = main_trajectory(session_dir)
    short = re.sub(r'[^0-9A-Za-z-]', '', session_dir.name)
    if source is None or not short:
        return '没有轨迹', None
    note = source.parent / 'assignment.md'
    # 读之前取修改时间：读的过程中轨迹又追加了，下次还会重导
    stamp = max(source.stat().st_mtime_ns, note.stat().st_mtime_ns if note.is_file() else 0)
    made = sorted(chat.glob(f'*_{agent}_{short}.md'))
    if not force and made and made[0].stat().st_mtime_ns >= stamp:
        return '已是最新', None
    turns, links = read_trajectory(source)
    users = [index for index, (role, _, _) in enumerate(turns) if role == 'user']
    if not users:
        return '没有用户消息', None
    times, timing = asked_times(note), '轨迹里没有逐条时间'
    if times and len(times) == len(users):   # 条数对不上就不标，免得把时间安到别的消息上
        for index, when in zip(users, times):
            turns[index] = ('user', when, turns[index][2])
    elif times:
        timing = 'assignment.md 记的条数和用户消息对不上，没有逐条标时间'
    day = turns[users[0]][1][:10] or datetime.date.fromtimestamp(source.stat().st_mtime).isoformat()
    target = made[0] if made else chat / f'{day}_{device}_{agent}_{short}.md'
    pictures = image_lines(links, chat / chat_archive.ATTACHMENTS / short if images else None)
    text = render(label, session_dir.name, source, turns, device, timing, pictures, short, images)
    kept = None
    if target.exists() and lost_content(target.read_text(encoding='utf-8-sig', errors='replace'), text):
        kept = agent_config.path(root, 'local') / BACKUPS / f'{target.stem}.{datetime.datetime.now():%Y%m%d_%H%M%S}.md'
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, kept)
    chat_archive.write_atomic(target, text, stamp)
    return f'{label} 导出', kept


def export(root, home=None, force=False, images=True, windows=None):
    """导出本机全部豆包会话；返回 (计数, 提醒)。"""
    root = Path(root).resolve()
    chat = agent_config.path(root, 'chat_exports')
    device = agent_config.device_name(root)
    stats, notes = Counter(), []
    dirs = client_dirs(home, windows)
    if dirs is None:
        return stats, ['只在 Windows 上自动找豆包的会话目录；其他系统请用 --home 指定用户目录。']
    if not dirs:
        return stats, ['没找到豆包 / 豆包Work 的会话目录，跳过。']
    for agent, label, folder in dirs:
        for session_dir in sorted(path for path in folder.iterdir() if path.is_dir()):
            try:
                status, kept = export_session(root, chat, device, agent, label, session_dir, force, images)
            except OSError as exc:   # 一个会话读写不了，不耽误别的会话
                stats['失败'] += 1
                notes.append(f'{label} 会话 {session_dir.name} 失败：{type(exc).__name__}: {exc}')
                continue
            stats[status] += 1
            if kept:
                notes.append(f'旧导出里有新轨迹已经没有的内容，旧文件另存在 {kept.relative_to(root).as_posix()}')
    return stats, notes


def main(argv=None):
    if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='豆包 / 豆包Work 会话逐字导出')
    parser.add_argument('--root', type=Path, default=ROOT, help='工作区根目录')
    parser.add_argument('--home', type=Path,
                        help='在这个用户目录的 AppData/Local 下找客户端数据'
                             '（默认 CHAT_ARCHIVE_HOME；都没有时 Windows 用 %%LOCALAPPDATA%%）')
    parser.add_argument('--force', action='store_true', help='已是最新的也重写')
    parser.add_argument('--no-images', action='store_true', help='不联网下载图片')
    args = parser.parse_args(argv)
    home = args.home or os.environ.get('CHAT_ARCHIVE_HOME') or None
    stats, notes = export(args.root, home, force=args.force, images=not args.no_images)
    print('[豆包导出] ' + ('；'.join(f'{key} {value}' for key, value in stats.items()) or '没有导出任何会话'))
    for note in notes:
        print('[注意] ' + note)
    return 1 if stats['失败'] else 0


if __name__ == '__main__':
    sys.exit(main())
