# 首次设置：每一项都问你，不替你选

每个人的习惯和存储选择都不同：有人用 GitHub 有人用 GitLab，有人用飞书有人用 Google Drive，有人想让密钥也同步、有人绝对不想。所以本框架推荐的组合（GitHub 私有仓 + 飞书 + Google Drive + 夸克）**只是推荐，不是绑定**。第一次使用时，所有设置都要逐项问你，由你自己选。

## 怎么做首次设置

**自己在终端里做：**

```bash
python tools/setup_wizard.py
```

一次问一项。每项先讲清楚“这是什么、有哪些选择、各自的代价”，再让你选编号。回车 = 采用界面上标明的默认值，输入 `s` 跳过这一项（保持当前值）。

**让 AI 带你做：**

> 先读 `AGENTS.md`，按“首次设置”一项一项问我怎么选，不要替我选。

AI 会运行 `python tools/setup_wizard.py --questions` 拿到当前该问的问题（含每个选项的代价说明和本机检测到的值），一次只问你一项；你回答后它用 `python tools/setup_wizard.py --answer 问题编号=值` 写入。你说“你定”它才会用推荐值，并告诉你选了什么。

**其他命令：**

```bash
python tools/setup_wizard.py --status              # 已回答的和待回答的
python tools/setup_wizard.py --redo main_library   # 把某一项重新设为待答，之后重新问
python tools/setup_wizard.py --answers answers.json   # 从文件批量写入（有一项不合格就整批不写）
```

设置写在 `.personal/agent.json`。向导**只改设置文件**（开启私有仓模式时再写 `.personal/.gitignore`），不联网、不装定时任务、不上传任何东西。问完后它会按你的回答打印一份“下一步”清单，每一步你同意了再做。

## 会问你什么

| 问题 | 你要决定的事 | 选项和代价 |
|---|---|---|
| `private_remote` | 状态和记忆要不要推到你自己的私有 Git 仓库 | 填仓库地址（GitHub / GitLab / Gitee / 自建都行，**必须是私有仓**）→ 任何设备、网页版 AI 能读到你的状态；留空 → 只在本机用，不同步 |
| `archive_sources` | 导出哪些 AI 的本机会话 | Claude Code / Codex / WorkBuddy / 豆包（豆包仅 Windows）。向导会标出本机检测到的 |
| `archive_all_projects` | 只收本工作区的会话，还是本机所有项目的会话 | 全收更完整，但会把你在别的项目里的对话也带进来 |
| `archive_local_only_projects` | 哪些项目的会话只存本机、不进 Git（上一项选“全收”才问） | 含私人内容、他人聊天的项目建议只留本机 |
| `main_library` | 原件资料（文档、表格、PDF、音视频）的主资料库 | **飞书**（推荐，需自己装飞书官方 CLI 并登录）/ **rclone 云盘**（Google Drive、OneDrive、Dropbox、WebDAV、S3…）/ **只放本机** |
| 飞书细节 | CLI 名称、配置档、目标文件夹、要不要每日自动同步 | 选了飞书才问；没填文件夹之前 `feishu_sync` 会提示；自动同步仍需先手动 `plan` + 确认一次 |
| `main_rclone_remote` | rclone 远端名 | 选了 rclone 才问 |
| `inbox_remote` | 要不要副仓 / 收件箱（推荐 Google Drive） | 给连不上主资料库的网页版 AI 先存东西；不用就留空 |
| **哪些不自动上传** | 见下一节 | 用了任一云端才问 |
| `require_task_card` | 提交前是否强制任务卡 | 开了更规范、AI 改代码有留痕，但每次提交要多写几项记录 |
| `schedule` | 定时任务的时间 | 默认 09:00 / 18:00 归档、每 10 分钟留痕；也可自己定 |
| `skill_targets` | Skill 完整副本同步到哪几家 AI | Claude / Codex / WorkBuddy / 豆包工作；不放指针，哪台电脑关机都能用，见 [Skill](skills.md) |
| `backup` | 有没有兜底备份 | 夸克等网盘客户端自动备份整个项目文件夹 / 别的方式 / 没有。只记录；**同步不是备份** |

## 哪些不自动上传

这是你最该认真选的一组。先记住固定的规矩：

- **`.personal/private/` 个人隐私文件夹永远不上传**，不进 Git，也不进任何云端。这一点固定，关不掉。
- **Git 一侧永远不收**凭据、隐私目录和原件资料（提交前的安全阀会拦）。下面的选择只管“自动上传到你自己的云端”。

然后向导会逐项问：

| 问题 | 说明 |
|---|---|
| `sync_credentials` | 密钥、令牌、`.env`、`*.pem`、`*.key`、名字里带 secret / password / 密码 的文件，要不要上传到你自己的云盘。**不传（推荐）**：丢了可以重新生成，也少一条泄露路径。**传**：别的 AI、别的设备能从你的云盘取到配置；代价是云盘一旦被访问就等于泄露，务必开两步验证。选“传”会再让你确认一次 |
| `sync_media` | **默认不限制任何文件格式**：音频、视频、压缩包等一律照常上传。只有你选择“排除音视频”（体积大、可能含隐私时）才不传。除此之外，只有 `.tmp`、`~$` 锁文件、`.DS_Store` 这类系统垃圾会被跳过——它们同步了只会制造冲突 |
| `sync_exclude_paths` | 还有哪些文件夹要留在本机，比如 `.personal/files/客户名单/`（追加到 `local_only`） |
| `sync_exclude_patterns` | 还有哪些文件名模式不上传，比如 `*.zip, 合同*` |

这些最终写进 `.personal/agent.json` 的 `sync_exclude` 和 `local_only`，飞书同步（`feishu_sync.py`）和云盘同步（`cloud_sync.py`）都会照做。对话归档里明显的密钥格式（如 `sk-…`、`ghp_…`）导出时本来就会被打码。

## 设置完成以后

- `setup.completed` 变成 `true`；`check_workspace.py` 不再提醒。
- 想改任何一项：`--redo 问题编号` 重新问，或直接改 `.personal/agent.json`。
- 向导最后打印的“下一步”通常包括：`git remote` 命令、`install_git_hooks.py`、`install_schedule.py --dry-run`、`skill_sync.py plan`、`feishu_sync.py whoami` / `plan` 或 `cloud_sync.py init`。每一步都联网或改系统，由你（或你授权的 AI）逐步执行。
