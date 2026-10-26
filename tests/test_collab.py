#!/usr/bin/env python3
"""规划执行模式：主模型规划 + 审核，executor 子代理用指定的执行模型干活。

运行：python3 tests/test_collab.py
"""
import _root  # noqa: F401  仓库根目录进 sys.path
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


class CollabTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.saved = {name: getattr(bot, name) for name in
                      ('WORK', 'AUTH_STATE', 'MODEL_STATE', 'MODEL_CACHE', 'COLLAB_STATE', 'PROVIDER_STATE')}
        bot.WORK = self.folder
        bot.AUTH_STATE = self.folder / 'auth-channel.json'
        bot.MODEL_STATE = self.folder / 'selected-model.json'
        bot.MODEL_CACHE = self.folder / 'model-catalog.json'
        bot.COLLAB_STATE = self.folder / 'collab.json'
        bot.PROVIDER_STATE = self.folder / 'providers.json'
        bot.MODEL_CACHE.write_text(json.dumps([{'id': 'big-model'}, {'id': 'fast-model'}]), encoding='utf-8')
        bot.MODEL_STATE.write_text(json.dumps({'model': 'big-model'}), encoding='utf-8')

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(bot, name, value)

    def command(self, engine='claude'):
        with patch.object(bot, 'current_engine', return_value=engine), \
             patch.object(bot, 'session_id', return_value=None), \
             patch.object(bot, 'engine_binary', return_value=engine):
            command, _ = bot.engine_command(engine, '任务', '基础指令', 1, {})
        return command

    def test_gateway_worker_is_defined_as_executor_subagent(self):
        bot.set_collab('claude', worker='fast-model')
        bot.set_collab('claude', enabled=True)
        command = self.command()
        agents = json.loads(command[command.index('--agents') + 1])
        self.assertEqual(agents['executor']['model'], 'fast-model')
        prompt = command[command.index('--append-system-prompt') + 1]
        self.assertTrue(prompt.startswith('基础指令'))
        self.assertIn('executor', prompt)
        self.assertIn('复核', prompt)

    def test_disabled_adds_nothing(self):
        bot.set_collab('claude', worker='fast-model')
        command = self.command()
        self.assertNotIn('--agents', command)
        self.assertNotIn(bot.COLLAB_PROMPT, command[command.index('--append-system-prompt') + 1])

    def test_cannot_enable_without_worker(self):
        with self.assertRaises(bot.MediaError):
            bot.set_collab('claude', enabled=True)

    def test_account_and_gateway_settings_are_separate(self):
        bot.set_collab('claude', worker='fast-model')
        bot.set_collab('claude', enabled=True)
        bot.set_auth_channel('account', 'claude')
        self.assertEqual(bot.collab_worker('claude'), '', '账号渠道不能沿用网关的执行模型')
        bot.set_collab('claude', worker='sonnet')
        bot.set_collab('claude', enabled=True)
        bot.set_account_model('claude', 'opus')
        command = self.command()
        self.assertEqual(json.loads(command[command.index('--agents') + 1])['executor']['model'], 'sonnet')
        self.assertEqual(command[command.index('--model') + 1], 'opus')
        bot.set_auth_channel('gateway', 'claude')
        self.assertEqual(bot.collab_worker('claude'), 'fast-model')

    def test_codex_is_not_supported(self):
        self.assertNotIn('codex', bot.COLLAB_ENGINES)
        self.assertEqual(bot.collab_worker('codex'), '')

    def test_task_keeps_worker_from_enqueue_time(self):
        bot.set_collab('claude', worker='fast-model')
        bot.set_collab('claude', enabled=True)
        bot.TASK_LOCAL.task = {'snapshot': {'engine': 'claude', 'worker': ''}}
        try:
            self.assertEqual(bot.collab_worker('claude'), '', '入队时没开，运行时就不该开')
        finally:
            bot.TASK_LOCAL.task = None

    def test_footer_shows_worker(self):
        bot.set_collab('claude', worker='fast-model')
        bot.set_collab('claude', enabled=True)
        with patch.dict(os.environ, {}):
            self.assertIn('执行模型：fast-model', bot.execution_footer('claude'))


if __name__ == '__main__':
    unittest.main()
