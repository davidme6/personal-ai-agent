# 多设备

每台设备可以把项目放在不同的绝对路径下。共享文件里只写相对路径，本机位置记在 `.device/device.json`。

## 推荐做法（私有仓模式）

1. 第一台设备按 README 开启私有仓模式并推送。
2. 其他设备 `git clone` 你的私有仓，运行 `python tools/init_workspace.py`（只补 `.device/`、`.local/` 等本机文件，不覆盖同步来的状态）。
3. 每台设备各装一次 `python tools/install_git_hooks.py` 和 `python tools/install_schedule.py`。
4. 开工先 `python tools/sync.py pull`，收尾 `python tools/sync.py push --message "说明" 文件…`（或交给每天的自动同步）。
5. 原件资料走飞书（`tools/feishu_sync.py`，每台设备用同一个飞书账号登录官方 CLI）或 rclone 云盘（`tools/cloud_sync.py`，每台设备配好同一个远端）。

## 注意

- 同一份文件尽量不要两台设备同时改。真的同时改了，`sync.py` 会三方合并，合不上的保留本机并列出来，不会覆盖。
- 刚开始用的时候，`current-state.md` 最好只由一台设备写，确认合并流程顺了再放开。
- 云盘同步盘里的锁文件不能当可靠的分布式锁。
- `.device/` 和 `.local/` 永远不同步；凭据每台设备各自配置。
- 同步不是备份，另留一份能恢复的副本。
