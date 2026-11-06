#!/usr/bin/env python3
"""读取工作区设置：.personal/agent.json（随你的私有仓同步）和 .device/device.json（只在本机）。

所有工具都从这里拿路径和开关，不在代码里写死任何账号、设备名或绝对路径。
"""
import copy
import fnmatch
import json
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 私有数据都在 .personal/ 下；这些相对路径是各工具共用的约定。
PATHS = {
    'personal': '.personal',
    'state': '.personal/current-state.md',
    'memory': '.personal/memory',
    'open_loops': '.personal/memory/open-loops.md',
    'handoff': '.personal/handoff',
    'chat_exports': '.personal/logs/chat-exports',
    'work_logs': '.personal/logs/work-logs',
    'task_cards': '.personal/logs/task-cards',
    'trace': '.personal/logs/trace',
    'knowledge': '.personal/knowledge',
    'files': '.personal/files',          # 原件资料库（文档、表格、PDF、音视频）：走云盘，不进 Git
    'inbox': '.personal/inbox',          # 副仓（如 Google Drive）收件箱落地处：网页版 AI 先存这里，本机归档时取回、并入 files/
    'private': '.personal/private',      # 只留本机：不进 Git、不上云盘
    'local': '.local',                   # 本机缓存、索引、日志、全量会话备份
}

DEFAULTS = {
    'version': 1,
    # 你自己的【私有】仓库地址。留空时 pre-push 钩子和自动同步一律不推送。
    'private_remote': '',
    # 只留本机的路径（相对工作区根目录）：不提交、不上传云盘、留痕只记目录名。
    'local_only': ['.personal/private/', '.device/', '.local/'],
    # 每次提交前是否要求任务卡（G0–G5、验证结果、工作日志、current-state 都对得上）。
    'require_task_card': False,
    # 云端（飞书 / rclone 云盘）哪些文件不自动上传。Git 一侧永远不收凭据、隐私目录和原件，不受这里影响。
    # .personal/private/ 永远不上云（固定，不可关）；整个文件夹想留在本机，加进上面的 local_only。
    'sync_exclude': {
        'credentials': True,            # .env*、*.pem、*.key、名字含 secret / password / 密码 的文件不上传。想让别的 AI 从云盘取配置才改成 false，自己承担风险
        'media': False,                 # True：音频、视频不上传（体积大或含隐私时）
        'patterns': [],                 # 额外不上传的文件名模式，如 ["*.zip", "合同*"]
    },
    'archive': {
        'sources': ['claude', 'codex', 'workbuddy'],   # 另可加 'doubao'（豆包桌面客户端，由 doubao_export.py 导出）
        'all_projects': False,          # True：把本机所有项目的 AI 会话都收进来
        'local_only_projects': [],      # 这些项目的会话只存 .local/，不进 Git
    },
    # 首次设置（tools/setup_wizard.py）：逐项问用户怎么选，问完 completed 才为 true。
    'setup': {
        'completed': False,
        'answered': [],                 # 已经回答过的问题编号，中断后可以接着问
        'backup': '',                   # 兜底备份：quark / other / none（只记录，备份由网盘客户端自己做）
        'schedule': '',                 # default / custom：定时时间用默认还是自己定（向导用来决定要不要问具体时间）
        'sync_credentials': '',         # exclude / include：凭据类文件要不要上传云盘（include 需用户本人二次确认）
    },
    # Skill 完整副本同步（tools/skill_sync.py）：把 skill/<名字>/ 整个目录放进各家 AI 的技能目录，不放指针。
    'skills': {
        'source': 'skill',
        'skills': 'all',                # 'all' 或名字列表
        'exclude': [],
        'targets': {},                  # 例如 {"claude": "~/.claude/skills", "codex": "~/.codex/skills"}
    },
    # 自动同步（auto_sync.py）只提交这些「归档与状态」路径，代码和配置留给人工提交。
    'auto_sync_paths': ['.personal/logs', '.personal/memory', '.personal/handoff',
                        '.personal/current-state.md', '.personal/feishu-map.json'],
    'schedule': {
        'refresh_times': ['09:00', '18:00'],
        'trace_minutes': 10,
        'auto_sync_time': '18:30',
        'feishu_sync_time': '18:40',
        'cloud_sync_time': '18:45',
    },
    # 推荐的主资料库：你自己的飞书云空间（通过官方飞书 CLI）。不用飞书就保持 enabled=false。
    'feishu': {
        'enabled': False,
        'cli': 'lark-cli',              # 飞书 CLI 可执行文件（名字或完整路径）
        'profile': '',                  # 你自己的 CLI 配置档名；留空用 CLI 默认身份
        'folder_token': '',             # 云空间里存放资料的文件夹 token
        'local_path': '.personal/files',
        'auto_sync': False,             # True：定时任务每天自动同步（仍然冲突不覆盖、不删除）
    },
    # 副仓 / 临时仓：任何 rclone 支持的云盘（Google Drive、OneDrive、WebDAV…），默认只同步收件箱，和飞书主资料库互不干扰。
    # 不用飞书、想让 rclone 云盘当主资料库时，把 local_path 改成 .personal/files。
    'cloud': {
        'remote': '',                   # rclone 远端，例如 gdrive:personal-agent-inbox
        'local_path': '.personal/inbox',
        'max_delete_percent': 10,
    },
}


def merge(base, override):
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}


def load(root=ROOT):
    """返回合并了默认值的设置；文件缺失或损坏时用默认值，不报错。"""
    return merge(DEFAULTS, read_json(Path(root) / '.personal/agent.json'))


def device_name(root=ROOT):
    """本机名称：优先 .device/device.json 的 device_name，否则用主机名。"""
    name = read_json(Path(root) / '.device/device.json').get('device_name') or socket.gethostname()
    return ''.join(ch if ch.isalnum() or ch in '-_' else '-' for ch in name) or 'device'


def path(root, key):
    return Path(root) / PATHS[key]


CREDENTIAL_GLOBS = ['.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx', '*.kdbx',
                    '*secret*', '*password*', '*密码*']
MEDIA_GLOBS = ['*.mp3', '*.m4a', '*.wav', '*.aac', '*.flac', '*.ogg', '*.opus', '*.amr',
               '*.mp4', '*.mov', '*.avi', '*.mkv', '*.wmv', '*.flv', '*.webm', '*.m4v']


def sync_exclude_globs(settings):
    """按 sync_exclude 设置得出“云端不上传”的文件名模式（只看文件名，不看目录）。"""
    rule = (settings or {}).get('sync_exclude') or {}
    globs = list(CREDENTIAL_GLOBS) if rule.get('credentials', True) else []
    if rule.get('media'):
        globs += MEDIA_GLOBS
    return globs + [str(p) for p in (rule.get('patterns') or []) if str(p).strip()]


def sync_excluded(rel, settings):
    """rel 的文件名命中“云端不上传”的模式吗（不区分大小写）。"""
    name = rel.replace('\\', '/').rsplit('/', 1)[-1].lower()
    return any(fnmatch.fnmatch(name, glob.lower()) for glob in sync_exclude_globs(settings))


def is_local_only(rel, settings=None):
    """rel 为相对工作区根目录的路径（/ 分隔）。"""
    rel = rel.replace('\\', '/')
    if rel.startswith('./'):
        rel = rel[2:]
    for prefix in (settings or load()).get('local_only', []):
        prefix = prefix.replace('\\', '/')
        if rel == prefix.rstrip('/') or rel.startswith(prefix.rstrip('/') + '/'):
            return True
    return False
