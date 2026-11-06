import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import agent_config
import cloud_sync
import feishu_sync


class SyncExcludeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sync-exclude-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '工作 区'
        (self.root / '.personal/files').mkdir(parents=True)
        self.local = self.root / '.personal/files'

    def settings(self, **exclude):
        data = agent_config.load(self.root)
        data['sync_exclude'] = dict(data['sync_exclude'], **exclude)
        return data

    def test_credentials_are_excluded_by_default_but_normal_files_are_not(self):
        """防的是：默认就把 .env、密钥、名字里带 secret/password 的文件传上云盘；或误伤普通文档。"""
        settings = self.settings()
        for name in ('.env', '.env.local', 'a/b/server.key', 'cert.PEM', 'Secret-plan.md', 'my password list.txt', '密码本.xlsx'):
            self.assertTrue(agent_config.sync_excluded(name, settings), name)
        for name in ('notes.md', 'report.pdf', 'sec-contact.md', '会议纪要.docx'):
            self.assertFalse(agent_config.sync_excluded(name, settings), name)

    def test_user_can_choose_to_upload_credentials(self):
        """防的是：用户明确选择“凭据也上传到我自己的云盘”后，设置被忽略、照样不传。"""
        self.assertFalse(agent_config.sync_excluded('.env', self.settings(credentials=False)))

    def test_media_and_custom_patterns_are_opt_in(self):
        """防的是：默认就不传音视频；或用户加的文件名模式不生效。"""
        self.assertFalse(agent_config.sync_excluded('talk.MP4', self.settings()))
        self.assertTrue(agent_config.sync_excluded('talk.MP4', self.settings(media=True)))
        custom = self.settings(patterns=['合同*', '*.zip'])
        self.assertTrue(agent_config.sync_excluded('合同-甲方.docx', custom))
        self.assertTrue(agent_config.sync_excluded('x/y/backup.zip', custom))
        self.assertFalse(agent_config.sync_excluded('notes.md', custom))

    def test_feishu_scan_skips_excluded_files(self):
        """防的是：飞书同步的文件清单里混进凭据类文件。"""
        settings = self.settings(media=True)
        self.assertTrue(feishu_sync.excluded(self.root, settings, self.local, '.env'))
        self.assertTrue(feishu_sync.excluded(self.root, settings, self.local, 'a/b.mp3'))
        self.assertFalse(feishu_sync.excluded(self.root, settings, self.local, 'a/b.md'))
        self.assertFalse(feishu_sync.excluded(self.root, self.settings(credentials=False), self.local, '.env'))

    def test_cloud_filters_exclude_credentials_case_insensitively_and_stay_stable(self):
        """防的是：rclone 过滤规则区分大小写，.ENV / Secret.txt 漏网；或规则每次内容不同导致反复要求重建基线。"""
        settings = self.settings(media=True, patterns=['合同*'])
        rules = cloud_sync.filter_rules(self.root, settings, self.local)
        self.assertIn('- .[eE][nN][vV]', rules)
        self.assertIn('- *[sS][eE][cC][rR][eE][tT]*', rules)
        self.assertIn('[mM][pP]4', rules)
        self.assertIn('合同*', rules)
        self.assertEqual(rules, cloud_sync.filter_rules(self.root, settings, self.local))
        off = cloud_sync.filter_rules(self.root, self.settings(credentials=False), self.local)
        self.assertNotIn('[eE][nN][vV]', off)


if __name__ == '__main__':
    unittest.main()
