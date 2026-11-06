#!/usr/bin/env python3
"""提交、推送前的安全检查（只读，不改任何文件）。

  python tools/upload_check.py                      检查将要提交的文件（未跟踪 + 已修改，已按 .gitignore 过滤）
  python tools/upload_check.py --all                另外检查全部已跟踪文件
  python tools/upload_check.py --staged             只检查已暂存的内容（pre-commit 钩子用）
  python tools/upload_check.py --check-remote URL   只检查推送地址（pre-push 钩子用）

检查项：
  [拦截] 疑似密钥 / token / 私钥 / 明文密钥赋值（只显示 文件:行号 和类型，不显示内容）
  [拦截] 中国居民身份证号（18 位且校验位正确）
  [拦截] 只留本机的路径（设置 local_only）、原件资料库 .personal/files/、凭据文件（.env、*.pem 等）
  [拦截] 目录里嵌着别的 Git 仓库（git add 只会记一个空链接，内容不会上传）
  [拦截] 单个文件超过 100MB（GitHub 会拒收）；[提示] 超过 50MB
  [拦截] 没装本仓库的钩子（python tools/install_git_hooks.py）
  [拦截] 设置里没填 private_remote，或推送地址不是它
  [拦截] 本机 gh 已登录且查到远端在 GitHub 上是公开仓；[提示] 查不了时提示无法确认可见性
  [提示] 手机号（只提示，不拦截）

可见性检查只在全量检查和 --check-remote 时做（要联网）；--staged 每次提交都跑，只做本机检查。
扫描范围：文本文件 + docx/xlsx/pptx 里的文字；pdf、图片、压缩包的内容不扫。
返回码：有拦截项 1，否则 0。只用标准库。
"""
import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

MB = 1024 * 1024
HARD_LIMIT = 100 * MB        # GitHub 拒收单个超过 100MB 的文件
WARN_LIMIT = 50 * MB
TEXT_SCAN_MAX = 5 * MB       # 超过这个大小的纯文本不逐行扫
OOXML_SCAN_MAX = 50 * MB     # docx/xlsx/pptx 超过这个大小不解压扫
SHOW_MAX = 30                # 每类最多列出多少条

OOXML_EXT = {'.docx', '.xlsx', '.pptx'}
CREDENTIAL_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.kdbx'}

# 固定格式的凭据：命中即拦。
SECRET_PATTERNS = [
    ('API Key（sk- 开头）', re.compile(r'\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}')),
    ('GitHub Token', re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}')),
    ('AWS AccessKey', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('阿里云 AccessKey', re.compile(r'\bLTAI[0-9A-Za-z]{12,30}\b')),
    ('Slack Token', re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{10,}')),
    ('JWT', re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')),
    ('私钥', re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----')),
    ('Bearer Token', re.compile(r'(?i)\bBearer\s+([A-Za-z0-9_\-\.=]{20,})')),
]
# 赋值形式：token = "xxxx"、app_secret: xxxx、密码：xxxx
ASSIGN_PATTERN = re.compile(
    r'(?i)(?:api[_-]?key|app[_-]?secret|client[_-]?secret|secret[_-]?key|access[_-]?key(?:[_-]?secret)?'
    r'|access[_-]?token|auth[_-]?token|refresh[_-]?token|token|password|passwd|pwd|密码|密钥)'
    r'["\']?\s*[:=：]\s*["\']?([A-Za-z0-9_\-\.+/=@#$%!]{12,})'
)
PLACEHOLDER_HINTS = ('xxxx', 'your', 'example', 'placeholder', 'environ', 'getenv', '****', '<', '${',
                     'here', 'dummy', 'test')
CODE_REF = re.compile(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+')  # process.env.KEY、args.api_key 这类代码引用

ID_PATTERN = re.compile(
    r'(?<![0-9A-Za-z])([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![0-9A-Za-z])'
)
ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
ID_CHECK = '10X98765432'
PHONE_PATTERN = re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')
XML_TAG = re.compile(r'<[^>]+>')

# 钩子文件里必须出现的标记，用来确认 git 实际会跑的是本仓库的钩子。
HOOK_MARKERS = {'pre-commit': 'upload_check.py --staged', 'pre-push': 'upload_check.py --check-remote'}


def git(root, *args, stdin=None):
    out = subprocess.run(['git', '-c', 'core.quotepath=false', *args], cwd=str(root), input=stdin,
                         capture_output=True, timeout=300, creationflags=NO_WINDOW)
    return out


def git_text(root, *args):
    return git(root, *args).stdout.decode('utf-8', 'replace')


# ---------- 远端地址 ----------

def normalize_remote(url):
    """把 https / ssh / scp 写法统一成 host/owner/repo；本地路径统一成正斜杠。末尾 .git 和 / 不计，大小写不计。"""
    text = (url or '').strip()
    if not text:
        return ''
    if re.match(r'^[A-Za-z]:[\\/]', text) or text.startswith(('/', '\\', '.')) or text.lower().startswith('file://'):
        result = text[7:] if text.lower().startswith('file://') else text
        result = result.replace('\\', '/')
    else:
        match = (re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)(?::\d+)?/+(.*)$', text)
                 or re.match(r'^(?:[^@/]+@)?([^/:]+):(?!//)/*(.+)$', text))
        result = f'{match.group(1)}/{match.group(2)}' if match else text
    result = result.rstrip('/')
    if result.lower().endswith('.git'):
        result = result[:-4]
    return result.rstrip('/').casefold()


def same_remote(a, b):
    return bool(normalize_remote(a)) and normalize_remote(a) == normalize_remote(b)


def redact(url):
    """去掉地址里的用户名和密码 / token 再显示。"""
    return re.sub(r'(://)[^/@]+@', r'\1***@', url or '')


def github_repo(url):
    parts = normalize_remote(url).split('/')
    if len(parts) == 3 and parts[0] == 'github.com':
        return parts[1], parts[2]
    return None


def gh_private(owner, repo):
    """用已登录的 gh 查仓库是否私有：True / False；没装、没登录、离线等查不了时返回 None。"""
    if not shutil.which('gh'):
        return None
    try:
        out = subprocess.run(['gh', 'api', f'repos/{owner}/{repo}', '--jq', '.private'],
                             capture_output=True, timeout=20, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode:
        return None
    return {'true': True, 'false': False}.get(out.stdout.decode('utf-8', 'replace').strip())


def origin_url(root):
    return git_text(root, 'remote', 'get-url', '--push', 'origin').strip()


def remote_problems(root, settings, url=None, visibility=False):
    """推送地址必须是设置里的 private_remote。返回 (拦截, 提示)；url 为空时查 origin。"""
    blocks, hints = [], []
    want = (settings.get('private_remote') or '').strip()
    if not want:
        blocks.append('还没设置私有仓：在 .personal/agent.json 里填 private_remote（你自己的私有仓地址），'
                      '设置之前一律不提交、不推送。')
        return blocks, hints
    if url is None:
        url = origin_url(root)
        if not url:
            blocks.append(f'没有 origin 远端。修正：git remote add origin {redact(want)}')
            return blocks, hints
    if not same_remote(url, want):
        blocks.append(f'推送地址 {redact(url)} 不是设置里的私有仓 {redact(want)}。'
                      f'修正：git remote set-url origin {redact(want)}')
        return blocks, hints
    if visibility:
        repo = github_repo(want)
        state = gh_private(*repo) if repo else None
        if state is False:
            blocks.append(f'{redact(want)} 在 GitHub 上是公开仓，推上去等于公开。先在仓库设置里改成私有。')
        elif state is None:
            hints.append(f'无法确认仓库可见性（需要已登录的 gh 命令行）；请自己确认 {redact(want)} 是私有仓。')
    return blocks, hints


def hooks_problem(root):
    """git 实际会用的 pre-commit / pre-push 必须是本仓库的版本；返回缺失或不对的钩子名。"""
    names = list(HOOK_MARKERS)
    found = git_text(root, 'rev-parse', *[item for name in names for item in ('--git-path', f'hooks/{name}')]).splitlines()
    missing = []
    for name, rel in zip(names, found + [''] * len(names)):
        rel = rel.strip()
        hook = Path(rel) if Path(rel).is_absolute() else Path(root) / rel
        try:
            ok = bool(rel) and hook.is_file() and HOOK_MARKERS[name] in hook.read_text(encoding='utf-8', errors='replace')
            if ok and os.name != 'nt' and not os.access(hook, os.X_OK):
                ok = False   # macOS / Linux 上钩子没有可执行位时 git 会悄悄跳过它
        except OSError:
            ok = False
        if not ok:
            missing.append(name)
    return missing


# ---------- 路径边界 ----------

def path_problem(rel, settings):
    """不许进 Git 的路径返回原因，否则返回 None。rel 相对工作区根目录。"""
    rel = rel.replace('\\', '/')
    if agent_config.is_local_only(rel, settings):
        return '只留本机的路径（设置 local_only）'
    areas = {agent_config.PATHS['files'], agent_config.PATHS['inbox'],
             (settings.get('cloud') or {}).get('local_path') or '',
             (settings.get('feishu') or {}).get('local_path') or ''}
    for area in areas:
        area = area.replace('\\', '/').strip('/')
        if area and (rel == area or rel.startswith(area + '/')):
            return '原件资料库（走云盘，不进 Git）'
    name = rel.rsplit('/', 1)[-1].casefold()
    if name.startswith('.env') or Path(name).suffix in CREDENTIAL_SUFFIXES:
        return '凭据文件'
    if 'password' in name or '密码' in name:
        return '文件名像密码文件'
    return None


# ---------- 内容扫描 ----------

def valid_id(text):
    text = text.upper()
    total = sum(int(c) * w for c, w in zip(text[:17], ID_WEIGHTS))
    return ID_CHECK[total % 11] == text[17]


def looks_like_secret(value, next_char):
    """赋值形式的值像不像真密钥：排除占位符、代码引用、函数调用、单一大小写的标识符、重复字符。"""
    value = value.rstrip('.')
    if next_char == '(' or any(h in value.lower() for h in PLACEHOLDER_HINTS):
        return False
    if CODE_REF.fullmatch(value) or re.fullmatch(r'[a-z_]+|[A-Z_]+', value) or len(set(value)) < 6:
        return False
    return bool(re.search(r'\d', value)) or len(value) >= 20


def find_secrets(text):
    """只找固定格式的凭据，返回类型名列表（不返回内容）。"""
    return [label for label, pattern in SECRET_PATTERNS if pattern.search(text)]


def scan_lines(lines):
    secrets, ids, phones = [], [], 0
    for no, line in enumerate(lines, 1):
        for label in find_secrets(line):
            secrets.append((no, label))
        for match in ASSIGN_PATTERN.finditer(line):
            if looks_like_secret(match.group(1), line[match.end():match.end() + 1]):
                secrets.append((no, '疑似明文密钥/密码赋值'))
        for match in ID_PATTERN.finditer(line):
            if valid_id(match.group(1)):
                ids.append(no)
        phones += len(PHONE_PATTERN.findall(line))
    return secrets, ids, phones


def text_lines(data, suffix):
    """返回文本行；二进制返回 None。docx/xlsx/pptx 取 XML 里的文字。"""
    if suffix in OOXML_EXT:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                texts = [XML_TAG.sub(' ', package.read(name).decode('utf-8', 'replace'))
                         for name in package.namelist()
                         if name.endswith('.xml') and name.startswith(('word/', 'xl/', 'ppt/slides/', 'ppt/notesSlides/'))]
            return '\n'.join(texts).splitlines()
        except (zipfile.BadZipFile, KeyError, OSError, ValueError):
            return None
    if b'\x00' in data[:8192]:
        return None
    try:
        return data.decode('utf-8').splitlines()
    except UnicodeDecodeError:
        return data.decode('gb18030', 'replace').splitlines()


# ---------- 候选文件 ----------

def pending_files(root, include_tracked):
    """返回 (要检查的文件, 待提交集合, 嵌套仓库)。待提交 = 未跟踪（已过 .gitignore）+ 新增/修改，删除的不算。"""
    entries = git_text(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all').split('\0')
    files, nested, i = [], [], 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, rel = entry[:2], entry[3:]
        if xy[0] in 'RC':
            i += 1   # 下一个条目是改名前的旧路径
        if 'D' in xy:
            continue
        if xy == '??' and rel.endswith('/'):
            nested.append(rel)   # --untracked-files=all 下仍以目录出现的，是带 .git 的嵌套仓库
            continue
        files.append(rel)
    pending = set(files)
    if include_tracked:
        files.extend(p for p in git_text(root, 'ls-files', '-z').split('\0') if p)
    seen, result = set(), []
    for rel in files:
        if rel not in seen and (Path(root) / rel).is_file():
            seen.add(rel)
            result.append(rel)
    return result, pending, nested


def staged_entries(root):
    """已暂存的新增/修改文件 [(路径, blob 号)]，和被当成嵌套仓库暂存的目录。"""
    out = git(root, 'diff', '--cached', '--raw', '-z', '--no-renames', '--no-abbrev', '--diff-filter=ACM')
    if out.returncode:
        raise OSError('无法读取 Git 暂存区：' + out.stderr.decode('utf-8', 'replace').strip())
    parts = out.stdout.decode('utf-8', 'replace').split('\0')
    files, nested = [], []
    for k in range(0, len(parts) - 1, 2):
        meta, rel = parts[k], parts[k + 1]
        if not meta.startswith(':'):
            continue
        fields = meta.split()   # :旧模式 新模式 旧号 新号 状态
        if fields[1] == '160000':
            nested.append(rel + '/')
        else:
            files.append((rel, fields[3]))
    return files, nested


def read_blobs(root, limits):
    """limits = {blob 号: 内容大小上限}。一次读出：返回 {blob 号: (大小, 内容；超过上限为 None)}。

    读的是暂存区里的版本，不是工作区：暂存后再改工作区文件也躲不过检查。
    """
    if not limits:
        return {}
    oids = list(limits)
    checked = git(root, 'cat-file', '--batch-check', stdin=('\n'.join(oids) + '\n').encode('ascii'))
    sizes = {}
    for line in checked.stdout.decode('ascii', 'replace').splitlines():
        parts = line.split()
        if len(parts) == 3:
            sizes[parts[0]] = int(parts[2])
    result = {oid: (sizes.get(oid, 0), None) for oid in oids}
    small = [oid for oid in oids if oid in sizes and sizes[oid] <= limits[oid]]
    if small:
        data = git(root, 'cat-file', '--batch', stdin=('\n'.join(small) + '\n').encode('ascii')).stdout
        offset = 0
        for oid in small:
            end = data.index(b'\n', offset)
            size = int(data[offset:end].split()[2])
            result[oid] = (size, data[end + 1:end + 1 + size])
            offset = end + 1 + size + 1
    return result


def scan_limit(rel):
    return OOXML_SCAN_MAX if Path(rel).suffix.lower() in OOXML_EXT else TEXT_SCAN_MAX


def human(size):
    return f'{size / 1024 / MB:.2f} GB' if size >= 1024 * MB else f'{size / MB:.1f} MB'


# ---------- 汇总 ----------

def check(root, mode='pending', settings=None):
    """mode：pending / all / staged。返回 (拦截类别, 提示类别, 输出行)。"""
    root = Path(root)
    settings = settings or agent_config.load(root)
    staged = mode == 'staged'
    if staged:
        entries, nested = staged_entries(root)
        blobs = read_blobs(root, {oid: scan_limit(rel) for rel, oid in entries})
        files = [rel for rel, _ in entries]
        pending, sources = set(files), {rel: blobs[oid] for rel, oid in entries}
    else:
        files, pending, nested = pending_files(root, mode == 'all')

    secret_hits, id_hits, phone_hits, path_hits = [], [], [], []
    big_block, big_warn = [], []
    total, unscanned = 0, 0
    for rel in files:
        size, data = sources[rel] if staged else ((root / rel).stat().st_size, None)
        total += size
        if rel in pending:
            reason = path_problem(rel, settings)
            if reason:
                path_hits.append(f'{rel}（{reason}）')
        if size > HARD_LIMIT:
            big_block.append(f'{rel}（{human(size)}）')
        elif size > WARN_LIMIT:
            big_warn.append(f'{rel}（{human(size)}）')
        if size > scan_limit(rel) or (staged and data is None):
            unscanned += 1
            continue
        lines = text_lines(data if staged else (root / rel).read_bytes(), Path(rel).suffix.lower())
        if lines is None:
            unscanned += 1
            continue
        secrets, ids, phones = scan_lines(lines)
        secret_hits += [f'{rel}:{no}  {label}' for no, label in secrets]
        id_hits += [f'{rel}:{no}' for no in ids]
        if phones:
            phone_hits.append((phones, rel))

    out, blocks, warns = [], [], []

    def show(title, items):
        out.append(f'  {title}（{len(items)} 处）')
        out.extend(f'    - {item}' for item in items[:SHOW_MAX])
        if len(items) > SHOW_MAX:
            out.append(f'    ……另有 {len(items) - SHOW_MAX} 处')

    scope = {'staged': '已暂存（本次提交）', 'all': '待提交 + 全部已跟踪', 'pending': '待提交（新增 + 修改）'}[mode]
    out.append(f'上传前检查  范围：{scope}  共 {len(files)} 个文件，{human(total)}；未扫内容（二进制或过大）{unscanned} 个')

    missing = hooks_problem(root)
    if missing:
        blocks.append('未装钩子')
        out.append(f"  [拦截] 本仓库的 {'、'.join(missing)} 钩子没启用：推错仓库、提交前漏检时没有最后一道闸。")
        out.append('    修正：在工作区根目录运行  python tools/install_git_hooks.py')
    remote_blocks, remote_hints = remote_problems(root, settings, visibility=not staged)
    if remote_blocks:
        blocks.append('远端')
        out.extend('  [拦截] ' + item for item in remote_blocks)
    if remote_hints:
        warns.append('可见性未确认')
        out.extend('  [提示] ' + item for item in remote_hints)
    if secret_hits:
        blocks.append('疑似密钥')
        show('[拦截] 疑似密钥 / token —— 删掉或挪进 .device/secrets/ 后再提交（确属误报就改写成占位符）', secret_hits)
    if id_hits:
        blocks.append('身份证号')
        show('[拦截] 身份证号 —— 打码或挪进 .personal/private/', id_hits)
    if path_hits:
        blocks.append('不该进 Git 的路径')
        show('[拦截] 只留本机 / 原件资料库 / 凭据文件出现在待提交列表 —— 检查 .gitignore，已暂存的 git reset 撤掉', path_hits)
    if nested:
        blocks.append('嵌套仓库')
        show('[拦截] 目录里有 .git（嵌套仓库），内容不会上传 —— 确认是旧副本后先备份，再删掉或改名其中的 .git', nested)
    if big_block:
        blocks.append('超 100MB')
        show('[拦截] 单个文件超过 100MB，GitHub 会拒收 —— 原件放 .personal/files/ 走云盘', big_block)
    if big_warn:
        warns.append('超 50MB')
        show('[提示] 单个文件超过 50MB', big_warn)
    if phone_hits:
        warns.append('手机号')
        phone_hits.sort(reverse=True)
        show('[提示] 含手机号的文件（只提示，确认可以进你的私有仓再提交）', [f'{rel}  {n} 个' for n, rel in phone_hits])
    if blocks:
        out.append('结果：未通过（' + '、'.join(blocks) + '）。处理完再提交 / 推送。')
    else:
        out.append('结果：通过。' + ('（有提示项：' + '、'.join(warns) + '）' if warns else ''))
    return blocks, warns, out


def check_remote(root, url, settings=None):
    """pre-push 用：只核对这次要推的地址。返回 (拦截, 提示)。"""
    return remote_problems(root, settings or agent_config.load(root), url=url, visibility=True)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description='提交、推送前的安全检查（只读）')
    parser.add_argument('--root', type=Path, default=ROOT)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--all', action='store_true', help='另外检查全部已跟踪文件')
    group.add_argument('--staged', action='store_true', help='只检查已暂存的内容')
    group.add_argument('--check-remote', metavar='URL', help='只检查推送地址')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.check_remote is not None:
        blocks, hints = check_remote(root, args.check_remote)
        for item in hints:
            print('[pre-push] [提示] ' + item)
        for item in blocks:
            print('[pre-push] [拦截] ' + item)
        return 1 if blocks else 0
    mode = 'staged' if args.staged else ('all' if args.all else 'pending')
    try:
        blocks, _, out = check(root, mode)
    except OSError as exc:
        print(f'[拦截] 检查没法完成：{exc}')
        return 1
    print('\n'.join(out))
    return 1 if blocks else 0


if __name__ == '__main__':
    sys.exit(main())
