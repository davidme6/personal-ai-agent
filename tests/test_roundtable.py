import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import archive_check
import roundtable

# 冒充 Codex 命令行：把收到的前几个参数和提示词开头写进 -o 指定的文件；第一个参数带 fail 就什么都不写并报错
FAKE = '''import sys
args = sys.argv[1:]
if 'fail' in args[0]:
    sys.exit(3)
open(args[args.index('-o') + 1], 'w', encoding='utf-8').write(' '.join(args[1:4]) + '|' + sys.stdin.buffer.read().decode('utf-8')[:20])
'''


class RoundtableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='roundtable-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.room = roundtable.room_path(self.root, '议题.md')
        self.fake = self.root / 'fake_codex.py'
        self.fake.write_text(FAKE, encoding='utf-8')

    def fake_codex(self, mode='ok'):
        return [sys.executable, str(self.fake), mode]

    def test_post_creates_room_and_keeps_speeches_verbatim_in_order(self):
        """防的是：发言被改写、顺序乱掉，或者记录文件散落在 chat-exports 以外的地方。"""
        self.assertEqual(self.room, self.root / '.personal/logs/chat-exports/roundtable/议题.md')
        self.assertTrue(roundtable.append(self.room, '用户', '第一句\n  带缩进的第二行'))
        self.assertFalse(roundtable.append(self.room, 'Claude', '第二句\n'))
        text = self.room.read_text(encoding='utf-8')
        self.assertLess(text.index('## 用户 `'), text.index('## Claude `'))
        self.assertIn('第一句\n  带缩进的第二行\n', text)
        self.assertTrue(text.endswith('第二句\n'))

    def test_record_passes_the_archive_check(self):
        """防的是：研讨记录本身过不了逐轮全文检查，审查过程没法当归档用。"""
        roundtable.append(self.room, '用户', '议题：要不要换存储方案')
        roundtable.ask_codex(self.root, self.room, 60, self.fake_codex())
        ok, detail = archive_check.check_file(self.room)
        self.assertTrue(ok, detail)

    def test_codex_runs_in_read_only_sandbox_and_reply_is_appended(self):
        """防的是：调用 Codex 时给了写权限，审的一方能改被审的文件。"""
        roundtable.append(self.room, '用户', '议题')
        reply = roundtable.ask_codex(self.root, self.room, 60, self.fake_codex())
        self.assertTrue(reply.startswith('exec -s read-only|你是 Codex'), reply)
        self.assertIn('## Codex `', self.room.read_text(encoding='utf-8'))

    def test_failed_codex_call_leaves_room_untouched(self):
        """防的是：Codex 报错或没回复时，记录里多出一段空发言或报错文字，被当成它的观点。"""
        roundtable.append(self.room, '用户', '议题')
        before = self.room.read_text(encoding='utf-8')
        with self.assertRaises(RuntimeError):
            roundtable.ask_codex(self.root, self.room, 60, self.fake_codex('fail'))
        self.assertEqual(self.room.read_text(encoding='utf-8'), before)

    def test_room_outside_chat_exports_is_refused(self):
        """防的是：--room 写错（比如 AGENTS.md 或 ../别处）时把发言追加进不相干的文件。"""
        (self.root / 'AGENTS.md').write_text('规则\n', encoding='utf-8')
        speech = self.root / 'speech.txt'
        speech.write_text('不该写进去', encoding='utf-8')
        for bad in (str(self.root / 'AGENTS.md'), 'docs/../AGENTS.md',
                    '.personal/logs/chat-exports/../../../AGENTS.md'):
            with redirect_stdout(io.StringIO()) as out:
                code = roundtable.main(['--root', str(self.root), 'post', '--room', bad,
                                        '--speaker', '用户', '--text-file', str(speech)])
            self.assertEqual(code, 1)
            self.assertIn('[停止]', out.getvalue())
        self.assertEqual((self.root / 'AGENTS.md').read_text(encoding='utf-8'), '规则\n')


if __name__ == '__main__':
    unittest.main()
