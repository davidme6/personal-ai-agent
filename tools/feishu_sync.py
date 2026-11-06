#!/usr/bin/env python3
"""飞书云空间同步：把原件资料库（默认 .personal/files/）和你自己飞书云空间里的一个文件夹像 Git 一样三方比较、双向同步。

用的是飞书官方命令行工具 lark-cli（npm 包 @larksuite/cli）。账号、应用都是你自己的，本工具不内置任何账号；
设置在 .personal/agent.json 的 "feishu" 段：enabled、cli、profile、folder_token、local_path、auto_sync。

  python tools/feishu_sync.py whoami                 看 CLI 现在登录的是哪个飞书账号，核对是不是你要存资料的那个
  python tools/feishu_sync.py plan                   只读：比较差异，列出计划和计划编号，不改任何东西
  python tools/feishu_sync.py sync --approve 编号     执行这个计划；执行前重新核对，出计划后又变了的跳过并列出
  python tools/feishu_sync.py sync --scheduled       定时任务用：feishu.enabled 和 feishu.auto_sync 都为 true 才执行，
                                                     只做没有冲突的动作，只写日志 .local/feishu_sync.log
  python tools/feishu_sync.py pull                   只把云端新增、云端更新的文件取回本机（本机旧版先备份）
  python tools/feishu_sync.py verify                 只读：逐个把云端文件读回来和本机比哈希
  python tools/feishu_sync.py resolve 路径…           处理冲突：告诉工具“云端版我看过了，以本机这份为准”

三方比较的基线是上次同步时的版本：
- 只本机改了 → 上传新版本（按 token 原地替换）；只云端改了 → 下载，本机旧版先备份到 .local/backups/feishu-pull/。
- 两边都改了 → 冲突：哪边都不覆盖，云端版存到 .local/feishu/conflicts/，命令返回 1。
- 云端列表里找不到的先按 token 再问一次：还在就不动（大目录可能列不全）；真删了就列出来等你定，不重新上传、不删本机。
- 本机删了的只列出，永远不删云端文件；云端的在线文档（docx/sheet/bitable 等）只列出；从不改分享和权限设置。
- local_only 和 .personal/private/ 下的东西永不上传。
- 第一次往某个账号、某个文件夹写入，必须先 plan 再 sync --approve；确认过以后定时同步才会自动执行。

基线登记表 .personal/feishu-map.json 随你的私有仓同步，另一台设备据此知道上次同步的版本；
这台设备自己的每个文件同步到了哪个版本记在 .local/feishu/state.json（只在本机），用来分清“本机改了”和“本机还是旧版”。
"""
import argparse
import contextlib
import datetime
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import agent_config

ROOT = Path(__file__).resolve().parent.parent
MAP = '.personal/feishu-map.json'
WORK = '.local/feishu'
STATE = WORK + '/state.json'
CONFLICTS = WORK + '/conflicts'
CONFLICT_INDEX = WORK + '/conflicts.json'
CACHE = WORK + '/cache'
STAGING = WORK + '/staging'
LOCK = WORK + '/sync.lock'
LOG = '.local/feishu_sync.log'
BACKUPS = '.local/backups/feishu-pull'
PLAN_VALID_SECONDS = 24 * 3600
TIME_SLACK = 2            # 列表里的修改时间比登记的晚这么几秒以内，算没改
UPLOAD_SLACK = 120        # 还没记下云端修改时间时用同步时间判断，留出上传到登记之间的时间差
RETRY_DELAYS = (2, 5)     # 只读命令（列目录、查元数据）出错时的重试间隔（秒）；飞书偶尔回可重试的错误
CALL_TIMEOUT = 300
TRANSFER_TIMEOUT = 3600   # 上传下载大文件（比如视频）要给足时间
SAVE_EVERY = 20           # 每做完这么多个动作存一次登记表，中途退出也丢不了多少
JUNK = ('~$*', '*.tmp', '.DS_Store', 'Thumbs.db', 'desktop.ini')   # 锁文件和系统垃圾，同步了只会制造冲突
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
PLAN_ID = re.compile(r'^[0-9a-f]{12}$')
SAFE_TOKEN = re.compile(r'^[A-Za-z0-9_-]{1,200}$')
RUN_ORDER = ('download_new', 'download_update', 'rename', 'adopt', 'upload_new', 'upload_update',
             'track', 'untrack', 'forget')                                  # 先拉后推
PULL_KINDS = ('download_new', 'download_update', 'rename', 'adopt')

KIND_LABELS = {
    'upload_new': '上传新文件',
    'upload_update': '上传新版本（只本机改了，按 token 原地替换云端文件）',
    'download_new': '下载到本机（云端有、这台设备还没有）',
    'download_update': '下载云端新版本（只云端改了，本机旧版先备份到 .local/backups/feishu-pull/）',
    'rename': '跟着云端改名或挪位置（本机文件跟着挪，不重新传）',
    'adopt': '只更新登记（两边内容已经一样）',
}
REPORT_LABELS = (
    ('conflict', '[冲突] 两边都改过（或分不清哪边新）：哪边都不覆盖，云端版已存到 .local/feishu/conflicts/。'
                 '要云端版就把副本复制回本机再 plan；要本机版就运行 resolve 路径'),
    ('cloud_deleted', '[注意] 云端已经删了（按 token 核实过）、本机还在：不重新上传，也不删本机；要重新上传就运行 resolve 路径'),
    ('unlisted', '云端列表里没找到、按 token 查还在（大目录可能列不全）：不重传、不动'),
    ('local_deleted', '本机删了、云端还在：只列出，永远不删云端文件'),
    ('cloud_docs', '云端的在线文档、快捷方式：只列出，不下载、不动'),
    ('skipped', '跳过'),
)

SETUP_STEPS = ('准备步骤：\n'
               '  1. 安装飞书官方命令行工具：npm install -g @larksuite/cli（需要 Node.js）；\n'
               '  2. 在飞书开放平台用你自己的账号创建一个应用，开通云空间（drive）相关权限；\n'
               '  3. 运行 lark-cli config init 填你自己应用的信息，再运行 lark-cli auth login --domain drive，'
               '用你要存资料的飞书账号授权（用了配置档就加 --profile 名字，例如 lark-cli --profile 名字 auth login --domain drive）；\n'
               '  4. 运行 python tools/feishu_sync.py whoami，核对显示的是不是你要存资料的那个账号。')
NOT_LOGGED_IN = ('飞书 CLI 没有以你的用户身份登录（或者登录过期了），什么都没做。运行 lark-cli auth login --domain drive'
                 '（用了配置档就加 --profile 名字），用你要存资料的飞书账号授权，再运行 python tools/feishu_sync.py whoami 核对。')
NO_FOLDER = ('feishu.folder_token 为空：还没指定存到云空间的哪个文件夹。在飞书网页版打开（或新建）一个文件夹，'
             '地址栏 /drive/folder/ 后面那一串就是 folder_token，填到 .personal/agent.json 的 "feishu": {"folder_token": "..."}。')
NOT_ENABLED = 'feishu.enabled 是 false：要用飞书同步，先在 .personal/agent.json 里把 "feishu": {"enabled": true} 打开。'


class Problem(Exception):
    """环境或配置没准备好、CLI 出错：这次不同步，消息直接给人看。"""


class Busy(Problem):
    """另一个同步正在进行。"""


# ---------------------------------------------------------------- 小工具

def now_text():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def epoch(text):
    try:
        return datetime.datetime.fromisoformat(str(text)).timestamp()
    except ValueError:
        return 0.0


def as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def local_digest(path, *hints):
    """本机文件的哈希。大小和修改时间都和某条记录一样就直接用记录里的哈希，省得每次把整个资料库重算一遍。"""
    stat = path.stat()
    for hint in hints:
        if hint and hint.get('sha256') and hint.get('size') == stat.st_size and hint.get('local_mtime_ns') == stat.st_mtime_ns:
            return hint['sha256']
    return sha256(path)


def move(source, target):
    try:
        os.replace(source, target)
    except OSError:            # 跨磁盘时 os.replace 不行，退回复制再删
        shutil.copy2(source, target)
        os.unlink(source)


def describe(answer):
    return json.dumps(answer.get('error', answer) if isinstance(answer, dict) else answer, ensure_ascii=False)[:300]


def load_json(path, strict=False):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        if strict:
            raise Problem(f'{path} 读不了（{exc.__class__.__name__}）：可能有合并冲突标记或被手改坏了，先修好再同步。')
        return None


def write_json(path, data):
    """先写临时文件再替换：中途断电也不会留下半个登记表。内容没变就不写，免得私有仓里无谓地多出改动。"""
    path = Path(path)
    text = json.dumps(data, ensure_ascii=False, indent=1) + '\n'
    try:
        if path.read_text(encoding='utf-8') == text:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(text, encoding='utf-8', newline='\n')
    os.replace(temp, path)


def make_logger(root, echo):
    log = Path(root) / LOG

    def say(text=''):
        text = str(text)
        if echo:
            print(text)
        log.parent.mkdir(parents=True, exist_ok=True)
        stamp = now_text()
        with open(log, 'a', encoding='utf-8', errors='replace') as stream:
            for line in text.splitlines() or ['']:
                stream.write(f'{stamp} {line}\n')
    return say


@contextlib.contextmanager
def run_lock(root):
    """同一时间只许一个同步在跑：定时任务和手动运行一起改登记表，会丢掉对方刚记下的版本。"""
    path = Path(root) / LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Busy(f'另一个飞书同步正在进行（{LOCK}）。如果确认没有同步在跑（比如上次被强行中断），删掉这个文件再来。')
    try:
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
        yield
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------- 本机目录

def rel_to(base, target):
    try:
        return Path(target).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return None


def local_dir(root, conf):
    return (Path(root) / (conf.get('local_path') or agent_config.PATHS['files'])).resolve()


def check_local_dir(root, settings, local):
    base = rel_to(root, local)
    if base is None:
        return            # 工作区外的目录：不涉及 local_only
    private = agent_config.PATHS['private']
    if rel_to(local, Path(root) / '.local') is not None:
        raise Problem(f'feishu.local_path（{base}）不能包含 .local/：那里是本机缓存和备份。')
    if agent_config.is_local_only(base, settings) or base == private or base.startswith(private + '/'):
        raise Problem(f'feishu.local_path（{base}）在只留本机的范围里，不能同步到云空间。')


def excluded(root, settings, local, rel):
    """rel 相对资料目录。垃圾文件、登记表本身、local_only 和 .personal/private/ 下的、sync_exclude 命中的（默认凭据类文件）都不同步。"""
    if any(fnmatch.fnmatch(rel.rsplit('/', 1)[-1], pattern) for pattern in JUNK):
        return True
    base = rel_to(root, local)
    if base is None:
        return False
    full = rel if base == '.' else f'{base}/{rel}'
    private = agent_config.PATHS['private']
    return (full == MAP or agent_config.is_local_only(full, settings)
            or full == private or full.startswith(private + '/')
            or agent_config.sync_excluded(rel, settings))


def scan_local(root, settings, local):
    found = {}
    if not local.is_dir():
        return found
    for folder, dirs, names in os.walk(local):
        here = Path(folder)
        prefix = '' if here == local else here.relative_to(local).as_posix() + '/'
        dirs[:] = sorted(name for name in dirs if not excluded(root, settings, local, prefix + name))
        for name in names:
            if not excluded(root, settings, local, prefix + name):
                found[prefix + name] = here / name
    return found


# ---------------------------------------------------------------- 飞书 CLI

def find_cli(name):
    found = shutil.which(name) or (name if Path(name).is_file() else None)
    if not found:
        raise Problem(f'没找到飞书 CLI（feishu.cli = {name}）。装在别处的话，把完整路径填到 .personal/agent.json 的 feishu.cli。\n'
                      + SETUP_STEPS)
    path = Path(found)
    if path.suffix.lower() == '.cmd':
        # Windows 上 npm 装的是 .cmd 包装：直接调里面的程序本体，少过一层 cmd，超时能结束干净
        program = path.parent / 'node_modules/@larksuite/cli/bin/lark-cli.exe'
        if program.is_file():
            return str(program)
    return found


class Feishu:
    """调飞书 CLI：总是以用户身份（--as user），只解析 stdout 里的 JSON。"""

    def __init__(self, root, conf):
        self.root = Path(root)
        self.exe = find_cli(str(conf.get('cli') or 'lark-cli'))
        self.profile = str(conf.get('profile') or '').strip()
        self.count = 0

    def call(self, *args, timeout=CALL_TIMEOUT):
        command = [self.exe] + (['--profile', self.profile] if self.profile else []) + list(args) + ['--as', 'user']
        env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1', LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1')
        try:
            proc = subprocess.run(command, cwd=str(self.root), capture_output=True, env=env, timeout=timeout,
                                  creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise Problem(f'飞书 CLI {timeout} 秒没回应（{" ".join(args[:2])}）。')
        except OSError as exc:
            raise Problem(f'运行飞书 CLI 失败：{exc}')
        text = (proc.stdout or b'').decode('utf-8', 'replace')
        try:
            answer = json.loads(text[text.index('{'):text.rindex('}') + 1])
        except ValueError:
            tail = (text + (proc.stderr or b'').decode('utf-8', 'replace')).strip()[-300:]
            raise Problem(f'飞书 CLI 没返回可解析的结果（{" ".join(args[:2])}）：{tail}')
        if not isinstance(answer, dict):
            raise Problem(f'飞书 CLI 返回的不是对象（{" ".join(args[:2])}）。')
        return answer

    def read(self, *args):
        """只读命令：出错重试几次；还不行就停，绝不把“出错”当成“里面是空的”。"""
        error = None
        for delay in (0,) + tuple(RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                answer = self.call(*args)
            except Problem as exc:
                error = exc
                continue
            if answer.get('ok') is False:
                error = Problem(f'飞书 CLI 报错（{" ".join(args[:3])}）：{describe(answer)}')
                continue
            return answer
        raise error

    @contextlib.contextmanager
    def arg_file(self, data):
        """JSON 参数写进工作区里的临时文件，用 @相对路径 传：避开 Windows 命令行的引号转义；CLI 只收工作区内的相对路径。"""
        self.count += 1
        path = self.root / WORK / f'args-{os.getpid()}-{self.count}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        try:
            yield '@' + path.relative_to(self.root).as_posix()
        finally:
            path.unlink(missing_ok=True)

    def whoami(self):
        """CLI 现在以谁的身份工作。没有用户身份（没登录、过期、只有应用身份）一律当失败，不往下走。"""
        answer = self.call('whoami')
        user = answer.get('onBehalfOf') if isinstance(answer.get('onBehalfOf'), dict) else {}
        if answer.get('ok') is False or not user.get('openId'):
            raise Problem(NOT_LOGGED_IN)
        extra = {key: value for source in (answer, user) for key, value in source.items()
                 if 'tenant' in key.lower() and isinstance(value, (str, int))}
        return {'app_id': str(answer.get('appId') or ''), 'open_id': str(user['openId']),
                'name': str(user.get('userName') or ''), 'extra': extra}


def list_folder(fs, folder):
    """一个文件夹的直接子项，读完所有分页。分页游标缺失或重复就停：不完整的清单会让后面误判“云端删了”。"""
    found, page, seen = [], None, set()
    while True:
        query = {'folder_token': folder, 'page_size': 200}
        if page:
            query['page_token'] = page
        with fs.arg_file(query) as params:
            answer = fs.read('drive', 'files', 'list', '--params', params, '--format', 'json')
        body = answer.get('data') if isinstance(answer.get('data'), dict) else answer
        found += [item for item in body.get('files') or [] if isinstance(item, dict)]
        if not body.get('has_more'):
            return found
        page = body.get('next_page_token')
        if not isinstance(page, str) or not page or page in seen:
            raise Problem('列云空间文件夹时分页游标缺失或重复：不把不完整的清单当成完整目录，这次不同步。')
        seen.add(page)


def cloud_tree(fs, folder, registered_at):
    """云端 folder 下面的全部内容：(files, folders, skipped)。
    files: {相对路径: 条目}，文件和在线文档都在里面，条目带 rel 和 parent（所在文件夹 token）；folders: {相对路径: token}，'' 是目标文件夹本身。
    同一个文件夹里重名的文件，优先认登记过的那个，另一个列进 skipped。"""
    files, folders, skipped = {}, {'': folder}, []
    queue = [('', folder)]
    while queue:
        prefix, token = queue.pop(0)
        for raw in list_folder(fs, token):
            name, item_token = str(raw.get('name') or ''), raw.get('token')
            if not item_token or not name or name in ('.', '..') or '/' in name or '\\' in name:
                skipped.append((prefix + (name or '?'), '云端名字为空或含斜杠，没法对应到本机路径'))
                continue
            item = dict(raw, rel=prefix + name, parent=token)
            rel = item['rel']
            if item.get('type') == 'folder':
                if rel in folders:
                    skipped.append((rel, '云端同一文件夹里有重名的文件夹，只认第一个'))
                    continue
                folders[rel] = item_token
                queue.append((rel + '/', item_token))
            elif rel in files:
                keep, other = files[rel], item
                if registered_at.get(item_token) == rel and registered_at.get(keep['token']) != rel:
                    keep, other = item, keep
                files[rel] = keep
                skipped.append((rel, f"云端同一文件夹里有重名的条目，只认{'登记过的' if registered_at.get(keep['token']) == rel else '第一个'}那个，另一个不动"))
            else:
                files[rel] = item
    return files, folders, skipped


def alive_tokens(fs, tokens):
    """按 token 直接问飞书这些文件还在不在：大目录的列表可能漏列，不能凭“列表里没有”就当删了。"""
    tokens, alive = sorted(tokens), set()
    for start in range(0, len(tokens), 100):
        body = {'request_docs': [{'doc_token': token, 'doc_type': 'file'} for token in tokens[start:start + 100]]}
        with fs.arg_file(body) as data:
            answer = fs.read('api', 'POST', '/open-apis/drive/v1/metas/batch_query', '--data', data)
        metas = (answer.get('data') or {}).get('metas') or []
        alive |= {meta.get('doc_token') for meta in metas if isinstance(meta, dict)}
    return alive


# ---------------------------------------------------------------- 登记表

def load_registry(root, folder):
    raw = load_json(Path(root) / MAP, strict=True)
    if raw is None:
        return {'folder_token': '', 'account': {}, 'files': {}}
    if not isinstance(raw, dict) or not isinstance(raw.get('files', []), list):
        raise Problem(f'{MAP} 格式不对，先修好再同步。')
    if raw.get('folder_token') and folder and raw['folder_token'] != folder:
        raise Problem(f'{MAP} 记的是另一个云空间文件夹，和现在的 feishu.folder_token 不一样。'
                      f'换文件夹要先把这份登记表改名备份（同时删掉 {STATE}），再重新 plan。')
    files = {entry['path']: entry for entry in raw.get('files') or [] if isinstance(entry, dict) and entry.get('path')}
    account = raw.get('account') if isinstance(raw.get('account'), dict) else {}
    return {'folder_token': raw.get('folder_token') or '', 'account': account, 'files': files}


def save_registry(root, registry):
    write_json(Path(root) / MAP, {'version': 1, 'folder_token': registry['folder_token'], 'account': registry['account'],
                                  'files': [registry['files'][rel] for rel in sorted(registry['files'])]})


def load_state(root, folder):
    data = load_json(Path(root) / STATE)
    if not isinstance(data, dict) or data.get('folder_token') != folder or not isinstance(data.get('files'), dict):
        return {'folder_token': folder, 'files': {}}
    return data


def save_all(job, registry, state):
    save_registry(job.root, registry)
    write_json(job.root / STATE, state)


def bound(registry, job):
    """这个账号和文件夹是不是已经确认过（plan + approve 过）。"""
    account = registry.get('account') or {}
    return (registry.get('folder_token') == job.folder and account.get('open_id') == job.account['open_id']
            and account.get('app_id') == job.account['app_id'])


def cloud_same(entry, item):
    """云端这份是不是还是上次同步的那份：token 没变，修改时间也没晚于登记的。列表没给修改时间就判断不了，按没改算。"""
    if item.get('token') != entry.get('file_token'):
        return False
    modified = as_int(item.get('modified_time'))
    if not modified:
        return True
    recorded = as_int(entry.get('cloud_modified_time'))
    if recorded:
        return modified <= recorded + TIME_SLACK
    synced = epoch(entry.get('synced_at'))
    return bool(synced) and modified <= synced + UPLOAD_SLACK


# ---------------------------------------------------------------- 一次运行

class Job:
    def __init__(self, root, settings, say):
        self.root, self.settings, self.say = Path(root), settings, say
        self.conf = settings.get('feishu') or {}
        self.local = local_dir(self.root, self.conf)
        self.folder = str(self.conf.get('folder_token') or '').strip()
        self.fs = self.account = None
        self.folders, self.items, self.uploaded = {}, {}, {}
        self.stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')

    def connect(self, require_setup=True):
        if require_setup:
            if not self.conf.get('enabled'):
                raise Problem(NOT_ENABLED)
            if not self.folder:
                raise Problem(NO_FOLDER)
            check_local_dir(self.root, self.settings, self.local)
        self.fs = Feishu(self.root, self.conf)
        self.account = self.fs.whoami()
        return self.account

    def open(self):
        self.connect()
        return load_registry(self.root, self.folder), load_state(self.root, self.folder)

    def is_excluded(self, rel):
        return excluded(self.root, self.settings, self.local, rel)

    def target(self, rel):
        parts = rel.split('/')
        if any(part in ('', '.', '..') for part in parts) or rel_to(self.local, self.local / rel) is None:
            raise Problem(f'路径不安全，跳过：{rel}')
        return self.local / rel

    def relative(self, path):
        return Path(path).relative_to(self.root).as_posix()

    def item(self, token):
        if token not in self.items:
            raise Problem('云端条目找不到了，这次跳过')
        return self.items[token]

    def download(self, token, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        answer = self.fs.call('drive', '+download', '--file-token', token, '--output', self.relative(dest), '--overwrite',
                              timeout=TRANSFER_TIMEOUT)
        if answer.get('ok') is False or not dest.is_file():
            raise Problem('下载没成：' + describe(answer))

    def fetch(self, item):
        """把云端文件下载到本机缓存，返回 (缓存路径, 哈希)。同一个 token、同一个修改时间只下载一次。"""
        token, modified = item['token'], str(item.get('modified_time') or '')
        name = token if SAFE_TOKEN.match(token) else hashlib.sha1(token.encode('utf-8')).hexdigest()
        cached = self.root / CACHE / name
        note = cached.with_name(name + '.json')
        meta = load_json(note) or {}
        if modified and meta.get('modified_time') == modified and cached.is_file() and cached.stat().st_size == meta.get('size'):
            return cached, meta['sha256']
        self.download(token, cached)
        digest = sha256(cached)
        write_json(note, {'modified_time': modified, 'sha256': digest, 'size': cached.stat().st_size})
        return cached, digest

    def ensure_folder(self, rel_dir):
        """云端这条文件夹链没有的就建，返回最里面一层的 token。一定带 --folder-token，不带会建到云空间根目录。"""
        token, built = self.folders[''], ''
        for part in [part for part in rel_dir.split('/') if part]:
            built = f'{built}/{part}' if built else part
            if built not in self.folders:
                answer = self.fs.call('drive', '+create-folder', '--name', part, '--folder-token', token)
                body = answer.get('data') if isinstance(answer.get('data'), dict) else {}
                new = body.get('token') or body.get('folder_token') or (body.get('folder') or {}).get('token')
                if answer.get('ok') is False or not new:
                    raise Problem(f'建云端文件夹“{built}”没成：' + describe(answer))
                self.folders[built] = new
            token = self.folders[built]
        return token


class Result:
    def __init__(self):
        self.actions, self.upkeep = [], []
        self.lists = {key: [] for key, _ in REPORT_LABELS}
        self.meta = {}            # 冲突和云端已删的详情，resolve 用


def action(kind, rel, local_sha=None, item=None, target=None):
    return {'kind': kind, 'path': rel, 'local_sha': local_sha,
            'token': item['token'] if item else None,
            'modified_time': str(item.get('modified_time') or '') if item else None,
            'target': target}


def fingerprint(act):
    """计划里记下的每个动作：动作、路径、本机哈希、云端 token 和修改时间。执行时重新比一遍，任何一项变了就不执行。"""
    return json.dumps([act.get(key) for key in ('kind', 'path', 'local_sha', 'token', 'modified_time', 'target')],
                      ensure_ascii=False)


def classify(job, local_files, files, skipped, registry, state):
    """三方比较：拿基线（这台设备上次同步的版本，没有就用登记表）分别和本机、云端比，决定每个路径做什么。只读，
    需要比内容时把云端文件下载到 .local 缓存。"""
    out = Result()
    out.lists['skipped'] += skipped
    entries, seen = registry['files'], state['files']
    where = {item['token']: rel for rel, item in files.items() if item.get('type') == 'file'}
    registered = {entry.get('file_token') for entry in entries.values() if entry.get('file_token')}
    digests = {}

    def mine_of(rel):
        if rel not in digests:
            digests[rel] = local_digest(local_files[rel], entries.get(rel), seen.get(rel))
        return digests[rel]

    def theirs_of(item):
        try:
            return job.fetch(item)[1]
        except (Problem, OSError):
            return None

    def conflict(rel, reason, item=None):
        meta = {'kind': 'conflict', 'reason': reason, 'cloud_path': item['rel'] if item else rel}
        if item and item.get('type') == 'file':
            try:
                cached, digest = job.fetch(item)
                copy = job.root / CONFLICTS / rel
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cached, copy)
                meta.update(token=item['token'], modified_time=str(item.get('modified_time') or ''), sha256=digest,
                            size=cached.stat().st_size, copy=job.relative(copy))
            except (Problem, OSError) as exc:
                reason += f'（云端版没能存下来：{exc}）'
        out.lists['conflict'].append(f'{rel}（{reason}）')
        out.meta[rel] = dict(meta, reason=reason)

    missing = {}
    for rel, entry in sorted(entries.items()):
        token, base_entry, here = entry.get('file_token'), seen.get(rel), rel in local_files
        if job.is_excluded(rel):
            out.lists['skipped'].append((rel, '现在属于只留本机的范围，不再同步'))
            continue
        if not token:
            out.lists['skipped'].append((rel, '登记里没有云端 token'))
            continue
        moved_to, item = where.get(token), files.get(rel)
        if moved_to and moved_to != rel and moved_to not in entries:          # 云端改了名或挪了位置
            if here and moved_to in local_files:
                reason = f'云端已改名为 {moved_to}，本机两个位置都有文件：先删掉或挪开其中一个'
                out.lists['conflict'].append(f'{rel}（{reason}）')
                out.meta[rel] = {'kind': 'rename', 'reason': reason, 'cloud_path': moved_to}
            elif job.is_excluded(moved_to) or (job.local / moved_to).exists():
                out.lists['skipped'].append((rel, f'云端已改名为 {moved_to}，本机那个位置不能用'))
            else:
                out.actions.append(action('rename', rel, mine_of(rel) if here else None, files[moved_to], target=moved_to))
            continue
        if item is None:
            missing[token] = rel
            continue
        if item.get('type') != 'file':
            conflict(rel, '云端同一位置现在是在线文档或文件夹：不动')
            continue
        same = cloud_same(entry, item)
        if not here:
            if base_entry:
                out.lists['local_deleted'].append(rel)
            else:
                out.actions.append(action('download_new', rel, None, item))
            continue
        mine = mine_of(rel)
        base = base_entry.get('sha256') if base_entry else (mine if mine == entry.get('sha256') else None)
        if base is None:
            theirs = entry.get('sha256') if same else theirs_of(item)
            if theirs == mine:
                out.actions.append(action('adopt', rel, mine, item))
            else:
                conflict(rel, '这台设备没有上次同步的记录，本机内容又和登记的版本不同，分不清哪边新', item)
            continue
        local_changed = mine != base
        cloud_changed = entry.get('sha256') != base if same else True
        if not local_changed and not cloud_changed:
            if not base_entry or base_entry.get('file_token') != item['token']:
                out.upkeep.append(action('track', rel, mine, item))
        elif not cloud_changed:
            out.actions.append(action('upload_update', rel, mine, item))
        elif not local_changed:
            out.actions.append(action('download_update', rel, mine, item))
        else:
            theirs = entry.get('sha256') if same else theirs_of(item)
            if theirs == mine:
                out.actions.append(action('adopt', rel, mine, item))
            elif theirs == base:                   # 云端只是修改时间变了，内容还是上次同步的那份
                out.actions.append(action('upload_update', rel, mine, item))
            else:
                conflict(rel, '上次同步之后两边都改过', item)

    if missing:
        try:
            alive = alive_tokens(job.fs, missing)
        except Problem:
            alive = None
        for token, rel in sorted(missing.items(), key=lambda pair: pair[1]):
            if alive is None:
                out.lists['unlisted'].append(f'{rel}（按 token 也没查成，这次不动）')
            elif token in alive:
                out.lists['unlisted'].append(rel)
            elif rel in local_files:
                out.lists['cloud_deleted'].append(rel)
                out.meta[rel] = {'kind': 'cloud_deleted', 'reason': '云端已删除'}
            else:
                out.upkeep.append(action('forget', rel))             # 两边都没了：登记可以去掉

    for rel in sorted(set(seen) - set(entries)):          # 这台设备同步过、登记表里已经没有的（别的设备确认过云端删了）
        if rel not in local_files:
            out.upkeep.append(action('untrack', rel))
        elif rel not in files:
            out.lists['cloud_deleted'].append(rel)
            out.meta[rel] = {'kind': 'cloud_deleted', 'reason': '登记表里已经没有它（另一台设备确认过云端删了）'}

    for rel in sorted(local_files):                       # 没登记过的本机文件
        if rel in entries or (rel in seen and rel not in files):
            continue
        item, mine = files.get(rel), mine_of(rel)
        if item is None:
            out.actions.append(action('upload_new', rel, mine))
        elif item.get('type') != 'file':
            conflict(rel, '云端同一位置是同名的在线文档或文件夹：不覆盖')
        elif item['token'] in registered:
            continue                                      # 是另一条登记在云端改名后的新位置，上面已经处理
        elif theirs_of(item) == mine:
            out.actions.append(action('adopt', rel, mine, item))
        else:
            conflict(rel, '云端已有同名文件、内容和本机不同，又没有上次同步的记录，分不清哪边新', item)

    for rel, item in sorted(files.items()):               # 没登记过的云端条目
        if item['token'] in registered or rel in local_files:
            continue
        if job.is_excluded(rel) or (job.local / rel).exists():
            out.lists['skipped'].append((rel, '对应的本机位置只留本机（或已被占用），不取回'))
        elif item.get('type') == 'file':
            out.actions.append(action('download_new', rel, None, item))
        else:
            out.lists['cloud_docs'].append(f"{rel}（{item.get('type')}）")
    return out


def analyze(job, registry, state):
    registered_at = {entry.get('file_token'): rel for rel, entry in registry['files'].items() if entry.get('file_token')}
    files, job.folders, skipped = cloud_tree(job.fs, job.folder, registered_at)
    job.items = {item['token']: item for item in files.values()}
    result = classify(job, scan_local(job.root, job.settings, job.local), files, skipped, registry, state)
    write_json(job.root / CONFLICT_INDEX, result.meta)
    return result


# ---------------------------------------------------------------- 执行

def record(registry, state, rel, path, digest, item):
    stat = path.stat()
    old = registry['files'].get(rel) or {}
    entry = {'path': rel, 'file_token': item['token'], 'sha256': digest, 'size': stat.st_size,
             'local_mtime_ns': stat.st_mtime_ns, 'cloud_modified_time': str(item.get('modified_time') or ''),
             'synced_at': now_text()}
    if all(old.get(key) == entry[key] for key in ('file_token', 'sha256', 'cloud_modified_time')):
        entry = old            # 登记的版本没变（比如别的设备刚传的、这台只是取回）：不改登记表，免得私有仓里无谓地多出改动
    registry['files'][rel] = entry
    state['files'][rel] = {'sha256': digest, 'file_token': item['token'], 'size': stat.st_size,
                           'local_mtime_ns': stat.st_mtime_ns}


def do_download(job, act, registry, state):
    item, target = job.item(act['token']), job.target(act['path'])
    if act['kind'] == 'download_new':
        if target.exists():
            raise Problem('本机这个位置已经有文件了，没覆盖')
    elif not target.is_file() or sha256(target) != act['local_sha']:
        raise Problem('出计划后本机又改过，没覆盖')
    cached, digest = job.fetch(item)
    if act['kind'] == 'download_update' and digest == act['local_sha']:
        record(registry, state, act['path'], target, digest, item)      # 云端只是修改时间变了，内容没变
        return
    if target.exists():
        backup = job.root / BACKUPS / job.stamp / act['path']
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    move(cached, target)
    cached.with_name(cached.name + '.json').unlink(missing_ok=True)
    record(registry, state, act['path'], target, digest, item)


def do_rename(job, act, registry, state):
    old, new = act['path'], act['target']
    source, target = job.target(old), job.target(new)
    if source.is_file():
        if target.exists():
            raise Problem('本机新位置已经有文件，没挪')
        if act['local_sha'] and sha256(source) != act['local_sha']:
            raise Problem('出计划后本机又改过，没挪')
        target.parent.mkdir(parents=True, exist_ok=True)
        move(source, target)
    registry['files'][new] = dict(registry['files'].pop(old), path=new)
    if old in state['files']:
        state['files'][new] = state['files'].pop(old)


def do_adopt(job, act, registry, state):
    path = job.target(act['path'])
    if sha256(path) != act['local_sha']:
        raise Problem('出计划后本机又改过，没登记')
    record(registry, state, act['path'], path, act['local_sha'], job.item(act['token']))


def do_upload(job, act, registry, state):
    rel = act['path']
    if job.is_excluded(rel):
        raise Problem('只留本机的路径，不上传')
    source = job.target(rel)
    before = source.stat()
    folder = job.root / STAGING / secrets.token_hex(4)
    staged = folder / source.name           # 暂存真实副本再传：本机文件可能正被别的程序改，传上去的必须就是算过哈希的那份
    folder.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, staged)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise Problem('复制时文件还在被改动，这次没传')
        digest = sha256(staged)
        if digest != act['local_sha']:
            raise Problem('出计划后本机又改过，这次没传')
        if act['kind'] == 'upload_update':
            where = ['--folder-token', job.item(act['token'])['parent'], '--file-token', act['token']]
        else:
            where = ['--folder-token', job.ensure_folder(rel.rpartition('/')[0])]
        answer = job.fs.call('drive', '+upload', '--file', job.relative(staged), *where, timeout=TRANSFER_TIMEOUT)
        token = (answer.get('data') or {}).get('file_token') if isinstance(answer.get('data'), dict) else None
        if answer.get('ok') is False or not token:
            raise Problem('上传没成：' + describe(answer))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    registry['files'][rel] = {'path': rel, 'file_token': token, 'sha256': digest, 'size': before.st_size,
                              'local_mtime_ns': before.st_mtime_ns, 'cloud_modified_time': '', 'synced_at': now_text()}
    state['files'][rel] = {'sha256': digest, 'file_token': token, 'size': before.st_size,
                           'local_mtime_ns': before.st_mtime_ns}
    job.uploaded[token] = rel


def do_track(job, act, registry, state):
    stat = job.target(act['path']).stat()
    state['files'][act['path']] = {'sha256': act['local_sha'], 'file_token': act['token'], 'size': stat.st_size,
                                   'local_mtime_ns': stat.st_mtime_ns}


def do_untrack(job, act, registry, state):
    state['files'].pop(act['path'], None)


def do_forget(job, act, registry, state):
    registry['files'].pop(act['path'], None)
    state['files'].pop(act['path'], None)


HANDLERS = {'download_new': do_download, 'download_update': do_download, 'rename': do_rename, 'adopt': do_adopt,
            'upload_new': do_upload, 'upload_update': do_upload, 'track': do_track, 'untrack': do_untrack,
            'forget': do_forget}


def execute(job, actions, registry, state):
    """一个一个做，哪个没成只记下原因、不影响别的。返回 (做成的, 没成的 [(路径, 原因)])。"""
    order = {kind: number for number, kind in enumerate(RUN_ORDER)}
    done, failed = [], []
    for count, act in enumerate(sorted(actions, key=lambda a: (order[a['kind']], a['path'])), 1):
        try:
            HANDLERS[act['kind']](job, act, registry, state)
            done.append(act)
        except (Problem, OSError) as exc:
            failed.append((act['path'], str(exc)))
            job.say(f"[没成] {KIND_LABELS.get(act['kind'], act['kind'])}：{act['path']}：{exc}")
        if count % SAVE_EVERY == 0:
            save_all(job, registry, state)
    save_all(job, registry, state)
    return done, failed


def finish(job, registry, state):
    """传完重新列一次云端：记下每个刚上传文件的云端修改时间，下次才分得清“云端后来又被改过”。"""
    if job.uploaded:
        try:
            registered_at = {entry.get('file_token'): rel for rel, entry in registry['files'].items()}
            files, _, _ = cloud_tree(job.fs, job.folder, registered_at)
        except Problem as exc:
            job.say(f'[提示] 上传后重新列云端没成（{exc}），下次 plan 再核对。')
        else:
            for token, rel in job.uploaded.items():
                item = files.get(rel)
                if item and item.get('token') == token and rel in registry['files']:
                    registry['files'][rel]['cloud_modified_time'] = str(item.get('modified_time') or '')
                else:
                    job.say(f'[提示] {rel} 上传后在云端列表里没找到，下次 plan 会再核对。')
    save_all(job, registry, state)


# ---------------------------------------------------------------- 输出

def show(say, title, rows, limit=20):
    rows = list(rows)
    if not rows:
        return
    say(f'{title}：{len(rows)} 个')
    for row in rows[:limit]:
        say('    ' + (f'{row[0]}（{row[1]}）' if isinstance(row, tuple) else str(row)))
    if len(rows) > limit:
        say(f'    ……共 {len(rows)} 个')


def show_actions(say, actions, prefix=''):
    for kind, label in KIND_LABELS.items():
        rows = [f"{a['path']} → {a['target']}" if a['kind'] == 'rename' else a['path'] for a in actions if a['kind'] == kind]
        show(say, prefix + label, rows)


def show_lists(say, result):
    for key, label in REPORT_LABELS:
        show(say, label, result.lists[key])


def show_account(job):
    account = job.account
    extra = ''.join(f'，{key}：{value}' for key, value in account['extra'].items())
    job.say(f"飞书账号（CLI 当前登录）：{account['name'] or '（没返回名字）'}（用户 {account['open_id']}，应用 {account['app_id'] or '?'}{extra}）")
    job.say(f"配置档：{job.fs.profile or '（默认）'}")


def summary(job, done, failed, changed, gone, result):
    counts = {}
    for act in done:
        if act['kind'] in KIND_LABELS:
            counts[act['kind']] = counts.get(act['kind'], 0) + 1
    job.say('[完成] ' + ('，'.join(f'{KIND_LABELS[k].split("（")[0]} {v} 个' for k, v in counts.items()) or '没有文件要传'))
    show(job.say, '[没成]', failed)
    show(job.say, '[跳过] 出计划后本机或云端又变了，这次没做（重新 plan 看最新情况）', [a['path'] for a in changed])
    if gone:
        job.say(f'计划里有 {gone} 个动作现在已经不需要或情况变了，没做。')
    show_lists(job.say, result)


# ---------------------------------------------------------------- 命令

def cmd_whoami(job):
    job.connect(require_setup=False)
    show_account(job)
    job.say(f'飞书 CLI：{job.fs.exe}')
    job.say(f"目标文件夹：{job.folder or '（还没设置 feishu.folder_token）'}")
    job.say(f'本机资料目录：{job.local}')
    job.say('请核对：上面是不是你自己要存资料的那个飞书账号？不是的话先用 lark-cli auth login 换成你的账号，再 plan。')
    return 0


def cmd_plan(job):
    registry, state = job.open()
    result = analyze(job, registry, state)
    show_account(job)
    job.say(f'目标文件夹：{job.folder}')
    job.say(f'本机资料目录：{job.local}')
    first = not bound(registry, job)
    if first:
        job.say('[注意] 第一次往这个账号、这个文件夹同步（或者 CLI 换了账号）：先核对上面的账号和文件夹是不是你自己要存资料的地方。')
    show_actions(job.say, result.actions)
    show_lists(job.say, result)
    if result.actions or first:
        plan_id = secrets.token_hex(6)
        write_json(job.root / WORK / f'plan-{plan_id}.json', {
            'id': plan_id, 'created_at': time.time(), 'created': now_text(),
            'account': {key: job.account[key] for key in ('app_id', 'open_id', 'name')},
            'folder_token': job.folder, 'local_path': str(job.local),
            'actions': [fingerprint(act) for act in result.actions]})
        if not result.actions:
            job.say('这次没有文件要同步；确认后只会记下你确认过的账号和文件夹。')
        job.say(f'计划编号：{plan_id}（{PLAN_VALID_SECONDS // 3600} 小时内有效，只能用一次）')
        job.say(f'确认无误后运行：python tools/feishu_sync.py sync --approve {plan_id}')
    elif any(result.lists[key] for key in ('conflict', 'cloud_deleted')):
        job.say('没有可以直接同步的动作；上面列出的要你处理。')
    else:
        job.say('云端和本机一致，没有要同步的。')
    return 1 if result.lists['conflict'] else 0


def cmd_sync_approve(job, plan_id):
    if not PLAN_ID.match(plan_id or ''):
        raise Problem('计划编号格式不对：先运行 plan，用它给出的编号。')
    path = job.root / WORK / f'plan-{plan_id}.json'
    plan = load_json(path)
    if not isinstance(plan, dict):
        raise Problem(f'找不到计划 {plan_id}：编号不对、已经用过，或者被清掉了。先重新运行 plan。')
    if time.time() - float(plan.get('created_at') or 0) > PLAN_VALID_SECONDS:
        raise Problem(f'计划 {plan_id} 已经过期（超过 {PLAN_VALID_SECONDS // 3600} 小时），什么都没做。重新运行 plan。')
    registry, state = job.open()
    planned = plan.get('account') or {}
    if ((planned.get('app_id'), planned.get('open_id')) != (job.account['app_id'], job.account['open_id'])
            or plan.get('folder_token') != job.folder or plan.get('local_path') != str(job.local)):
        raise Problem('现在 CLI 登录的账号、目标文件夹或本机目录和出计划时不一样，什么都没做。重新运行 plan 核对。')
    result = analyze(job, registry, state)
    path.unlink()                                  # 计划只能用一次
    approved = set(plan.get('actions') or [])
    todo = [act for act in result.actions if fingerprint(act) in approved]
    changed = [act for act in result.actions if fingerprint(act) not in approved]
    gone = len(approved - {fingerprint(act) for act in result.actions})
    registry['account'] = {'app_id': job.account['app_id'], 'open_id': job.account['open_id'],
                           'name': job.account['name'], 'approved_at': now_text()}
    registry['folder_token'] = job.folder
    done, failed = execute(job, todo + result.upkeep, registry, state)
    finish(job, registry, state)
    summary(job, done, failed, changed, gone, result)
    return 1 if result.lists['conflict'] or failed or changed else 0


def cmd_sync_scheduled(job):
    registry, state = job.open()
    if not bound(registry, job):
        raise Problem('这个飞书账号和文件夹还没有确认过（或者 CLI 换了账号）：先手动运行 plan 核对账号和文件夹，'
                      '再 sync --approve 计划编号；确认过以后定时同步才会自动执行。')
    result = analyze(job, registry, state)
    done, failed = execute(job, result.actions + result.upkeep, registry, state)
    finish(job, registry, state)
    summary(job, done, failed, [], 0, result)
    return 1 if result.lists['conflict'] or failed else 0


def cmd_pull(job):
    registry, state = job.open()
    result = analyze(job, registry, state)
    todo = [act for act in result.actions if act['kind'] in PULL_KINDS]
    done, failed = execute(job, todo + result.upkeep, registry, state)
    finish(job, registry, state)
    summary(job, done, failed, [], 0, result)
    pending = [act['path'] for act in result.actions if act['kind'] not in PULL_KINDS]
    show(job.say, '本机有改动、等着上传的（pull 不上传，用 plan / sync --approve）', pending)
    return 1 if result.lists['conflict'] or failed else 0


def cmd_verify(job):
    registry, state = job.open()
    entries = registry['files']
    files, _, skipped = cloud_tree(job.fs, job.folder, {e.get('file_token'): rel for rel, e in entries.items()})
    local_files = scan_local(job.root, job.settings, job.local)
    cloud_files = {rel: item for rel, item in files.items() if item.get('type') == 'file'}
    problems, same = [], 0
    temp = job.root / WORK / 'verify' / 'download'
    for rel in sorted(set(local_files) | set(cloud_files) | set(entries)):
        if job.is_excluded(rel):
            continue
        entry, item, here = entries.get(rel), cloud_files.get(rel), rel in local_files
        if not here and not item:
            continue
        if not item:
            problems.append(f'云端没有（或列表里没列出）：{rel}')
            continue
        if not here:
            problems.append(f'本机没有：{rel}')
            continue
        try:
            job.download(item['token'], temp)
            theirs = sha256(temp)
        except Problem as exc:
            problems.append(f'读不回来：{rel}（{exc}）')
            continue
        finally:
            temp.unlink(missing_ok=True)
        mine = sha256(local_files[rel])
        if theirs != mine:
            problems.append(f'内容不同：{rel}')
        elif not entry or entry.get('sha256') != mine or entry.get('file_token') != item['token']:
            problems.append(f'内容一致，但登记表没记上这个版本：{rel}（运行 plan / sync 会补登记）')
        else:
            same += 1
    docs = sum(1 for item in files.values() if item.get('type') != 'file')
    show_account(job)
    job.say(f'[查验] 读回比对一致 {same} 个；云端在线文档 {docs} 个（不比对）；问题 {len(problems)} 条')
    show(job.say, '问题', problems, limit=60)
    show(job.say, '跳过', skipped)
    return 1 if problems else 0


def cmd_resolve(job, paths):
    if not job.folder:
        raise Problem(NO_FOLDER)
    registry = load_registry(job.root, job.folder)
    state = load_state(job.root, job.folder)
    index = load_json(job.root / CONFLICT_INDEX) or {}
    bad = 0
    for raw in paths:
        rel = raw.replace('\\', '/').strip('/')
        meta = index.get(rel)
        if not isinstance(meta, dict):
            job.say(f'{rel}：没有待处理的冲突记录。先运行 plan 看最新情况。')
            bad += 1
            continue
        if meta.get('kind') == 'cloud_deleted':
            registry['files'].pop(rel, None)
            state['files'].pop(rel, None)
            job.say(f'{rel}：已从登记里去掉。下次 plan 会把本机这份列进“上传新文件”（仍要你确认）；不想再同步它，就把它挪出资料目录。')
        elif meta.get('kind') == 'conflict' and meta.get('sha256') and meta.get('cloud_path') == rel:
            registry['files'][rel] = {'path': rel, 'file_token': meta['token'], 'sha256': meta['sha256'],
                                      'size': meta.get('size'), 'local_mtime_ns': None,
                                      'cloud_modified_time': meta.get('modified_time') or '', 'synced_at': now_text()}
            state['files'][rel] = {'sha256': meta['sha256'], 'file_token': meta['token'], 'size': meta.get('size'),
                                   'local_mtime_ns': None}
            job.say(f'{rel}：已记下你看过云端版本。下次 plan 会把本机这份作为新版本上传（仍要你确认）；'
                    f'想用云端版，先把 {meta.get("copy") or CONFLICTS} 复制回本机再 plan。')
        else:
            job.say(f"{rel}：{meta.get('reason') or '这种冲突没法自动记下'}。先按提示处理本机文件，再运行 plan。")
            bad += 1
            continue
        del index[rel]
    save_all(job, registry, state)
    write_json(job.root / CONFLICT_INDEX, index)
    return 1 if bad else 0


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')          # 输出被重定向成本地编码时，个别字符打不出也不崩
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--root', type=Path, default=argparse.SUPPRESS, help='工作区根目录（默认是本脚本的上一级）')
    parser = argparse.ArgumentParser(description='飞书云空间同步：像 Git 一样三方比较，冲突两份都留，从不删除云端。',
                                     parents=[common])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('whoami', parents=[common], help='看 CLI 当前登录的飞书账号')
    sub.add_parser('plan', parents=[common], help='只读：比较差异，出计划和计划编号')
    sync_parser = sub.add_parser('sync', parents=[common], help='按计划执行，或给定时任务用')
    sync_parser.add_argument('--approve', metavar='计划编号', help='执行 plan 给出的这个计划')
    sync_parser.add_argument('--scheduled', action='store_true', help='定时任务用：两个开关都开才执行，只做没有冲突的动作')
    sub.add_parser('pull', parents=[common], help='只取回云端新增和更新的文件')
    sub.add_parser('verify', parents=[common], help='只读：逐个读回云端文件和本机比哈希')
    resolve_parser = sub.add_parser('resolve', parents=[common], help='处理冲突：以本机这份为准，或允许重新上传云端已删的')
    resolve_parser.add_argument('paths', nargs='+', help='相对资料目录的路径')
    args = parser.parse_args(argv)
    root = Path(getattr(args, 'root', ROOT)).resolve()
    scheduled = getattr(args, 'scheduled', False)
    say = make_logger(root, echo=not scheduled)
    job = Job(root, agent_config.load(root), say)
    try:
        if args.command == 'whoami':
            return cmd_whoami(job)
        if args.command == 'verify':
            return cmd_verify(job)
        if args.command == 'sync':
            if scheduled and args.approve:
                raise Problem('--approve 和 --scheduled 二选一。')
            if not scheduled and not args.approve:
                raise Problem('sync 要带计划编号：先运行 plan 看清楚要做什么，再运行 sync --approve 计划编号。')
            if scheduled and not (job.conf.get('enabled') and job.conf.get('auto_sync')):
                say('飞书自动同步未开启，跳过（feishu.enabled 和 feishu.auto_sync 都为 true 才执行）。')
                return 0
        with run_lock(root):
            if args.command == 'plan':
                return cmd_plan(job)
            if args.command == 'pull':
                return cmd_pull(job)
            if args.command == 'resolve':
                return cmd_resolve(job, args.paths)
            return cmd_sync_scheduled(job) if scheduled else cmd_sync_approve(job, args.approve)
    except Problem as exc:
        say(f'[未同步] {exc}')
        return 0 if scheduled else 2


if __name__ == '__main__':
    sys.exit(main())
