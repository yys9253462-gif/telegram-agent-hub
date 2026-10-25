#!/usr/bin/env python3
"""最小假电报接口，只用于安装验收；绑定回环地址，不接触真实令牌。"""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALLS = []
LOCK = threading.Lock()


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

    def do_GET(self):
        name = urllib.parse.urlsplit(self.path).path.rstrip('/').rsplit('/', 1)[-1]
        with LOCK:
            CALLS.append(name)
        if name == 'getMe':
            self._reply({'ok': True, 'result': {'id': 1, 'is_bot': True,
                                                'first_name': 'Probe', 'username': 'probe_bot'}})
        elif name == 'getUpdates':
            self._reply({'ok': True, 'result': []})
        else:
            self._reply({'ok': True, 'result': {'message_id': len(CALLS), 'date': 0, 'text': ''}})

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length:
            self.rfile.read(length)
        name = urllib.parse.urlsplit(self.path).path.rstrip('/').rsplit('/', 1)[-1]
        with LOCK:
            CALLS.append(name)
        self._reply({'ok': True, 'result': {'message_id': len(CALLS), 'date': 0, 'text': ''}})


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 18081), Handler)
    print('假接口已启动 127.0.0.1:18081', flush=True)
    server.serve_forever()
