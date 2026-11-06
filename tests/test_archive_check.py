import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import archive_check
import chat_archive

WEB = '''# 2026-03-14 发布计划讨论

- 来源：网页版 ChatGPT，用户在对话里逐轮复制
- 完整性：尽可能完整版本

## 用户

我们的资料放哪？

## ChatGPT

建议分三层。

## 用户

那网页版怎么写回来？

## Claude（另一个窗口）

【原文不可取得】
'''


class ArchiveCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='archive-check-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.chat = self.root / '.personal/logs/chat-exports'
        self.chat.mkdir(parents=True)

    def write(self, name, text):
        path = self.chat / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_chat_archive_export_format_passes(self):
        """防的是：自家导出脚本写出的标题（如“## 助手（Claude Code）”带全角括号）被误判成不合格。"""
        turns = [('user', '2026-03-13 20:00:00', '你好'), ('assistant', '2026-03-13 20:00:05', '你好，有什么事？'),
                 ('user', '2026-03-13 20:01:00', '再问一句'), ('assistant', '2026-03-13 20:01:09', '请讲')]
        text = chat_archive.render('Claude', 'abcdef12-0000', Path('abcdef12-0000.jsonl'), '/work', turns,
                                   'laptop', 'abcdef12')
        ok, detail = archive_check.check_file(self.write('2026-03-13_laptop_Claude_abcdef12.md', text))
        self.assertTrue(ok, detail)
        self.assertIn('用户 2 轮，AI 2 轮', detail)

    def test_hand_written_and_hook_formats_pass(self):
        """防的是：网页版 AI 手写的“## 用户 / ## ChatGPT”或旧钩子的“## [Claude] 助手”被拒，逼人改格式。"""
        ok, detail = archive_check.check_file(self.write('web.md', WEB))
        self.assertTrue(ok, detail)
        self.assertIn('1 处标了【原文不可取得】', detail)
        hook = ('# 会话全文（自动导出）\n\n- 来源：transcript.jsonl\n- 轮次：2\n\n---\n\n'
                '## 用户 `2026-03-13T12:00:00`\n\n问题\n\n## [Claude] 助手 `2026-03-13T12:00:05`\n\n回答\n')
        ok, detail = archive_check.check_file(self.write('hook.md', hook))
        self.assertTrue(ok, detail)

    def test_summary_is_not_accepted_as_full_archive(self):
        """防的是：只有“要点 / 用户需求 / 结论”的整理稿被当成逐轮全文。"""
        summary = '# 对话摘要\n\n- 来源：ChatGPT\n\n## 用户需求\n\n要存资料。\n\n## 结论\n\n分三层。\n'
        ok, detail = archive_check.check_file(self.write('summary.md', summary))
        self.assertFalse(ok)
        self.assertIn('不是逐轮全文', detail)

    def test_truncated_empty_or_unsourced_archives_fail(self):
        """防的是：被截断（声明条数对不上）、有空轮次、没写来源的归档蒙混过关。"""
        truncated = WEB.replace('- 完整性', '- 条数：用户 3，助手 2\n- 完整性')
        empty = WEB.replace('建议分三层。', '<!-- 待补 -->')
        unsourced = WEB.replace('- 来源：网页版 ChatGPT，用户在对话里逐轮复制\n', '')
        for name, text, reason in (('t.md', truncated, '可能被截断'), ('e.md', empty, '下面没有正文'),
                                   ('u.md', unsourced, '没写来源')):
            ok, detail = archive_check.check_file(self.write(name, text))
            self.assertFalse(ok, name)
            self.assertIn(reason, detail)

    def test_day_checks_that_days_transcripts_only(self):
        """防的是：按天检查时把单次索引、总索引当成归档报缺口，漏查其他项目目录，或者这天没归档也报通过。"""
        self.write('2026-03-14_web_ChatGPT.md', WEB)
        self.write('2026-03-14_web_ChatGPT_INDEX.md', '# 单次索引\n\n- 主题：存储\n')
        self.write('other-projects/demo/2026-03-14_laptop_Codex_0123456789abc.md', WEB)
        self.write('00_index.md', '# 总索引\n')
        self.write('2026-03-15_x.md', '# 别的日子\n')
        with redirect_stdout(io.StringIO()) as out:
            code = archive_check.main(['--root', str(self.root), 'day', '2026-03-14'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(out.getvalue().count('[通过]'), 2)
        self.assertNotIn('INDEX', out.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            code = archive_check.main(['--root', str(self.root), 'day', '2026-10-06'])
        self.assertEqual(code, 1)
        self.assertIn('没有这一天的归档', out.getvalue())


if __name__ == '__main__':
    unittest.main()
