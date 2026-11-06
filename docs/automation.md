# 定时自动化：每天自动归档

装一次，以后不用记得归档。每台电脑各装一次（计划任务登记在各自系统里，不随仓库同步）。

```bash
python tools/install_schedule.py --dry-run     # 只打印将要装的任务，不改系统
python tools/install_schedule.py               # 安装
python tools/install_schedule.py --uninstall   # 卸载
```

| 任务 | 默认时间 | 跑什么 | 条件 |
|---|---|---|---|
| 变更留痕 | 每 10 分钟 | `trace_changes.py scan --trigger scheduled` | 总是 |
| 归档刷新 | 每天 09:00、18:00 | `refresh.py`：导出所有 AI 的逐字对话 → 更新语义索引 → 生成总索引 → 列出超过 30 天没核实的记忆 | 总是 |
| 同步私有仓 | 每天 18:30 | `auto_sync.py`：只提交“归档与状态”路径，先拉后推，推送后回读核对 | `.personal/agent.json` 里设了 `private_remote` |
| 飞书主资料库同步 | 每天 18:40 | `feishu_sync.py sync --scheduled`：只做没有冲突的动作，从不删除云端 | `feishu.enabled` 和 `feishu.auto_sync` 都为 `true`；还没手动 `plan` + 确认过账号和文件夹时，定时任务会跳过并写日志 |
| 云盘同步（Drive 等） | 每天 18:45 | `cloud_sync.py run --scheduled` | 设了 `cloud.remote` |
| 兜底备份 | 网盘客户端自己定 | 夸克等网盘客户端的“自动备份文件夹”，选整个项目目录 | 你自己在客户端里打开 |

时间在 `.personal/agent.json` 的 `schedule` 里改，改完重新运行一次安装器。

## 各系统怎么装的

- **Windows**：任务计划程序里的 `\PersonalAgent\` 文件夹；用 `pythonw.exe` 无窗口运行；开启“错过后尽快运行”，关机错过的开机补跑。
- **macOS / Linux**：写进当前用户 crontab 的一个带标记的区块，重复安装只替换这个区块，不动你的其他 cron 行。cron 不会补跑关机期间错过的任务；笔记本经常合盖的，可以把归档时间挪到你通常开机的时段。

## 出了问题看哪里

| 文件 | 内容 |
|---|---|
| `.local/refresh_report.txt` | 最近一次归档刷新的报告（每一步成功与否、过期记忆清单） |
| `.local/refresh.log` | 每次归档刷新一行 |
| `.local/auto_sync.log` | 每次自动同步一行：完成 / 跳过 / 停止原因 |
| `.local/feishu_sync.log` | 飞书同步记录 |
| `.local/cloud_sync.log` | 云盘同步记录 |
| `.personal/logs/trace/<设备名>/` | 变更留痕流水 |

自动同步遇到任何问题（冲突、门禁不通过、疑似密钥、云端刚有新提交）都会撤销暂存、保留你的改动、写日志并尽量弹系统通知，**绝不硬推、不绕过钩子**。看到通知后对 AI 说“归档本地云端”，人工处理。

## 无人值守的边界

- 自动同步只提交 `auto_sync_paths` 里的路径（默认是日志、记忆、交接和当前状态）；代码和配置的改动留给你和 AI 走任务卡提交。
- 同步私有仓（`auto_sync.py`）和飞书同步（`feishu_sync.py`）不会删除任何云端文件，也不会覆盖两边都改过的文件。
- 云盘同步（`cloud_sync.py`，rclone bisync）会双向传播删除，靠删除阈值和本机侧备份兜底，见 [同步与差异](sync-and-diff.md)；不放心就不要给它装定时任务，手动 `plan` 后再 `run`。
- 飞书和云盘的定时同步出问题时只写日志、不弹通知（`.local/feishu_sync.log`、`.local/cloud_sync.log`），隔几天看一眼。
