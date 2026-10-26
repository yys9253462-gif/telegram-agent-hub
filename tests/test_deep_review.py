#!/usr/bin/env python3
"""2026-10-26 深度排查修复项的回归测试。每个用例对应一处线上会出问题的缺陷。

运行：python3 tests/test_deep_review.py
"""
import _root  # noqa: F401  仓库根目录进 sys.path
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


def controller(folder, state=None):
    if state is not None:
        (folder / 'bridge-state.json').write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    return bot.TaskController(folder)


def snapshot(**extra):
    value = {'session_key': 'a', 'model': 'm', 'target': '本机', 'strategy': '均衡执行'}
    value.update(extra)
    return value


class DeepReviewTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def test_load_with_missing_session_does_not_crash(self):
        """任务引用的会话已不存在时，启动不能 KeyError（原来取会话 ID 先于校正会话键）。"""
        state = {'active_session': 'a', 'sessions': {'a': {'name': '默认会话', 'session_id': None}},
                 'tasks': [{'id': 't1', 'status': 'completed', 'kind': 'agent',
                            'snapshot': snapshot(session_key='gone')}]}
        c = controller(self.folder, state)
        self.assertEqual(c.data['tasks'][0]['snapshot']['session_key'], 'a')

    def test_finished_tasks_do_not_keep_handoff(self):
        state = {'active_session': 'a', 'sessions': {'a': {'name': '默认会话', 'session_id': None}},
                 'tasks': [{'id': 't1', 'status': 'completed', 'kind': 'agent',
                            'snapshot': snapshot(handoff=[{'request': 'x' * 1600, 'result': 'y' * 2400}] * 6)}]}
        c = controller(self.folder, state)
        self.assertNotIn('handoff', c.data['tasks'][0]['snapshot'])
        self.assertLess((self.folder / 'bridge-state.json').stat().st_size, 4000)

    def test_concurrent_session_carries_context(self):
        """上一个任务还在跑时追问，新开的并发会话必须带上原会话的上下文。"""
        c = controller(self.folder)
        key = c.data['active_session']
        c.data['sessions'][key]['handoff'] = [{'engine': 'claude', 'request': '查东京商城', 'result': '找到对账脚本读旧库'}]
        first = c.enqueue(1, '先检查一下东京的商城')
        first['status'] = 'running'
        second = c.enqueue(1, '那个对账脚本顺便修一下')
        self.assertNotEqual(second['snapshot']['session_key'], key)
        requests = [item['request'] for item in second['snapshot']['handoff']]
        self.assertIn('查东京商城', requests)
        self.assertIn('先检查一下东京的商城', requests, '进行中的任务也要让新会话知道')

    def test_sessions_are_capped(self):
        c = controller(self.folder)
        for index in range(c.MAX_SESSIONS + 20):
            c.data['sessions']['s' + str(index)] = {'name': str(index), 'session_id': None}
        c._prune_sessions()
        self.assertLessEqual(len(c.data['sessions']), c.MAX_SESSIONS)
        self.assertIn(c.data['active_session'], c.data['sessions'])

    def test_interrupted_tasks_are_announced_once(self):
        state = {'active_session': 'a', 'sessions': {'a': {'name': '默认会话', 'session_id': None}},
                 'tasks': [{'id': 't1', 'status': 'running', 'kind': 'agent', 'chat': 5, 'user_context': '部署新版本',
                            'created': time.time(), 'started': time.time(), 'snapshot': snapshot()}]}
        c = controller(self.folder, state)
        sent = []
        with patch.object(bot, 'send', side_effect=lambda chat, text: sent.append((chat, text))):
            c.announce_interrupted()
            c.announce_interrupted()
        self.assertEqual(len(sent), 1)
        self.assertIn('部署新版本', sent[0][1])

    def test_monitored_service_uses_install_record(self):
        app = Path(tempfile.mkdtemp())
        (app / 'update-install.json').write_text(json.dumps({'service': 'claude-telegram.service'}), encoding='utf-8')
        with patch.object(bot, 'APP_DIR', app), patch.dict(os.environ, {'MONITORED_SERVICES': ''}):
            self.assertEqual(bot.monitored_services(), ['claude-telegram'])
        with patch.dict(os.environ, {'MONITORED_SERVICES': 'a,b'}):
            self.assertEqual(bot.monitored_services(), ['a', 'b'])

    def test_providers_file_cannot_be_sent(self):
        self.assertTrue(bot.sensitive_path(Path('/home/x/tasks/providers.json')))

    def test_registered_secret_is_redacted(self):
        bot.register_secret('plainsecretvalue123456')
        self.assertNotIn('plainsecretvalue123456', bot.redact('echo plainsecretvalue123456'))
        bot.register_secret('short')
        self.assertIn('short', bot.redact('a short word'), '太短的值不能按原文替换，会误伤正常文字')


if __name__ == '__main__':
    unittest.main()
