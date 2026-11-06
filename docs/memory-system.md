# 记忆系统

AI 换一个窗口就忘，靠的不是“让它记性更好”，而是把要记的东西写成文件，并且规定好：什么写在哪、什么时候读哪一层、旧的怎么作废。

一句话原则：**长期记忆回答“现在是什么状态”，原始数据回答“为什么这么认为”。**

思路借鉴了公开的 AI 记忆产品设计（原文、综合状态、按需检索分层），具体落成了下面几个普通的 Markdown 文件和目录，不依赖任何数据库。

## 五层逻辑

```text
① Source 原始数据 → ② Event 事件 → ③ State 当前状态 → ④ Router 检索路由 → ⑤ Working Context 本次上下文 → AI
```

这是逻辑上的分层，不对应五个新目录。每一层落在哪：

| 层 | 管什么 | 在哪 |
|---|---|---|
| ① Source 原始数据 | 没加工过的一手内容 | `.personal/logs/chat-exports/`（逐字对话和附件）、`.personal/files/`（原件）、各项目目录里的证据 |
| ② Event 事件 | 什么时候发生了什么、谁做了什么 | `.personal/logs/work-logs/`、`.personal/logs/task-cards/`、`.personal/logs/trace/`、`.personal/handoff/`（某一天停在哪） |
| ③ State 当前状态 | 综合后的“现在”，不是历史 | `.personal/current-state.md`、`.personal/memory/*.md`、`.personal/memory/open-loops.md`、`.personal/profile.md`、各项目的 `STATE.md` |
| ④ Router 检索路由 | 判断这次该用状态还是回原文 | `AGENTS.md` 第二节、本文、`tools/memory_recall.py`（语义定位的辅助） |
| ⑤ Working Context | 这次任务真正要用的信息 | 当前对话，按需组装，不把全部历史塞进来 |

不要再另建一套“项目记忆”目录：`.personal/memory/` 和 `current-state.md` 已经是状态层，再叠一套就会出现两份“最新”。

## 读的顺序：必读、按需、回源

| 层 | 什么时候读 | 读什么 |
|---|---|---|
| 必读 | 每次开工，控制在几千字以内 | `.personal/profile.md`、`.personal/current-state.md`、`.personal/memory/open-loops.md`；换窗口接手时加上最近一份 `.personal/handoff/YYYY-MM-DD.md` |
| 按需 | 按任务关键词匹配，匹配上才读 | `.personal/memory/` 下对应主题的文件、相关项目的 `ENTRY.md` 和 `STATE.md`、`.personal/knowledge/verified/` |
| 回源 | 明确需要证据时才读 | `.personal/logs/chat-exports/`、`.personal/files/`、项目证据、更早的交接和工作日志 |

步骤：

1. 先读必读层，拿到底线规则和当前状态。
2. 看这次任务的关键词，只打开匹配的按需层文件。
3. 必读加按需还不够，才去回源层，按日期或关键词精确找。
4. 找到就停，不接着读不相关的。

关键词怎么对到文件，举几个例子：

| 用户说 | 读哪 |
|---|---|
| “现在做到哪了” | `current-state.md` |
| “上次卡在哪” | `open-loops.md` |
| “我跟你说过周报怎么写” | `.personal/memory/` 里写作或偏好相关的文件 |
| “那个项目下一步是什么” | 该项目的 `STATE.md` |
| “上个月那次讨论原话怎么说的” | 回源：先 `memory_recall.py` 定位，再打开 `chat-exports/` 原文 |

## Router：什么时候直接用状态，什么时候必须回原文

**直接用状态**（不回源）：

- 问“现在什么情况”“做到哪了”“当前的规矩是什么”；
- 入口依次是 `current-state.md` → `open-loops.md` → `.personal/memory/` 对应主题。

**必须回原文**（不能凭印象或摘要回答），出现下面任一情况：

- 要精确的日期、时间；
- 要引用谁说过什么、原话；
- 要原始需求、当时的决策依据；
- 要具体数字、价格、指标；
- 要讲前后变化的过程；
- 几处信息互相矛盾；
- 要拿去对外当证据；
- 长期记忆里写得不够、没有来源，或者拿不准还对不对。

回原文的步骤：

1. 跑 `python tools/memory_recall.py "问题"` 做语义定位（没装索引就直接按日期、关键词在原文目录里找）。
2. **召回的片段只是线索**，必须打开它指向的原文核对，不能把片段直接当事实。
3. 还是找不到，就说找不到，不编。

状态和证据都要的时候，两层一起查，查完汇总再回答。

## 长期记忆怎么写

每条长期记忆（`.personal/memory/` 里的段落、`open-loops.md` 里的每一项）都要能追到来源、能判断过没过期。用一个标题开一条，标题下面写元数据，再写内容：

```markdown
## 周报格式
- status: active
- source: .personal/logs/chat-exports/2026-01-05_周报格式_ChatGPT网页版.md（第 3–5 轮）
- updated: 2026-01-05
- last_verified: 2026-02-10
- recorded_by: Claude Code

周报分三段：本周完成、下周计划、需要支持。每段不超过五条，不写过程。
```

| 字段 | 必填 | 意思 |
|---|---|---|
| `status` | 是 | `active` 现行有效；`superseded` 已被新的一条取代；`closed` 事项已闭环（未闭环事项用） |
| `source` | 决策类必填 | 指回原始讨论或官方材料的相对路径，能写到第几轮、哪一节更好。没有来源的写 `待核`，不能当正式结论用 |
| `updated` | 是 | 内容最后一次改动的日期 |
| `last_verified` | 建议 | 最后一次核实还对的日期。只是重新核实、内容没变时，只改这一项 |
| `superseded_by` | 取代时必填 | 被哪一条取代了，写标题和日期 |
| `recorded_by` | 建议 | 实际写入的是哪个 AI 或哪个人，和“这条信息是谁说的”分开 |

写的时候注意：

- 一条只写一件事，写结论，不写来回讨论的过程（过程在原文里）。
- 只追加，不覆盖别的条目；改自己这条时同时改 `updated`。
- 不写密码、密钥、证件号。
- 原始对话不直接变成长期记忆：只有用户明确定下的、或者核实过的，才写进来。知识类内容走 [知识源与验证](knowledge-sources.md)，不往记忆里堆。
- 旧文件不用一次改完，哪条用到、改到，就给哪条补齐元数据。

## 过期与取代

状态会变：“打算用 A 方案”过了一个月变成“已经换成 B 方案”。处理方式：

1. 旧条目改成 `status: superseded`，加一行 `superseded_by`，**原文留着不删**，方便以后追“当时为什么这么定”。
2. 另写一条新的，带新的 `source`、`updated`。

```markdown
## 资料库放哪（旧）
- status: superseded
- superseded_by: 「资料库放哪」2026-03-01
- source: .personal/logs/work-logs/2026-01-10_资料库选型.md
- updated: 2026-03-01

原件放本机移动硬盘。

## 资料库放哪
- status: active
- source: .personal/logs/chat-exports/2026-03-01_存储调整_Claude.md（第 2 轮）
- updated: 2026-03-01

原件放云盘，移动硬盘只做兜底备份。
```

- 很久以前的状态不能不经核实就当成今天的事实。`python tools/refresh.py` 每次会列出 `updated` / `last_verified` 超过 30 天的段落（`closed`、`superseded` 的不查）；其中涉及容易变的事（人员、价格、进度、别人的状态），引用之前先核实，再更新 `last_verified`。
- 把当天发生的事综合进状态层，是需要判断的活，由 AI 在会话里做（收尾见 `workflows/session-end.md`，一天的汇总见 `workflows/daily-review.md`）。脚本只做导出原文、建索引、列出过期段落这些机械的事，不自动改写记忆。

## 日常维护

- 交接记录多了（比如超过十来份），按月归一个文件夹，写一份月度摘要，原文不删。
- 一个记忆文件太大（比如超过 20KB），按主题拆开，在文件开头写清拆到了哪里。
- 隔一段时间合并重复的条目：保留一条 `active`，其余标 `superseded` 并指过去，不直接删。
- 语义索引 `.local/memory_index.db` 只是可重建的缓存，原文改了重跑 `python tools/memory_index.py`，见 [本地语义记忆](semantic-memory.md)。
