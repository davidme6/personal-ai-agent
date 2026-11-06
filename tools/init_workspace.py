#!/usr/bin/env python3
"""Create the private runtime beside the public framework without overwriting user files.

python tools/init_workspace.py                          # 本机初始化（只建缺的，不覆盖）
python tools/init_workspace.py --private-repo <url>     # 另外开启“私有仓模式”：状态和记忆可提交到你自己的私有仓
"""
import argparse
import json
import re
import shutil
import socket
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

DIRECTORIES = (
    '.personal/projects/example-project/logs', '.personal/knowledge', '.personal/memory',
    '.personal/handoff', '.personal/logs/chat-exports', '.personal/logs/work-logs',
    '.personal/logs/task-cards', '.personal/logs/trace', '.personal/files', '.personal/inbox', '.personal/private',
    '.device/secrets', '.local',
)
TEMPLATES = (
    ('templates/profile.md', '.personal/profile.md'),
    ('templates/current-state.md', '.personal/current-state.md'),
    ('templates/open-loops.md', '.personal/memory/open-loops.md'),
    ('config/project-registry.example.json', '.personal/project-registry.json'),
    ('config/agent.example.json', '.personal/agent.json'),
    ('templates/project-entry.md', '.personal/projects/example-project/ENTRY.md'),
    ('templates/project-state.md', '.personal/projects/example-project/STATE.md'),
)
# 私有仓模式：.personal/ 里的文字状态进你的私有仓；原件、收件箱和只留本机的目录照样不进 Git。
# 先放开一切（覆盖根目录 .gitignore 对 .personal/ 的忽略），再把凭据、临时文件和不该进 Git 的目录重新挡住。
# 顺序有意义：后面的规则覆盖前面的。目录名前的 / 表示只挡 .personal/ 下这一层。
PRIVATE_REPO_IGNORE = """# 私有仓模式（由 tools/init_workspace.py --private-repo 生成）
!*
!*/
/private/
/files/
/inbox/
.env
.env.*
*.pem
*.key
*.p12
*.pfx
*.kdbx
*password*
*secret*
*.db
*.sqlite
*.tmp
*.bak
__pycache__/
*.py[cod]
"""
REMOTE = re.compile(r'^(?:https?://[^\s/]+(?::\d+)?/[^\s]+|ssh://[^\s]+|[\w.-]+@[^\s:]+:[^\s]+)$')


def copy_new(source, target):
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def device_label():
    name = socket.gethostname()
    return ''.join(ch if ch.isalnum() or ch in '-_' else '-' for ch in name) or 'device'


def initialize(root):
    root = root.resolve()
    if not (root / 'AGENTS.md').is_file():
        raise ValueError('AGENTS.md not found; run this tool from a valid framework copy.')
    for name in DIRECTORIES:
        (root / name).mkdir(parents=True, exist_ok=True)
    created = []
    for source, target in TEMPLATES:
        if copy_new(root / source, root / target):
            created.append(target)
    device_file = root / '.device/device.json'
    if not device_file.exists():
        device_file.write_text(json.dumps({
            'version': 1,
            'workspace_root': str(root),
            'device_name': device_label(),
            'note': 'Machine-local file. Do not sync or commit.'
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        created.append('.device/device.json')
    return created


def enable_private_repo(root, url):
    """记下私有仓地址并让 .personal/ 的文字状态可以提交。已设了别的地址时拒绝改。"""
    root = root.resolve()
    url = url.strip()
    if not REMOTE.match(url):
        raise ValueError(f'不像是仓库地址：{url}')
    settings_file = root / '.personal/agent.json'
    settings = json.loads(settings_file.read_text(encoding='utf-8-sig'))
    current = settings.get('private_remote', '')
    if current and current != url:
        raise ValueError(f'已设置为 {current}；要换仓库请先手动改 .personal/agent.json')
    settings['private_remote'] = url
    settings_file.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    ignore = root / '.personal/.gitignore'
    if not ignore.exists():
        ignore.write_text(PRIVATE_REPO_IGNORE, encoding='utf-8')
    return url


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--private-repo', metavar='URL', help='你自己的【私有】仓库地址')
    args = parser.parse_args()
    try:
        created = initialize(args.root)
        url = enable_private_repo(args.root, args.private_repo) if args.private_repo else None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, f'Initialization failed: {exc}\n')
    print(f'Workspace ready. Created {len(created)} missing files; existing files were preserved.')
    print('下一步：python tools/setup_wizard.py（逐项选择存储方式等；用 AI 的话，让 AI 按 AGENTS.md 的“首次设置”逐项问你）')
    if url:
        print('私有仓模式已开启。确认这个仓库在 GitHub 上是 Private 后，依次运行：')
        print('  git remote rename origin upstream   # 模板地址留着拉更新；本来就是你自己仓库的可跳过')
        print(f'  git remote add origin {url}')
        print('  python tools/install_git_hooks.py')
        print('  git add .personal && git commit -m "初始化我的数字分身"')
        print('  git push -u origin main')


if __name__ == '__main__':
    main()
