import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import skill_sync

POINTER = '---\nname: demo\ndescription: d\n---\n\n# demo（指针）\n\n本文件只是指针。原版在项目里。先读原版再执行。\n'


class SkillSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill-sync-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '工作 区'
        self.src = self.root / 'skill/demo'
        (self.src / 'references').mkdir(parents=True)
        (self.src / 'SKILL.md').write_text('---\nname: demo\ndescription: d\n---\n\n版本一\n', encoding='utf-8')
        (self.src / 'references/a.md').write_text('参考一', encoding='utf-8')
        self.claude = Path(self.temp.name) / 'claude-skills'
        self.codex = Path(self.temp.name) / 'codex-skills'
        self.config = self.root / 'cfg.json'
        self.config.write_text(json.dumps({'source': 'skill', 'skills': 'all',
                                           'targets': {'claude': str(self.claude), 'codex': str(self.codex)}}),
                               encoding='utf-8')

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = skill_sync.main(['--root', str(self.root), '--config', str(self.config), *args])
        return code, out.getvalue()

    def read(self, base, rel='SKILL.md'):
        return (base / 'demo' / rel).read_text(encoding='utf-8')

    def test_new_target_gets_full_copy_without_backup_files(self):
        """防的是：只放了 SKILL.md 而漏掉 references/ 和脚本，或把 .bak 备份文件也复制过去。"""
        (self.src / 'SKILL.md.bak-20261106').write_text('旧备份', encoding='utf-8')
        (self.src / '__pycache__').mkdir()
        (self.src / '__pycache__/x.pyc').write_bytes(b'x')
        code, _ = self.run_cli('apply')
        self.assertEqual(code, 0)
        for base in (self.claude, self.codex):
            self.assertEqual(self.read(base, 'references/a.md'), '参考一')
            self.assertFalse((base / 'demo/SKILL.md.bak-20261106').exists())
            self.assertFalse((base / 'demo/__pycache__').exists())

    def test_pointer_is_replaced_by_full_copy_after_backup(self):
        """防的是：旧的指针式 SKILL.md 没被换掉（设备一关 AI 就读不到正文），或换之前没留备份。"""
        (self.claude / 'demo').mkdir(parents=True)
        (self.claude / 'demo/SKILL.md').write_text(POINTER, encoding='utf-8')
        code, text = self.run_cli('apply')
        self.assertEqual(code, 0, text)
        self.assertIn('版本一', self.read(self.claude))
        backups = list((self.root / '.local/backups/skill_sync').glob('*/claude/demo/SKILL.md'))
        self.assertEqual(len(backups), 1)
        self.assertIn('指针', backups[0].read_text(encoding='utf-8'))

    def test_second_run_changes_nothing_and_project_update_flows_down(self):
        """防的是：重复运行反复改文件；项目里改了 Skill，各家没跟上。"""
        self.run_cli('apply')
        code, text = self.run_cli('plan')
        self.assertEqual(code, 0)
        self.assertNotIn('要写', text)
        (self.src / 'SKILL.md').write_text('---\nname: demo\ndescription: d\n---\n\n版本二\n', encoding='utf-8')
        code, _ = self.run_cli('apply')
        self.assertEqual(code, 0)
        self.assertIn('版本二', self.read(self.claude))
        self.assertIn('版本二', self.read(self.codex))

    def test_edit_on_one_agent_is_kept_not_overwritten(self):
        """防的是：某一家里手工改过的内容被项目版悄悄盖掉。"""
        self.run_cli('apply')
        (self.claude / 'demo/references/a.md').write_text('我在这一家改过', encoding='utf-8')
        code, text = self.run_cli('apply')
        self.assertEqual(code, 0)
        self.assertEqual(self.read(self.claude, 'references/a.md'), '我在这一家改过')
        self.assertIn('保留', text)

    def test_both_sides_changed_is_a_conflict_and_nothing_is_touched(self):
        """防的是：两边都改过时按“谁新谁赢”覆盖，丢掉一边的修改。"""
        self.run_cli('apply')
        (self.claude / 'demo/references/a.md').write_text('那一家的改法', encoding='utf-8')
        (self.src / 'references/a.md').write_text('项目的改法', encoding='utf-8')
        code, text = self.run_cli('apply')
        self.assertEqual(code, 1)
        self.assertIn('冲突', text)
        self.assertEqual(self.read(self.claude, 'references/a.md'), '那一家的改法')
        self.assertEqual(self.read(self.codex, 'references/a.md'), '项目的改法')   # 没冲突的那一家照常更新

    def test_force_overwrites_conflict_only_after_explicit_request_and_backs_up(self):
        """防的是：强制覆盖不留备份，或一次强制波及别的 Skill / 别的家。"""
        self.run_cli('apply')
        (self.claude / 'demo/references/a.md').write_text('那一家的改法', encoding='utf-8')
        (self.src / 'references/a.md').write_text('项目的改法', encoding='utf-8')
        code, _ = self.run_cli('apply', '--force', 'claude/demo')
        self.assertEqual(code, 0)
        self.assertEqual(self.read(self.claude, 'references/a.md'), '项目的改法')
        backups = list((self.root / '.local/backups/skill_sync').glob('*/claude/demo/references/a.md'))
        self.assertEqual(backups[0].read_text(encoding='utf-8'), '那一家的改法')

    def test_no_baseline_and_different_is_a_conflict(self):
        """防的是：第一次同步时，目标里已有不一样的版本，工具猜它是旧的就覆盖。"""
        (self.codex / 'demo/references').mkdir(parents=True)
        (self.codex / 'demo/SKILL.md').write_text('自己写的另一个版本，不是指针', encoding='utf-8')
        (self.codex / 'demo/references/a.md').write_text('参考一', encoding='utf-8')
        code, text = self.run_cli('apply')
        self.assertEqual(code, 1)
        self.assertEqual(self.read(self.codex), '自己写的另一个版本，不是指针')
        self.assertIn('冲突', text)

    def test_files_only_in_target_are_never_deleted(self):
        """防的是：同步时把那一家自己加的文件当成多余的删掉。"""
        self.run_cli('apply')
        (self.claude / 'demo/my-notes.md').write_text('自己的笔记', encoding='utf-8')
        self.run_cli('apply')
        self.assertEqual(self.read(self.claude, 'my-notes.md'), '自己的笔记')

    def test_only_and_agents_filters(self):
        """防的是：想只更新一个 Skill 或一家 AI 时，范围被扩大到全部。"""
        other = self.root / 'skill/other'
        other.mkdir(parents=True)
        (other / 'SKILL.md').write_text('---\nname: other\n---\n', encoding='utf-8')
        code, _ = self.run_cli('apply', '--only', 'demo', '--agents', 'claude')
        self.assertEqual(code, 0)
        self.assertTrue((self.claude / 'demo/SKILL.md').is_file())
        self.assertFalse((self.claude / 'other').exists())
        self.assertFalse(self.codex.exists())

    def test_missing_targets_setting_stops_with_instructions(self):
        """防的是：没配置要同步到哪里时悄悄什么都不做，用户以为已经同步了。"""
        self.config.write_text(json.dumps({'skills': 'all', 'targets': {}}), encoding='utf-8')
        code, text = self.run_cli('plan')
        self.assertEqual(code, 2)
        self.assertIn('targets', text)

    def test_target_inside_source_tree_is_skipped(self):
        """防的是：目标目录就是项目 skill/ 本身时，自己覆盖自己。"""
        self.config.write_text(json.dumps({'skills': 'all', 'targets': {'self': str(self.root / 'skill')}}),
                               encoding='utf-8')
        code, text = self.run_cli('apply')
        self.assertEqual(code, 0)
        self.assertNotIn('self', text.split('\n', 1)[1])


if __name__ == '__main__':
    unittest.main()
