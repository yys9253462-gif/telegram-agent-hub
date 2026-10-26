#!/usr/bin/env python3
"""最小假电报接口，只用于安装验收；绑定回环地址，不接触真实令牌。"""
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALLS = []
LOCK = threading.Lock()
# 长轮询上限，避免假接口在无消息时瞬间返回导致被测程序空转。
MAX_LONG_POLL = 25


def read_timeout(handler):
    """取请求体里的 timeout 参数，模拟真实接口的挂起时长。"""
    length = int(handler.headers.get('Content-Length') or 0)
    if not length:
        return 0
    raw = handler.rfile.read(length)
    try:
        value = json.loads(raw or b'{}').get('timeout', 0)
        return max(0, min(MAX_LONG_POLL, int(value)))
    except (ValueError, TypeError):
        return 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _method(self):
        # 路径形如 /bot<token>/<method>，末段才是方法名。
        path = urllib.parse.urlsplit(self.path).path.rstrip('/')
        return path.rsplit('/', 1)[-1]

    def _get_updates(self, wait):
        # 真实接口在没有消息时会挂起至多 timeout 秒。不模拟这一点，
        # 被测程序会以每秒上百次的频率空转，测出来的 CPU 占用毫无意义。
        if wait:
            time.sleep(wait)
        # 与真实接口一致：无消息时返回空数组，而不是对象。
        self._reply({'ok': True, 'result': []})

    def do_GET(self):
        name = self._method()
        wait = read_timeout(self)
        with LOCK:
            CALLS.append(name)
        if name == 'getMe':
            self._reply({'ok': True, 'result': {'id': 1, 'is_bot': True,
                                                'first_name': 'Probe', 'username': 'probe_bot'}})
        elif name == 'getUpdates':
            self._get_updates(wait)
        else:
            self._reply({'ok': True, 'result': {'message_id': len(CALLS), 'date': 0, 'text': ''}})

    def do_POST(self):
        name = self._method()
        wait = read_timeout(self)
        with LOCK:
            CALLS.append(name)
        # 真实接口方法不区分 GET/POST，method 决定返回结构。
        if name == 'getUpdates':
            self._get_updates(wait)
        elif name == 'getMe':
            self._reply({'ok': True, 'result': {'id': 1, 'is_bot': True,
                                                'first_name': 'Probe', 'username': 'probe_bot'}})
        else:
            self._reply({'ok': True, 'result': {'message_id': len(CALLS), 'date': 0, 'text': ''}})


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 18081), Handler)
    print('假接口已启动 127.0.0.1:18081', flush=True)
    server.serve_forever()
