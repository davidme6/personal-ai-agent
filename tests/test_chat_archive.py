import base64
import gzip
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import agent_config
import chat_archive

STAMP = '2026-10-03T12:00:00.000Z'


def claude(kind, content=None, stamp=STAMP, **extra):
    item = {'type': kind, 'timestamp': stamp, **extra}
    if content is not None:
        item['message'] = {'role': kind, 'content': content}
    return json.dumps(item, ensure_ascii=False)


def queued(prompt, **extra):
    return json.dumps({'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': prompt, **extra}},
                      ensure_ascii=False)


def picture(data):
    return {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                        'data': base64.b64encode(data).decode()}}


def codex(role, *parts, kind='input_text'):
    return json.dumps({'type': 'response_item', 'timestamp': STAMP,
                       'payload': {'type': 'message', 'role': role,
                                   'content': [{'type': kind, 'text': part} for part in parts]}}, ensure_ascii=False)


def codex_meta(cwd, source='cli'):
    return json.dumps({'type': 'session_meta', 'timestamp': STAMP, 'payload': {'cwd': cwd, 'source': source}},
                      ensure_ascii=False)


class ChatArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chat-archive-test-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.root = base / 'workspace'
        self.home = base / 'home'
        self.elsewhere = base / 'elsewhere'
        (self.root / '.device').mkdir(parents=True)
        (self.root / '.device/device.json').write_text('{"device_name": "testbox"}', encoding='utf-8')
        self.chat = self.root / '.personal/logs/chat-exports'
        self.day = chat_archive.local_time(STAMP)[:10]

    def settings(self, **archive):
        return agent_config.merge(agent_config.DEFAULTS, {'archive': archive})

    def claude_session(self, name, lines, cwd=None):
        cwd = str(cwd or self.root)
        folder = self.home / '.claude/projects' / ''.join(ch if ch.isalnum() else '-' for ch in cwd)
        folder.mkdir(parents=True, exist_ok=True)
        lines = [claude('user', None, cwd=cwd)] + lines   # 真实记录每行都带 cwd；这里放一行就够 peek 用
        path = folder / f'{name}.jsonl'
        path.write_text('\n'.join(lines), encoding='utf-8')
        return path

    def export(self, everything=False, **archive):
        return chat_archive.export_sessions(self.root, self.home, self.settings(**archive), everything=everything)

    def test_claude_export_keeps_dialogue_and_drops_injected_content(self):
        """防的是：系统提示、推理、工具调用和输出、子代理支线、后台通知被当成对话写进归档。"""
        self.claude_session('abcdef12-0000', [
            claude('user', '第一句，原样保留<system-reminder>系统塞进来的</system-reminder>'),
            claude('assistant', [{'type': 'thinking', 'thinking': '不该出现的推理'}, {'type': 'text', 'text': '过程回复'}]),
            claude('assistant', [{'type': 'tool_use', 'name': 'Bash', 'input': {}}]),
            claude('user', [{'type': 'tool_result', 'content': '工具输出'}]),
            claude('assistant', [{'type': 'text', 'text': '最终回复'}]),
            claude('user', '元信息', isMeta=True),
            claude('user', '压缩摘要正文', isCompactSummary=True),
            queued('中途插话', timestamp=STAMP),
            queued('<task-notification>后台任务</task-notification>'),
            claude('user', [{'type': 'image', 'source': {}}, {'type': 'text', 'text': '看图 [Image: source: x.png]'}]),
            claude('assistant', [{'type': 'text', 'text': '子代理的话'}], isSidechain=True),
        ])
        stats, _, _ = self.export()
        target = self.chat / f'{self.day}_testbox_Claude_abcdef12.md'
        text = target.read_text(encoding='utf-8')
        for kept in ('第一句，原样保留', '过程回复\n\n最终回复', '（助手还在处理上一条时发来）\n\n中途插话',
                     '看图\n\n（img01，记录里没有原图）', '上下文被压缩', '- 平台：Claude Code'):
            self.assertIn(kept, text)
        for dropped in ('系统塞进来的', '不该出现的推理', '工具输出', '元信息', '压缩摘要正文', '后台任务',
                        '子代理的话', 'x.png'):
            self.assertNotIn(dropped, text)
        self.assertEqual(text.count('## 助手（Claude Code） `'), 1)   # 同一轮的两段回复合成一段
        self.assertEqual(text.count('## 用户 `'), 3)
        self.assertEqual(stats['Claude Code 导出'], 1)

    def test_images_are_saved_and_skip_marker_is_respected(self):
        """防的是：用户发的图片丢失，或已标记为敏感的那张被再次写出来。"""
        self.claude_session('abcdef12-0000', [
            claude('user', [picture(b'first'), {'type': 'text', 'text': '普通截图'}]),
            claude('user', [picture(b'secret'), {'type': 'text', 'text': '带密钥的截图'}]),
            queued([picture(b'third'), {'type': 'text', 'text': '中途发的图'}]),
            claude('assistant', [{'type': 'text', 'text': '收到'}]),
        ])
        folder = self.chat / 'attachments/abcdef12'
        folder.mkdir(parents=True)
        (folder / 'img02.skip').write_text('', encoding='utf-8')
        self.export()
        text = (self.chat / f'{self.day}_testbox_Claude_abcdef12.md').read_text(encoding='utf-8')
        self.assertEqual((folder / 'img01.png').read_bytes(), b'first')
        self.assertEqual((folder / 'img03.png').read_bytes(), b'third')
        self.assertFalse((folder / 'img02.png').exists())
        self.assertIn('普通截图\n\n![img01](<attachments/abcdef12/img01.png>)', text)
        self.assertIn('带密钥的截图\n\n（img02：已标记为敏感，不保存）', text)

    def test_obvious_secrets_are_masked(self):
        """防的是：对话里贴过的密钥、口令、证件号原样进了归档文件。"""
        # 假密钥拼出来写，免得泄露扫描把这个测试文件本身拦下
        api_key = 'sk-' + 'A1b2C3d4' * 3
        github = 'gh' + 'p_' + 'x' * 24
        assignment = 'pass' + 'word=' + 'hunter22' * 2
        id_number = '110101' + '19900307' + '123X'
        self.claude_session('abcdef12-0000', [
            claude('user', f'帮我看看 {api_key} 和 {github}'),
            claude('assistant', [{'type': 'text', 'text': f'{assignment}\n证件号码 {id_number}'}]),
        ])
        self.export()
        text = (self.chat / f'{self.day}_testbox_Claude_abcdef12.md').read_text(encoding='utf-8')
        for secret in (api_key, github, 'hunter22', id_number):
            self.assertNotIn(secret, text)
        self.assertIn('sk-*** 和 gh*_***', text)
        self.assertIn('password=***\n证件号码 ***证件号已隐藏***', text)

    def test_unchanged_sessions_are_skipped_and_updated_ones_rewritten(self):
        """防的是：每次重跑都整批重写，或原始记录追加了新对话后归档还停在旧版本。"""
        source = self.claude_session('abcdef12-0000', [
            claude('user', '第一问'), claude('assistant', [{'type': 'text', 'text': '第一答'}])])
        self.export()
        target = self.chat / f'{self.day}_testbox_Claude_abcdef12.md'
        self.assertEqual(self.export()[0]['已是最新'], 1)
        with open(source, 'a', encoding='utf-8') as stream:
            stream.write('\n' + claude('user', '追加的问题') + '\n'
                         + claude('assistant', [{'type': 'text', 'text': '追加的回答'}]))
        later = target.stat().st_mtime_ns + 5_000_000_000
        os.utime(source, ns=(later, later))
        stats, _, _ = self.export()
        self.assertEqual(stats['Claude Code 导出'], 1)
        self.assertIn('追加的回答', target.read_text(encoding='utf-8'))
        self.assertEqual(len(list(self.chat.glob('*.md'))), 1)   # 重写原文件，不另起一份

    def test_export_scopes_and_local_only_projects(self):
        """防的是：别的项目的对话混进本工作区，或设为只留本机的项目被写进会同步的 .personal。"""
        self.claude_session('aaaaaaaa-0000', [claude('user', '本工作区的问题'),
                                              claude('assistant', [{'type': 'text', 'text': '答'}])])
        self.claude_session('bbbbbbbb-0000', [claude('user', '店铺的问题'),
                                              claude('assistant', [{'type': 'text', 'text': '答'}])],
                            cwd=self.elsewhere / 'shop')
        self.claude_session('cccccccc-0000', [claude('user', '日记里的私事'),
                                              claude('assistant', [{'type': 'text', 'text': '答'}])],
                            cwd=self.elsewhere / 'diary')
        _, projects, _ = self.export()
        self.assertEqual(dict(projects), {'本工作区': 1})
        self.assertFalse((self.chat / 'other-projects').exists())

        leftover = self.chat / 'other-projects/diary/old.md'
        leftover.parent.mkdir(parents=True)
        leftover.write_text('以前导出的', encoding='utf-8')
        _, projects, warnings = self.export(everything=True, local_only_projects=['Diary'])
        self.assertEqual(projects['shop'], 1)
        self.assertEqual(projects['diary'], 1)
        self.assertTrue(next((self.chat / 'other-projects/shop').glob('*_Claude_bbbbbbbb.md')).is_file())
        private = next((self.root / '.local/chat-exports-local/diary').glob('*_Claude_cccccccc.md'))
        self.assertIn('日记里的私事', private.read_text(encoding='utf-8'))
        in_personal = ''.join(path.read_text(encoding='utf-8') for path in self.chat.rglob('*.md'))
        self.assertNotIn('日记里的私事', in_personal)
        self.assertEqual(leftover.read_text(encoding='utf-8'), '以前导出的')   # 旧文件不动，只提醒
        self.assertEqual(len(warnings), 1)
        self.assertIn('other-projects/diary/', warnings[0])

    def test_session_file_with_bom_is_still_exported(self):
        """防的是：文件头带 BOM 时第一行（含工作目录和第一句话）解析失败，整场会话被当成不是对话漏掉。"""
        source = self.claude_session('abcdef12-0000', [])
        lines = [claude('user', '第一句', cwd=str(self.root)), claude('assistant', [{'type': 'text', 'text': '答'}])]
        source.write_bytes(b'\xef\xbb\xbf' + '\n'.join(lines).encode('utf-8'))
        self.export()
        text = (self.chat / f'{self.day}_testbox_Claude_abcdef12.md').read_text(encoding='utf-8')
        self.assertIn('第一句', text)

    def test_sources_setting_turns_a_platform_off(self):
        """防的是：用户在设置里关掉的平台照样被导出。"""
        self.claude_session('abcdef12-0000', [claude('user', '问'), claude('assistant', [{'type': 'text', 'text': '答'}])])
        stats, _, _ = self.export(sources=['codex'])
        self.assertEqual(sum(stats.values()), 0)
        self.assertFalse(self.chat.exists())

    def test_codex_drops_injected_context_and_internal_sessions(self):
        """防的是：Codex 塞进用户消息的环境、AGENTS 指令、压缩摘要被当成用户原话，审批子会话被当成对话。"""
        folder = self.home / '.codex/sessions/2026/10/03'
        folder.mkdir(parents=True)
        image = 'data:image/png;base64,' + base64.b64encode(b'shot').decode()
        (folder / 'rollout-2026-10-03T20-00-00-22222222-0000-0000-0000-000000000000.jsonl').write_text('\n'.join([
            codex_meta(str(self.root)),
            codex('developer', '开发者指令'),
            codex('user', '<environment_context>环境</environment_context>', '# AGENTS.md instructions 规则'),
            codex('user', '<image>', '</image>', '<in-app-browser-context source="ui">界面状态</in-app-browser-context>\n看这张图'),
            json.dumps({'type': 'response_item', 'timestamp': STAMP,
                        'payload': {'type': 'message', 'role': 'user',
                                    'content': [{'type': 'input_image', 'image_url': image}]}}),
            codex('user', '<realtime_delegation><input>语音里说的</input></realtime_delegation>'),
            codex('user', chat_archive.SUMMARY + ' 摘要正文'),
            json.dumps({'type': 'response_item', 'payload': {'type': 'reasoning', 'encrypted_content': 'QUJD' * 600}}),
            codex('assistant', '过程说明', kind='output_text'),
            json.dumps({'type': 'response_item', 'payload': {'type': 'function_call', 'arguments': '命令'}},
                       ensure_ascii=False),
            codex('assistant', '最终答复', kind='output_text'),
        ]), encoding='utf-8')
        (folder / 'rollout-2026-10-03T20-00-30-22222222-1111-0000-0000-000000000000.jsonl').write_text('\n'.join([
            codex_meta(str(self.root), {'subagent': 'review'}), codex('user', '内部审批'),
            codex('assistant', '通过', kind='output_text')]), encoding='utf-8')
        stats, _, _ = self.export()
        text = (self.chat / f'{self.day}_testbox_Codex_22222222-0000.md').read_text(encoding='utf-8')
        self.assertIn(f'## 用户 `{chat_archive.local_time(STAMP)}`\n\n看这张图', text)
        self.assertIn('![img01](<attachments/22222222-0000/img01.png>)', text)
        self.assertEqual((self.chat / 'attachments/22222222-0000/img01.png').read_bytes(), b'shot')
        self.assertIn('<input>语音里说的</input>', text)   # 认不出的标签原样留着
        self.assertIn('上下文压缩后的摘要', text)
        self.assertIn('过程说明\n\n最终答复', text)
        for dropped in ('开发者指令', '环境', '规则', '界面状态', '命令', '<image>', '摘要正文', 'QUJD', '内部审批'):
            self.assertNotIn(dropped, text)
        self.assertEqual(stats['不是对话（内部会话或只有一方发言）'], 1)
        self.assertEqual(len(list(self.chat.glob('*.md'))), 1)

    def test_workbuddy_keeps_user_query_and_saves_blob_images(self):
        """防的是：WorkBuddy 把系统提示和用户的话写在同一段被整段照抄，被打断的 aborted 被当成回复，图片丢失。"""
        blob_id = 'ab' * 32
        (self.home / '.workbuddy/blobs/ab').mkdir(parents=True)
        (self.home / f'.workbuddy/blobs/ab/{blob_id}.png').write_bytes(b'blob')
        folder = self.home / '.workbuddy/projects/demo'
        folder.mkdir(parents=True)
        rows = [
            {'type': 'message', 'role': 'user', 'timestamp': 1700000000000, 'cwd': str(self.root),
             'content': [{'type': 'input_text', 'text': '<system-reminder data-role="ctx">塞进来的提示</system-reminder>\n'
                                                       '<user_query>这两个方案有什么区别？</user_query>'},
                         {'type': 'image_blob_ref', 'blob_id': blob_id, 'mime': 'image/png'}]},
            {'type': 'message', 'role': 'assistant', 'timestamp': 1700000013400,
             'content': [{'type': 'output_text', 'text': 'aborted'}]},
            {'type': 'message', 'role': 'user', 'timestamp': 1700000013420,
             'content': [{'type': 'input_text', 'text': '<system-reminder data-role="retry">continue</system-reminder>'}]},
            {'type': 'reasoning', 'timestamp': 1700000016000, 'content': [{'type': 'text', 'text': '不该出现的推理'}]},
            {'type': 'message', 'role': 'assistant', 'timestamp': 1700000054000,
             'content': [{'type': 'output_text', 'text': '是同一条路线。'}]},
        ]
        (folder / 'c0ffee12-0000.jsonl').write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows),
                                                   encoding='utf-8')
        self.export()
        target = next(self.chat.glob('*_testbox_WorkBuddy_c0ffee12.md'))
        text = target.read_text(encoding='utf-8')
        self.assertIn('这两个方案有什么区别？\n\n![img01](<attachments/c0ffee12/img01.png>)', text)
        self.assertIn('是同一条路线。', text)
        for dropped in ('塞进来的提示', 'aborted', 'continue', '不该出现的推理'):
            self.assertNotIn(dropped, text)
        self.assertEqual((self.chat / 'attachments/c0ffee12/img01.png').read_bytes(), b'blob')

    def test_export_full_strips_base64_and_stays_local(self):
        """防的是：全量备份写进会同步的 .personal，或把整块图片 base64 原样塞进备份。"""
        self.claude_session('abcdef12-0000', [
            claude('user', [picture(b'x' * 3000), {'type': 'text', 'text': '问'}]),
            claude('assistant', [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': '列目录'}}]),
            claude('assistant', [{'type': 'text', 'text': '答'}]),
        ])
        stats = chat_archive.export_full(self.root, self.home, self.settings())
        self.assertEqual(stats['新存'], 1)
        packed = next((self.root / '.local/backups/sessions/workspace').glob('*_Claude_abcdef12-0000.jsonl.gz'))
        full = gzip.open(packed, 'rt', encoding='utf-8').read()
        self.assertIn('列目录', full)   # 工具调用留着
        self.assertIn('chars omitted]', full)
        self.assertNotIn(base64.b64encode(b'x' * 3000).decode()[:100], full)
        self.assertFalse((self.root / '.personal').exists())
        self.assertEqual(chat_archive.export_full(self.root, self.home, self.settings())['已是最新'], 1)

    def test_index_lists_real_files_and_hides_local_only_titles(self):
        """防的是：总索引漏列或列出不存在的文件，或把只留本机路径里的对话首句写进会同步的索引。"""
        self.claude_session('aaaaaaaa-0000', [claude('user', '本工作区的第一句'),
                                              claude('assistant', [{'type': 'text', 'text': '答'}])])
        self.claude_session('bbbbbbbb-0000', [claude('user', '店铺的第一句'),
                                              claude('assistant', [{'type': 'text', 'text': '答'}])],
                            cwd=self.elsewhere / 'shop')
        self.export(everything=True)
        (self.chat / 'notes.md').write_text('没有日期的手写记录', encoding='utf-8')
        single = f'{self.day}_testbox_Claude_aaaaaaaa_INDEX.md'
        (self.chat / single).write_text('主题、结论、原文位置', encoding='utf-8')
        (self.chat / 'orphan_INDEX.md').write_text('找不到全文的单次索引', encoding='utf-8')
        settings = self.settings()
        self.assertEqual(chat_archive.build_index(self.root, settings), 4)
        index = (self.chat / '00_index.md').read_text(encoding='utf-8')
        rows = [line for line in index.splitlines() if line.startswith('| ') and not line.startswith('| 日期')]
        mine = next(row for row in rows if '本工作区的第一句' in row)
        self.assertTrue(mine.endswith(f'[打开](<{single}>) |'))   # 单次索引挂在全文那一行
        self.assertIn('Claude Code', mine)
        self.assertIn('【shop】店铺的第一句', index)
        self.assertEqual(index.count('aaaaaaaa_INDEX'), 1)
        self.assertEqual(sorted(row.split(' | ')[1] for row in rows[-2:]), ['notes', 'orphan_INDEX'])   # 没有日期的排最后
        self.assertEqual(chat_archive.build_index(self.root, settings), 4)   # 总索引自己不被收录

        settings['local_only'] = settings['local_only'] + ['.personal/logs/chat-exports/other-projects/shop/']
        chat_archive.build_index(self.root, settings)
        index = (self.chat / '00_index.md').read_text(encoding='utf-8')
        self.assertIn('【shop】（只留本机）', index)
        self.assertNotIn('店铺的第一句', index)

    def test_cli_reads_sessions_from_the_given_home(self):
        """防的是：命令行忽略 CHAT_ARCHIVE_HOME，去读真实用户目录下的会话。"""
        self.claude_session('abcdef12-0000', [claude('user', '命令行导出'),
                                              claude('assistant', [{'type': 'text', 'text': '答'}])])
        env = {**os.environ, 'CHAT_ARCHIVE_HOME': str(self.home), 'PYTHONIOENCODING': 'utf-8'}
        done = subprocess.run([sys.executable, str(ROOT / 'tools/chat_archive.py'), '--root', str(self.root), 'export'],
                              env=env, capture_output=True, encoding='utf-8', errors='replace')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('Claude Code 导出 1', done.stdout)
        self.assertEqual([path.name for path in self.chat.glob('*.md')], [f'{self.day}_testbox_Claude_abcdef12.md'])


if __name__ == '__main__':
    unittest.main()
