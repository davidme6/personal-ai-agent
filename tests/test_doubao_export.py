import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import doubao_export

WORK = 'AppData/Local/DoubaoWork/User Data/Default/.doubaowork/agent_mode/workspace/.sessions'
PLAIN = 'AppData/Local/Doubao/User Data/Default/.doubao/agent_mode/workspace/.sessions'
OLD = 1_700_000_000


def user(text):
    return {'role': 'user', 'content': text}


def assistant(text):
    return {'role': 'assistant', 'content': text}


class DoubaoExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='doubao-export-test-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.root, self.home = base / 'workspace', base / 'home'
        (self.root / '.device').mkdir(parents=True)
        (self.root / '.device/device.json').write_text('{"device_name": "test-pc"}', encoding='utf-8')
        self.chat = self.root / '.personal/logs/chat-exports'

    def session(self, session, rows, app=WORK, agent='main01', note=None, mtime=None):
        folder = self.home / app / session / 'agents' / agent / 'system'
        folder.mkdir(parents=True, exist_ok=True)
        trajectory = folder / 'trajectory.jsonl'
        trajectory.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows), encoding='utf-8')
        if note is not None:
            (folder / 'assignment.md').write_text(note, encoding='utf-8')
        if mtime:
            for path in folder.iterdir():
                os.utime(path, (mtime, mtime))
        return trajectory

    def export(self, **options):
        options.setdefault('images', False)
        return doubao_export.export(self.root, self.home, **options)

    def only_export(self, pattern='*.md'):
        files = sorted(self.chat.glob(pattern))
        self.assertEqual(len(files), 1, files)
        return files[0], files[0].read_text(encoding='utf-8')

    def test_user_and_assistant_text_is_kept_verbatim_without_tool_output(self):
        """防的是：导出把用户原话或助手回复改了、丢了，或者把工具输出和客户端塞的系统块当成对话收进去。"""
        self.session('70000000000000001', [
            user('<system-reminder>客户端附带的提示</system-reminder>\n帮我把周报模板\n改成三段式'),
            assistant('好的，我先看一下模板。'),
            {'role': 'tool', 'content': '工具读到的文件内容'},
            assistant('改好了：\n1. 本周进展\n2. 问题\n3. 下周计划'),
        ])
        stats, _ = self.export()
        self.assertEqual(stats['豆包Work 导出'], 1)
        path, text = self.only_export()
        self.assertRegex(path.name, r'^\d{4}-\d{2}-\d{2}_test-pc_DoubaoWork_70000000000000001\.md$')
        self.assertIn('- 平台：豆包Work', text)
        self.assertIn('帮我把周报模板\n改成三段式', text)
        self.assertIn('好的，我先看一下模板。\n\n改好了：\n1. 本周进展\n2. 问题\n3. 下周计划', text)
        self.assertNotIn('工具读到的文件内容', text)
        self.assertNotIn('客户端附带的提示', text)
        self.assertIn('- 缺助手回复：0 条', text)
        self.assertNotIn(doubao_export.GAP, text)

    def test_user_times_are_only_attached_when_counts_match(self):
        """防的是：assignment.md 的时间条数和用户消息对不上时，把时间安到了别的消息上。"""
        note = '# Assignment\n\n## [2026-03-01T02:03:04.123456789Z] 需求\n第一问\n\n## [2026-03-01T02:10:00Z] 需求\n第二问\n'
        self.session('70000000000000002', [user('第一问'), assistant('一答'), user('第二问'), assistant('二答')], note=note)
        self.export()
        _, text = self.only_export()
        first = doubao_export.local_time('2026-03-01T02:03:04.123456789Z')   # 纳秒精度也要认得
        self.assertTrue(first)
        self.assertIn(f'## 用户 `{first}`\n\n第一问', text)
        self.session('70000000000000002', [user('第一问'), assistant('一答'), user('第二问'), assistant('二答'),
                                           user('第三问'), assistant('三答')], note=note)
        self.export(force=True)
        _, text = self.only_export()
        self.assertNotIn(first, text)
        self.assertEqual(text.count('## 用户 `时间不详`'), 3)
        self.assertIn('条数和用户消息对不上', text)

    def test_missing_replies_are_marked_and_counted(self):
        """防的是：轨迹里缺助手回复，导出却看不出来，文件头还说是完整的。"""
        self.session('70000000000000003', [user('第一问'), assistant('第一答'), user('第二问'),
                                           {'role': 'tool', 'content': '工具输出'}, user('第三问')])
        self.export()
        _, text = self.only_export()
        self.assertEqual(text.count(doubao_export.GAP), 2)
        self.assertIn('- 缺助手回复：2 条', text)
        self.assertIn('尽可能完整版本', text)
        self.assertIn('另有 2 条用户消息之后', text)
        self.assertLess(text.index('第二问'), text.index(doubao_export.GAP))
        self.assertLess(text.index(doubao_export.GAP), text.index('第三问'))
        self.assertNotIn('工具输出', text)

    def test_unchanged_sessions_are_skipped_and_updated_ones_rewritten(self):
        """防的是：没变的会话每次都重写；轨迹追加了内容却因为“已导出”不更新，或者更新时多出第二份文件。"""
        self.session('70000000000000004', [user('问'), assistant('答')], mtime=OLD)
        self.assertEqual(self.export()[0]['豆包Work 导出'], 1)
        self.assertEqual(self.export()[0]['已是最新'], 1)
        self.session('70000000000000004', [user('问'), assistant('答'), user('追问'), assistant('补充的回答')],
                     mtime=OLD + 10)
        self.assertEqual(self.export()[0]['豆包Work 导出'], 1)
        _, text = self.only_export()
        self.assertIn('补充的回答', text)

    def test_rewrite_backs_up_old_export_when_trajectory_lost_content(self):
        """防的是：轨迹后来变短，重导时把以前导出过的回复覆盖掉，再也找不回来。"""
        self.session('70000000000000005', [user('问'), assistant('后来轨迹里没了的回答'), user('再问'), assistant('答')],
                     mtime=OLD)
        self.export()
        self.session('70000000000000005', [user('问'), user('再问'), assistant('答')], mtime=OLD + 10)
        _, notes = self.export()
        _, text = self.only_export()
        self.assertNotIn('后来轨迹里没了的回答', text)
        backups = list((self.root / '.local/backups/doubao-exports').glob('*.md'))
        self.assertEqual(len(backups), 1)
        self.assertIn('后来轨迹里没了的回答', backups[0].read_text(encoding='utf-8'))
        self.assertTrue(any('另存' in note for note in notes))
        self.session('70000000000000005', [user('问'), user('再问'), assistant('答'), user('新问题'), assistant('新回答')],
                     mtime=OLD + 20)
        self.export()
        self.assertIn('新回答', self.only_export()[1])
        self.assertEqual(len(list((self.root / '.local/backups/doubao-exports').glob('*.md'))), 1)   # 只是追加不用备份

    def test_obvious_secrets_are_masked(self):
        """防的是：对话里贴过的密钥、口令、证件号原样进了归档文件。"""
        # 假密钥拼出来写，免得泄露扫描把这个测试文件本身拦下
        api_key = 'sk-' + 'Q9w8E7r6' * 3
        password = 'pass' + 'word=' + 'fakepass' * 2
        id_number = '110105' + '19900307' + '123X'
        self.session('70000000000000006', [user(f'这个密钥对吗 {api_key}'), assistant(f'{password}\n证件 {id_number}')])
        self.export()
        _, text = self.only_export()
        for secret in (api_key, 'fakepass', id_number):
            self.assertNotIn(secret, text)
        self.assertIn('sk-***', text)
        self.assertIn('password=***', text)

    def test_only_main_agent_is_exported_and_ids_sharing_a_prefix_stay_apart(self):
        """防的是：子代理的轨迹盖掉主对话；会话号前几位相同的会话、两个客户端的同号会话写进同一个文件。"""
        self.session('80000000111111111', [user('甲会话的问题'), assistant('甲的回答')], agent='a_main',
                     note='# Assignment\n- Role: main\n')
        self.session('80000000111111111', [user('主代理派的活'), assistant('子代理产出的很长很长的内容' * 20)],
                     agent='b_sub', note='# Assignment\n- Role: subagent\n')
        self.session('80000000222222222', [user('乙会话的问题'), assistant('乙的回答')])
        self.session('80000000111111111', [user('普通版的问题'), assistant('普通版的回答')], app=PLAIN)
        stats, _ = self.export()
        self.assertEqual((stats['豆包Work 导出'], stats['豆包 导出']), (2, 1))
        _, first = self.only_export('*_DoubaoWork_80000000111111111.md')
        _, second = self.only_export('*_DoubaoWork_80000000222222222.md')
        _, plain = self.only_export('*_Doubao_80000000111111111.md')
        self.assertIn('甲的回答', first)
        self.assertNotIn('子代理产出', first)
        self.assertIn('乙的回答', second)
        self.assertIn('普通版的回答', plain)
        self.assertIn('- 平台：豆包\n', plain)

    def test_images_are_downloaded_once_and_survive_expired_links(self):
        """防的是：每次重导都重新联网下载图片；链接过期后重导，把已经存下的图片引用弄丢。"""
        self.session('70000000000000007', [user('看看这张图'),
                                           {'role': 'tool', 'content': '', 'image_link_list': ['https://example.com/a.png']},
                                           assistant('图里是一张表格')], mtime=OLD)
        with mock.patch.object(doubao_export, 'download', return_value=(b'fake-png', 'png')) as fetch:
            self.export(images=True)
        self.assertEqual(fetch.call_count, 1)
        image = self.chat / 'attachments/70000000000000007/img01.png'
        self.assertEqual(image.read_bytes(), b'fake-png')
        self.session('70000000000000007', [user('看看这张图'),
                                           {'role': 'tool', 'content': '', 'image_link_list': ['https://example.com/a.png']},
                                           assistant('图里是一张表格'), user('再说说'), assistant('好的')],
                     mtime=OLD + 10)
        with mock.patch.object(doubao_export, 'download', return_value=(None, '')) as fetch:
            self.export(images=True)
        self.assertEqual(fetch.call_count, 0)
        _, text = self.only_export()
        self.assertIn('![img01](<attachments/70000000000000007/img01.png>)', text)
        self.assertIn('好的', text)

    def test_missing_client_folder_is_not_an_error(self):
        """防的是：没装豆包、或者不在 Windows 上时，定时任务每天报失败，或者随手建出空目录。"""
        self.home.mkdir()
        stats, notes = self.export()
        self.assertEqual(sum(stats.values()), 0)
        self.assertIn('没找到', notes[0])
        stats, notes = doubao_export.export(self.root, None, windows=False)
        self.assertEqual(sum(stats.values()), 0)
        self.assertIn('--home', notes[0])
        with mock.patch('sys.stdout'):
            self.assertEqual(doubao_export.main(['--root', str(self.root), '--home', str(self.home)]), 0)
        self.assertFalse(self.chat.exists())


if __name__ == '__main__':
    unittest.main()
