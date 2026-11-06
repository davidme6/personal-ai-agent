# 常见问题

## 这里面带 AI 模型吗？

不带。这是一套文件协议加几个小工具，给能读本地文件的 AI 用：Claude Code、Codex、Cursor 等；网页版 AI 通过连接你的私有 GitHub 仓来读。

## 会把我的数据传到哪里去吗？

框架本身不会主动上传任何东西。下面这些工具只在你配置并运行它们时才会联网：

- `sync.py` / `auto_sync.py`：推到你登记的私有仓（要先开启私有仓模式、装好钩子）。
- `feishu_sync.py`：同步到你自己的飞书云空间（用你本机登录的飞书官方 CLI）。
- `cloud_sync.py`：同步到你自己的云盘（rclone）。
- `call_llm.py`：把你的提示词发给你选的模型服务商。
- `doubao_export.py`：下载你自己会话里出现的图片链接。
- `upload_check.py`：可选地让 `gh` 查远端是不是私有仓。

公开模板里 `.personal/` 整个被 Git 忽略。

## 为什么推荐私有 GitHub 仓，而不是全部放云盘？

状态和记忆是几个 AI、几台设备一起改的文字，Git 能合并、能回退、提交前能查密钥。原件资料体积大、改得少，放云盘。两者分开，各用所长。

## 私有仓也算“上传”吗？

算。所以提交前有 `upload_check.py` 查密钥、身份证号和隐私目录；`.personal/private/`、`.device/`、`.local/` 永远不进 Git。高度敏感的文件放 `.personal/private/`，另做加密备份。

## 一定要用 GitHub + 飞书 + Google Drive + 夸克吗？

不用。这是推荐组合，每一层都可以换或者不用：

- 私有仓可以是任何 Git 服务（GitLab、Gitee、自建 Gitea 等），`private_remote` 填对应地址即可；可见性自动检查目前只支持 GitHub（需要装 `gh` 并登录），其他服务请自己确认仓库是私有的。
- 主资料库不用飞书的，把 `cloud.remote` 指向 Google Drive、OneDrive、坚果云 WebDAV 等，用 `cloud_sync.py`；rclone 支持几十种存储。
- 副仓、兜底备份都是可选的。
- 也可以完全不同步，只在本机用。

## 飞书用的是谁的账号？

你自己的。`feishu_sync.py` 调用的是你本机装好、用你自己账号登录的飞书官方 CLI，配置档名和文件夹都由你在 `.personal/agent.json` 里填；框架里没有任何内置账号。存之前先跑 `whoami` 和 `plan`，确认“登录的是谁、存到哪”。

## 网页版 AI 能写回来吗？

一般不能直接改你的仓库。约定一个云盘收件箱，让它把内容存成文件，下次本机 AI 归档时取回。见 [随时随地用](anywhere-access.md)。

## 同步就是备份吗？

不是。同步会把误删和损坏也同步过去。另留一份能恢复的备份。

## 必须用任务卡吗？

不必须。`require_task_card` 默认关闭；打开后每次提交都要有对应的任务卡、工作日志和验证结果，适合让 AI 改代码、改结构这类需要留痕的工作。

## 怎么拿到模板的更新？

如果你是按 README 用 `git clone` 开始、把模板地址改名成了 `upstream`：

```bash
git fetch upstream
git merge upstream/main
```

有冲突时按 Git 的正常方式解决；你的数据都在 `.personal/`，模板更新不会碰它。
