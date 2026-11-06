"""测试用的真实 Git 环境：一个 bare 远端 + 两个克隆（两台设备），钩子、设置都是真的。

模板只建一次，每个测试复制一份到新的临时目录（远端用相对地址 ../cloud.git，复制后各自独立）。
仓库用本地 user.name / user.email，不读也不改全局 Git 配置里的身份。
"""
import atexit
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / 'tools'
TOOL_FILES = ('agent_config.py', 'upload_check.py', 'workflow_gate.py', 'install_git_hooks.py')
GITIGNORE = '.device/\n.local/\n.personal/private/\n.personal/files/\n__pycache__/\n*.tmp\n'
LOCAL_CONFIG = (('user.name', 'Test'), ('user.email', 'test@example.com'), ('core.autocrlf', 'false'),
                ('commit.gpgsign', 'false'), ('core.hooksPath', 'tools/git-hooks'))
REMOTE_URL = '../cloud.git'
TEXT = '第一行\n第二行\n第三行\n第四行\n第五行\n'
FILES = {'笔记/规则.md': TEXT, '笔记/别的.md': TEXT}
_template = None


def hook_env():
    """钩子用跑测试的这个 Python；不带外面可能有的任务卡环境变量。"""
    env = dict(os.environ)
    env['AGENT_PYTHON'] = Path(sys.executable).as_posix()
    env['PYTHONIOENCODING'] = 'utf-8'
    env.pop('PROJECT_TASK_CARD', None)
    return env


def git_run(cwd, *args, env=None):
    return subprocess.run(['git', '-c', 'core.quotepath=false', *args], cwd=str(cwd),
                          capture_output=True, env=env or hook_env())


def git(cwd, *args, env=None):
    out = git_run(cwd, *args, env=env)
    if out.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{(out.stderr + out.stdout).decode('utf-8', 'replace')}")
    return out.stdout.decode('utf-8', 'replace').strip()


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)
    return path


def remove_tree(path):
    def retry(func, target, _):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def settings_for(**changes):
    return {'private_remote': REMOTE_URL, **changes}


def build_template():
    base = Path(tempfile.mkdtemp(prefix='agent-git-template-'))
    atexit.register(remove_tree, base)
    git(base, 'init', '--quiet', '--bare', 'cloud.git')
    git(base / 'cloud.git', 'symbolic-ref', 'HEAD', 'refs/heads/main')
    options = [item for key, value in LOCAL_CONFIG for item in ('-c', f'{key}={value}')]
    for name in ('here', 'other'):
        git(base, 'clone', '--quiet', *options, 'cloud.git', name)
        clone = base / name
        git(clone, 'remote', 'set-url', 'origin', REMOTE_URL)
        git(clone, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        write(clone, '.device/device.json', json.dumps({'device_name': 'test-' + name}))
    here = base / 'here'
    write(here, '.gitignore', GITIGNORE)
    (here / 'tools').mkdir()
    for name in TOOL_FILES:
        shutil.copy2(TOOLS / name, here / 'tools' / name)
    shutil.copytree(TOOLS / 'git-hooks', here / 'tools/git-hooks')
    write(here, '.personal/agent.json', json.dumps(settings_for()))
    for rel, text in FILES.items():
        write(here, rel, text)
    git(here, 'add', '-A')
    git(here, 'commit', '--quiet', '-m', 'init')   # 钩子已启用：顺带验证工具源码本身不会被误拦
    git(here, 'push', '--quiet', 'origin', 'main')
    git(base / 'other', 'fetch', '--quiet', 'origin')
    git(base / 'other', 'checkout', '--quiet', '-B', 'main', 'origin/main')
    return base


class Repos:
    """base/cloud.git 是云端；base/here 是本机；base/other 是另一台设备。"""

    def __init__(self):
        global _template
        if _template is None:
            _template = build_template()
        self.base = Path(tempfile.mkdtemp(prefix='agent-git-test-')) / 'w'
        shutil.copytree(_template, self.base)
        self.cloud, self.here, self.other = self.base / 'cloud.git', self.base / 'here', self.base / 'other'

    def cleanup(self):
        remove_tree(self.base.parent)

    def set_settings(self, clone=None, **changes):
        """改本机工作区里的设置（不提交）。"""
        write(clone or self.here, '.personal/agent.json', json.dumps(settings_for(**changes), ensure_ascii=False))

    def write(self, clone, rel, text):
        return write(clone, rel, text)

    def read(self, clone, rel):
        return (Path(clone) / rel).read_bytes().decode('utf-8')

    def commit_other(self, rel, text):
        """另一台设备改一个文件并推到云端，返回云端新提交号。"""
        write(self.other, rel, text)
        git(self.other, 'add', '--', rel)
        git(self.other, 'commit', '--quiet', '-m', '另一台设备改 ' + rel)
        git(self.other, 'push', '--quiet', 'origin', 'main')
        return git(self.other, 'rev-parse', 'HEAD')

    def head(self, clone=None):
        return git(clone or self.here, 'rev-parse', 'HEAD')

    def cloud_head(self):
        return git(self.cloud, 'rev-parse', 'main')

    def commit_count(self, clone=None):
        return int(git(clone or self.here, 'rev-list', '--count', 'HEAD'))

    def committed_files(self, clone=None, rev='HEAD'):
        return sorted(git(clone or self.here, 'show', '--name-only', '--format=', rev).splitlines())

    def staged(self, clone=None):
        return [name for name in git(clone or self.here, 'diff', '--cached', '--name-only').splitlines() if name]
