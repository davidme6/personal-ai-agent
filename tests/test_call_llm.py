import io
import json
import os
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import call_llm

FAKE_KEY = 'test-key-not-real-0123456789'


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reply(text):
    return FakeResponse({'choices': [{'message': {'role': 'assistant', 'content': text}}]})


class CallLLMTests(unittest.TestCase):
    def run_main(self, argv, env=None, response=None, stdin=''):
        """跑 main，网络换成假的；返回 (退出码, 标准输出, 标准错误, 发出去的请求列表)。"""
        sent = []

        def fake_urlopen(request, timeout=None):
            sent.append(request)
            if isinstance(response, Exception):
                raise response
            return response

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(call_llm.urllib.request, 'urlopen', fake_urlopen), \
                mock.patch.object(sys, 'stdin', io.StringIO(stdin)), \
                redirect_stdout(out), redirect_stderr(err):
            code = call_llm.main(argv)
        return code, out.getvalue(), err.getvalue(), sent

    def test_key_comes_from_env_and_is_never_printed(self):
        """防的是：密钥被打印到终端或日志里（包括服务端报错把密钥原样回显的情况）。"""
        code, out, _, sent = self.run_main(['--provider', 'deepseek', '--prompt', '你好'],
                                           {'DEEPSEEK_API_KEY': FAKE_KEY}, reply('回答'))
        self.assertEqual((code, out), (0, '回答\n'))
        self.assertEqual(sent[0].get_header('Authorization'), 'Bearer ' + FAKE_KEY)
        self.assertEqual(sent[0].full_url, 'https://api.deepseek.com/chat/completions')

        echoed = urllib.error.HTTPError('https://api.deepseek.com', 401, 'Unauthorized', {},
                                        io.BytesIO(f'invalid key {FAKE_KEY}'.encode()))
        code, out, err, _ = self.run_main(['--provider', 'deepseek', '--prompt', '你好'],
                                          {'DEEPSEEK_API_KEY': FAKE_KEY}, echoed)
        self.assertEqual(code, 1)
        self.assertIn('HTTP 401', err)
        self.assertNotIn(FAKE_KEY, out + err)

    def test_missing_key_stops_before_any_request(self):
        """防的是：没设密钥时照样发请求，拿到一个看不懂的 401，或者用户以为要把密钥写进文件。"""
        code, _, err, sent = self.run_main(['--provider', 'moonshot', '--prompt', '你好'], {}, reply('x'))
        self.assertEqual(code, 1)
        self.assertEqual(sent, [])
        self.assertIn('MOONSHOT_API_KEY', err)

    def test_custom_local_service_works_without_a_key(self):
        """防的是：本地 Ollama 这类不要密钥的服务被强制要求密钥，或者拼错接口地址。"""
        code, out, _, sent = self.run_main(['--provider', 'custom', '--base-url', 'http://localhost:11434/v1/',
                                            '--model', 'llama3', '--prompt', 'hi'], {}, reply('hello'))
        self.assertEqual((code, out), (0, 'hello\n'))
        self.assertEqual(sent[0].full_url, 'http://localhost:11434/v1/chat/completions')
        self.assertIsNone(sent[0].get_header('Authorization'))
        code, _, err, sent = self.run_main(['--provider', 'custom', '--model', 'llama3', '--prompt', 'hi'])
        self.assertEqual((code, sent), (1, []))
        self.assertIn('--base-url', err)

    def test_stdin_system_and_model_override_are_sent(self):
        """防的是：--system 提示被丢掉、--model 没生效（按默认模型计费）、管道输入读不到。"""
        code, _, _, sent = self.run_main(['--provider', 'zhipu', '--model', 'glm-4-plus', '--system', '你是审稿人',
                                          '--stdin'], {'ZHIPU_API_KEY': FAKE_KEY}, reply('ok'), stdin='  问题正文\n')
        self.assertEqual(code, 0)
        body = json.loads(sent[0].data.decode('utf-8'))
        self.assertEqual(body['model'], 'glm-4-plus')
        self.assertEqual(body['messages'], [{'role': 'system', 'content': '你是审稿人'},
                                            {'role': 'user', 'content': '问题正文'}])

    def test_unexpected_response_is_an_error_not_a_crash(self):
        """防的是：服务返回结构不对（没有文字回复）时脚本抛出堆栈，或者把空回复当成功。"""
        for payload in (b'not json', {'choices': []}, {'choices': [{'message': {'content': None}}]}):
            code, out, err, _ = self.run_main(['--provider', 'openai', '--prompt', 'hi'],
                                              {'OPENAI_API_KEY': FAKE_KEY}, FakeResponse(payload))
            self.assertEqual((code, out), (1, ''))
            self.assertIn('解析失败', err)


if __name__ == '__main__':
    unittest.main()
