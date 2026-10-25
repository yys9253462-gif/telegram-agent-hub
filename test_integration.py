#!/usr/bin/env python3
"""隔离消息服务：不使用真实机器人令牌，不记录请求头。"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=19873)
parser.add_argument('--events', default='/tmp/agent-hub-test-events.jsonl')
args = parser.parse_args()
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        if self.path.endswith('/getMe'):
            self.reply({'ok':True,'result':{'id':123456,'is_bot':True,'username':'isolated_test_bot'}})
        else:
            self.reply({'object':'list','data':[{'id':'test-model','supports_tool_call':True}]})
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        data = json.loads(self.rfile.read(length) or b'{}')
        method = self.path.rsplit('/', 1)[-1]
        if method == 'getUpdates':
            time.sleep(.2)
            result = []
        elif method == 'getMe':
            result = {'id': 123456, 'is_bot':True,'first_name':'Isolated Test','username':'isolated_test_bot'}
        else:
            result = {'message_id': 1}
        with Path(args.events).open('a') as stream:
            stream.write(json.dumps({'method':method,'time':time.time()})+'\n')
        self.reply({'ok':True,'result':result})
    def reply(self, data):
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
