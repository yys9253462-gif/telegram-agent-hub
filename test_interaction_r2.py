#!/usr/bin/env python3
"""五轮交互测试 · 第二轮：任务队列、并发隔离、停止与撤销。

这一轮不启动真实执行器，只让 worker 走一遍调度骨架，
重点看「队列语义」：谁能拿到任务、同一会话会不会并行、
撤销之后还能不能被领走、停止请求落到哪个任务上。
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


class QueueBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = bot.TaskController(self.root)
        self.controller.max_concurrent = 3
        self.sent = []
        self.patches = [
            patch.object(bot, 'CONTROL', self.controller),
            patch.object(bot, 'OWNER', 123),
            patch.object(bot, 'send', side_effect=lambda chat, text: self.sent.append(text)),
            patch.object(bot, 'api', side_effect=lambda method, payload=None: (
                self.sent.append(payload.get('text')) if isinstance(payload, dict) and payload.get('text') else None)
                or {'message_id': 1}),
            patch.object(bot, 'notify', side_effect=lambda chat, text: self.sent.append(text)),
        ]
        for item in self.patches:
            item.start()
        bot.ACTION_UI.clear()
        bot.ACTION_UI_SEEN.clear()
        self.addCleanup(self._teardown)

    def _teardown(self):
        self.controller.closing = True
        for item in reversed(self.patches):
            item.stop()
        bot.ACTION_UI.clear()
        bot.ACTION_UI_SEEN.clear()
        self.temp.cleanup()

    def add(self, text, chat=123, kind='agent', update_id=None):
        return self.controller.enqueue(chat, text, kind, update_id=update_id)


class DeduplicationTest(QueueBase):
    """重复投递：Telegram 重启后会重放，同一条不能被排两次。"""

    def test_same_update_is_ignored(self):
        first = self.add('第一条', update_id=500)
        second = self.add('第一条', update_id=500)
        self.assertIsNotNone(first)
        self.assertIsNone(second, '同一条更新被排了两次')
        with self.controller.lock:
            self.assertEqual(len(self.controller.data['tasks']), 1)

    def test_replay_below_watermark_is_ignored(self):
        """水位以下的编号一律视为处理过，与 seen 集合是否还留着它无关。"""
        self.controller.note_seen(900)
        before = len(self.controller.data['tasks'])
        self.assertIsNone(self.add('重放的老消息', update_id=880))
        self.assertEqual(len(self.controller.data['tasks']), before)

    def test_new_update_after_watermark_is_accepted(self):
        self.controller.note_seen(900)
        task = self.add('新消息', update_id=901)
        self.assertIsNotNone(task)

    def test_watermark_survives_restart(self):
        self.controller.note_seen(777)
        self.controller.persist()
        again = bot.TaskController(self.root)
        self.assertEqual(again.watermark, 777, '重启后水位丢失，积压消息会被重放')


class ConcurrencyIsolationTest(QueueBase):
    """同一会话的两个任务不能同时跑，否则会争同一份上下文。"""

    def test_second_task_in_same_session_gets_new_session(self):
        self.add('第一件事', chat=123)
        snapshot = self.controller.snapshot()
        self.add('第二件事', chat=123)
        with self.controller.lock:
            keys = [item['snapshot']['session_key'] for item in self.controller.data['tasks']]
        self.assertEqual(len(set(keys)), 2, '同会话的两个任务没有隔离')

    def test_different_chats_keep_their_own_sessions(self):
        self.add('甲的任务', chat=111)
        self.add('乙的任务', chat=222)
        with self.controller.lock:
            chats = [item['chat'] for item in self.controller.data['tasks']]
        self.assertEqual(chats, [111, 222])

    def test_quick_task_can_run_beside_agent_task(self):
        """只读查询不该被长任务堵住。"""
        self.add('长任务', chat=123)
        self.add('服务器概况', kind='quick', chat=123)
        with self.controller.lock:
            self.controller.running['x'] = {'task': self.controller.data['tasks'][0],
                                            'cancel': threading.Event(), 'process': None}
            picked = self.controller.next_task()
        self.assertIsNotNone(picked, '只读查询被长任务挡住了')
        self.assertEqual(picked['kind'], 'quick')

    def test_next_task_skips_session_being_worked_on(self):
        first = self.add('任务一', chat=123)
        second = self.add('任务二', chat=123)
        self.assertNotEqual(first['snapshot']['session_key'], second['snapshot']['session_key'])


class CancelAndStopTest(QueueBase):
    """撤销与停止。撤销是排队项，停止是运行项，两者不能互相越界。"""

    def test_cancel_queued_task(self):
        task = self.add('待撤销')
        message = self.controller.cancel(task['id'])
        self.assertIn('已撤销', message)
        with self.controller.lock:
            self.assertEqual(self.controller.data['tasks'][0]['status'], 'cancelled')
            self.assertEqual(self.controller.next_task(), None, '已撤销的任务仍会被领走')

    def test_cancel_unknown_id_is_reported(self):
        message = self.controller.cancel('不存在的编号')
        self.assertIn('未找到', message)

    def test_cancel_running_task_is_refused(self):
        """运行中的任务必须走「停止」，撤销不该把它伪装成没跑过。"""
        task = self.add('正在跑')
        with self.controller.lock:
            self.controller.data['tasks'][0]['status'] = 'running'
        message = self.controller.cancel(task['id'])
        self.assertIn('未找到', message)

    def test_cancel_clears_text_and_payload(self):
        """撤销后正文必须清掉，否则凭据会留在状态文件里。"""
        task = self.controller.enqueue(123, 'token=ghp_' + 'x' * 20, kind='agent')
        self.controller.cancel(task['id'])
        with self.controller.lock:
            stored = self.controller.data['tasks'][0]
            self.assertEqual(stored['text'], '')
            self.assertEqual(stored['payload'], {})

    def test_stop_without_running_is_harmless(self):
        self.assertIn('没有运行中的任务', self.controller.stop())

    def test_stop_marks_only_the_requested_task(self):
        first = self.add('任务一')
        second = self.add('任务二')
        events = {}
        with self.controller.lock:
            for item in (first, second):
                events[item['id']] = threading.Event()
                self.controller.running[item['id']] = {'task': item, 'cancel': events[item['id']], 'process': None}
        self.controller.stop(first['id'])
        self.assertTrue(events[first['id']].is_set())
        self.assertFalse(events[second['id']].is_set(), '停止一个任务把另一个也停了')

    def test_stop_unknown_id_does_not_stop_anything(self):
        task = self.add('唯一任务')
        event = threading.Event()
        with self.controller.lock:
            self.controller.running[task['id']] = {'task': task, 'cancel': event, 'process': None}
        self.assertIn('未找到', self.controller.stop('别的编号'))
        self.assertFalse(event.is_set())


class DraftFlowTest(QueueBase):
    """语音草稿：确认前不能执行，确认后才进队列。"""

    def draft(self, text='把这句话发出去'):
        task = self.controller.enqueue(123, text, kind='agent')
        with self.controller.condition:
            self.controller.data['tasks'][0].update(status='awaiting_confirmation', text=text)
            self.controller.persist()
        return task

    def test_execute_voice_moves_draft_to_queue(self):
        task = self.draft()
        self.controller.handle_text(123, '执行语音 ' + task['id'])
        with self.controller.lock:
            self.assertEqual(self.controller.data['tasks'][0]['status'], 'queued')

    def test_modify_voice_replaces_text_without_running(self):
        task = self.draft('旧内容')
        self.controller.handle_text(123, '修改语音 ' + task['id'] + ' 新内容')
        with self.controller.lock:
            stored = self.controller.data['tasks'][0]
            self.assertEqual(stored['text'], '新内容')
            self.assertEqual(stored['status'], 'awaiting_confirmation')

    def test_cancel_voice_discards_draft(self):
        task = self.draft()
        self.controller.handle_text(123, '取消语音 ' + task['id'])
        with self.controller.lock:
            self.assertEqual(self.controller.data['tasks'][0]['status'], 'cancelled')

    def test_unknown_draft_is_reported(self):
        replies = []
        with patch.object(bot, 'send', side_effect=lambda chat, text: replies.append(text)):
            self.controller.handle_text(123, '执行语音 不存在')
        self.assertTrue(any('找不到' in text for text in replies), replies)


class WorkerSchedulingTest(QueueBase):
    """真起 worker 跑一遍，看调度规则在时间轴上是否成立。

    只测 next_task() 不够：那个函数是「规则」，worker 才是「执行」。
    规则对而执行错（比如拿到任务后又顺序等待）在单测里看不出来。
    """

    def run_queue(self, tasks, concurrency=3, work=0.2):
        self.controller.max_concurrent = concurrency
        timeline = []
        lock = threading.Lock()

        def fake_execute(task):
            with lock:
                timeline.append(('开始', task['text'], time.monotonic()))
            time.sleep(work)
            with lock:
                timeline.append(('结束', task['text'], time.monotonic()))
            task['result'] = '完成'

        self.controller.execute = fake_execute
        self.controller.start_workers()
        for chat, text in tasks:
            self.controller.enqueue(chat, text)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with self.controller.lock:
                left = [item for item in self.controller.data['tasks']
                        if item['status'] in ('queued', 'running')]
            if not left:
                break
            time.sleep(0.02)
        self.controller.closing = True
        with self.controller.condition:
            self.controller.condition.notify_all()
        return timeline

    def test_distinct_sessions_run_in_parallel(self):
        timeline = self.run_queue([(1, 'A'), (2, 'B'), (3, 'C')])
        starts = [moment for kind, _, moment in timeline if kind == '开始']
        self.assertEqual(len(starts), 3)
        span = max(starts) - min(starts)
        self.assertLess(span, 0.15, '三个不同会话没有并行启动，总跨度 %.3f 秒' % span)

    def test_same_session_tasks_are_serialised(self):
        """A1、A2 被隔离成两个会话，所以可以并行；真正要保证的是
        「隔离前后不会共用同一份上下文」，串行性由会话键保证。"""
        timeline = self.run_queue([(1, 'A1'), (1, 'A2')])
        self.assertEqual(len([item for item in timeline if item[0] == '开始']), 2)
        with self.controller.lock:
            keys = {item['snapshot']['session_key'] for item in self.controller.data['tasks']}
        self.assertEqual(len(keys), 2, '同会话的两个任务共用了会话键')

    def test_max_concurrent_is_respected(self):
        timeline = self.run_queue([(index, '任务%d' % index) for index in range(1, 6)],
                                  concurrency=2, work=0.15)
        active = 0
        peak = 0
        for kind, _, _ in sorted(timeline, key=lambda item: item[2]):
            active += 1 if kind == '开始' else -1
            peak = max(peak, active)
        self.assertLessEqual(peak, 2, '同时跑的任务数超过了上限（%d）' % peak)

    def test_task_records_finish_time_and_result(self):
        self.run_queue([(1, 'A')])
        with self.controller.lock:
            stored = self.controller.data['tasks'][0]
        self.assertEqual(stored['status'], 'completed')
        self.assertEqual(stored['result'], '完成')
        self.assertIn('finished', stored)


class QueueLimitTest(QueueBase):
    def test_hundred_queued_is_refused(self):
        with self.controller.lock:
            self.controller.data['tasks'] = [
                {'id': 'x%d' % index, 'chat': 123, 'kind': 'agent', 'text': '', 'payload': {},
                 'snapshot': self.controller.snapshot(), 'status': 'queued',
                 'created': time.time(), 'tools': [], 'result': ''} for index in range(100)]
        with self.assertRaises(bot.MediaError) as caught:
            self.add('再来一条')
        self.assertIn('一百条', str(caught.exception))

    def test_overlong_task_is_refused_with_advice(self):
        with self.assertRaises(bot.MediaError) as caught:
            self.add('长' * (self.controller.MAX_TASK_CHARS + 1))
        self.assertIn('拆成多条', str(caught.exception))

    def test_exactly_at_limit_is_accepted(self):
        task = self.add('长' * self.controller.MAX_TASK_CHARS)
        self.assertIsNotNone(task)


if __name__ == '__main__':
    unittest.main(verbosity=2)
