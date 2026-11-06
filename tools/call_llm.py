#!/usr/bin/env python3
"""第三方大模型统一调用（OpenAI 兼容的 /chat/completions 接口），只用标准库。

  python tools/call_llm.py --provider deepseek --prompt "用三句话解释什么是向量数据库"
  python tools/call_llm.py --provider dashscope --model qwen-plus --system "你是严谨的审稿人" --prompt "..."
  echo "问题" | python tools/call_llm.py --provider moonshot --stdin
  python tools/call_llm.py --provider custom --base-url http://localhost:11434/v1 --model llama3 --prompt "hi"

内置预设：deepseek、dashscope（通义千问）、moonshot（Kimi）、zhipu（智谱）、openai、openrouter；
custom 是任意兼容服务（本地 Ollama、自建网关等），自己给 --base-url、--model，要密钥再加 --api-key-env。
预设也可以用 --base-url / --api-key-env / --model 覆盖（比如走公司代理）。

密钥只从环境变量读，脚本不写文件、不打印密钥；服务端报错里回显了密钥也会先打码再输出。
成功时把回复正文打印到标准输出；失败时把原因打印到标准错误，退出码 1。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# 预设：(接口地址, 密钥环境变量, 默认模型)。模型名会更新，用 --model 覆盖即可。
PROVIDERS = {
    'deepseek': ('https://api.deepseek.com', 'DEEPSEEK_API_KEY', 'deepseek-chat'),
    'dashscope': ('https://dashscope.aliyuncs.com/compatible-mode/v1', 'DASHSCOPE_API_KEY', 'qwen-plus'),
    'moonshot': ('https://api.moonshot.cn/v1', 'MOONSHOT_API_KEY', 'moonshot-v1-8k'),
    'zhipu': ('https://open.bigmodel.cn/api/paas/v4', 'ZHIPU_API_KEY', 'glm-4-flash'),
    'openai': ('https://api.openai.com/v1', 'OPENAI_API_KEY', 'gpt-4o-mini'),
    'openrouter': ('https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY', 'openai/gpt-4o-mini'),
    'custom': (None, None, None),
}


class LLMError(Exception):
    pass


def chat(base_url, api_key, model, messages, temperature=0.3, timeout=120):
    """发一次请求，返回回复正文；任何失败都抛 LLMError，消息里不含密钥。"""
    def hide(text):
        return text.replace(api_key, '***') if api_key else text

    body = json.dumps({'model': model, 'messages': messages, 'temperature': temperature},
                      ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = 'Bearer ' + api_key
    request = urllib.request.Request(base_url.rstrip('/') + '/chat/completions', data=body,
                                     headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', 'replace')[:800]
        raise LLMError(hide(f'[HTTP {error.code}] 调用失败：{detail}')) from None
    except urllib.error.URLError as error:
        raise LLMError(f'[网络错误] {error.reason}（检查网络和 --base-url）') from None
    except OSError as error:   # 超时等
        raise LLMError(f'[网络错误] {error}') from None
    try:
        content = json.loads(raw)['choices'][0]['message']['content']
    except (ValueError, KeyError, IndexError, TypeError):
        content = None
    if not isinstance(content, str):
        raise LLMError(hide('[解析失败] 返回里没有文字回复：' + raw[:500]))
    return content


def build_parser():
    parser = argparse.ArgumentParser(description='第三方大模型统一调用（OpenAI 兼容接口）')
    parser.add_argument('--provider', required=True, choices=sorted(PROVIDERS))
    parser.add_argument('--model', help='覆盖预设的默认模型；custom 必填')
    parser.add_argument('--base-url', help='接口地址（到 /v1 这一级）；custom 必填')
    parser.add_argument('--api-key-env', help='从哪个环境变量读密钥；custom 不填就不带密钥（本地服务）')
    parser.add_argument('--system', help='可选的 system 提示')
    parser.add_argument('--prompt', help='问题；不给就从标准输入读')
    parser.add_argument('--stdin', action='store_true', help='从标准输入读问题')
    parser.add_argument('--temperature', type=float, default=0.3)
    parser.add_argument('--timeout', type=int, default=120, help='秒')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    def stop(message):
        print(message, file=sys.stderr)
        return 1

    base, env_name, model = PROVIDERS[args.provider]
    base = args.base_url or base
    env_name = args.api_key_env or env_name
    model = args.model or model
    if not base:
        return stop('custom 必须给 --base-url')
    if not model:
        return stop('custom 必须给 --model')
    api_key = os.environ.get(env_name, '').strip() if env_name else ''
    if env_name and not api_key:
        return stop(f'环境变量 {env_name} 没有设置。先把密钥设成环境变量（不要写进任何文件），重开终端再试。')

    if args.prompt and not args.stdin:
        prompt = args.prompt
    else:
        if sys.stdin.isatty():
            print('输入问题，结束按 Ctrl+D（Windows 按 Ctrl+Z 再回车）：', file=sys.stderr)
        prompt = sys.stdin.read()
    prompt = prompt.strip()
    if not prompt:
        return stop('没有收到问题（用 --prompt 或 --stdin）')

    messages = [{'role': 'system', 'content': args.system}] if args.system else []
    messages.append({'role': 'user', 'content': prompt})
    try:
        print(chat(base, api_key, model, messages, args.temperature, args.timeout))
    except LLMError as error:
        return stop(str(error))
    return 0


if __name__ == '__main__':
    sys.exit(main())
