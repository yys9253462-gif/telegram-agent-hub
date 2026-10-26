"""执行器账号渠道、环境与排队快照回归，不访问网络。"""
import _root  # noqa: F401  仓库根目录进 sys.path
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


class EngineAccountsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = bot.TaskController(self.root)
        self.patches = [patch.object(bot, 'CONTROL', self.controller),
                        patch.object(bot, 'AUTH_STATE', self.root / 'auth-channel.json'),
                        patch.object(bot, 'MODEL_STATE', self.root / 'model.json'),
                        patch.object(bot, 'MODEL_CACHE', self.root / 'catalog.json'),
                        patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key',
                                               'ANTHROPIC_BASE_URL': 'https://gateway.invalid',
                                               'ANTHROPIC_MODEL': 'gateway-model',
                                               'OPENAI_API_KEY': 'openai-gateway-key',
                                               'OPENAI_BASE_URL': 'https://openai.invalid',
                                               'CODEX_API_KEY': 'codex-key',
                                               'CODEBUDDY_API_KEY': 'buddy-key',
                                               'CODEBUDDY_BASE_URL': 'https://buddy.invalid',
                                               'UNRELATED_KEY': 'keep-me'})]
        for item in self.patches:
            item.start()
        bot.TASK_LOCAL.task = None

    def tearDown(self):
        bot.TASK_LOCAL.task = None
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_legacy_channel_applies_only_to_claude(self):
        bot.AUTH_STATE.write_text(json.dumps({'channel': 'account'}))
        self.assertEqual(bot.auth_channel('claude'), 'account')
        self.assertEqual(bot.auth_channel('codebuddy'), 'gateway')
        self.assertEqual(bot.auth_channel('codex'), 'gateway')
        bot.set_auth_channel('account', 'codex')
        self.assertEqual(json.loads(bot.AUTH_STATE.read_text()),
                         {'channels': {'claude': 'account', 'codex': 'account'}})

    def test_concurrent_engine_writes_preserve_other_channels(self):
        barrier = threading.Barrier(3)
        def switch(engine):
            barrier.wait()
            bot.set_auth_channel('account', engine)
        threads = [threading.Thread(target=switch, args=(engine,)) for engine in bot.ENGINES]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(json.loads(bot.AUTH_STATE.read_text())['channels'],
                         dict.fromkeys(bot.ENGINES, 'account'))

    def test_queued_account_does_not_follow_channel_switch(self):
        self.controller.data['engine'] = 'codex'
        bot.set_auth_channel('account', 'codex')
        task = self.controller.enqueue(1, '测试任务')
        bot.set_auth_channel('gateway', 'codex')
        self.controller.data['engine'] = 'claude'
        bot.TASK_LOCAL.task = task
        self.assertEqual(bot.current_engine(), 'codex')
        self.assertEqual(bot.auth_channel(), 'account')
        environment = bot.model_environment()
        command, _ = bot.engine_command('codex', '测试', '指令', 1, environment)
        self.assertNotIn('--model', command)
        self.assertIn('model_provider="openai"', command)
        self.assertIn('forced_login_method="chatgpt"', command)
        for name in bot.GATEWAY_VARS + bot.MODEL_PIN_VARS + bot.OPENAI_GATEWAY_VARS:
            self.assertNotIn(name, environment)
        self.assertEqual(environment['UNRELATED_KEY'], 'keep-me')

    def test_queued_gateway_does_not_follow_account_switch(self):
        self.controller.data['engine'] = 'codex'
        task = self.controller.enqueue(1, '测试任务')
        bot.set_auth_channel('account', 'codex')
        bot.TASK_LOCAL.task = task
        environment = bot.model_environment()
        command, _ = bot.engine_command('codex', '测试', '指令', 1, environment)
        self.assertIn('--model', command)
        self.assertIn('gateway-model', command)
        self.assertEqual(environment['OPENAI_API_KEY'], 'openai-gateway-key')

    def test_old_task_without_channel_keeps_gateway(self):
        bot.set_auth_channel('account', 'codex')
        bot.TASK_LOCAL.task = {'snapshot': {'engine': 'codex', 'model': 'gateway-model'}}
        self.assertEqual(bot.auth_channel(), 'gateway')

    def test_account_catalog_never_reuses_gateway_cache(self):
        bot.MODEL_CACHE.write_text(json.dumps([{'id': 'private-gateway-model'}]))
        for engine in bot.ENGINES:
            self.controller.data['engine'] = engine
            bot.set_auth_channel('account', engine)
            with patch.object(bot.urllib.request, 'urlopen') as request:
                self.assertEqual(bot.catalog(), [{'id': 'default', 'provider': bot.ENGINES[engine]['label'] + ' 账号'}])
                self.assertEqual(bot.catalog(refresh=True)[0]['id'], 'default')
                request.assert_not_called()

    def test_codebuddy_account_does_not_send_gateway_model(self):
        self.controller.data['engine'] = 'codebuddy'
        bot.set_auth_channel('account', 'codebuddy')
        environment = bot.model_environment()
        command, _ = bot.engine_command('codebuddy', '测试', '指令', 1, environment)
        self.assertNotIn('gateway-model', command)
        for name in bot.GATEWAY_VARS + bot.MODEL_PIN_VARS + bot.CODEBUDDY_GATEWAY_VARS:
            self.assertNotIn(name, environment)

    def test_codex_account_needs_no_gateway_key(self):
        self.controller.data['engine'] = 'codex'
        bot.set_auth_channel('account', 'codex')
        with patch.dict(os.environ, {}, clear=True):
            environment = bot.model_environment()
            command, prompt = bot.engine_command('codex', '测试', '指令', 1, environment)
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertNotIn('OPENAI_API_KEY', environment)
        self.assertEqual(prompt, '指令\n\n测试')

    def test_default_channel_follows_selected_engine(self):
        bot.set_auth_channel('account', 'codebuddy')
        self.controller.data['engine'] = 'codebuddy'
        self.assertEqual(bot.auth_channel(), 'account')
        self.controller.data['engine'] = 'claude'
        self.assertEqual(bot.auth_channel(), 'gateway')

    def test_claude_status_nonzero_is_not_authorized(self):
        with patch.object(bot.subprocess, 'run') as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ''
            run.return_value.stderr = 'network failure'
            self.assertFalse(bot.account_authorized()[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
