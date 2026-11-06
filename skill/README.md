# Skill 库

把你自己写的、或者别人分享来要用的 Skill 原样放在这里：`skill/<名字>/SKILL.md`（连同它的参考文件、脚本和素材）。

这里是**备份和统一修改的地方**。各家 AI 的技能目录（如 `~/.claude/skills/`、`~/.codex/skills/`）里放的是**完整副本，不是指针**，用 `python tools/skill_sync.py apply` 同步过去。原因、规矩和命令见 [docs/skills.md](../docs/skills.md)。

## 登记

| Skill | 用途 | 同步到哪些 AI | 备注 |
|---|---|---|---|
| [cloud-write-guard](cloud-write-guard/SKILL.md) | 往云端存东西之前的护栏：先查清账号和去处，用户确认后才存 | 按你的 `skills.targets` | 模板自带 |
| （示例）weekly-report | 写周报 | Claude、Codex | 自己写的 |
