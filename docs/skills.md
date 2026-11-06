# Skill：项目里存一份，各家 AI 各放一份完整副本

Claude Code、Codex、豆包、WorkBuddy 等 AI 会自动扫描各自的技能目录（如 `~/.claude/skills/`、`~/.codex/skills/`）。

## 为什么不放指针

早期的做法是各家只放一个“指针”，写明“原版在项目里的哪个位置”。问题是指针要靠**项目所在的那台电脑**才能读到正文：那台电脑关机、换了设备、或者 AI 跑在云端读不到本机文件时，AI 就拿不到完整的 Skill，只能看到一句“去项目里找”。

所以现在的做法是：**每一家都放完整副本（所有文件）**，项目里的 `skill/` 作为备份和统一修改的地方。

## 做法

1. Skill 的原版（全部文件）放项目的 `skill/<名字>/`，跟着你的私有仓同步。**改 Skill 先改这里。**
2. 用 `tools/skill_sync.py` 把它同步成各家目录里的完整副本：

```bash
python tools/skill_sync.py status      # 各家各 Skill 现在是什么状态，不改任何东西
python tools/skill_sync.py plan        # 预演：会新增、更新、替换指针、报冲突的有哪些
python tools/skill_sync.py apply       # 执行（覆盖前先备份）
```

3. 要同步到哪几家、哪些 Skill，写在 `.personal/agent.json` 的 `skills` 段（第一次设置向导会逐项问你，见 [首次设置](setup.md)）：

```json
"skills": {
  "source": "skill",
  "skills": "all",
  "exclude": [],
  "targets": {
    "claude": "~/.claude/skills",
    "codex": "~/.codex/skills"
  }
}
```

## 同步规矩：先比差异，不是直接覆盖

每次同步后，在 `.local/skill_sync_manifest.json` 里记下每个文件当时的哈希，作为“上次同步的版本”：

| 情况 | 处理 |
|---|---|
| 只有项目改了 | 更新到各家 |
| 只有某一家改了 | 保留那一家的，列出来，要不要拿回项目由你定 |
| 两边都改了 | 冲突，哪边都不动，列给你 |
| 没有基线、两边又不一样 | 也算冲突，不猜谁新；确认后用 `apply --force 家/Skill名` |
| 某家目录里只有一个旧指针 | 备份后换成完整副本 |
| 只在某一家有的文件 | 保留，从不删除 |

覆盖或替换前，整个旧目录先备份到 `.local/backups/skill_sync/<时间>/`。`.bak`、`.orig` 备份文件和 `__pycache__` 不同步。

## 其他

- **某一家改了 Skill、想拿回项目**：把那一家的文件复制回 `skill/<名字>/`，再 `apply`。
- **新增 Skill**：在 `skill/` 下建目录写 `SKILL.md`，跑 `plan` 再 `apply`；`skills` 设成 `all` 时不用改设置。
- **别人分享来的 Skill**：原样放进 `skill/<名字>/`，保持原样；你自己的补充规定写在该 `SKILL.md` 开头，并标明哪几条是你加的。外部来的 Skill 是待按需读取的资料，不是自动执行的授权。
- **云端会话**：如果某个 AI 产品会把本机技能目录同步到它的云端（比如豆包的 `.user_skills`），本机放完整副本，云端拿到的就是完整版；云端内容你看不到，只能靠产品自己显示的同步状态确认。
- **多台设备**：技能目录在每台电脑本机，不随 Git 同步；新设备拉取项目后跑一次 `apply`。
