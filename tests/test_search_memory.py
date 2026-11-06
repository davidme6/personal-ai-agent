import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import search_memory


class SearchMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='search-memory-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def found(self, **query):
        return {rel: hits for rel, hits in search_memory.search(self.root, **query)}

    def test_keyword_is_case_insensitive_and_stays_in_default_scope(self):
        """防的是：大小写不同就搜不到，或者默认范围把 .personal/private/、留痕流水、代码也翻出来塞给 AI。"""
        self.put('.personal/memory/decisions.md', '# 决定\n改用 GitHub 私有仓\n')
        self.put('.personal/knowledge/git.md', 'github 用法\n')
        self.put('.personal/current-state.md', '当前：在迁移 GITHUB\n')
        self.put('.personal/private/diary.md', 'github 密码提示\n')
        self.put('.personal/logs/trace/pc/changes_2026-10.jsonl', '{"path": "github"}\n')
        self.put('tools/x.md', 'github\n')
        self.assertEqual(sorted(self.found(keyword='GitHub')),
                         ['.personal/current-state.md', '.personal/knowledge/git.md', '.personal/memory/decisions.md'])
        self.assertEqual(self.found(keyword='GitHub')['.personal/memory/decisions.md'], [(2, '改用 GitHub 私有仓')])

    def test_date_matches_file_name_or_content_and_combines_with_keyword(self):
        """防的是：-k 加 -d 时要求同一行既有关键词又有日期而几乎搜不到；跨天的会话按文件名找不到。"""
        chat = '.personal/logs/chat-exports/'
        self.put(chat + '2026-03-13_pc_Claude_abcdef12.md',
                 '## 用户 `2026-03-13 23:50:00`\n\n发布计划先放着\n\n## 用户 `2026-03-14 00:10:00`\n\n继续聊发布计划\n')
        self.put(chat + '2026-03-14_web.md', '没有关键词\n')
        self.put(chat + '2026-03-15_pc.md', '发布计划 但不是这天\n')
        both = self.found(keyword='发布计划', date='2026-03-14')
        self.assertEqual(list(both), [chat + '2026-03-13_pc_Claude_abcdef12.md'])
        self.assertEqual(len(both[chat + '2026-03-13_pc_Claude_abcdef12.md']), 2)
        day = self.found(date='2026-03-14')
        self.assertEqual(sorted(day), [chat + '2026-03-13_pc_Claude_abcdef12.md', chat + '2026-03-14_web.md'])
        self.assertEqual(day[chat + '2026-03-13_pc_Claude_abcdef12.md'], [(5, '## 用户 `2026-03-14 00:10:00`')])

    def test_bad_bytes_and_missing_workspace_do_not_crash(self):
        """防的是：一个非 UTF-8 文件让整次检索崩掉，或者新工作区没有 .personal/ 时甩出堆栈。"""
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = search_memory.main(['--root', str(self.root), '-k', 'x'])
        self.assertEqual(code, 1)
        self.assertIn('init_workspace.py', out.getvalue())
        path = self.root / '.personal/handoff/old.md'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'\xff\xfe\x00 \xd6\xd0\xce\xc4 keyword here\n')
        with redirect_stdout(io.StringIO()) as out:
            code = search_memory.main(['--root', str(self.root), '-k', 'KEYWORD'])
        self.assertEqual(code, 0)
        self.assertIn('命中 1 行 / 1 个文件', out.getvalue())


if __name__ == '__main__':
    unittest.main()
