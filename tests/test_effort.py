#!/usr/bin/env python3
"""账号模型真正传给 CLI、思考强度设置与生效。

线上缺陷（2026-10-26）：Claude 账号模式选了 opus，命令行里却从没带 --model，
实际跑的是 CLI 默认模型；思考强度则完全没有入口。

运行：python3 tests/test_effort.py
"""
import _root  # noqa: F401  仓库根目录进 sys.path
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


class EffortTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.saved = {name: getattr(bot, name) for name in ('WORK', 'AUTH_STATE', 'MODEL_STATE')}
        bot.WORK = self.folder
        bot.AUTH_STATE = self.folder / 'auth-channel.json'
        bot.MODEL_STATE = self.folder / 'selected-model.json'

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(bot, name, value)

    def command(self, engine):
        with patch.object(bot, 'current_engine', return_value=engine), \
             patch.object(bot, 'session_id', return_value=None), \
             patch.object(bot, 'engine_binary', return_value=engine):
            command, _stdin = bot.engine_command(engine, '任务', '指令', 1, {})
        return command

    def test_claude_account_model_reaches_command_line(self):
        bot.set_auth_channel('account', 'claude')
        bot.set_account_model('claude', 'opus[1m]')
        command = self.command('claude')
        self.assertIn('--model', command)
        self.assertEqual(command[command.index('--model') + 1], 'opus[1m]')

    def test_effort_reaches_each_engine(self):
        bot.set_effort('claude', 'xhigh')
        command = self.command('claude')
        self.assertEqual(command[command.index('--effort') + 1], 'xhigh')
        bot.set_effort('codebuddy', 'minimal')
        command = self.command('codebuddy')
        self.assertEqual(command[command.index('--effort') + 1], 'minimal')
        bot.set_effort('codex', 'high')
        self.assertIn('model_reasoning_effort="high"', self.command('codex'))

    def test_default_effort_adds_no_flag(self):
        self.assertNotIn('--effort', self.command('claude'))

    def test_unsupported_level_is_rejected(self):
        with self.assertRaises(bot.MediaError):
            bot.set_effort('codex', 'max')

    def test_task_keeps_effort_from_enqueue_time(self):
        bot.set_effort('claude', 'low')
        task = {'snapshot': {'engine': 'claude', 'effort': 'max'}}
        bot.TASK_LOCAL.task = task
        try:
            self.assertEqual(bot.current_effort('claude'), 'max')
        finally:
            bot.TASK_LOCAL.task = None
        self.assertEqual(bot.current_effort('claude'), 'low')

    def test_footer_shows_readable_model_and_effort(self):
        bot.set_auth_channel('account', 'claude')
        bot.set_account_model('claude', 'sonnet[1m]')
        bot.set_effort('claude', 'high')
        footer = bot.execution_footer('claude')
        self.assertIn('Sonnet 5.5 · 1M 上下文', footer)
        self.assertIn('思考：高（high）', footer)


if __name__ == '__main__':
    unittest.main()
