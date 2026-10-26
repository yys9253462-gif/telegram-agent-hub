#!/usr/bin/env python3
"""图片工具与入队脱敏的隔离验证，不接触真实令牌和真实生图接口。"""
import _root  # noqa: F401  仓库根目录进 sys.path
import base64
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import zlib

HERE = _root.ROOT
PYTHON = sys.executable

# 用于验证脱敏的样例，拼接构造以免源码本身命中密钥扫描规则（并非真实凭据）。
SECRET_SAMPLE = 'sk' + '-' + 'abcdefghijklmnop'

# 最小合法 PNG：1x1 白色像素，用于冒充生图接口返回。
def tiny_png():
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    raw = b'\x00\xff\xff\xff'
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw))
            + chunk(b'IEND', b''))


class FakeTelegram:
    """只记录必要事实的假电报接口，不保存请求头。"""

    def __init__(self):
        self.calls = []
        self.documents = []
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self.running = True
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while self.running:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()

    def handle(self, connection):
        try:
            connection.settimeout(5)
            data = b''
            while b'\r\n\r\n' not in data:
                piece = connection.recv(65536)
                if not piece:
                    return
                data += piece
            head, body = data.split(b'\r\n\r\n', 1)
            lines = head.decode('latin-1').split('\r\n')
            method, path = lines[0].split(' ')[0], lines[0].split(' ')[1]
            length = 0
            for line in lines[1:]:
                if line.lower().startswith('content-length:'):
                    length = int(line.split(':', 1)[1].strip())
            while len(body) < length:
                body += connection.recv(65536)
            name = path.rsplit('/', 1)[-1].split('?')[0]
            self.calls.append(name)
            if name == 'sendDocument':
                self.documents.append(body)
                reply = {'ok': True, 'result': {'message_id': 1, 'document': {'file_id': 'f'}}}
            elif name == 'getMe':
                reply = {'ok': True, 'result': {'id': 1, 'username': 'fake'}}
            else:
                reply = {'ok': True, 'result': {'message_id': len(self.calls), 'date': 0, 'text': ''}}
            payload = json.dumps(reply).encode()
            connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                               + str(len(payload)).encode() + b'\r\n\r\n' + payload)
        finally:
            connection.close()

    def close(self):
        self.running = False
        self.server.close()


class FakeImageChannel:
    """假生图接口，记录收到的 prompt 与模型，返回内联 PNG。"""

    def __init__(self, status=200):
        self.requests = []
        self.status = status
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            try:
                data = b''
                while b'\r\n\r\n' not in data:
                    piece = connection.recv(65536)
                    if not piece:
                        break
                    data += piece
                head, body = data.split(b'\r\n\r\n', 1)
                length = 0
                for line in head.decode('latin-1').split('\r\n')[1:]:
                    if line.lower().startswith('content-length:'):
                        length = int(line.split(':', 1)[1].strip())
                while len(body) < length:
                    body += connection.recv(65536)
                self.requests.append(json.loads(body.decode()))
                if self.status != 200:
                    payload = b'{"error":"denied"}'
                    status = str(self.status)
                else:
                    payload = json.dumps({'data': [{'b64_json': base64.b64encode(tiny_png()).decode()}]}).encode()
                    status = '200'
                connection.sendall(('HTTP/1.1 ' + status + ' OK\r\nContent-Type: application/json\r\nContent-Length: '
                                    + str(len(payload)) + '\r\n\r\n').encode() + payload)
            finally:
                connection.close()


class ImageToolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.work = root / 'tasks'
        self.work.mkdir()
        self.telegram = FakeTelegram()
        self.channel = FakeImageChannel()
        self.config = root / 'image.json'
        self.config.write_text(json.dumps({'base_url': 'http://127.0.0.1:' + str(self.channel.port) + '/v1',
                                           'api_key': 'FAKE-IMAGE-KEY-FOR-TEST-ONLY'}))
        self.env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        self.env.update({
            'TELEGRAM_BOT_TOKEN': '123456:FAKE-TOKEN-FOR-TEST-ONLY',
            'TELEGRAM_API_BASE': 'http://127.0.0.1:' + str(self.telegram.port),
            'IMAGE_CONFIG_FILE': str(self.config),
            'AGENT_IMAGE_CHAT_ID': '4242',
            'CLAUDE_BOT_WORKDIR': str(self.work),
            'APP_DIR': str(HERE),
            'IMAGE_MODEL': 'gpt-image-1',
        })

    def tearDown(self):
        self.telegram.close()
        self.channel.server.close()
        self.temp.cleanup()

    def run_tool(self, messages):
        process = subprocess.run([PYTHON, str(HERE / 'image_tool.py')], input=messages, env=self.env,
                                 capture_output=True, text=True, encoding='utf-8', timeout=120)
        return process

    def skip_on_windows(self):
        # 桥接程序依赖 /proc/self/fd 做二次身份校验，仅 Linux 生产环境有效。
        if sys.platform.startswith('win'):
            self.skipTest('生产为 Linux；Windows 缺少 /proc/self/fd，发送链路的二次校验不可用')

    def test_protocol_handshake_and_tool_list(self):
        lines = '\n'.join(json.dumps(m) for m in [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'},
        ]) + '\n'
        process = self.run_tool(lines)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.strip().splitlines() if line.strip()]
        # 通知不产生响应，其余三条必须一一对应。
        self.assertEqual([r['id'] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]['result']['serverInfo']['name'], 'private-image-channel')
        tools = replies[1]['result']['tools']
        self.assertEqual([t['name'] for t in tools], ['generate_image'])
        self.assertIn('自然语言', tools[0]['description'])
        self.assertIn('prompt', tools[0]['inputSchema']['required'])
        self.assertEqual(replies[2]['result'], {})
        # 标准输出只能是 JSON-RPC，不允许混入程序打印。
        for line in process.stdout.strip().splitlines():
            self.assertEqual(json.loads(line)['jsonrpc'], '2.0')

    def test_tool_call_generates_and_sends_image(self):
        self.skip_on_windows()
        request = {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
                   'params': {'name': 'generate_image',
                              'arguments': {'prompt': '一只趴在窗边的橘猫，水彩插画', 'size': '1536x1024'}}}
        process = self.run_tool(json.dumps(request) + '\n')
        self.assertEqual(process.returncode, 0, process.stderr)
        reply = json.loads(process.stdout.strip().splitlines()[0])
        self.assertFalse(reply['result']['isError'])
        self.assertIn('图片已生成', reply['result']['content'][0]['text'])
        # 生图接口确实收到干净的自然语言描述与画幅。
        self.assertEqual(len(self.channel.requests), 1)
        self.assertEqual(self.channel.requests[0]['prompt'], '一只趴在窗边的橘猫，水彩插画')
        self.assertEqual(self.channel.requests[0]['size'], '1536x1024')
        self.assertEqual(self.channel.requests[0]['model'], 'gpt-image-1')
        # 图片真的回传到当前聊天。
        self.assertIn('sendDocument', self.telegram.calls)
        self.assertTrue(self.telegram.documents[0].startswith(b'--'))
        self.assertIn(b'\x89PNG', self.telegram.documents[0])

    def test_tool_error_does_not_leak_secret(self):
        self.channel.status = 401
        request = {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
                   'params': {'name': 'generate_image', 'arguments': {'prompt': '测试失败路径'}}}
        process = self.run_tool(json.dumps(request) + '\n')
        reply = json.loads(process.stdout.strip().splitlines()[0])
        self.assertTrue(reply['result']['isError'])
        text = reply['result']['content'][0]['text']
        self.assertIn('图片未完成', text)
        self.assertNotIn('FAKE-IMAGE-KEY-FOR-TEST-ONLY', text)
        self.assertNotIn('127.0.0.1', text)
        self.assertNotIn('Authorization', text)

    def test_reject_bad_arguments(self):
        for arguments in ({'prompt': ''}, {'prompt': 'x' * 12001}, {'prompt': 'ok', 'size': '999x999'}):
            request = {'jsonrpc': '2.0', 'id': 11, 'method': 'tools/call',
                       'params': {'name': 'generate_image', 'arguments': arguments}}
            process = self.run_tool(json.dumps(request) + '\n')
            reply = json.loads(process.stdout.strip().splitlines()[0])
            self.assertTrue(reply['result']['isError'], arguments)
        self.assertEqual(self.channel.requests, [])


class RedactionTest(unittest.TestCase):
    """原始输入必须进入执行器，落盘状态必须脱敏。"""

    def test_task_text_kept_but_disk_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
            env.update({'CLAUDE_BOT_WORKDIR': str(state), 'APP_DIR': str(HERE),
                        'TELEGRAM_BOT_TOKEN': '123456:FAKE-TOKEN-FOR-TEST-ONLY',
                        'TELEGRAM_API_BASE': 'http://127.0.0.1:9'})
            script = (
                'import json, sys\n'
                'sys.path.insert(0, ' + json.dumps(str(HERE)) + ')\n'
                'import bot\n'
                'bot.WORK = __import__("pathlib").Path(' + json.dumps(str(state)) + ')\n'
                'bot.TOKEN = "123456:FAKE-TOKEN-FOR-TEST-ONLY"\n'
                'controller = bot.TaskController(bot.WORK)\n'
                'raw = "请把 " + ' + json.dumps(SECRET_SAMPLE) + ' + " 写进配置并测试"\n'
                'task = controller.enqueue(4242, raw)\n'
                'print(json.dumps({"memory": task["text"], "disk": json.loads((bot.WORK / "bridge-state.json").read_text())["tasks"][0]["text"]}, ensure_ascii=False))\n'
            )
            process = subprocess.run([PYTHON, '-c', script], env=env, capture_output=True, text=True,
                                     encoding='utf-8', timeout=60)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout.strip().splitlines()[-1])
            # 执行器拿到的必须是原文。
            self.assertEqual(result['memory'], '请把 ' + SECRET_SAMPLE + ' 写进配置并测试')
            # 落盘必须已脱敏。
            self.assertNotIn(SECRET_SAMPLE, result['disk'])
            self.assertIn('[已隐藏令牌]', result['disk'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
