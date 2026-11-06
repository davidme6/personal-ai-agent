# 多模型接入

这套框架的规则、记忆、状态和知识都是 Markdown / JSON 文本，和具体用哪家模型无关。所以模型可以换，资产不丢。下面先分清三种“接入”，再讲怎么用 `tools/call_llm.py` 调第三方模型。

## 一、三种“接入”，别混为一谈

| 层面 | 含义 | 现状 | 做法 |
|---|---|---|---|
| ① 换执行入口 | 换一个 AI 客户端（Claude Code、Codex、Cursor、网页版 ChatGPT 等）来读同一套项目资产、接着干活 | 可以 | 打开工作区根目录，先读 `AGENTS.md`，按接手协议加载状态和资料。规则只有这一份，不为每个模型复制一套。各客户端能读写哪些文件、能不能跑脚本，要各自实测 |
| ② 工具式调用 | 当前干活的 AI 用 API 调另一个模型，做第二意见、交叉验证或补位 | 脚本就绪 | `python tools/call_llm.py`，OpenAI 兼容接口，只用标准库，密钥只从环境变量读 |
| ③ 自动选模型 | 按任务类型自动派给不同模型 | 暂不做 | 先用现成的 AI 客户端把真实任务跑顺，确认确实需要再做；自动路由会多一层要维护、要排错的东西 |

日常最常用的是①；②用在“重要结论让另一家独立再判一遍”；③不在本框架范围内。

## 二、四步接入（以 DeepSeek 为例，其他同理）

1. **拿密钥**：到厂商官网注册、创建 API Key（地址见下表）。密钥只出现在官网页面和你的环境变量里，不要贴进对话、文档或任何项目文件。
2. **设成环境变量**（变量名见下表）：
   - Windows：系统搜索“编辑账户的环境变量”→ 新建，变量名 `DEEPSEEK_API_KEY`，值粘贴密钥 → 确定，然后**重开**终端和 AI 客户端才生效。命令行的 `setx DEEPSEEK_API_KEY "..."` 也行，但密钥会留在命令历史里。
   - macOS / Linux：只在当前终端用，`read -rs DEEPSEEK_API_KEY && export DEEPSEEK_API_KEY`（输入时不回显、不进历史）；要长期生效，写进 `~/.zshrc` 或 `~/.bashrc`（在项目目录之外，不会被提交）。
3. **测试**：
   ```bash
   python tools/call_llm.py --provider deepseek --prompt "用三句话解释什么是向量数据库"
   ```
   能打印出回答就是接通了。报“环境变量没有设置”就是第 2 步没生效（多半是没重开终端）；报 `HTTP 401` 是密钥不对，`HTTP 402/429` 一般是余额或频率限制。
4. **记录**：在 `.personal/memory/` 里记一笔（比如一份“模型接入”笔记）：哪个 `--provider`、哪个模型、哪天接通、打算用来做什么、对应的环境变量名。**只写变量名，不写密钥本身。**

## 三、内置预设

| `--provider` | 厂商 | 默认模型 | 密钥环境变量 | 申请地址 |
|---|---|---|---|---|
| `deepseek` | DeepSeek | `deepseek-chat` | `DEEPSEEK_API_KEY` | platform.deepseek.com |
| `dashscope` | 阿里云通义千问（DashScope 兼容模式） | `qwen-plus` | `DASHSCOPE_API_KEY` | dashscope.aliyun.com |
| `moonshot` | 月之暗面 Kimi | `moonshot-v1-8k` | `MOONSHOT_API_KEY` | platform.moonshot.cn |
| `zhipu` | 智谱 GLM | `glm-4-flash` | `ZHIPU_API_KEY` | open.bigmodel.cn |
| `openai` | OpenAI | `gpt-4o-mini` | `OPENAI_API_KEY` | platform.openai.com |
| `openrouter` | OpenRouter（一个密钥调多家模型） | `openai/gpt-4o-mini` | `OPENROUTER_API_KEY` | openrouter.ai |
| `custom` | 任意 OpenAI 兼容服务：本地 Ollama、自建网关、其他厂商 | 自己给 `--model` | 自己给 `--api-key-env`（不要密钥的本地服务不用给） | — |

模型名和价格经常变，以官网为准；用 `--model 新模型名` 覆盖默认值即可。预设的接口地址也能用 `--base-url` 覆盖（比如走代理）。

常用写法：

```bash
# 带 system 提示，换模型
python tools/call_llm.py --provider dashscope --model qwen-max --system "你是严谨的审稿人" --prompt "..."

# 长材料用管道送进去
cat 方案.md | python tools/call_llm.py --provider moonshot --model moonshot-v1-32k --stdin

# 本地 Ollama（先在本机装好并拉取模型），不要密钥
python tools/call_llm.py --provider custom --base-url http://localhost:11434/v1 --model llama3 --prompt "hi"

# 其他兼容服务：密钥放在你自己起名的环境变量里
python tools/call_llm.py --provider custom --base-url https://api.example.com/v1 --api-key-env MY_LLM_KEY --model some-model --prompt "hi"
```

脚本成功时只把回复正文打印到标准输出，方便 AI 直接读；失败时原因打到标准错误、退出码 1。密钥不打印，服务端报错里回显了密钥也会先打码。

## 四、在工作流里怎么用

1. **第二意见 / 对抗式审查**：重要结论让另一家模型独立再判一遍，减少单一模型的偏差。也可以用 `tools/roundtable.py` 让本机的 Codex 进同一个记录文件发言，做“一家做、另一家审”。
2. **能力补位**：长文档交给长上下文模型，推理和代码交给擅长的模型，最后由主 AI 汇总。
3. **来源标注**：引用第三方模型的输出时写明“某模型于某日给出，属推断，待核实”，不直接当结论。
4. **留痕**：在工作日志里记下调了哪个 provider / 模型、问了什么、用在哪；不记密钥。
5. **花费**：按量计费的接口，长材料、多轮调用会累积。先用便宜模型试，必要时再换大模型；各家价格以官网为准。

## 五、让别的模型当“主脑”

1. 在目标客户端里打开工作区根目录（包含 `AGENTS.md` 和 `config/agent.example.json` 的那个目录），不要把 `skill/` 之类的子目录当根。
2. 让它先读 `AGENTS.md`，再按接手协议读 `.personal/current-state.md`、未闭环事项和本次任务的入口。
3. 客户端特有的差异（比如它读不了某类文件、不能跑脚本）写一行适配说明，不另起一套规则。

这样换模型、换电脑、换客户端，记忆和做法都能接上。
