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

    def press(self, text):
        """模拟机主点按钮，返回 (send 的文字列表, send_options 的 (标题, 按钮命令列表))。"""
        with patch.object(bot, 'current_engine', return_value='claude'), \
             patch.object(bot, 'send') as send, patch.object(bot, 'send_options') as options:
            bot.TaskController(self.folder).handle_text(1, text)
        texts = [call.args[1] for call in send.call_args_list]
        page = None
        if options.call_args:
            title, labels = options.call_args.args[1], options.call_args.args[2]
            page = (title, [item[1] if isinstance(item, tuple) else item for item in labels])
        return texts, page

    def test_guided_flow_main_then_worker_then_enable(self):
        """规划执行要按顺序引导：先选主模型 → 再选执行模型 → 最后确认开启。"""
        _, (title, commands) = self.press('规划执行')
        self.assertIn('第 1 步', title)
        self.assertIn('规划主模型 big-model', commands)
        _, (title, commands) = self.press('规划主模型 big-model')
        self.assertIn('主模型已设为：big-model', title)
        self.assertIn('第 2 步', title)
        self.assertIn('执行模型 fast-model', commands)
        self.assertFalse(bot.collab_worker('claude'), '还没确认开启，不能先生效')
        _, (title, commands) = self.press('执行模型 fast-model')
        self.assertIn('第 3 步', title)
        self.assertIn('主模型（规划 + 审核）：big-model', title)
        self.assertIn('执行模型（执行具体操作）：fast-model', title)
        self.assertEqual(commands[0], '开启规划执行')
        texts, _ = self.press('开启规划执行')
        self.assertIn('规划执行已开启', texts[-1])
        self.assertEqual(bot.collab_worker('claude'), 'fast-model')
        self.assertEqual(bot.current_model(), 'big-model')

    def test_entry_shows_status_when_enabled(self):
        bot.set_collab('claude', worker='fast-model')
        bot.set_collab('claude', enabled=True)
        _, (title, commands) = self.press('规划执行')
        self.assertIn('规划执行已开启', title)
        self.assertEqual(commands, ['关闭规划执行', '重新设置规划执行'])
        _, (title, _) = self.press('重新设置规划执行')
        self.assertIn('第 1 步', title)

    def test_account_main_model_goes_to_account_setting(self):
        bot.set_auth_channel('account', 'claude')
        _, (_, commands) = self.press('规划执行')
        self.assertIn('规划主模型 opus', commands)
        self.press('规划主模型 opus')
        self.assertEqual(bot.account_model('claude'), 'opus')

    def test_paging_and_search(self):
        bot.MODEL_CACHE.write_text(json.dumps([{'id': 'model-' + str(i)} for i in range(30)]), encoding='utf-8')
        _, (title, commands) = self.press('规划执行')
        self.assertIn('第 1/3 页', title)
        self.assertIn('规划模型页 main  1', commands)
        _, (title, commands) = self.press('规划模型页 main  1')
        self.assertIn('第 2/3 页', title)
        self.assertIn('规划主模型 model-12', commands)
        _, (title, commands) = self.press('搜索执行模型 model-2')
        self.assertIn('第 2 步', title)
        self.assertIn('执行模型 model-25', commands)

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(bot.MediaError):
            self.press('规划主模型 not-exist')

    def test_footer_shows_worker(self):
        bot.set_collab('claude', worker='fast-model')
        bot.set_collab('claude', enabled=True)
        with patch.dict(os.environ, {}):
            self.assertIn('执行模型：fast-model', bot.execution_footer('claude'))


if __name__ == '__main__':
    unittest.main()
