#!/usr/bin/env python3
"""变更留痕：对比工作区和上次快照，把新增、修改、删除追加到流水文件。不改工作区里的任何文件。

不管是 AI、人工还是同步软件动了文件，都留下“什么时候、哪个文件、怎么变”的记录，事后能和工作日志对上。
只有内容真的变了才记；只是修改时间变了的不记。

  python tools/trace_changes.py scan [--trigger manual|scheduled] [--actor 名字] [--root R]
  python tools/trace_changes.py report [--date YYYY-MM-DD] [--root R]

快照：.local/trace_snapshot.json（只在本机）
流水：.personal/logs/trace/<设备名>/changes_YYYY-MM.jsonl（只追加）
local_only 下的文件和疑似密钥文件只记到目录层，不记文件名、大小和哈希（短密钥的哈希可以被暴力反推）。
流水只说明“什么时候哪个文件变了”；“谁改的”只是根据当时活跃的 AI 会话推断，原因仍要看工作日志。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import agent_config

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {'.git', '.local', '.device', '__pycache__', 'node_modules'}
SNAPSHOT = '.local/trace_snapshot.json'
LOCK = '.local/trace.lock'
LOCK_STALE_SECONDS = 300        # 锁文件超过这么久视为上次扫描异常退出留下的
HASH_LIMIT = 20 * 1024 * 1024   # 超过这个大小只记大小和时间，不算哈希
DETAIL_LIMIT = 300              # 单次扫描逐条记录的上限，超出部分只记汇总
SECRET_SUFFIXES = ('.pem', '.key', '.p12', '.pfx', '.kdbx')
TYPE_LABELS = {'baseline': '基线', 'added': '新增', 'modified': '修改', 'deleted': '删除', 'bulk': '批量'}
TRIGGER_LABELS = {'scheduled': '定时', 'manual': '手动'}


def redacted(rel, settings):
    """敏感路径返回只到目录层的写法（如 '.personal/private/*'）；普通路径返回 None。"""
    parts = rel.split('/')
    if agent_config.is_local_only(rel, settings):
        for i in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:i])
            if agent_config.is_local_only(prefix, settings):
                return prefix + '/*'
    for i, part in enumerate(parts[:-1]):
        lower = part.casefold()
        if 'secret' in lower or 'password' in lower:
            return '/'.join(parts[:i + 1]) + '/*'
    name = parts[-1].casefold()
    if (name.startswith('.env') or 'secret' in name or 'password' in name
            or name.endswith(SECRET_SUFFIXES)):
        return '/'.join(parts[:-1] + ['*'])
    return None


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def walk(root):
    """返回 {相对路径: [大小, 修改时间纳秒, 哈希或 None]}，哈希留到需要时再算。"""
    trace_dir = agent_config.PATHS['trace']
    files = {}
    for base, dirs, names in os.walk(root):
        rel_base = Path(base).relative_to(root).as_posix()
        prefix = '' if rel_base == '.' else rel_base + '/'
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and prefix + d != trace_dir]
        for name in names:
            if name.startswith('~$') or name.endswith('.tmp'):
                continue   # Office 锁文件和临时文件
            try:
                st = os.stat(os.path.join(base, name))
            except OSError:
                continue
            files[prefix + name] = [st.st_size, st.st_mtime_ns, None]
    return files


def fingerprint(root, rel, size, hidden):
    """内容哈希；敏感路径、超大文件、读不了的文件返回 None。"""
    if hidden or size > HASH_LIMIT:
        return None
    try:
        return sha256(root / rel)
    except OSError:
        return None


def local_time(mtime_ns):
    try:
        moment = datetime.datetime.fromtimestamp(mtime_ns / 1e9, datetime.timezone.utc)
        return moment.astimezone().isoformat(timespec='seconds')
    except (OSError, OverflowError, ValueError):
        return None   # 个别文件的修改时间不合法


def short(digest):
    return digest and digest[:16]


def diff(root, old, new, settings):
    events, hidden_counts = [], Counter()
    for rel in sorted(new):
        entry = new[rel]
        size, mtime, _ = entry
        before = old.get(rel)
        hidden = redacted(rel, settings)
        if before and before[0] == size and before[1] == mtime:
            entry[2] = before[2] or fingerprint(root, rel, size, hidden)   # 旧快照没算过的补算
            continue
        entry[2] = fingerprint(root, rel, size, hidden)
        if before and entry[2] and entry[2] == before[2]:
            continue   # 只是修改时间变了，内容没变
        kind = 'modified' if before else 'added'
        if hidden:
            hidden_counts[(kind, hidden)] += 1
            continue
        event = {'type': kind, 'path': rel, 'size': size, 'sha256': short(entry[2]),
                 'mtime': local_time(mtime)}
        if before:
            event['prev_sha256'] = short(before[2])
        events.append(event)
    for rel in sorted(old.keys() - new.keys()):
        hidden = redacted(rel, settings)
        if hidden:
            hidden_counts[('deleted', hidden)] += 1
        else:
            events.append({'type': 'deleted', 'path': rel, 'size': old[rel][0],
                           'prev_sha256': short(old[rel][2])})
    for (kind, path), count in sorted(hidden_counts.items()):
        events.append({'type': kind, 'path': path, 'count': count})
    return events


def active_sessions(root, since):
    """上次扫描以来有写入的 AI 会话记录。只说明当时谁在活动，不等于就是它改的。"""
    home = Path.home()
    sources = (('Claude', home / '.claude/projects' / re.sub(r'[^A-Za-z0-9]', '-', str(root)), '*.jsonl'),
               ('Codex', home / '.codex/sessions', '**/*.jsonl'))
    found = set()
    for label, folder, pattern in sources:
        if not folder.is_dir():
            continue
        for item in folder.glob(pattern):
            try:
                if item.stat().st_mtime > since:
                    found.add(f'{label}:{item.stem[-36:][:8]}')
            except OSError:
                pass
    return sorted(found)


def load_snapshot(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data.get('files'), dict) else None
    except (OSError, ValueError, AttributeError):
        return None   # 没有或损坏：重新建基线


def append_lines(ledger, lines):
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, 'a', encoding='utf-8', errors='backslashreplace', newline='\n') as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + '\n')


def scan(root, trigger='manual', actor=None):
    root = Path(root)
    settings = agent_config.load(root)
    lock = root / LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - lock.stat().st_mtime < LOCK_STALE_SECONDS:
            print('[留痕] 另一次扫描正在进行，本次跳过。')
            return 0
    except OSError:
        pass   # 没有锁
    lock.write_text(str(os.getpid()), encoding='utf-8')
    try:
        snapshot_path = root / SNAPSHOT
        old = load_snapshot(snapshot_path)
        files = walk(root)
        stamp = datetime.datetime.now().astimezone()
        device = agent_config.device_name(root)
        base = {'ts': stamp.isoformat(timespec='seconds'), 'device': device, 'trigger': trigger}
        if actor:
            base['actor'] = actor
        lines = []
        if old is None:
            for rel, entry in files.items():   # 基线就算好哈希，之后第一次改动才有“改前哈希”
                entry[2] = fingerprint(root, rel, entry[0], redacted(rel, settings))
            lines.append({**base, 'type': 'baseline', 'files': len(files)})
        else:
            events = diff(root, old['files'], files, settings)
            if events:
                shared = dict(base, since=old.get('taken_at'))
                try:
                    since = datetime.datetime.fromisoformat(old['taken_at']).timestamp()
                    shared['active_sessions'] = active_sessions(root, since)
                except (KeyError, TypeError, ValueError):
                    pass
                lines.extend({**shared, **event} for event in events[:DETAIL_LIMIT])
                if len(events) > DETAIL_LIMIT:
                    lines.append({**shared, 'type': 'bulk',
                                  'total': dict(Counter(event['type'] for event in events)),
                                  'note': f'本次共 {len(events)} 处变化，只逐条记录了前 {DETAIL_LIMIT} 处'})
        if lines:
            ledger = agent_config.path(root, 'trace') / device / f'changes_{stamp:%Y-%m}.jsonl'
            append_lines(ledger, lines)
        temp = snapshot_path.with_suffix('.tmp')
        temp.write_text(json.dumps({'version': 1, 'taken_at': base['ts'], 'files': files}), encoding='utf-8')
        os.replace(temp, snapshot_path)
        print(f"[留痕] {base['ts']} 记录 {len(lines)} 条")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
    return 0


def read_rows(root, date):
    rows = []
    for ledger in sorted(agent_config.path(root, 'trace').glob(f'*/changes_{date[:7]}.jsonl')):
        for line in ledger.read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get('ts', '')).startswith(date):
                rows.append(row)
    rows.sort(key=lambda row: row['ts'])
    return rows


def report(root, date):
    root = Path(root)
    rows = read_rows(root, date)
    if not rows:
        print(f'{date} 没有留痕记录。')
        return 0
    print(f'{date} 的变更留痕（{len(rows)} 条）')
    last = None
    for row in rows:
        key = (row['ts'], row.get('device'))
        if key != last:
            who = row.get('actor') or '、'.join(row.get('active_sessions') or []) or '人工或未知'
            trigger = TRIGGER_LABELS.get(row.get('trigger'), row.get('trigger'))
            print(f"\n{row['ts'][11:19]}  {row.get('device')}  {trigger}  当时活跃：{who}")
            last = key
        detail = row.get('path') or row.get('note') or f"{row.get('files')} 个文件"
        if row.get('count'):
            detail += f"（{row['count']} 个文件）"
        print(f"   {TYPE_LABELS.get(row['type'], row['type'])}  {detail}")

    work_prefix = agent_config.PATHS['work_logs'] + '/'
    logs_prefix = Path(agent_config.PATHS['work_logs']).parent.as_posix() + '/'   # 归档目录，自动导出的不算“改动”
    changed = [row for row in rows if row['type'] in ('added', 'modified', 'deleted', 'bulk')]
    work_logs = {row['path'] for row in changed
                 if row['type'] != 'deleted' and row.get('path', '').startswith(work_prefix)}
    folder = agent_config.path(root, 'work_logs')
    if folder.is_dir():
        work_logs |= {work_prefix + item.relative_to(folder).as_posix()
                      for item in folder.rglob(f'*{date}*') if item.is_file()}
    print(f'\n当天的工作日志（{len(work_logs)} 个）：')
    for item in sorted(work_logs):
        print('   ' + item)
    real_changes = [row for row in changed if not row.get('path', '').startswith(logs_prefix)]
    if real_changes and not work_logs:
        print(f'⚠ 当天有 {len(real_changes)} 处文件改动（不含 {logs_prefix} 下的归档），'
              f'但没有任何工作日志（{work_prefix}）。')
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--root', type=Path, default=argparse.SUPPRESS, help='工作区根目录（默认是本脚本的上一级）')
    parser = argparse.ArgumentParser(description='变更留痕：扫描工作区的文件变化并记入流水。', parents=[common])
    sub = parser.add_subparsers(dest='command', required=True)
    scan_parser = sub.add_parser('scan', parents=[common], help='对比上次快照，记录变化')
    scan_parser.add_argument('--trigger', choices=sorted(TRIGGER_LABELS), default='manual')
    scan_parser.add_argument('--actor', help='是谁触发的这次扫描（例如某个 AI 会话名）')
    report_parser = sub.add_parser('report', parents=[common], help='列出某天的变化')
    report_parser.add_argument('--date', default=datetime.date.today().isoformat())
    args = parser.parse_args(argv)
    root = Path(getattr(args, 'root', ROOT)).resolve()
    if args.command == 'scan':
        return scan(root, args.trigger, args.actor)
    try:
        datetime.date.fromisoformat(args.date)
    except ValueError:
        parser.error(f'日期格式不对：{args.date}（要 YYYY-MM-DD）')
    return report(root, args.date)


if __name__ == '__main__':
    sys.exit(main())
