#!/usr/bin/env python3
"""音视频本地转写：用 faster-whisper 在本机转成文字和字幕，不上传、不花 API 费。

  python -m pip install -r requirements-media.txt      第一次用先装依赖
  python tools/transcribe.py <音视频文件或目录> [--model small] [--lang zh]

在源文件同目录写 <原名>.txt（纯文字）和 <原名>.srt（带时间戳的字幕）。
传目录时递归处理里面的音视频；已有同名 .txt 的跳过，所以可以反复运行，也不会盖掉你改过的文字稿。
一个文件转到一半出错，什么都不写，下次重跑会再转它。

模型（第一次用某个模型时 faster-whisper 会自动下载，之后离线可用）：
  tiny / base   最快，适合草稿
  small         速度和准确率平衡（默认）
  medium        更准，CPU 上慢
  large-v3      最准，CPU 上很慢
默认自动识别语言；明确知道是哪种语言时用 --lang（如 zh、en）更稳。在 CPU 上用 int8 运行，任何电脑都能跑。
"""
import argparse
import datetime
import os
import sys
from pathlib import Path

from media_inventory import MEDIA_EXT, SKIP_DIRS

INSTALL_HINT = ('没装 faster-whisper。先运行：python -m pip install -r requirements-media.txt'
                '（在工作区根目录；只装一次）')


def fmt_timestamp(seconds):
    """秒数 → SRT 时间戳 00:00:00,000"""
    total = max(0, round(seconds * 1000))
    hours, rest = divmod(total, 3600 * 1000)
    minutes, rest = divmod(rest, 60 * 1000)
    secs, millis = divmod(rest, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}'


def load_model(name):
    """返回 faster-whisper 模型；没装时抛 ImportError。"""
    from faster_whisper import WhisperModel
    return WhisperModel(name, device='cpu', compute_type='int8')


def find_media(target):
    if target.is_file():
        return [target] if target.suffix.lower() in MEDIA_EXT else []
    return sorted(path for path in target.rglob('*')
                  if path.is_file() and path.suffix.lower() in MEDIA_EXT
                  and not SKIP_DIRS.intersection(path.relative_to(target).parts))


def write_text(path, text):
    temp = path.with_name(path.name + '.part')
    temp.write_text(text, encoding='utf-8', newline='\n')
    os.replace(temp, path)


def transcribe_one(path, model, language=None):
    """转一个文件，写 .srt 和 .txt；返回段数。全部转完才写，.txt 最后写（它在就表示转完了）。"""
    segments, info = model.transcribe(str(path), beam_size=5, vad_filter=True, language=language)
    texts, srt = [], []
    for number, segment in enumerate(segments, 1):   # 转写是边迭代边做的，出错会在这里抛出
        text = segment.text.strip()
        texts.append(text)
        srt += [str(number), f'{fmt_timestamp(segment.start)} --> {fmt_timestamp(segment.end)}', text, '']
    print(f'  语言 {info.language}（置信度 {info.language_probability:.2f}），'
          f'时长 {datetime.timedelta(seconds=round(info.duration))}')
    write_text(path.with_suffix('.srt'), '\n'.join(srt))
    write_text(path.with_suffix('.txt'), '\n'.join(texts) + '\n')
    return len(texts)


def main(argv=None, loader=load_model):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='音视频本地转写（faster-whisper）')
    parser.add_argument('input', type=Path, help='音视频文件或目录')
    parser.add_argument('--model', default='small', help='tiny / base / small / medium / large-v3，默认 small')
    parser.add_argument('--lang', help='语言代码，如 zh、en；不填自动识别')
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f'[停止] 找不到：{args.input}')
        return 1
    files = find_media(args.input)
    todo = [path for path in files if not path.with_suffix('.txt').exists()]
    for path in files:
        if path not in todo:
            print(f'[跳过] {path.name}（已有 .txt）')
    if not todo:
        print(f'[完成] 共 {len(files)} 个音视频，没有要转写的')
        return 0
    try:
        model = loader(args.model)
    except ImportError:
        print('[停止] ' + INSTALL_HINT)
        return 1

    print(f'[转写] {len(todo)} 个文件，模型 {args.model}')
    done, failed = 0, []
    for path in todo:
        print(f'\n[处理] {path}')
        try:
            count = transcribe_one(path, model, args.lang)
        except Exception as error:   # 坏文件、解码失败只算这一个失败，接着转下一个
            failed.append(path.name)
            print(f'  [失败] {type(error).__name__}: {error}')
            continue
        done += 1
        print(f'  → {path.with_suffix(".txt").name}（{count} 段）、{path.with_suffix(".srt").name}')
    print(f'\n[完成] 转写 {done}，跳过 {len(files) - len(todo)}，失败 {len(failed)}'
          + (f'：{"、".join(failed)}' if failed else ''))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
