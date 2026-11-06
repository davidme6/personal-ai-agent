#!/usr/bin/env python3
"""首次设置向导：存储方式、归档范围、定时任务、Skill 同步等，逐项问你怎么选。

每个人的习惯和存储选择不一样，推荐组合只是推荐，所以每一项第一次都要你自己选。
向导只改 .personal/agent.json（和开启私有仓模式时的 .personal/.gitignore），不联网、不装任务、不上传。

  python tools/setup_wizard.py                     # 终端里一项一项问；回车=默认值，输入 s 跳过这一项
  python tools/setup_wizard.py --questions         # 输出当前适用、还没回答的问题（JSON），给 AI 在对话里逐项问
  python tools/setup_wizard.py --answer main_library=feishu --answer feishu_cli=lark-cli
  python tools/setup_wizard.py --answers answers.json   # {"问题编号": 值, ...}
  python tools/setup_wizard.py --status            # 已答 / 待答
  python tools/setup_wizard.py --redo main_library # 把某一项重新设为待答

答案格式：多选用逗号分隔（claude,codex），是否用 yes / no，文字留空就写空字符串（private_remote=）。
批量答案里只要有一项不合格，整批都不写。有的问题要等前一项答完才出现（比如选了飞书才问飞书的细节），
答完一批后再运行 --questions，直到列表为空。
"""
import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path

import agent_config
import init_workspace

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = '.personal/agent.json'

HHMM = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')
RCLONE_REMOTE = re.compile(r'^[\w][\w .-]*:\S*$')
TOKEN = re.compile(r'^[A-Za-z0-9_-]*$')

SKILL_DIRS = {
    'claude': '~/.claude/skills',
    'codex': '~/.codex/skills',
    'workbuddy': '~/.workbuddy/skills',
    'doubao-work': '%LOCALAPPDATA%/DoubaoWork/User Data/Default/.doubaowork/agent_mode/workspace/.user_skills',
}
DEFAULT_LIBRARY_PATHS = ('.personal/files', '.personal/inbox')


# ---------- 本机检测（只看目录在不在，不读内容） ----------

def is_windows():
    return sys.platform == 'win32'


def home_dir():
    return Path(os.environ.get('CHAT_ARCHIVE_HOME') or Path.home())


def appdata_dir():
    return Path(os.environ.get('LOCALAPPDATA') or home_dir() / 'AppData' / 'Local')


def detect_archive_sources():
    home = home_dir()
    found = [name for name, sub in (('claude', '.claude'), ('codex', '.codex'), ('workbuddy', '.workbuddy'))
             if (home / sub).is_dir()]
    if is_windows() and any((appdata_dir() / app).is_dir() for app in ('DoubaoWork', 'Doubao')):
        found.append('doubao')
    return found


def detect_skill_targets():
    home = home_dir()
    found = [name for name, sub in (('claude', '.claude'), ('codex', '.codex'), ('workbuddy', '.workbuddy'))
             if (home / sub).is_dir()]
    if is_windows() and (appdata_dir() / 'DoubaoWork').is_dir():
        found.append('doubao-work')
    return found


# ---------- 设置读写 ----------

def get(settings, dotted):
    node = settings
    for part in dotted.split('.'):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def put(raw, dotted, value):
    parts = dotted.split('.')
    node = raw
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = copy.deepcopy(value)


def effective(raw):
    return agent_config.merge(agent_config.DEFAULTS, raw)


def read_raw(root):
    path = Path(root) / SETTINGS
    if not path.is_file():
        raise FileNotFoundError(f'没有 {SETTINGS}：先运行 python tools/init_workspace.py')
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError(f'{SETTINGS} 不是 JSON 对象')
    return data


def write_raw(root, raw):
    path = Path(root) / SETTINGS
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def main_library(eff):
    if get(eff, 'feishu.enabled') is True:
        return 'feishu'
    if get(eff, 'cloud.local_path') == '.personal/files':
        return 'rclone'
    return 'none'


# ---------- 问题清单 ----------

def opt(value, label, description=''):
    return {'value': value, 'label': label, 'description': description}


def split_list(text):
    return [part.strip() for part in re.split(r'[,，]', text) if part.strip()]


def check_private_remote(value, eff):
    if not value:
        return value
    if not init_workspace.REMOTE.match(value):
        raise ValueError('不像是仓库地址（例如 https://github.com/<你的用户名>/my-agent-private.git 或 git@host:<你>/<仓库>.git）')
    current = str(eff.get('private_remote') or '')
    if current and current != value:
        raise ValueError(f'已经设置了另一个私有仓地址；要换仓库请先手动改 {SETTINGS} 的 private_remote')
    return value


def check_rclone(value, eff):
    if not value:
        return value
    if re.match(r'^[A-Za-z]:[\\/]', value) or not RCLONE_REMOTE.match(value):
        raise ValueError('要写成 rclone 远端名:路径，例如 gdrive:personal-agent-inbox（远端名先用 rclone config 建好）')
    return value


def check_cli(value, eff):
    if not value:
        raise ValueError('不能为空；不确定就用 lark-cli')
    return value


def check_token(value, eff):
    if not TOKEN.match(value):
        raise ValueError('文件夹 token 只含字母、数字、下划线和短横线（在文件夹链接的最后一段）')
    return value


def check_projects(value, eff):
    names = split_list(value)
    bad = [name for name in names if '/' in name or '\\' in name]
    if bad:
        raise ValueError('写项目文件夹的名字，不要写路径：' + '、'.join(bad))
    return names


def check_times(value, eff):
    times = split_list(value)
    if not times:
        raise ValueError('至少写一个时间，例如 09:00,18:00')
    out = []
    for item in times:
        match = HHMM.match(item)
        if not match:
            raise ValueError(f'时间格式不对：{item}（要 HH:MM，例如 09:00）')
        out.append(f'{int(match.group(1)):02d}:{match.group(2)}')
    return out


def check_minutes(value, eff):
    try:
        minutes = int(value)
    except ValueError:
        raise ValueError('要一个整数，例如 10') from None
    if not 1 <= minutes <= 1440:
        raise ValueError('要在 1 到 1440 之间')
    if minutes >= 60 and minutes % 60:
        raise ValueError('60 以上要是 60 的整数倍（macOS / Linux 的 cron 只能这样写）')
    return minutes


def check_skill_targets(values, eff):
    if 'none' in values and len(values) > 1:
        raise ValueError('none 不能和别的选项一起选')
    if not values:
        raise ValueError('至少选一项；都不要就选 none')
    if 'doubao-work' in values and not is_windows():
        raise ValueError('doubao-work 的技能目录只在 Windows 上有，这台电脑选不了')
    return values


SYNC_NOTE = ('先说清楚固定不变的：.personal/private/ 个人隐私文件夹永远不上传到任何云端或 Git，这一条不可关；'
             'Git 一侧永远不收凭据、隐私目录和原件，另有提交前的安全阀，不受下面的选择影响；'
             '对话归档里明显的密钥格式本来就会被打码。下面这几项只管“自动上传到你的云端资料库”。')


def uses_cloud(eff):
    return main_library(eff) != 'none' or bool(str(get(eff, 'cloud.remote') or '').strip())


def credentials_choice(eff):
    choice = get(eff, 'setup.sync_credentials')
    if choice in ('exclude', 'include'):
        return choice
    return 'include' if get(eff, 'sync_exclude.credentials') is False else 'exclude'


def normalize_dir(text):
    text = text.replace('\\', '/').strip()
    while text.startswith('./'):
        text = text[2:]
    return text.rstrip('/') + '/'


def extra_local_only(eff):
    builtin = {normalize_dir(item) for item in agent_config.DEFAULTS['local_only']}
    return [item for item in (eff.get('local_only') or []) if normalize_dir(item) not in builtin]


def merge_local_only(eff, extra):
    merged, seen = [], set()
    for item in list(eff.get('local_only') or []) + list(extra):
        key = normalize_dir(item)
        if key not in seen:
            seen.add(key)
            merged.append(item if item in (eff.get('local_only') or []) else key)
    return merged


def check_paths(value, eff):
    out = []
    for item in split_list(value):
        norm = normalize_dir(item)
        if norm.startswith('/') or re.match(r'^[A-Za-z]:', norm) or '..' in norm.split('/'):
            raise ValueError(f'要写工作区里的相对路径，不能是绝对路径或带 ..：{item}')
        out.append(norm)
    return out


QUESTIONS = [
    {
        'id': 'private_remote', 'type': 'text',
        'title': '状态和记忆要不要推到你自己的私有 Git 仓库？',
        'help': ('填了：规则、状态、记忆、对话文字可以推到你自己的仓库（GitHub / GitLab / Gitee / 自建都行），'
                 '换电脑、网页版 AI 都能接着用；必须是私有仓，推到公开仓等于公开你的全部记录。'
                 '留空：暂时只存本机，换电脑或硬盘坏了就没了，以后随时可以再开。'),
        'default': lambda eff: str(eff.get('private_remote') or ''),
        'current': lambda eff: str(eff.get('private_remote') or ''),
        'check': check_private_remote,
        'apply': lambda raw, v: put(raw, 'private_remote', v) if v else None,
    },
    {
        'id': 'archive_sources', 'type': 'multi',
        'title': '要把哪些 AI 的本机会话导出归档？',
        'help': ('选中的会把本机会话逐字导出成文字存进 .personal/logs/chat-exports/，以后能搜、能回看。'
                 '没装的选了也不会出错，只是导不出东西。doubao 是豆包桌面客户端，只支持 Windows。可以都不选（留空）。'),
        'options': [opt('claude', 'Claude Code'), opt('codex', 'Codex'), opt('workbuddy', 'WorkBuddy'),
                    opt('doubao', '豆包桌面客户端', '仅 Windows')],
        'detect': detect_archive_sources,
        'default': lambda eff: detect_archive_sources() or list(get(eff, 'archive.sources') or []),
        'current': lambda eff: list(get(eff, 'archive.sources') or []),
        'apply': lambda raw, v: put(raw, 'archive.sources', v),
    },
    {
        'id': 'archive_all_projects', 'type': 'yesno',
        'title': '归档范围：只收本工作区的会话，还是本机所有项目的会话都收进来？',
        'help': ('no：只收在本工作区里开的会话，干净。'
                 'yes：本机其他项目的会话也收进来（放在 other-projects/<项目名>/），防止各软件自动清理旧会话后丢失；'
                 '代价是别的项目的内容（可能含工作资料）也会进你的私有仓，下一题可以挑出只留本机的项目。'),
        'default': lambda eff: bool(get(eff, 'archive.all_projects')),
        'current': lambda eff: bool(get(eff, 'archive.all_projects')),
        'apply': lambda raw, v: put(raw, 'archive.all_projects', v),
        'children': ['archive_local_only_projects'],
    },
    {
        'id': 'archive_local_only_projects', 'type': 'text',
        'title': '哪些项目的会话只存本机、不进 Git？',
        'help': ('写项目文件夹名，逗号分隔；这些项目的会话只导到 .local/，不提交、不上云。'
                 '含私人聊天、别人的数据、账号信息的项目建议放这里。留空表示都可以进私有仓。'),
        'when': lambda eff: get(eff, 'archive.all_projects') is True,
        'default': lambda eff: ','.join(get(eff, 'archive.local_only_projects') or []),
        'current': lambda eff: ','.join(get(eff, 'archive.local_only_projects') or []),
        'check': check_projects,
        'apply': lambda raw, v: put(raw, 'archive.local_only_projects', v),
    },
    {
        'id': 'main_library', 'type': 'choice',
        'title': '原件资料（文档、表格、PDF、音视频）的主资料库用什么？',
        'help': ('原件不进 Git，放在 .personal/files/，由这里选的地方做云端主档。'
                 'feishu：用你自己的飞书云空间，需要自己装飞书官方 CLI 并登录，同步前会先出计划给你确认。'
                 'rclone：任何 rclone 支持的云盘（Google Drive、OneDrive、Dropbox、WebDAV、S3 等），需要自己装 rclone 并配置远端。'
                 'none：只放本机，最省事，但电脑坏了就没了（靠下面的兜底备份）。'),
        'options': [opt('feishu', '飞书云空间（推荐）', '需飞书官方 CLI'),
                    opt('rclone', 'rclone 云盘', 'Google Drive / OneDrive / Dropbox / WebDAV / S3 等'),
                    opt('none', '只放本机', '不上云')],
        'recommended': 'feishu',
        'default': main_library,
        'current': main_library,
        'apply': lambda raw, v: apply_main_library(raw, v),
        'children': ['feishu_cli', 'feishu_profile', 'feishu_folder_token', 'feishu_auto_sync',
                     'main_rclone_remote', 'inbox_remote'],
    },
    {
        'id': 'feishu_cli', 'type': 'text',
        'title': '飞书 CLI 的命令名或完整路径？',
        'help': '在 PATH 里能直接运行的写命令名；装在别处的写完整路径。默认 lark-cli。',
        'when': lambda eff: main_library(eff) == 'feishu',
        'default': lambda eff: str(get(eff, 'feishu.cli') or 'lark-cli'),
        'current': lambda eff: str(get(eff, 'feishu.cli') or ''),
        'check': check_cli,
        'apply': lambda raw, v: put(raw, 'feishu.cli', v),
    },
    {
        'id': 'feishu_profile', 'type': 'text',
        'title': '飞书 CLI 用哪个配置档（profile）？',
        'help': ('CLI 里登录了多个账号时，写你要存资料的那个配置档名，避免存进别人的空间。'
                 '只登录了一个账号就留空，用 CLI 默认身份。同步前 feishu_sync.py whoami 会显示实际是谁。'),
        'when': lambda eff: main_library(eff) == 'feishu',
        'default': lambda eff: str(get(eff, 'feishu.profile') or ''),
        'current': lambda eff: str(get(eff, 'feishu.profile') or ''),
        'apply': lambda raw, v: put(raw, 'feishu.profile', v),
    },
    {
        'id': 'feishu_folder_token', 'type': 'text',
        'title': '飞书云空间里存资料的文件夹 token？',
        'help': ('在飞书里建好文件夹，打开后链接最后一段就是 token。可以先留空稍后再填；'
                 '没填之前 feishu_sync.py 会提示你，不会往别处传。'),
        'when': lambda eff: main_library(eff) == 'feishu',
        'default': lambda eff: str(get(eff, 'feishu.folder_token') or ''),
        'current': lambda eff: str(get(eff, 'feishu.folder_token') or ''),
        'check': check_token,
        'apply': lambda raw, v: put(raw, 'feishu.folder_token', v),
    },
    {
        'id': 'feishu_auto_sync', 'type': 'yesno',
        'title': '要不要每天定时自动同步飞书资料库？',
        'help': ('yes：装定时任务时会加一个每天同步飞书的任务（有冲突不覆盖、从不删除云端）；'
                 '但第一次仍要你手动 plan 并确认账号和文件夹，没确认过定时任务会跳过。'
                 'no：只在你手动运行时同步。'),
        'when': lambda eff: main_library(eff) == 'feishu',
        'default': lambda eff: bool(get(eff, 'feishu.auto_sync')),
        'current': lambda eff: bool(get(eff, 'feishu.auto_sync')),
        'apply': lambda raw, v: put(raw, 'feishu.auto_sync', v),
    },
    {
        'id': 'main_rclone_remote', 'type': 'text',
        'title': '主资料库的 rclone 远端和文件夹？',
        'help': ('写成 远端名:文件夹，例如 gdrive:personal-agent-files（远端名先用 rclone config 建好）。'
                 '可以先留空稍后再填，没填之前不会同步。'),
        'when': lambda eff: main_library(eff) == 'rclone',
        'default': lambda eff: str(get(eff, 'cloud.remote') or ''),
        'current': lambda eff: str(get(eff, 'cloud.remote') or ''),
        'check': check_rclone,
        'apply': lambda raw, v: (put(raw, 'cloud.remote', v), put(raw, 'cloud.local_path', '.personal/files')),
    },
    {
        'id': 'inbox_remote', 'type': 'text',
        'title': '要不要一个副仓 / 收件箱（推荐 Google Drive）？',
        'help': ('网页版 AI（比如网页版 ChatGPT）连不上你的主资料库时，先把东西存进这个云盘文件夹，'
                 '本机归档时取回到 .personal/inbox/。写成 rclone 远端名:文件夹，例如 gdrive:personal-agent-inbox；'
                 '不用网页版 AI 存东西就留空。'),
        'when': lambda eff: main_library(eff) != 'rclone',
        'default': lambda eff: str(get(eff, 'cloud.remote') or ''),
        'current': lambda eff: str(get(eff, 'cloud.remote') or ''),
        'check': check_rclone,
        'apply': lambda raw, v: (put(raw, 'cloud.remote', v), put(raw, 'cloud.local_path', '.personal/inbox')),
    },
    {
        'id': 'sync_credentials', 'type': 'choice',
        'title': '密钥、令牌这类凭据文件要不要自动上传到你的云端资料库？',
        'help': SYNC_NOTE + ('凭据类指 .env、*.pem、*.key、名字含 secret / password / 密码 的文件。'
                             'exclude：不上传（推荐；丢了可以重新生成，也少一个泄露的地方）。'
                             'include：上传，好处是别的 AI、别的电脑能从你自己的云盘取到配置；'
                             '代价是云盘账号一旦被人登录就等于凭据泄露，账号务必开两步验证。选 include 还会再确认一次。'),
        'options': [opt('exclude', '不上传（推荐）'), opt('include', '上传', '需要再确认一次')],
        'recommended': 'exclude',
        'when': lambda eff: uses_cloud(eff),
        'default': lambda eff: credentials_choice(eff),
        'current': lambda eff: credentials_choice(eff),
        # include 先不放开，等下一题确认了才把 credentials 改成 false
        'apply': lambda raw, v: (put(raw, 'setup.sync_credentials', v),
                                 put(raw, 'sync_exclude.credentials', True) if v == 'exclude' else None),
        'children': ['sync_credentials_confirm'],
    },
    {
        'id': 'sync_credentials_confirm', 'type': 'yesno',
        'title': '确认要把凭据类文件上传到云盘吗？',
        'help': SYNC_NOTE + ('yes：.env、*.pem、*.key 等凭据会随资料库上传到你的云盘。'
                             'no：保持不上传（等于上一题选 exclude）。'),
        'when': lambda eff: uses_cloud(eff) and get(eff, 'setup.sync_credentials') == 'include',
        'default': lambda eff: False,
        'current': lambda eff: get(eff, 'sync_exclude.credentials') is False,
        'apply': lambda raw, v: (put(raw, 'sync_exclude.credentials', not v),
                                 None if v else put(raw, 'setup.sync_credentials', 'exclude')),
    },
    {
        'id': 'sync_media', 'type': 'yesno',
        'title': '音频、视频文件要不要排除出自动上传？',
        'help': SYNC_NOTE + ('yes：音视频只留本机，不自动上传（体积大、录音录像可能含隐私时选这个）。'
                             'no：和其他原件一样上传，换电脑也能拿到。'),
        'when': lambda eff: uses_cloud(eff),
        'default': lambda eff: bool(get(eff, 'sync_exclude.media')),
        'current': lambda eff: bool(get(eff, 'sync_exclude.media')),
        'apply': lambda raw, v: put(raw, 'sync_exclude.media', v),
    },
    {
        'id': 'sync_exclude_paths', 'type': 'text',
        'title': '还有哪些文件夹要留在本机、不自动上传也不归档？',
        'help': SYNC_NOTE + ('写相对工作区根目录的文件夹，逗号分隔，例如 .personal/files/<某个文件夹>/；'
                             '会追加到 local_only（原有的保留，要删请手动改设置），这些文件夹不提交、不上传。留空表示不再追加。'),
        'when': lambda eff: uses_cloud(eff),
        'default': lambda eff: ','.join(extra_local_only(eff)),
        'current': lambda eff: ','.join(extra_local_only(eff)),
        'check': check_paths,
        'apply': lambda raw, v: put(raw, 'local_only', merge_local_only(effective(raw), v)),
    },
    {
        'id': 'sync_exclude_patterns', 'type': 'text',
        'title': '还有哪些文件名模式不自动上传？',
        'help': SYNC_NOTE + ('逗号分隔的通配符，只看文件名，例如 *.zip,合同*。留空表示没有。'),
        'when': lambda eff: uses_cloud(eff),
        'default': lambda eff: ','.join(get(eff, 'sync_exclude.patterns') or []),
        'current': lambda eff: ','.join(get(eff, 'sync_exclude.patterns') or []),
        'check': lambda value, eff: split_list(value),
        'apply': lambda raw, v: put(raw, 'sync_exclude.patterns', v),
    },
    {
        'id': 'require_task_card', 'type': 'yesno',
        'title': '提交前要不要强制任务卡？',
        'help': ('yes：每次提交都要有任务卡，写清做了什么、怎么验证的，适合经常让 AI 改代码、改结构的人，代价是提交多一步。'
                 'no：不强制，日常归档更顺手。'),
        'default': lambda eff: bool(eff.get('require_task_card')),
        'current': lambda eff: bool(eff.get('require_task_card')),
        'apply': lambda raw, v: put(raw, 'require_task_card', v),
    },
    {
        'id': 'schedule', 'type': 'choice',
        'title': '定时任务的时间用默认的，还是自己定？',
        'help': ('default：每天 09:00、18:00 归档对话和更新索引，每 10 分钟记一次文件变化。'
                 'custom：接下来让你填自己的时间。这里只记设置，装不装定时任务由你之后运行 install_schedule.py 决定。'),
        'options': [opt('default', '用默认时间', '09:00、18:00 归档；每 10 分钟留痕'),
                    opt('custom', '自己定', '')],
        'default': lambda eff: str(get(eff, 'setup.schedule') or 'default'),
        'current': lambda eff: str(get(eff, 'setup.schedule') or ''),
        'apply': lambda raw, v: apply_schedule(raw, v),
        'children': ['refresh_times', 'trace_minutes'],
    },
    {
        'id': 'refresh_times', 'type': 'text',
        'title': '每天几点归档对话、更新索引？',
        'help': 'HH:MM，多个用逗号分隔，例如 09:00,18:00。电脑没开机的时间点，Windows 开机后会补跑，macOS / Linux 不补。',
        'when': lambda eff: get(eff, 'setup.schedule') == 'custom',
        'default': lambda eff: ','.join(get(eff, 'schedule.refresh_times') or []),
        'current': lambda eff: ','.join(get(eff, 'schedule.refresh_times') or []),
        'check': check_times,
        'apply': lambda raw, v: put(raw, 'schedule.refresh_times', v),
    },
    {
        'id': 'trace_minutes', 'type': 'text',
        'title': '每隔几分钟记录一次文件变化（留痕）？',
        'help': '1–1440 的整数；60 以上要是 60 的整数倍。越短记得越细，越长越省电。',
        'when': lambda eff: get(eff, 'setup.schedule') == 'custom',
        'default': lambda eff: str(get(eff, 'schedule.trace_minutes') or 10),
        'current': lambda eff: str(get(eff, 'schedule.trace_minutes') or ''),
        'check': check_minutes,
        'apply': lambda raw, v: put(raw, 'schedule.trace_minutes', v),
    },
    {
        'id': 'skill_targets', 'type': 'multi',
        'title': 'Skill 要同步到哪几家 AI 的技能目录？',
        'help': ('项目里的 skill/ 是原版，同步时把完整副本放进各家 AI 的技能目录（不放指针：指针要靠项目所在的电脑开着、路径不变才能用）。'
                 '同步先比差异、覆盖前备份、从不删除。选 none 就不同步。doubao-work 只支持 Windows。'
                 '这一项会整体替换 skills.targets。'),
        'options': [opt('claude', 'Claude', SKILL_DIRS['claude']), opt('codex', 'Codex', SKILL_DIRS['codex']),
                    opt('workbuddy', 'WorkBuddy', SKILL_DIRS['workbuddy']),
                    opt('doubao-work', '豆包桌面客户端', '仅 Windows'),
                    opt('none', '不同步', '')],
        'detect': detect_skill_targets,
        'default': lambda eff: (list((get(eff, 'skills.targets') or {}).keys())
                                or detect_skill_targets() or ['none']),
        'current': lambda eff: list((get(eff, 'skills.targets') or {}).keys()),
        'check': check_skill_targets,
        'apply': lambda raw, v: put(raw, 'skills.targets',
                                    {} if v == ['none'] else {name: SKILL_DIRS[name] for name in v}),
    },
    {
        'id': 'backup', 'type': 'choice',
        'title': '有没有兜底备份？',
        'help': ('同步不是备份：误删、改坏的内容会被同步到所有地方。兜底备份指另有一份能找回旧版本的拷贝。'
                 'quark：用夸克等网盘客户端自动备份整个项目文件夹（推荐，设一次就不用管）。'
                 'other：你有别的方式（移动硬盘、系统备份、别的网盘）。'
                 'none：暂时没有，私有仓和云盘丢了就找不回。向导只记下你的选择，备份由你自己的工具来做。'),
        'options': [opt('quark', '网盘客户端自动备份（推荐，如夸克）'), opt('other', '别的方式'), opt('none', '暂时没有')],
        'recommended': 'quark',
        'default': lambda eff: str(get(eff, 'setup.backup') or 'none'),
        'current': lambda eff: str(get(eff, 'setup.backup') or ''),
        'apply': lambda raw, v: put(raw, 'setup.backup', v),
    },
]
BY_ID = {q['id']: q for q in QUESTIONS}


def apply_main_library(raw, value):
    put(raw, 'feishu.enabled', value == 'feishu')
    eff = effective(raw)
    if value == 'rclone':
        if get(eff, 'cloud.local_path') != '.personal/files':
            put(raw, 'cloud.remote', '')
        put(raw, 'cloud.local_path', '.personal/files')
    elif get(eff, 'cloud.local_path') == '.personal/files':
        # 之前 rclone 是主资料库：远端换了角色，清掉让收件箱一题重新问
        put(raw, 'cloud.remote', '')
        put(raw, 'cloud.local_path', '.personal/inbox')


def apply_schedule(raw, value):
    put(raw, 'setup.schedule', value)
    if value == 'default':
        put(raw, 'schedule.refresh_times', agent_config.DEFAULTS['schedule']['refresh_times'])
        put(raw, 'schedule.trace_minutes', agent_config.DEFAULTS['schedule']['trace_minutes'])


def applicable(eff):
    return [q for q in QUESTIONS if q.get('when', lambda _: True)(eff)]


def answered(eff):
    return list(get(eff, 'setup.answered') or [])


def pending(eff):
    done = set(answered(eff))
    return [q for q in applicable(eff) if q['id'] not in done]


# ---------- 答案解析与应用 ----------

def parse(question, raw_value, eff):
    """把字符串 / 列表 / 布尔值转成设置里存的值；不合格抛 ValueError。"""
    kind = question['type']
    if kind == 'yesno':
        if isinstance(raw_value, bool):
            return raw_value
        text = str(raw_value).strip().lower()
        if text in ('yes', 'y'):
            return True
        if text in ('no', 'n'):
            return False
        raise ValueError('要 yes 或 no')
    if kind == 'multi':
        values = list(raw_value) if isinstance(raw_value, (list, tuple)) else split_list(str(raw_value))
        allowed = [o['value'] for o in question['options']]
        bad = [v for v in values if v not in allowed]
        if bad:
            raise ValueError(f"不认识：{'、'.join(map(str, bad))}（可选：{', '.join(allowed)}）")
        values = [v for v in allowed if v in values]   # 去重并按选项顺序
        return question['check'](values, eff) if 'check' in question else values
    if isinstance(raw_value, (list, dict, bool)) or raw_value is None:
        raise ValueError('要一个文字值')
    text = str(raw_value).strip()
    if kind == 'choice':
        allowed = [o['value'] for o in question['options']]
        if text not in allowed:
            raise ValueError(f"不认识：{text}（可选：{', '.join(allowed)}）")
        return text
    return question['check'](text, eff) if 'check' in question else text


def stage(raw, answers):
    """在副本上按问题顺序应用答案。返回 (新的 raw, 错误列表)。"""
    raw = copy.deepcopy(raw)
    errors = []
    unknown = [key for key in answers if key not in BY_ID]
    for key in unknown:
        errors.append(f'{key}：没有这个问题（可选：{", ".join(BY_ID)}）')
    for question in QUESTIONS:
        qid = question['id']
        if qid not in answers:
            continue
        eff = effective(raw)
        if not question.get('when', lambda _: True)(eff):
            errors.append(f'{qid}：按目前的选择这一项不适用（先回答它前面的问题，或改前面的选择）')
            continue
        try:
            value = parse(question, answers[qid], eff)
        except ValueError as exc:
            errors.append(f'{qid}：{exc}')
            continue
        children = [BY_ID[c] for c in question.get('children', [])]
        before = {c['id'] for c in children if c.get('when', lambda _: True)(eff)}
        question['apply'](raw, value)
        new_eff = effective(raw)
        after = {c['id'] for c in children if c.get('when', lambda _: True)(new_eff)}
        # 追问的适用与否变了（比如换了主资料库），这些追问要重新问
        done = [item for item in answered(new_eff) if item not in before ^ after]
        if qid not in done:
            done.append(qid)
        put(raw, 'setup.answered', done)
    eff = effective(raw)
    put(raw, 'setup.completed', not pending(eff))
    return raw, errors


def apply_answers(root, answers):
    """校验全部答案；全部合格才写入。返回错误列表（空表示已写入）。"""
    raw = read_raw(root)
    new_raw, errors = stage(raw, answers)
    if errors:
        return errors
    write_raw(root, new_raw)
    remote = str(answers.get('private_remote') or '').strip()
    if remote:
        init_workspace.enable_private_repo(Path(root), remote)
    return []


def redo(root, qid):
    if qid not in BY_ID:
        raise ValueError(f'没有这个问题：{qid}（可选：{", ".join(BY_ID)}）')
    raw = read_raw(root)
    done = [item for item in answered(effective(raw)) if item != qid]
    put(raw, 'setup.answered', done)
    put(raw, 'setup.completed', not pending(effective(raw)))
    write_raw(root, raw)


# ---------- 输出 ----------

def show_value(value):
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, list):
        return ','.join(map(str, value))
    return str(value)


def question_json(question, eff):
    item = {
        'id': question['id'], 'title': question['title'], 'help': question['help'], 'type': question['type'],
        'options': question.get('options', []),
        'default': show_value(question['default'](eff)),
        'detected': question['detect']() if 'detect' in question else None,
        'current': show_value(question['current'](eff)),
    }
    if question.get('recommended'):
        item['recommended'] = question['recommended']
    if question['type'] == 'yesno':
        item['options'] = [opt('yes', '是'), opt('no', '否')]
    return item


def questions_payload(eff):
    items = [question_json(q, eff) for q in pending(eff)]
    return {
        'completed': not items,
        'questions': items,
        'note': ('一次只问一项，讲清选项和代价，不替用户选；用 --answer 问题编号=值 写入。'
                 '有的问题要前一项答完才出现，写入后再运行 --questions，直到列表为空。'),
    }


def next_steps(eff):
    steps = []
    remote = str(eff.get('private_remote') or '')
    if remote:
        steps += ['私有仓模式已开启。确认这个仓库是 Private 后，依次运行：',
                  '  git remote rename origin upstream   # 模板地址留着拉更新；本来就是你自己仓库的可跳过',
                  f'  git remote add origin {remote}',
                  '  python tools/install_git_hooks.py',
                  '  git add .personal && git commit -m "初始化我的数字分身"',
                  '  git push -u origin main']
    else:
        steps.append('python tools/install_git_hooks.py        # 装安全阀：提交前查密钥，没设私有仓时不推送')
    steps += ['python tools/install_schedule.py --dry-run  # 先看会装哪些定时任务，确认后去掉 --dry-run 再运行']
    if get(eff, 'skills.targets'):
        steps.append('python tools/skill_sync.py plan            # 先看 Skill 同步会做什么，确认后 apply')
    library = main_library(eff)
    if library == 'feishu':
        steps.append('装好飞书官方 CLI 并用你自己的账号登录')
        if not get(eff, 'feishu.folder_token'):
            steps.append(f'在 {SETTINGS} 的 feishu.folder_token 填上资料文件夹的 token（或 --redo feishu_folder_token）')
        steps += ['python tools/feishu_sync.py whoami         # 确认登录的是你要存的那个账号',
                  'python tools/feishu_sync.py plan           # 只出计划，确认后 sync --approve <计划编号>']
    if get(eff, 'cloud.remote'):
        steps += ['装好 rclone 并用 rclone config 建好远端',
                  'python tools/cloud_sync.py init            # 第一次先预览，确认后 init --yes']
    elif library == 'rclone':
        steps.append(f'在 {SETTINGS} 的 cloud.remote 填上 rclone 远端（或 --redo main_rclone_remote）')
    if get(eff, 'setup.backup') == 'none':
        steps.append('建议加一个兜底备份（网盘客户端自动备份整个项目文件夹即可）')
    return steps


def print_next_steps(eff):
    print('\n首次设置完成。下一步（向导不会替你运行，请自己确认后执行）：')
    for line in next_steps(eff):
        print('  ' + line)


def print_status(eff):
    done = set(answered(eff))
    for question in applicable(eff):
        mark = '已答' if question['id'] in done else '待答'
        print(f"  [{mark}] {question['id']:<28} {show_value(question['current'](eff))}")
    if pending(eff):
        print(f'还有 {len(pending(eff))} 项待答：python tools/setup_wizard.py（或 --questions）')
    else:
        print('首次设置已完成。')


# ---------- 终端交互 ----------

def prompt_value(question, eff, text):
    """把终端输入转成答案：编号换成选项值；空输入用默认值。"""
    if text == '':
        return question['default'](eff)
    if text == '-' and question['type'] in ('text', 'multi'):
        return ''
    if question['type'] in ('choice', 'multi'):
        values = [o['value'] for o in question['options']]

        def pick(part):
            return values[int(part) - 1] if part.isdigit() and 1 <= int(part) <= len(values) else part
        if question['type'] == 'choice':
            return pick(text)
        return ','.join(pick(part) for part in split_list(text))
    if question['type'] == 'yesno' and text in ('1', '2'):
        return text == '1'
    return text


def show_question(question, eff, index, total):
    print(f"\n[{index}/{total}] {question['title']}  ({question['id']})")
    print('  ' + question['help'])
    if question['type'] in ('choice', 'multi'):
        detected = question['detect']() if 'detect' in question else []
        for number, option in enumerate(question['options'], 1):
            extra = f" — {option['description']}" if option['description'] else ''
            seen = '  [本机检测到]' if option['value'] in (detected or []) else ''
            print(f"    {number}. {option['value']}  {option['label']}{extra}{seen}")
    elif question['type'] == 'yesno':
        print('    1. yes    2. no')
    hint = {'multi': '多选用逗号分隔编号', 'text': '直接输入文字'}.get(question['type'], '输入编号或值')
    default = show_value(question['default'](eff))
    print(f"  当前：{show_value(question['current'](eff)) or '（空）'}；默认：{default or '（空）'}")
    clear = '；- =留空' if question['type'] in ('text', 'multi') else ''
    print(f'  {hint}；回车=默认值{clear}；s=跳过（保持当前值，下次再问）')


def interactive(root):
    if sys.stdin is None or not sys.stdin.isatty():
        print('这里不是交互终端，向导没法逐项提问。请改用：\n'
              '  python tools/setup_wizard.py --questions          # 看要回答的问题\n'
              '  python tools/setup_wizard.py --answer 问题编号=值   # 写入答案')
        return 2
    skipped = set()
    print('首次设置：一项一项选。推荐组合只是推荐，每项都可以换或者不用。')
    while True:
        eff = effective(read_raw(root))
        todo = [q for q in pending(eff) if q['id'] not in skipped]
        if not todo:
            break
        question = todo[0]
        show_question(question, eff, len(answered(eff)) + len(skipped) + 1,
                      len(applicable(eff)))
        while True:
            try:
                text = input('> ').strip()
            except (EOFError, KeyboardInterrupt):
                print('\n已中断。已回答的都保存了，下次运行接着问。')
                return 1
            if text.lower() == 's':
                skipped.add(question['id'])
                break
            errors = apply_answers(root, {question['id']: prompt_value(question, eff, text)})
            if not errors:
                break
            for error in errors:
                print('  ' + error)
    eff = effective(read_raw(root))
    if pending(eff):
        print(f'\n还有 {len(pending(eff))} 项跳过了，没有完成首次设置。之后再运行本向导接着答。')
        return 0
    print_next_steps(eff)
    return 0


# ---------- 入口 ----------

def parse_answer_args(items):
    answers = {}
    for item in items or []:
        if '=' not in item:
            raise ValueError(f'--answer 要写成 问题编号=值：{item}')
        key, value = item.split('=', 1)
        answers[key.strip()] = value
    return answers


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=ROOT)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--questions', action='store_true', help='输出适用且未回答的问题（JSON）')
    group.add_argument('--status', action='store_true', help='显示已答 / 待答')
    group.add_argument('--redo', metavar='问题编号', help='把某一项重新设为待答')
    parser.add_argument('--answer', action='append', metavar='问题编号=值', help='写入答案，可重复')
    parser.add_argument('--answers', type=Path, metavar='文件.json', help='从 JSON 文件读答案 {问题编号: 值}')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except (OSError, ValueError):
            pass
    try:
        if args.answer or args.answers:
            answers = parse_answer_args(args.answer)
            if args.answers:
                data = json.loads(args.answers.read_text(encoding='utf-8-sig'))
                if not isinstance(data, dict):
                    raise ValueError('--answers 文件要是 {"问题编号": 值} 形式的 JSON 对象')
                answers.update(data)
            errors = apply_answers(root, answers)
            if errors:
                print('没有写入任何设置，下面这些不合格：')
                for error in errors:
                    print('  ' + error)
                return 1
            eff = effective(read_raw(root))
            print(f'已写入 {len(answers)} 项。')
            left = pending(eff)
            if left:
                print('还有待答：' + '、'.join(q['id'] for q in left) + '（运行 --questions 看详情）')
            else:
                print_next_steps(eff)
            return 0
        if args.questions:
            payload = questions_payload(effective(read_raw(root)))
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
            return 0
        if args.status:
            print_status(effective(read_raw(root)))
            return 0
        if args.redo:
            redo(root, args.redo)
            print(f'{args.redo} 已设为待答。')
            return 0
        return interactive(root)
    except (OSError, ValueError) as exc:
        print(f'[停止] {exc}')
        return 2


if __name__ == '__main__':
    sys.exit(main())
