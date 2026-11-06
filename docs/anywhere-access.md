# 随时随地存，随时随地用

目标：在任何一台电脑、任何一个 AI 窗口里，都能读到同一份规则、状态和记忆，做完的事也能写回去。

## 推荐组合：各管各的，都可替换

| 角色 | 放什么 | 推荐 | 为什么 |
|---|---|---|---|
| 程序 + 状态 | `AGENTS.md`、工具、模板；`.personal/` 里的当前状态、记忆、未闭环事项、任务卡、工作日志、逐字对话 | 你自己的 **私有** GitHub 仓（可换 GitLab、Gitee、自建 Git） | 几个 AI 和几台设备都在改，Git 能合并、能回退，提交前还能查密钥 |
| 主资料库 | `.personal/files/` 里的文档、表格、PDF、图片、音视频原件，存完整版 | 你自己的飞书云空间（`feishu_sync.py`；可换 Google Drive、OneDrive 等，用 `cloud_sync.py`） | 体积大、二进制、主要是查阅，不适合放 Git；飞书在国内访问稳定、空间大、能在线预览 |
| 副仓 / 临时仓 | 连不上主资料库的平台（如网页版 ChatGPT）先存的内容 | Google Drive 的收件箱文件夹（`cloud_sync.py`） | 网页版 AI 大多能连 Google Drive，连不上飞书；本机归档时取回并入主库 |
| 兜底备份（可选） | 整个项目文件夹 | 夸克网盘客户端的自动备份（可换百度网盘、OneDrive、移动硬盘） | 同步不是备份；误删、损坏时还能找回 |
| 只留本机 | `.personal/private/`、`.device/`（含凭据）、`.local/`（索引、全量会话备份、日志） | 不同步 | 泄露代价高，或者每台机器各自生成 |

同一个文件只认一个主档：文字状态以私有仓为准，原件以主资料库为准。账号全是你自己的，用哪几层你自己定。

## 本机 AI（Claude Code、Codex、Cursor 等）

1. 开工：`python tools/sync.py pull`，然后让 AI 读 `AGENTS.md`。
2. 干活：AI 按 `AGENTS.md` 读状态、写日志。
3. 收尾说“归档本地云端”：AI 跑 `refresh.py` 归档，再 `sync.py push` 推到私有仓。
4. 就算忘了，定时任务每天也会自动归档和同步（见 [定时自动化](automation.md)）。

## 另一台电脑

```bash
git clone https://github.com/<你的用户名>/my-agent-private.git my-agent
cd my-agent
python tools/init_workspace.py      # 只补本机文件（.device/、.local/），不覆盖同步来的状态
python tools/install_git_hooks.py
python tools/install_schedule.py    # 每台电脑各装一次定时任务
```

路径可以和第一台不一样：共享文件里只写相对路径，本机路径记在 `.device/device.json`。

## 网页版 AI（ChatGPT、Claude 网页版等）

- **读**：在网页版里连接 GitHub，授权它读你的私有仓，然后说“先读 AGENTS.md 和 .personal/current-state.md”。
- **写**：网页版一般不能直接改你的仓库，也大多连不上飞书。约定一个“收件箱”：让它把要存的内容整理成文件，放到副仓（如 Google Drive）的 `inbox/<来源>/` 文件夹（或者直接复制给你）；下次本机 AI 归档时把收件箱里的东西取回本机、归到对应目录，再随私有仓和主资料库同步，核对无误后清掉收件箱里已取回的，避免两边重复。
- 网页版没有本机会话文件，归档时由它逐轮写下原文，拿不到的标【原文不可取得】。

## 手机

手机上的 GitHub App 或网页能直接看私有仓里的状态和日志；需要 AI 接着做时，用支持连 GitHub 的 AI App 读私有仓。

## 换设备、换模型不丢上下文的关键

- 全局只有一份 `current-state.md`，不要在各家 AI 的本机目录里各存一份“最新状态”。
- 换之前把还没落盘的决定写进 `.personal/handoff/YYYY-MM-DD.md` 或任务卡。
- 同一份文件尽量不要两台设备同时改；真的同时改了，`sync.py` 会三方合并，合不上的保留本机并列出来交给你定。
