import contextlib
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import trace_changes


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='trace-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '工作 区'
        (self.root / 'config').mkdir(parents=True)
        (self.root / '.device').mkdir()
        (self.root / '.device/device.json').write_text('{"device_name": "test-box"}', encoding='utf-8')
        (self.root / 'config/rules.md').write_text('v1', encoding='utf-8')
        (self.root / 'keep.txt').write_text('same', encoding='utf-8')
        self.scan()   # 基线

    def scan(self, actor=None):
        with contextlib.redirect_stdout(io.StringIO()):
            trace_changes.scan(self.root, 'manual', actor)

    def events(self):
        rows = []
        for ledger in (self.root / '.personal/logs/trace').glob('*/*.jsonl'):
            rows += [json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()]
        return rows

    def changes(self):
        return {(row['type'], row['path']) for row in self.events() if 'path' in row}

    def report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            trace_changes.report(self.root, datetime.date.today().isoformat())
        return out.getvalue()

    def test_baseline_only_records_one_line_under_device_folder(self):
        """防止首次扫描把整个工作区当成“新增”刷满流水，且流水要落在设置的设备名下。"""
        self.assertEqual([row['type'] for row in self.events()], ['baseline'])
        self.assertTrue(list((self.root / '.personal/logs/trace/test-box').glob('changes_*.jsonl')))

    def test_add_modify_delete_recorded_and_skipped_dirs_ignored(self):
        """防止漏记真实变化，或把 .git、.local、.device、node_modules 这类目录的噪音记进流水。"""
        (self.root / 'config/rules.md').write_text('v2 改了', encoding='utf-8')
        (self.root / 'new.md').write_text('new', encoding='utf-8')
        (self.root / 'keep.txt').unlink()
        for folder in ('.git', '.local/cache', 'node_modules/pkg', 'sub/__pycache__'):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
            (self.root / folder / 'x.txt').write_text('noise', encoding='utf-8')
        (self.root / '.device/device.json').write_text('{"device_name": "test-box", "x": 1}', encoding='utf-8')
        self.scan('tester')
        self.assertEqual(self.changes(), {('modified', 'config/rules.md'), ('added', 'new.md'),
                                          ('deleted', 'keep.txt')})
        self.assertTrue(all(row.get('actor') == 'tester' for row in self.events() if row['type'] != 'baseline'))
        self.scan()
        self.assertEqual(len(self.changes()), 3)   # 没有新变化就不再追加

    def test_touch_without_content_change_is_ignored(self):
        """防止同步软件只改修改时间时制造大量假“修改”。"""
        target = self.root / 'keep.txt'
        os.utime(target, (1, 1))
        self.scan()
        self.assertEqual(self.changes(), set())
        target.write_text('changed', encoding='utf-8')
        self.scan()
        row = [row for row in self.events() if row['type'] == 'modified'][0]
        self.assertTrue(row['prev_sha256'])   # 基线后第一次改动也有改前哈希

    def test_local_only_and_secret_paths_only_recorded_at_directory_level(self):
        """防止隐私文件名、密钥文件的哈希（短密钥可被反推）写进会同步到仓库的流水。"""
        (self.root / '.personal').mkdir(exist_ok=True)
        (self.root / '.personal/agent.json').write_text(
            json.dumps({'local_only': ['.personal/private/', '.device/', '.local/', 'notes/diary/']}),
            encoding='utf-8')
        self.scan()
        (self.root / '.personal/private/合同').mkdir(parents=True)
        (self.root / '.personal/private/合同/扫描件.jpg').write_bytes(b'img')
        (self.root / '.personal/private/身份证.png').write_bytes(b'img')
        (self.root / 'notes/diary').mkdir(parents=True)
        (self.root / 'notes/diary/某天心情.md').write_text('x', encoding='utf-8')
        (self.root / 'local-secret').mkdir()
        (self.root / 'local-secret/token.txt').write_text('hunter2', encoding='utf-8')
        (self.root / '.env').write_text('KEY=1', encoding='utf-8')
        self.scan()
        rows = [row for row in self.events() if row['type'] != 'baseline' and row['path'] != '.personal/agent.json']
        self.assertEqual({row['path'] for row in rows},
                         {'.personal/private/*', 'notes/diary/*', 'local-secret/*', '*'})
        private = next(row for row in rows if row['path'] == '.personal/private/*')
        self.assertEqual(private['count'], 2)
        text = json.dumps(rows, ensure_ascii=False)
        for secret in ('扫描件', '身份证', '某天心情', 'token.txt', '.env'):
            self.assertNotIn(secret, text)
        self.assertTrue(all('sha256' not in row and 'size' not in row for row in rows))
        snapshot = json.loads((self.root / trace_changes.SNAPSHOT).read_text(encoding='utf-8'))
        self.assertIsNone(snapshot['files']['local-secret/token.txt'][2])   # 本机快照里也不算密钥哈希

    def test_ledger_itself_is_not_scanned(self):
        """防止流水文件自己的追加被当成变化，每次扫描都多一条，越记越多。"""
        (self.root / 'a.md').write_text('a', encoding='utf-8')
        self.scan()
        self.scan()
        self.assertEqual(self.changes(), {('added', 'a.md')})

    def test_large_batch_is_summarized_after_detail_limit(self):
        """防止一次批量改动（如解压上千文件）把流水撑爆。"""
        for i in range(5):
            (self.root / f'f{i}.md').write_text(str(i), encoding='utf-8')
        with mock.patch.object(trace_changes, 'DETAIL_LIMIT', 2):
            self.scan()
        rows = [row for row in self.events() if row['type'] != 'baseline']
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]['type'], 'bulk')
        self.assertEqual(rows[-1]['total'], {'added': 5})

    def test_huge_files_are_not_hashed(self):
        """防止定时扫描对超大音视频反复算哈希，拖慢电脑。"""
        with mock.patch.object(trace_changes, 'HASH_LIMIT', 3):
            (self.root / 'video.mp4').write_bytes(b'0123456789')
            self.scan()
        row = next(row for row in self.events() if row.get('path') == 'video.mp4')
        self.assertIsNone(row['sha256'])
        self.assertEqual(row['size'], 10)

    def test_report_warns_when_changes_have_no_work_log(self):
        """防止“有改动却没写工作日志”的日子被忽略，事后查不到原因。"""
        (self.root / 'config/rules.md').write_text('v2', encoding='utf-8')
        self.scan()
        self.assertIn('⚠', self.report())
        today = datetime.date.today().isoformat()
        work_logs = self.root / '.personal/logs/work-logs'
        work_logs.mkdir(parents=True)
        (work_logs / f'{today}_改规则.md').write_text('why', encoding='utf-8')
        self.scan()
        text = self.report()
        self.assertNotIn('⚠', text)
        self.assertIn(f'{today}_改规则.md', text)

    def test_archive_only_changes_do_not_need_work_log(self):
        """防止自动导出的对话归档每天都触发“缺工作日志”的误报。"""
        exports = self.root / '.personal/logs/chat-exports'
        exports.mkdir(parents=True)
        (exports / 'session.md').write_text('hi', encoding='utf-8')
        self.scan()
        self.assertNotIn('⚠', self.report())

    def test_cli_accepts_root_after_subcommand(self):
        """防止定时任务或测试传 --root 时被参数解析拒绝。"""
        (self.root / 'cli.md').write_text('x', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(trace_changes.main(['scan', '--trigger', 'scheduled', '--root', str(self.root)]), 0)
        row = next(row for row in self.events() if row.get('path') == 'cli.md')
        self.assertEqual(row['trigger'], 'scheduled')


if __name__ == '__main__':
    unittest.main()
