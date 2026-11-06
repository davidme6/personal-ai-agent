# 存储地图

## 公开框架（本仓库）

- `AGENTS.md`：所有 AI 的统一入口和规则
- `config/`：示例配置（`agent.example.json`、`project-registry.example.json`）、隐私与存储说明
- `workflows/`：开工、收尾、G0–G6 改造流程
- `templates/`：复制到私有工作区的模板
- `tools/`、`tests/`：工具和测试
- `docs/`：说明文档

## 你的数据：`.personal/`

| 路径 | 内容 | 私有仓模式下 |
|---|---|---|
| `agent.json` | 你的设置：私有仓、云盘、定时时间、只留本机的路径 | 进私有仓 |
| `profile.md` | 稳定的偏好和边界 | 进私有仓 |
| `current-state.md` | 全局唯一当前状态 | 进私有仓 |
| `project-registry.json`、`projects/` | 项目登记和各项目入口、状态、证据 | 进私有仓 |
| `memory/` | 长期记忆、`open-loops.md` 未闭环事项 | 进私有仓 |
| `handoff/` | 按日期的交接 | 进私有仓 |
| `logs/chat-exports/` | 逐字对话、附件、总索引 `00_index.md` | 进私有仓 |
| `logs/work-logs/`、`logs/task-cards/`、`logs/trace/` | 工作日志、任务卡、变更留痕 | 进私有仓 |
| `knowledge/` | 你整理过的知识 | 进私有仓 |
| `files/` | 原件资料库（文档、表格、PDF、音视频） | 不进 Git，走主资料库（推荐飞书）和副仓（推荐 Google Drive） |
| `feishu-map.json` | 飞书同步的基线登记（上次同步时每个文件的版本） | 进私有仓 |
| `inbox/` | 副仓（如 Google Drive）收件箱落地处：网页版 AI 先存的内容，取回核对后并入对应目录 | 不进 Git |
| `private/` | 只留本机的隐私文件 | 不进 Git、不上云 |

## 只在本机

- `.device/device.json`：本机路径和设备名
- `.device/secrets/`：凭据或加密保险库的指针
- `.local/`：语义索引、全量会话备份、日志、留痕快照

不要在各家 AI 的本机运行目录里再存一份状态；它们只放指向本项目的指针。
