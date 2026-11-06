#!/usr/bin/env python3
"""Skill 完整副本同步：把 skill/<名字>/ 整个目录同步到各家 AI 的技能目录，不放指针。

为什么不放指针：指针只写“原版在项目里的哪个位置”，项目所在的电脑关机、云端会话读不到本机文件时，
AI 就拿不到完整的 Skill。每家都放一份完整副本，哪台设备关了都不影响；项目里的 skill/ 是备份和统一修改的地方。

  python tools/skill_sync.py status                    各家各 Skill 现在是什么状态，不改任何东西
  python tools/skill_sync.py plan                      预演：会新增、更新、替换指针、报冲突的有哪些
  python tools/skill_sync.py apply                     执行（只做没有冲突的动作；覆盖前先备份）
  python tools/skill_sync.py apply --force 家/Skill名   你确认过后，强制用项目版覆盖某一家的某个 Skill（先备份）
  参数：--only 名字,名字   只处理这几个 Skill；--agents 家,家   只处理这几家；--config 文件   指定设置文件

设置（.personal/agent.json 的 skills 段，或 --config 指定的 JSON 文件）：
  {"source": "skill", "skills": "all" 或 ["名字", …], "exclude": ["名字"],
   "targets": {"claude": "~/.claude/skills", "codex": "~/.codex/skills", …}}
  targets 的路径里 ~ 和环境变量（如 %LOCALAPPDATA%）会展开；没填 targets 就什么都不同步。

规矩（和 sync.py / feishu_sync.py 一样，先比差异，不是直接覆盖）：
- 基线：每次同步后记下每个文件当时的哈希（.local/skill_sync_manifest.json，只在本机）。
- 只有项目改了 → 更新到那一家；只有那一家改了 → 保留，列出来（要不要拿回项目由你定）；两边都改了 → 冲突，两边都不动。
- 没有基线、两边又不一样 → 也算冲突，不猜谁新（确认后用 --force）。
- 那一家目录里只有一个指针 SKILL.md（旧做法）→ 备份后换成完整副本。
- 只在那一家有的文件一律保留，从不删除。覆盖或替换前，整个旧目录先备份到 .local/backups/skill_sync/<时间>/。
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import agent_config
except ImportError:   # 没有 agent_config 的工作区只能用 --config 或 config/skill-sync.json
    agent_config = None

MANIFEST = '.local/skill_sync_manifest.json'
DEFAULT_CONFIG_FILE = 'config/skill-sync.json'
IGNORE = ('*.bak', '*.bak-*', '*.orig', '*.tmp', '.DS_Store', 'Thumbs.db', '*.pyc', '__pycache__', '.git')
POINTER_MARKS = ('只是指针', '（指针）', '先读原版再执行')


class ConfigError(Exception):
    pass


def ignored(name):
    return any(fnmatch.fnmatch(name, pattern) for pattern in IGNORE)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def tree(directory):
    """{相对路径: 完整路径}；跳过备份、缓存等不该同步的文件。"""
    found = {}
    directory = Path(directory)
    if not directory.is_dir():
        return found
    for current, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not ignored(d)]
        for name in files:
            if not ignored(name):
                path = Path(current) / name
                found[path.relative_to(directory).as_posix()] = path
    return found


def load_config(root, config_path=None):
    root = Path(root)
    if config_path:
        data = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    elif agent_config is not None and (root / '.personal/agent.json').is_file():
        data = agent_config.load(root).get('skills') or {}
    elif (root / DEFAULT_CONFIG_FILE).is_file():
        data = json.loads((root / DEFAULT_CONFIG_FILE).read_text(encoding='utf-8-sig'))
    else:
        raise ConfigError('没有找到设置：在 .personal/agent.json 里填 skills 段，或用 --config 指定文件。')
    targets = {}
    for agent, raw in (data.get('targets') or {}).items():
        targets[str(agent)] = Path(os.path.expandvars(os.path.expanduser(str(raw))))
    if not targets:
        raise ConfigError('设置里没有 skills.targets：先填要同步到哪几家 AI 的技能目录（见 docs/skills.md）。')
    return {'source': Path(root) / (data.get('source') or 'skill'), 'skills': data.get('skills', 'all'),
            'exclude': set(data.get('exclude') or []), 'targets': targets}


def skill_names(config, only=None):
    source = config['source']
    if not source.is_dir():
        raise ConfigError(f'技能库目录不存在：{source}')
    wanted = config['skills']
    names = sorted(p.name for p in source.iterdir() if p.is_dir() and (p / 'SKILL.md').is_file()) \
        if wanted == 'all' else list(wanted)
    names = [n for n in names if n not in config['exclude'] and (only is None or n in only)]
    missing = [n for n in names if not (source / n / 'SKILL.md').is_file()]
    if missing:
        raise ConfigError('技能库里没有（或缺 SKILL.md）：' + '、'.join(missing))
    return names


def is_pointer(files):
    if list(files) != ['SKILL.md']:
        return False
    text = files['SKILL.md'].read_text(encoding='utf-8', errors='replace')
    return any(mark in text for mark in POINTER_MARKS)


def load_manifest(root):
    try:
        return json.loads((Path(root) / MANIFEST).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_manifest(root, data):
    path = Path(root) / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding='utf-8')
    os.replace(temp, path)


def classify(source_files, target_files, baseline):
    """返回 (状态, 要写的文件 [相对路径], 被保留的“只在那一家改过”的文件, 冲突文件, 只在那一家有的文件)。
    状态：new / pointer / ok / update / conflict / kept"""
    if not target_files:
        return 'new', sorted(source_files), [], [], []
    if is_pointer(target_files):
        return 'pointer', sorted(source_files), [], [], []
    write, kept, conflict = [], [], []
    for rel, src in source_files.items():
        dst = target_files.get(rel)
        if dst is None:
            write.append(rel)
            continue
        source_hash, target_hash, base = sha256(src), sha256(dst), baseline.get(rel)
        if source_hash == target_hash:
            continue
        if base is None:
            conflict.append(rel)                     # 没有基线又不一样：不猜谁新
        elif target_hash == base:
            write.append(rel)                        # 那一家没动过，项目改了 → 更新
        elif source_hash == base:
            kept.append(rel)                         # 项目没改，那一家改过 → 保留
        else:
            conflict.append(rel)                     # 两边都改过
    extra = sorted(set(target_files) - set(source_files))
    if conflict:
        return 'conflict', write, kept, conflict, extra
    if write:
        return 'update', write, kept, conflict, extra
    return ('kept' if kept else 'ok'), write, kept, conflict, extra


def build_plan(root, config, only=None, agents=None, force=()):
    manifest = load_manifest(root)
    plan = []
    for name in skill_names(config, only):
        source_files = tree(config['source'] / name)
        for agent, base_dir in config['targets'].items():
            if agents and agent not in agents:
                continue
            target_dir = base_dir / name
            if target_dir.resolve() == (config['source'] / name).resolve():
                continue
            target_files = tree(target_dir)
            baseline = (manifest.get(agent) or {}).get(name) or {}
            state, write, kept, conflict, extra = classify(source_files, target_files, baseline)
            if state == 'conflict' and f'{agent}/{name}' in force:
                state, write, conflict = 'forced', sorted(source_files), []
            plan.append({'agent': agent, 'skill': name, 'state': state, 'write': write, 'kept': kept,
                         'conflict': conflict, 'extra': extra, 'target': target_dir,
                         'source_files': source_files})
    return plan


LABELS = {'new': '新增完整副本', 'pointer': '指针 → 换成完整副本', 'ok': '已一致', 'update': '更新',
          'conflict': '冲突（不动）', 'kept': '那一家改过、项目没改（保留）', 'forced': '强制覆盖（先备份）'}


def show(plan):
    for item in plan:
        extra = f"，{len(item['write'])} 个文件要写" if item['state'] in ('new', 'pointer', 'update', 'forced') else ''
        print(f"  {item['agent']:<10} {item['skill']:<36} {LABELS[item['state']]}{extra}")
        for rel in item['conflict']:
            print(f'      冲突：{rel}（项目和那一家都和上次同步时不一样，或没有基线）')
        for rel in item['kept']:
            print(f'      保留：{rel}（只有那一家改过；要不要拿回项目由你定）')
        if item['extra']:
            print(f"      只在那一家有（保留，不删）：{', '.join(item['extra'][:5])}{' …' if len(item['extra']) > 5 else ''}")


def backup_dir(root, stamp, agent, name, target):
    if not Path(target).is_dir():
        return None
    dest = Path(root) / '.local/backups/skill_sync' / stamp / agent / name
    shutil.copytree(target, dest)
    return dest


def apply_plan(root, plan):
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    manifest = load_manifest(root)
    failures = []
    for item in plan:
        agent, name, target = item['agent'], item['skill'], item['target']
        try:
            if item['state'] in ('new', 'pointer', 'update', 'forced'):
                if item['state'] != 'new':
                    backup_dir(root, stamp, agent, name, target)
                for rel in item['write']:
                    dst = target / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    temp = dst.with_name(dst.name + '.skillsync.tmp')
                    shutil.copy2(item['source_files'][rel], temp)
                    os.replace(temp, dst)
            if item['state'] in ('conflict',):
                continue
            now = tree(target)
            recorded = (manifest.setdefault(agent, {}).setdefault(name, {}))
            for rel, src in item['source_files'].items():
                if rel in now and rel not in item['kept'] and sha256(now[rel]) == sha256(src):
                    recorded[rel] = sha256(src)
        except OSError as exc:
            failures.append(f'{agent}/{name}：{exc}')
    save_manifest(root, manifest)
    return failures


def verify(plan):
    """重新比较一遍：本次该写的文件，现在是否和项目一致。"""
    bad = []
    for item in plan:
        if item['state'] not in ('new', 'pointer', 'update', 'forced'):
            continue
        for rel in item['write']:
            dst = item['target'] / rel
            if not dst.is_file() or sha256(dst) != sha256(item['source_files'][rel]):
                bad.append(f"{item['agent']}/{item['skill']}/{rel}")
    return bad


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['status', 'plan', 'apply'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--only', default='')
    parser.add_argument('--agents', default='')
    parser.add_argument('--force', action='append', default=[], metavar='家/Skill名')
    args = parser.parse_args(argv)
    split = lambda text: {part.strip() for part in text.split(',') if part.strip()} or None
    try:
        config = load_config(args.root, args.config)
        plan = build_plan(args.root, config, split(args.only), split(args.agents), set(args.force))
    except (ConfigError, OSError, ValueError) as exc:
        print(f'[未同步] {exc}')
        return 2
    print(f"技能库：{config['source']}；{len(plan)} 项")
    show(plan)
    conflicts = [i for i in plan if i['state'] == 'conflict']
    if args.command != 'apply':
        if conflicts:
            print('\n有冲突：哪边都没动。确认以项目版为准后，用 apply --force 家/Skill名。')
        return 1 if conflicts else 0
    failures = apply_plan(args.root, plan)
    failures += ['写完回读不一致：' + path for path in verify(plan)]
    for failure in failures:
        print('[失败] ' + failure)
    if conflicts:
        print('\n有冲突没有处理（哪边都没动）：' + '、'.join(f"{i['agent']}/{i['skill']}" for i in conflicts))
    done = sum(1 for i in plan if i['state'] in ('new', 'pointer', 'update', 'forced'))
    print(f'\n已写 {done} 项，回读核对{"通过" if not failures else "未通过"}。')
    return 1 if failures or conflicts else 0


if __name__ == '__main__':
    sys.exit(main())
