#!/usr/bin/env python3
"""音视频转写清单：只读扫描工作区里的音视频，生成一份文字清单，让任何设备、任何 AI 都知道
有哪些原件、在哪、转没转、有多大。

  python tools/media_inventory.py                       扫描 .personal/
  python tools/media_inventory.py D:/录音 E:/会议录像    再加扫工作区外的目录（可写多个）

输出 .personal/logs/media-inventory.md，每个文件一行：状态、路径、大小、修改日期、文字稿、备注。
  已转写：同目录下有文件名以该音视频文件名开头的 .md/.txt/.srt/.vtt/.docx/.doc/.pdf
          （例：周会.mp3 ↔ 周会.txt / 周会_纪要.md；transcribe.py 写出的 .txt 自然算）
  不转写：备注里写了“不转写”或“不需要”
  待转写：其余
备注列手填，重跑时按路径保留。设置 local_only 里的路径（默认 .personal/private/ 等）不扫，
免得只留本机的文件名进到会同步的清单里。

只读：不移动、删除、修改任何音视频，只写清单这一个文件。只用标准库。
"""
import argparse
import datetime
import os
import sys
from pathlib import Path

import agent_config

ROOT = Path(__file__).resolve().parent.parent
OUT = 'logs/media-inventory.md'   # 在 .personal/ 下
MEDIA_EXT = {
    '.mp3', '.m4a', '.wav', '.amr', '.aac', '.flac', '.ogg', '.wma', '.opus',
    '.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm', '.m4v', '.3gp', '.mpg', '.mpeg',
}
TRANSCRIPT_EXT = {'.md', '.txt', '.srt', '.vtt', '.docx', '.doc', '.pdf'}
SKIP_DIRS = {'.git', '__pycache__', 'node_modules'}
NO_NEED = ('不转写', '不需要')
ORDER = ('待转写', '不转写', '已转写')
HEADER = '| 状态 | 文件 | 大小 | 修改日期 | 文字稿 | 备注 |'
MB = 1024 * 1024


def human(size):
    return f'{size / (1024 * MB):.2f} GB' if size >= 1024 * MB else f'{size / MB:.1f} MB'


def cell(value):
    return str(value).replace('|', '／').replace('\n', ' ').strip()


def scan(base, label, skip=lambda rel: False):
    """返回 base 下全部音视频的行；skip(相对路径) 为真的目录和文件不看。"""
    rows = []
    for dirpath, dirnames, filenames in os.walk(base):
        folder = Path(dirpath)
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS
                             and not skip((folder / name).relative_to(base).as_posix()))
        texts = sorted(name for name in filenames if Path(name).suffix.lower() in TRANSCRIPT_EXT)
        for name in filenames:
            path = folder / name
            rel = path.relative_to(base).as_posix()
            if path.suffix.lower() not in MEDIA_EXT or skip(rel):
                continue
            stem = path.stem.lower()
            info = path.stat()
            rows.append({
                'path': label + rel,
                'size': info.st_size,
                'mtime': datetime.date.fromtimestamp(info.st_mtime).isoformat(),
                'transcript': '、'.join(n for n in texts if Path(n).stem.lower().startswith(stem)),
            })
    return rows


def load_notes(out):
    """读旧清单里手填的备注，按路径保留。"""
    notes, in_table = {}, False
    if not out.is_file():
        return notes
    for line in out.read_text(encoding='utf-8').splitlines():
        if line.strip() == HEADER:
            in_table = True
        elif in_table and line.startswith('|') and not line.startswith('|---'):
            cells = [part.strip() for part in line.strip().strip('|').split('|')]
            if len(cells) == 6 and cells[5]:
                notes[cells[1].strip('`')] = cells[5]
    return notes


def build(root, extra_dirs=()):
    """扫描并写清单；返回 (行列表, 清单路径, 提示)。"""
    root = Path(root).resolve()
    settings = agent_config.load(root)
    personal = agent_config.path(root, 'personal')
    prefix = agent_config.PATHS['personal'] + '/'
    out = personal / OUT
    rows, notices = [], []
    if personal.is_dir():
        rows += scan(personal, prefix, lambda rel: agent_config.is_local_only(prefix + rel, settings))
    for extra in extra_dirs:
        base = Path(extra)
        if not base.is_dir():
            notices.append(f'[跳过] 不是目录：{extra}')
            continue
        rows += scan(base, f'[外部:{base.resolve().name}]/')

    notes = load_notes(out)
    for row in rows:
        row['note'] = notes.get(row['path'], '')
        if row['transcript']:
            row['status'] = '已转写'
        elif any(word in row['note'] for word in NO_NEED):
            row['status'] = '不转写'
        else:
            row['status'] = '待转写'
    rows.sort(key=lambda row: (ORDER.index(row['status']), row['path']))

    lines = ['# 音视频转写清单', '',
             f'> 生成：{datetime.datetime.now():%Y-%m-%d %H:%M}，由 `python tools/media_inventory.py` 生成，'
             '重跑会刷新（备注列保留）。',
             '> 转写稿放在原件同目录、文件名以原件名开头，就会识别为已转写；不需要转写的在备注列写“不转写”。',
             '> 转写用 `python tools/transcribe.py <文件或目录>`（本地运行，不花 API 费）。',
             '', '## 汇总', '', '| 状态 | 数量 | 大小 |', '|---|---|---|']
    for status in ORDER:
        picked = [row for row in rows if row['status'] == status]
        lines.append(f'| {status} | {len(picked)} | {human(sum(row["size"] for row in picked))} |')
    lines += [f'| 合计 | {len(rows)} | {human(sum(row["size"] for row in rows))} |',
              '', '## 明细', '', HEADER, '|---|---|---|---|---|---|']
    for row in rows:
        lines.append(f"| {row['status']} | `{cell(row['path'])}` | {human(row['size'])} | {row['mtime']} | "
                     f"{cell(row['transcript'])} | {cell(row['note'])} |")
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + '.part')
    temp.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
    os.replace(temp, out)
    return rows, out, notices


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='生成音视频转写清单（只读扫描）')
    parser.add_argument('--root', type=Path, default=ROOT, help='工作区根目录')
    parser.add_argument('extra', nargs='*', help='另外要扫的工作区外目录')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    rows, out, notices = build(root, args.extra)
    for notice in notices:
        print(notice)
    print(f'音视频共 {len(rows)} 个，{human(sum(row["size"] for row in rows))}')
    for status in ORDER:
        picked = [row for row in rows if row['status'] == status]
        print(f'  {status}：{len(picked)} 个，{human(sum(row["size"] for row in picked))}')
    print(f'清单已写入：{out.relative_to(root).as_posix()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
