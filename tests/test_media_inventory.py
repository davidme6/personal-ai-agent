import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import media_inventory


class MediaInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='media-inventory-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'workspace'
        self.files = self.root / '.personal/files'

    def put(self, rel, data=b'x'):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def statuses(self):
        rows, out, _ = media_inventory.build(self.root)
        return {row['path']: row['status'] for row in rows}, out

    def test_status_and_manual_notes_survive_a_rerun(self):
        """防的是：重跑清单把手填的“不转写”备注冲掉，或者有文字稿的录音还被列成待转写。"""
        self.put('.personal/files/会议/周会.mp3')
        self.put('.personal/files/会议/周会_纪要.md', '纪要'.encode('utf-8'))
        self.put('.personal/files/会议/闲聊.m4a')
        self.put('.personal/files/会议/培训.MP4')
        found, out = self.statuses()
        self.assertEqual(found['.personal/files/会议/周会.mp3'], '已转写')
        self.assertEqual(found['.personal/files/会议/培训.MP4'], '待转写')
        lines = out.read_text(encoding='utf-8').splitlines()   # 像人一样在备注列（最后一格）填字
        lines = [line[:-2] + '不转写，噪音太大 |' if '闲聊.m4a' in line else line for line in lines]
        out.write_text('\n'.join(lines), encoding='utf-8')
        found, out = self.statuses()
        self.assertEqual(found['.personal/files/会议/闲聊.m4a'], '不转写')
        self.assertIn('不转写，噪音太大', out.read_text(encoding='utf-8'))

    def test_scan_is_read_only_and_skips_local_only_paths(self):
        """防的是：扫描时改动原件，或者把只留本机的 .personal/private/ 里的文件名写进会同步的清单。"""
        original = self.put('.personal/files/录音.wav', b'RIFF-original')
        before = (original.read_bytes(), original.stat().st_mtime_ns)
        self.put('.personal/private/体检录音.mp3')
        found, out = self.statuses()
        self.assertEqual(list(found), ['.personal/files/录音.wav'])
        self.assertNotIn('体检录音', out.read_text(encoding='utf-8'))
        self.assertEqual((original.read_bytes(), original.stat().st_mtime_ns), before)
        self.assertEqual(out, self.root / '.personal/logs/media-inventory.md')

    def test_external_dirs_are_labelled_and_bad_paths_skipped(self):
        """防的是：工作区外的录音目录漏扫，或者参数写错一个就整个报错退出。"""
        outside = Path(self.temp.name).resolve() / '录音盘'
        (outside / '2026').mkdir(parents=True)
        (outside / '2026/访谈.amr').write_bytes(b'x')
        (outside / 'code.ts').write_text('export {}', encoding='utf-8')
        with redirect_stdout(io.StringIO()) as printed:
            code = media_inventory.main(['--root', str(self.root), str(outside), str(outside / '不存在')])
        self.assertEqual(code, 0)
        self.assertIn('[跳过] 不是目录', printed.getvalue())
        text = (self.root / '.personal/logs/media-inventory.md').read_text(encoding='utf-8')
        self.assertIn('`[外部:录音盘]/2026/访谈.amr`', text)
        self.assertNotIn('code.ts', text)


if __name__ == '__main__':
    unittest.main()
