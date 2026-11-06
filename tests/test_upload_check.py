"""tools/upload_check.py：远端必须是私有仓、密钥和身份证号拦截、路径边界、钩子实际生效。"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'tools'))
import install_git_hooks  # noqa: E402
import upload_check  # noqa: E402
from git_fixture import Repos, git, git_run, write  # noqa: E402

PRIVATE = 'https://github.com/example-user/notes-private'
# 测试数据在运行时拼出来，源码里不出现完整的假密钥、假身份证号、假手机号。
SECRET = 'sk-' + 'A1b2C3d4' * 4
PHONE = '1' + '38' + '0013' + '8000'


def fake_id(valid=True):
    prefix = '110101' + '1990' + '0307' + '123'
    check = upload_check.ID_CHECK[sum(int(c) * w for c, w in zip(prefix, upload_check.ID_WEIGHTS)) % 11]
    if not valid:
        check = '0' if check != '0' else '1'
    return prefix + check


class RemoteTests(unittest.TestCase):
    def test_https_and_ssh_forms_count_as_the_same_repo(self):
        """防：同一个私有仓换了 https / ssh 写法、带不带 .git 就被误拦。"""
        for url in (PRIVATE, PRIVATE + '.git', PRIVATE + '/', 'git@github.com:example-user/notes-private.git',
                    'ssh://git@github.com/example-user/notes-private', 'https://GitHub.com/Example-User/notes-private'):
            with self.subTest(url=url):
                self.assertTrue(upload_check.same_remote(url, PRIVATE))

    def test_other_repo_or_empty_is_not_the_same(self):
        """防：名字相近的别的仓库（比如公开模板）被当成私有仓放行。"""
        self.assertFalse(upload_check.same_remote('https://github.com/example-user/notes', PRIVATE))
        self.assertFalse(upload_check.same_remote('https://github.com/someone-else/notes-private', PRIVATE))
        self.assertFalse(upload_check.same_remote('', ''))

    def test_messages_do_not_show_token_in_url(self):
        """防：地址里带的 token 被打印到终端或日志里。"""
        url = 'https://user:' + 'tok' + 'en123' + '@' + 'github.com/example-user/notes'
        blocks, _ = upload_check.check_remote(Path('.'), url, {'private_remote': PRIVATE})
        self.assertTrue(blocks)
        self.assertNotIn('token123', ''.join(blocks))

    def test_empty_private_remote_blocks(self):
        """防：还没设置私有仓就推送。"""
        blocks, _ = upload_check.check_remote(Path('.'), PRIVATE, {'private_remote': ''})
        self.assertTrue(blocks)

    def test_visibility_check_blocks_public_repo_and_only_hints_when_unknown(self):
        """防：设置里登记的仓库其实是公开的；查不了时也不能误拦。"""
        settings = {'private_remote': PRIVATE}
        ssh = 'git@github.com:example-user/notes-private.git'
        with mock.patch.object(upload_check, 'gh_private', return_value=False) as probe:
            self.assertTrue(upload_check.check_remote(Path('.'), ssh, settings)[0])
        probe.assert_called_once_with('example-user', 'notes-private')
        with mock.patch.object(upload_check, 'gh_private', return_value=None):
            blocks, hints = upload_check.check_remote(Path('.'), ssh, settings)
            self.assertEqual((blocks, len(hints)), ([], 1))
        with mock.patch.object(upload_check, 'gh_private', return_value=True):
            self.assertEqual(upload_check.check_remote(Path('.'), ssh, settings), ([], []))


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.repos = Repos()
        self.addCleanup(self.repos.cleanup)
        self.here = self.repos.here

    def stage(self, rel, text, force=False):
        write(self.here, rel, text)
        git(self.here, 'add', *(['-f'] if force else []), '--', rel)

    def staged_check(self):
        return upload_check.check(self.here, 'staged')

    def test_secret_is_blocked_without_printing_it(self):
        """防：密钥被提交；或者报错时把密钥本身打印出来。"""
        self.stage('笔记/配置.md', f'key: {SECRET}\n')
        blocks, _, out = self.staged_check()
        self.assertIn('疑似密钥', blocks)
        self.assertNotIn(SECRET, '\n'.join(out))

    def test_plain_assignment_is_blocked_but_placeholder_is_not(self):
        """防：明文写的密码漏过去；占位符被误拦。"""
        self.stage('笔记/a.md', 'password = "' + 'Zq8vLm2p' + 'Xw9k"\n')
        self.assertIn('疑似密钥', self.staged_check()[0])
        self.stage('笔记/a.md', 'password = "<your-password-here>"\napi_key = os.environ.get("KEY")\n')
        self.assertEqual(self.staged_check()[0], [])

    def test_checks_staged_content_not_working_tree(self):
        """防：暂存了带密钥的版本、再把工作区文件改干净，就躲过检查。"""
        self.stage('笔记/配置.md', f'key: {SECRET}\n')
        write(self.here, '笔记/配置.md', '已经删掉\n')
        self.assertIn('疑似密钥', self.staged_check()[0])

    def test_valid_id_number_is_blocked_and_wrong_checksum_is_not(self):
        """防：身份证号被提交；校验位不对的普通数字被误拦。"""
        self.stage('笔记/名单.md', f'证件：{fake_id()}\n')
        self.assertIn('身份证号', self.staged_check()[0])
        self.stage('笔记/名单.md', f'编号：{fake_id(valid=False)}\n')
        self.assertEqual(self.staged_check()[0], [])

    def test_phone_number_only_hints(self):
        """防：手机号把提交拦死（私有仓里可以有，只提醒）。"""
        self.stage('笔记/通讯录.md', f'电话 {PHONE}\n')
        blocks, warns, _ = self.staged_check()
        self.assertEqual(blocks, [])
        self.assertIn('手机号', warns)

    def test_local_only_and_file_library_paths_are_blocked(self):
        """防：只留本机的隐私文件、走云盘的原件被强行 git add -f 提交。"""
        for rel in ('.personal/private/日记.md', '.personal/files/合同.md', 'keys/server.pem'):
            with self.subTest(rel=rel):
                self.stage(rel, '内容\n', force=True)
                self.assertIn('不该进 Git 的路径', self.staged_check()[0])
                git(self.here, 'reset', '-q', '--', rel)

    def test_big_file_blocks_and_warns(self):
        """防：超过 GitHub 上限的文件让推送失败；接近上限的没有提醒。"""
        with mock.patch.object(upload_check, 'HARD_LIMIT', 100), mock.patch.object(upload_check, 'WARN_LIMIT', 50):
            self.stage('笔记/大.md', 'x' * 80)
            blocks, warns, _ = self.staged_check()
            self.assertEqual((blocks, warns), ([], ['超 50MB']))
            self.stage('笔记/大.md', 'x' * 120)
            self.assertIn('超 100MB', self.staged_check()[0])

    def test_origin_must_match_private_remote(self):
        """防：origin 被改到别的地方之后照常提交。"""
        git(self.here, 'remote', 'set-url', 'origin', '../somewhere-else.git')
        self.stage('笔记/a.md', '正常内容\n')
        self.assertIn('远端', self.staged_check()[0])
        self.repos.set_settings(private_remote='')
        git(self.here, 'remote', 'set-url', 'origin', '../cloud.git')
        self.assertIn('远端', self.staged_check()[0])

    def test_nested_repository_is_blocked(self):
        """防：带 .git 的旧副本被当成空链接提交，内容其实没上传。"""
        git(self.here, 'init', '--quiet', '旧副本')
        write(self.here, '旧副本/a.md', '内容\n')
        self.assertIn('嵌套仓库', upload_check.check(self.here, 'pending')[0])


class HookTests(unittest.TestCase):
    def setUp(self):
        self.repos = Repos()
        self.addCleanup(self.repos.cleanup)
        self.here = self.repos.here

    def test_pre_push_only_allows_private_remote(self):
        """防：手敲 git push 推到别的仓库（钩子要按设置拦，不靠脚本自觉）。"""
        git(self.repos.base, 'init', '--quiet', '--bare', 'public.git')
        git(self.here, 'remote', 'add', 'public', '../public.git')
        write(self.here, '笔记/a.md', '私人内容\n')
        git(self.here, 'add', '--', '笔记/a.md')
        git(self.here, 'commit', '--quiet', '-m', '私人内容')
        self.assertNotEqual(git_run(self.here, 'push', '--quiet', 'public', 'main').returncode, 0)
        self.assertEqual(git(self.repos.base / 'public.git', 'for-each-ref'), '')
        self.assertEqual(git_run(self.here, 'push', '--quiet', 'origin', 'main').returncode, 0)
        self.assertEqual(self.repos.cloud_head(), self.repos.head())

    def test_install_and_uninstall_hooks(self):
        """防：安装脚本没真正启用钩子；钩子文件变成 CRLF 后在 Git Bash 下跑不起来。"""
        with redirect_stdout(io.StringIO()):
            self.assertTrue(install_git_hooks.uninstall(self.here))
            self.assertEqual(upload_check.hooks_problem(self.here), ['pre-commit', 'pre-push'])
            hook = self.here / 'tools/git-hooks/pre-push'
            hook.write_bytes(hook.read_bytes().replace(b'\n', b'\r\n'))
            self.assertEqual(install_git_hooks.install(self.here), [])
        self.assertEqual(upload_check.hooks_problem(self.here), [])
        self.assertNotIn(b'\r\n', hook.read_bytes())

    def test_pre_commit_blocks_secret_without_printing_it(self):
        """防：钩子链路失效让带密钥的提交通过；或者钩子输出里带出密钥。"""
        write(self.here, '笔记/a.md', f'key: {SECRET}\n')
        git(self.here, 'add', '--', '笔记/a.md')
        count = self.repos.commit_count()
        result = git_run(self.here, 'commit', '-m', '带密钥')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(SECRET, (result.stdout + result.stderr).decode('utf-8', 'replace'))
        self.assertEqual(self.repos.commit_count(), count)


if __name__ == '__main__':
    unittest.main()
