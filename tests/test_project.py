#!/usr/bin/env python3
"""不接触真实账号的项目回归测试。"""
import _root  # noqa: F401  仓库根目录进 sys.path
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

ROOT = _root.ROOT

def load(name):
    spec = importlib.util.spec_from_file_location(name, (_root.REPO / 'scripts' / (name + '.py') if name == 'security_check' else ROOT / (name + '.py')))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.b = load('bot')
        self.b.WORK = self.root
        for key, filename in [('STATE', 'offset'), ('SESSION_STATE', 'session.json'), ('MODEL_STATE', 'model.json'), ('STRATEGY_STATE', 'strategy.json'), ('MENU_STATE', 'menu.json')]:
            setattr(self.b, key, self.root / filename)
        self.b.api = lambda method, payload: {'message_id': 1}
        self.b.send = lambda *args: None
        self.b.CONTROL = self.b.TaskController(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_queue_and_recovery(self):
        c = self.b.CONTROL
        task = c.enqueue(1, '任务', update_id=1)
        self.assertIsNone(c.enqueue(1, '重复', update_id=1))
        task['status'] = 'running'
        c.enqueue(1, '下一任务')
        c.persist()
        restored = self.b.TaskController(self.root)
        self.assertEqual([t['status'] for t in restored.data['tasks']], ['interrupted', 'queued'])

    def test_snapshot_and_sessions(self):
        c = self.b.CONTROL
        task = c.enqueue(1, '旧任务')
        snapshot = dict(task['snapshot'])
        c.handle_text(1, '新建会话')
        c.data['engine'] = 'codebuddy'
        self.assertEqual(task['snapshot'], snapshot)
        c.update_session('old-session', snapshot['session_key'])
        self.assertIsNone(c.active_session()['session_id'])

    def test_voice_confirmation(self):
        c = self.b.CONTROL
        task = c.enqueue(1, '语音')
        task.update(kind='media', status='awaiting_confirmation')
        c.handle_text(1, '执行语音 ' + task['id'])
        c.handle_text(1, '执行语音 ' + task['id'])
        self.assertEqual(len(c.data['tasks']), 1)
        self.assertEqual(task['status'], 'queued')

    def test_worker_cancel(self):
        b, c = self.b, self.b.CONTROL
        started = threading.Event()
        def execute(task):
            started.set()
            while True:
                b.check_cancelled()
                time.sleep(.02)
        c.execute = execute
        task = c.enqueue(1, '停止测试')
        thread = threading.Thread(target=c.worker, daemon=True)
        thread.start()
        self.assertTrue(started.wait(2))
        c.stop()
        deadline = time.monotonic() + 2
        while task['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(task['status'], 'cancelled')
        with c.condition:
            c.closing = True
            c.condition.notify_all()
        thread.join(2)

    def test_file_boundaries(self):
        with self.assertRaises(Exception):
            self.b.send_file(1, '/etc/passwd')
        path = self.root / '.env'
        path.write_text('测试')
        with self.assertRaises(Exception):
            self.b.send_file(1, str(path))

    def test_render_and_scan_negative_control(self):
        self.assertEqual(self.b.render_reply('## 标题'), '<b>标题</b>')
        self.assertEqual(self.b.render_reply('<script>'), '&lt;script&gt;')
        scanner = load('security_check')
        fake = self.root / 'negative.txt'
        fake.write_text('sk-' + 'FAKEFORSCANNINGONLY' * 2)
        self.assertTrue(scanner.scan(self.root))
        fake.write_text('无敏感信息')
        self.assertFalse(scanner.scan(self.root))

if __name__ == '__main__':
    unittest.main()
