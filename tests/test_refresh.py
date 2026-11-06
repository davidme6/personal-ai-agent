import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import agent_config
import refresh

TODAY = datetime.date(2026, 10, 5)


def python_step(name, code):
    return (name, ['-c', code], '')


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='refresh-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'workspace'
        self.memory = self.root / '.personal/memory'
        self.memory.mkdir(parents=True)

    def test_failed_step_does_not_stop_later_steps(self):
        """防的是：一步出错就整个停掉，后面的导出、索引都没做；或者失败了还报成功。"""
        steps = [python_step('坏的一步', 'import sys; print("出错了"); sys.exit(3)'),
                 python_step('好的一步', 'open("ran.txt", "w").write("ok")')]
        text, failed = refresh.refresh(self.root, steps, TODAY)
        self.assertEqual(failed, ['坏的一步'])
        self.assertTrue((self.root / 'ran.txt').is_file())
        self.assertIn('[坏的一步] 失败\n  出错了', text)
        self.assertIn('[好的一步] 完成', text)
        self.assertEqual((self.root / '.local/refresh_report.txt').read_text(encoding='utf-8'), text)
        refresh.refresh(self.root, steps, TODAY)
        log = (self.root / '.local/refresh.log').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(log), 2)   # 日志是追加的，报告是覆盖的
        self.assertIn('有 1 步失败：坏的一步', log[-1])

    def test_missing_fastembed_is_a_skip_not_a_failure(self):
        """防的是：没装可选的语义索引依赖时，每次定时任务都被记成失败。"""
        with mock.patch.object(refresh, 'semantic_ready', return_value=False):
            steps = refresh.build_steps(self.root, agent_config.load(self.root))
        self.assertEqual([step[1] for step in steps if step[0] == '语义索引'], [None])
        missing = python_step('语义索引', f'import sys; print("{refresh.MISSING_DEPS}. Run pip"); sys.exit(1)')
        text, failed = refresh.refresh(self.root, [('语义索引', None, '没装 fastembed'), missing], TODAY)
        self.assertEqual(failed, [])
        self.assertEqual(text.count('[语义索引] 跳过'), 2)
        self.assertIn('结果：全部完成；跳过：语义索引、语义索引', text)

    def test_all_projects_setting_picks_export_all(self):
        """防的是：设置了收全部项目却只导出本工作区（或反过来）。"""
        settings = agent_config.load(self.root)
        self.assertEqual(refresh.build_steps(self.root, settings)[0][1][-1], 'export')
        settings['archive']['all_projects'] = True
        steps = refresh.build_steps(self.root, settings)
        self.assertEqual(steps[0][1][-1], 'export-all')
        self.assertIn(str(self.root), steps[0][1])   # 子步骤用同一个 --root，不会写到别的工作区

    def test_runs_without_stdout_like_pythonw(self):
        """防的是：用 pythonw 无窗口定时运行时没有标准输出，脚本一打印就崩，连日志都没留下。"""
        steps = [python_step('一步', 'pass')]
        with mock.patch.object(refresh, 'build_steps', return_value=steps), \
                mock.patch.object(sys, 'stdout', None):
            code = refresh.main(['--root', str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn('全部完成', (self.root / '.local/refresh.log').read_text(encoding='utf-8'))

    def test_archive_steps_run_end_to_end_against_a_fake_home(self):
        """防的是：refresh 调子脚本时丢了 --root 或用户目录设置，导出写错地方或读到真实会话。"""
        home = Path(self.temp.name).resolve() / 'home'
        folder = home / '.claude/projects/workspace'
        folder.mkdir(parents=True)
        lines = [{'type': 'user', 'timestamp': '2026-10-03T12:00:00.000Z', 'cwd': str(self.root),
                  'message': {'role': 'user', 'content': '端到端的问题'}},
                 {'type': 'assistant', 'timestamp': '2026-10-03T12:00:05.000Z',
                  'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '端到端的回答'}]}}]
        (folder / 'abcdef12-0000.jsonl').write_text('\n'.join(json.dumps(line, ensure_ascii=False) for line in lines),
                                                    encoding='utf-8')
        with mock.patch.object(refresh, 'semantic_ready', return_value=False):
            steps = refresh.build_steps(self.root, agent_config.load(self.root))
        with mock.patch.dict(os.environ, {'CHAT_ARCHIVE_HOME': str(home)}):
            text, failed = refresh.refresh(self.root, steps, TODAY)
        self.assertEqual(failed, [], text)
        chat = self.root / '.personal/logs/chat-exports'
        exported = next(chat.glob('*_Claude_abcdef12.md')).read_text(encoding='utf-8')
        self.assertIn('端到端的回答', exported)
        self.assertIn('端到端的问题', (chat / '00_index.md').read_text(encoding='utf-8'))
        self.assertTrue(next((self.root / '.local/backups/sessions/workspace').glob('*.jsonl.gz')).is_file())

    def test_doubao_step_runs_only_when_enabled(self):
        """防的是：没开豆包来源也去读豆包的会话目录；或者开了却没导出、写到了别的工作区。"""
        with mock.patch.object(refresh, 'semantic_ready', return_value=False):
            names = [step[0] for step in refresh.build_steps(self.root, agent_config.load(self.root))]
        self.assertNotIn('豆包导出', names)
        (self.root / '.personal/agent.json').write_text('{"archive": {"sources": ["claude", "doubao"]}}',
                                                       encoding='utf-8')
        with mock.patch.object(refresh, 'semantic_ready', return_value=False):
            steps = refresh.build_steps(self.root, agent_config.load(self.root))
        self.assertEqual([step[0] for step in steps][:2], ['会话导出', '豆包导出'])
        home = Path(self.temp.name).resolve() / 'home'
        folder = (home / 'AppData/Local/DoubaoWork/User Data/Default/.doubaowork/agent_mode/workspace/.sessions'
                  / '90000000000000001/agents/main/system')
        folder.mkdir(parents=True)
        (folder / 'trajectory.jsonl').write_text(
            '\n'.join(json.dumps(line, ensure_ascii=False) for line in
                      [{'role': 'user', 'content': '豆包里问的问题'}, {'role': 'assistant', 'content': '豆包的回答'}]),
            encoding='utf-8')
        with mock.patch.dict(os.environ, {'CHAT_ARCHIVE_HOME': str(home)}):
            text, failed = refresh.refresh(self.root, steps, TODAY)
        self.assertEqual(failed, [], text)
        self.assertIn('[豆包导出] 完成', text)
        exported = next((self.root / '.personal/logs/chat-exports').glob('*_DoubaoWork_90000000000000001.md'))
        self.assertIn('豆包的回答', exported.read_text(encoding='utf-8'))

    def write_memory(self, name, text):
        (self.memory / name).write_text(text, encoding='utf-8')

    def test_stale_sections_are_listed_and_fresh_ones_are_not(self):
        """防的是：长期记忆里很久没核实的结论一直被当成现状，没人提醒去核对。"""
        self.write_memory('decisions.md',
                          '# 决定\n\n## 旧决定\n- updated: 2026-08-01\n- last_verified: 2026-08-20\n内容\n\n'
                          '## 刚好三十天\n- updated: 2026-09-05\n\n'
                          '## 新决定\n- updated/last_verified: 2026-10-01\n内容\n\n'
                          '## 没日期\n只有正文\n')
        self.write_memory('open-loops.md',
                          '### 1. 早就办完的\n- status: closed\n- updated: 2026-01-01\n\n'
                          '### 2. 还开着的\n- status: open\n- updated: 2026-08-01\n')
        stale, undated = refresh.stale_memory(self.root, TODAY)
        self.assertEqual(stale, [('.personal/memory/decisions.md', '旧决定', '2026-08-20', 46),
                                 ('.personal/memory/open-loops.md', '2. 还开着的', '2026-08-01', 65)])
        self.assertEqual(undated, 1)

    def test_memory_file_with_bom_still_has_its_first_section_checked(self):
        """防的是：Windows 工具存的带 BOM 的记忆文件，第一个标题认不出，过期的段被漏报。"""
        (self.memory / 'bom.md').write_bytes('﻿## 第一段\n- updated: 2026-01-01\n'.encode('utf-8'))
        self.assertEqual([row[1] for row in refresh.stale_memory(self.root, TODAY)[0]], ['第一段'])

    def test_missing_memory_folder_is_not_an_error(self):
        """防的是：还没建记忆目录的新工作区，每次都报过期检查失败。"""
        self.memory.rmdir()
        self.assertEqual(refresh.stale_memory(self.root, TODAY), ([], 0))


if __name__ == '__main__':
    unittest.main()
