#!/usr/bin/env python3
"""无网络、无模型请求的多任务并发回归。"""
import io
import os
import urllib.error
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from test_project import load


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.b = load('bot')
        root = Path(self.temp.name)
        self.b.WORK = root
        self.b.SESSION_STATE = root / 'session.json'
        self.b.MODEL_STATE = root / 'model.json'
        self.b.send = lambda *args: None
        self.c = self.b.TaskController(root)
        self.b.CONTROL = self.c
        self.addCleanup(self.c.close)

    def wait_status(self, task, expected):
        with self.c.condition:
            self.assertTrue(self.c.condition.wait_for(lambda: task['status'] == expected, timeout=3), task)

    def test_parallel_entry_and_targeted_stop(self):
        tasks = [self.c.enqueue(1, str(index)) for index in range(2)]
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        self.addCleanup(release.set)
        processes = [object(), object()]
        events = {}

        def execute(task):
            index = tasks.index(task)
            events[index] = self.b.TASK_LOCAL.cancel
            self.b.bind_process(processes[index])
            entered[index].set()
            if index == 0:
                self.assertTrue(events[index].wait(3))
                self.b.check_cancelled()
            else:
                self.assertTrue(release.wait(3))
                self.b.check_cancelled()
                self.b.unbind_process(processes[index])

        self.c.execute = execute
        self.c.start_workers()
        self.assertTrue(all(event.wait(3) for event in entered))
        with self.c.lock:
            self.assertEqual(len(self.c.running), 2)
            for index, task in enumerate(tasks):
                self.assertIs(self.c.running[task['id']]['process'], processes[index])
        self.assertIsNot(events[0], events[1])
        messages = []
        self.b.send = lambda chat, text: messages.append(text)
        with patch.object(self.b, 'send_options') as options:
            self.c.handle_text(1, '当前任务')
        self.assertTrue(all(task['id'] in options.call_args.args[1] for task in tasks))
        # 选项既可能是 (label, command) 元组，也可能是纯命令字符串（如「刷新用量」），
        # 两种写法都要取到命令本身，否则加一个纯字符串按钮就会让解析炸掉。
        commands = [item[1] if isinstance(item, tuple) else item
                    for item in options.call_args.args[2]]
        self.assertEqual(commands[:len(tasks)], ['停止任务 ' + task['id'] for task in tasks])
        response = self.c.stop()
        self.assertIn('停止任务 ID', response)
        self.assertTrue(all(task['id'] in response for task in tasks))
        with patch.object(self.b, 'terminate_process') as terminate:
            self.c.handle_text(1, '停止任务 ' + tasks[0]['id'])
        terminate.assert_called_once_with(processes[0])
        self.wait_status(tasks[0], 'cancelled')
        self.assertFalse(events[1].is_set())
        self.assertEqual(tasks[1]['status'], 'running')
        release.set()
        self.wait_status(tasks[1], 'completed')

    def test_enqueue_isolates_busy_session_without_copying_native_id(self):
        key = self.c.data['active_session']
        self.c.data['sessions'][key].update(session_id='native', engine_sessions={'claude': 'native'})
        first = self.c.enqueue(1, '第一项')
        snapshot = dict(first['snapshot'])
        second = self.c.enqueue(1, '第二项', snapshot=snapshot)
        self.assertNotEqual(first['snapshot']['session_key'], second['snapshot']['session_key'])
        self.assertEqual(snapshot['session_key'], key)
        self.assertIsNone(second['snapshot']['session_id'])
        self.assertEqual(self.c.data['active_session'], second['snapshot']['session_key'])
        self.assertEqual(self.c.data['sessions'][second['snapshot']['session_key']]['engine_sessions'], {})

    def test_legacy_same_session_queue_never_resumes_together(self):
        first = self.c.enqueue(1, '第一项')
        second = self.c.enqueue(1, '第二项')
        second['snapshot']['session_key'] = first['snapshot']['session_key']
        entered_first, entered_second, release = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def execute(task):
            if task is first:
                entered_first.set()
                self.assertTrue(release.wait(3))
            else:
                entered_second.set()
        self.c.execute = execute
        self.c.start_workers()
        self.assertTrue(entered_first.wait(3))
        with self.c.condition:
            self.assertIsNone(self.c.next_task())
            self.assertEqual(second['status'], 'queued')
        self.assertFalse(entered_second.is_set())
        release.set()
        self.assertTrue(entered_second.wait(3))
        self.wait_status(first, 'completed')
        self.wait_status(second, 'completed')

    def test_new_task_never_clears_existing_cancel_event(self):
        first = self.c.enqueue(1, '第一项')
        first_entered, second_entered, release = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        saved = {}
        def execute(task):
            if task is first:
                saved['cancel'] = self.b.TASK_LOCAL.cancel
                first_entered.set()
                self.assertTrue(release.wait(3))
                self.b.check_cancelled()
            else:
                second_entered.set()
        self.c.execute = execute
        self.c.start_workers()
        self.assertTrue(first_entered.wait(3))
        self.c.stop(first['id'])
        second = self.c.enqueue(1, '第二项')
        self.assertTrue(second_entered.wait(3))
        self.assertTrue(saved['cancel'].is_set())
        release.set()
        self.wait_status(first, 'cancelled')
        self.wait_status(second, 'completed')

    def test_close_cancels_all_and_joins_workers(self):
        tasks = [self.c.enqueue(1, str(index)) for index in range(2)]
        entered = [threading.Event(), threading.Event()]
        events = []
        processes = [object(), object()]
        def execute(task):
            cancel = self.b.TASK_LOCAL.cancel
            events.append(cancel)
            self.b.bind_process(processes[tasks.index(task)])
            entered[tasks.index(task)].set()
            self.assertTrue(cancel.wait(3))
            self.b.check_cancelled()
        self.c.execute = execute
        self.c.start_workers()
        self.assertTrue(all(event.wait(3) for event in entered))
        lock_available = []
        def terminate(process):
            acquired = threading.Event()
            def inspect_lock():
                with self.c.lock:
                    acquired.set()
            probe = threading.Thread(target=inspect_lock)
            probe.start()
            lock_available.append(acquired.wait(1))
            probe.join(1)
        with patch.object(self.b, 'terminate_process', side_effect=terminate) as terminated:
            self.c.close(timeout=3)
        self.assertEqual({call.args[0] for call in terminated.call_args_list}, set(processes))
        self.assertEqual(terminated.call_count, 2)
        self.assertTrue(all(lock_available))
        self.assertTrue(all(event.is_set() for event in events))
        self.assertTrue(all(not worker.is_alive() for worker in self.c.workers))
        self.assertTrue(all(task['status'] == 'cancelled' for task in tasks))
        self.assertEqual(self.c.running, {})

    def test_three_running_and_fourth_waits_for_capacity(self):
        self.c.max_concurrent = 3
        tasks = [self.c.enqueue(1, str(index)) for index in range(4)]
        entered = [threading.Event() for _ in tasks]
        release = [threading.Event() for _ in tasks]
        for event in release:
            self.addCleanup(event.set)
        def execute(task):
            index = tasks.index(task)
            entered[index].set()
            if not release[index].wait(3):
                raise RuntimeError('测试未释放任务')
        self.c.execute = execute
        self.c.start_workers()
        self.assertTrue(all(event.wait(3) for event in entered[:3]))
        with self.c.lock:
            self.assertEqual(len(self.c.running), 3)
            self.assertEqual(tasks[3]['status'], 'queued')
        self.assertFalse(entered[3].is_set())
        release[0].set()
        self.assertTrue(entered[3].wait(3))
        for event in release:
            event.set()
        for task in tasks:
            self.wait_status(task, 'completed')

    def test_media_enqueue_isolates_agent_and_other_media(self):
        first = self.c.enqueue(1, '第一项')
        media = self.c.enqueue(1, kind='media')
        next_agent = self.c.enqueue(1, '第三项')
        keys = [task['snapshot']['session_key'] for task in (first, media, next_agent)]
        self.assertEqual(len(set(keys)), 3)
        self.assertTrue(all(task['snapshot']['session_id'] is None for task in (media, next_agent)))

    def test_quick_query_runs_during_same_session_agent(self):
        first = self.c.enqueue(1, '长任务')
        quick = self.c.enqueue(1, '查询', kind='quick')
        self.assertEqual(first['snapshot']['session_key'], quick['snapshot']['session_key'])
        entered, queried, release = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def execute(task):
            if task is first:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('测试未释放任务')
            else:
                queried.set()
        self.c.execute = execute
        self.c.start_workers()
        self.assertTrue(entered.wait(3))
        self.assertTrue(queried.wait(3))
        self.wait_status(quick, 'completed')
        self.assertEqual(first['status'], 'running')
        release.set()
        self.wait_status(first, 'completed')

    def test_task_model_environment_is_thread_local(self):
        first = self.c.enqueue(1, '第一项')
        second = self.c.enqueue(1, '第二项')
        tasks = [first, second]
        for index, task in enumerate(tasks):
            task['snapshot']['model'] = 'model-' + str(index)
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        self.addCleanup(release.set)
        observed = {}
        def execute(task):
            index = tasks.index(task)
            entered[index].set()
            if not release.wait(3):
                raise RuntimeError('测试未释放任务')
            observed[index] = (self.b.current_model(), self.b.model_environment())
        self.c.execute = execute
        with patch.object(self.b, 'auth_channel', return_value='gateway'):
            self.c.start_workers()
            self.assertTrue(all(event.wait(3) for event in entered))
            release.set()
            for task in tasks:
                self.wait_status(task, 'completed')
        for index in range(2):
            self.assertEqual(observed[index][0], 'model-' + str(index))
            for name in self.b.MODEL_PIN_VARS:
                self.assertEqual(observed[index][1][name], 'model-' + str(index))

    def test_stream_label_survives_edits_and_rate_limit_fallback(self):
        task = self.c.enqueue(1, '输入摘要')
        for limited in (False, True):
            with self.subTest(limited=limited):
                calls, sent = [], []
                process = Mock(returncode=0)
                bound = threading.Event()
                def write_prompt(value):
                    if not bound.is_set():
                        raise RuntimeError('写入前未登记进程')
                process.stdin.write.side_effect = write_prompt
                process.stdout = io.StringIO('{"type":"result","result":"测试结果"}\n')
                def api(method, payload):
                    calls.append((method, payload))
                    if method == 'editMessageText' and limited:
                        raise urllib.error.HTTPError('https://example.invalid', 429, '限流', {},
                                                     io.BytesIO(b'{"parameters":{"retry_after":60}}'))
                    return {'message_id': 1}
                self.b.TASK_LOCAL.task = task
                self.b.TASK_LOCAL.cancel = threading.Event()
                try:
                    with patch.object(self.b, 'api', side_effect=api), \
                         patch.object(self.b, 'send_formatted', side_effect=lambda chat, text: sent.append(text)), \
                         patch.object(self.b, 'agent_instructions', return_value='指令'), \
                         patch.object(self.b, 'model_environment', return_value={}), \
                         patch.object(self.b, 'credential_environment', return_value={}), \
                         patch.object(self.b, 'engine_command', return_value=(['mock'], '输入正文')), \
                         patch.object(self.b, 'bind_process', side_effect=lambda process: bound.set()), \
                         patch.object(self.b.subprocess, 'Popen', return_value=process), \
                         patch.object(self.b, 'terminate_process'):
                        self.b.run_stream(1, '输入摘要')
                finally:
                    self.b.TASK_LOCAL.task = None
                    self.b.TASK_LOCAL.cancel = None
                self.assertIn(task['id'], calls[0][1]['text'])
                self.assertIn('输入摘要', calls[0][1]['text'])
                edits = [payload for method, payload in calls if method == 'editMessageText']
                self.assertTrue(edits)
                self.assertTrue(all(task['id'] in payload['text'] for payload in edits))
                if limited:
                    self.assertTrue(sent)
                    self.assertTrue(all(task['id'] in text for text in sent))

    def test_concurrency_environment_bounds(self):
        for raw, expected in [('0', 1), ('99', 6), ('2', 2), ('invalid', 3)]:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as folder:
                with patch.dict(os.environ, {'AGENT_MAX_CONCURRENT': raw}):
                    self.assertEqual(self.b.TaskController(folder).max_concurrent, expected)
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ):
            os.environ.pop('AGENT_MAX_CONCURRENT', None)
            self.assertEqual(self.b.TaskController(folder).max_concurrent, 3)


if __name__ == '__main__':
    unittest.main()
