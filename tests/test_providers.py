#!/usr/bin/env python3
"""菜单里管理模型供应商：添加、切换、更换密钥、删除。

固化几件事：密钥消息必须被删掉且不出现在任何回复里；不像地址/密钥的文字
照常当任务；切换供应商后执行环境与会话都跟着换；验证不过不保存。

运行：python3 tests/test_providers.py
"""
import _root  # noqa: F401  仓库根目录进 sys.path
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import bot

KEY = 'testkey-0123456789abcdefWXYZ'
NEW_KEY = 'testkey-9876543210fedcbaQRST'
MODELS = [{'id': 'model-a', 'provider': 'x', 'tools': True}, {'id': 'model-b', 'provider': 'x', 'tools': True}]


def wait_verify():
    """密钥验证在后台线程里跑，等它结束再断言。"""
    for thread in threading.enumerate():
        if thread.name == 'provider-verify':
            thread.join(5)


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.saved = {name: getattr(bot, name) for name in
                      ('WORK', 'PROVIDER_STATE', 'MODEL_STATE', 'MODEL_CACHE', 'AUTH_STATE')}
        bot.WORK = self.folder
        bot.PROVIDER_STATE = self.folder / 'providers.json'
        bot.MODEL_STATE = self.folder / 'selected-model.json'
        bot.MODEL_CACHE = self.folder / 'model-catalog.json'
        bot.AUTH_STATE = self.folder / 'auth-channel.json'
        self.env = patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'https://install.example.com',
                                           'ANTHROPIC_API_KEY': 'testkey-install-000000001111',
                                           'ANTHROPIC_AUTH_TOKEN': 'old-token', 'ANTHROPIC_MODEL': 'install-model'})
        self.env.start()
        self.sent, self.deleted = [], []
        self.patches = [
            patch.object(bot, 'send', side_effect=lambda chat, text: self.sent.append(text)),
            patch.object(bot, 'send_options', side_effect=lambda chat, text, options: self.sent.append(text + ' ' + ' '.join(map(str, options)))),
            patch.object(bot, 'fetch_provider_models', side_effect=self.fetch),
            patch.object(bot, 'api', side_effect=self.api),
        ]
        for item in self.patches:
            item.start()
        self.valid_keys = {KEY, NEW_KEY}
        bot.PROVIDER_INPUT.clear()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.env.stop()
        for name, value in self.saved.items():
            setattr(bot, name, value)
        bot.PROVIDER_INPUT.clear()
        bot.INCOMING['message_id'] = None

    def fetch(self, base_url, api_key):
        if api_key not in self.valid_keys:
            raise bot.MediaError('密钥无效或没有权限（HTTP 401）。')
        return MODELS

    def api(self, method, payload, retries=None):
        if method == 'deleteMessage':
            self.deleted.append(payload['message_id'])
        return {}

    def add(self, url='https://api.other.com/v1', key=KEY):
        bot.start_provider_input(1, 'url')
        self.assertTrue(bot.provider_input(1, url))
        bot.INCOMING['message_id'] = 77
        self.assertTrue(bot.provider_input(1, key))
        wait_verify()
        return next(item for item in bot.provider_list() if item['id'] != bot.BUILTIN_PROVIDER)

    def test_add_provider_deletes_key_message_and_never_echoes_key(self):
        item = self.add()
        self.assertEqual(item['base_url'], 'https://api.other.com')
        self.assertEqual(item['api_key'], KEY)
        self.assertEqual(self.deleted, [77], '含密钥的消息必须被删掉')
        self.assertFalse(any(KEY in text for text in self.sent), '回复里不能出现密钥原文')
        self.assertIn('…WXYZ', bot.provider_overview())
        self.assertNotIn(KEY, bot.provider_overview())
        self.assertEqual(bot.PROVIDER_STATE.stat().st_mode & 0o777, 0o600)

    def test_bad_key_is_not_saved(self):
        bot.start_provider_input(1, 'url')
        bot.provider_input(1, 'https://api.other.com')
        self.assertTrue(bot.provider_input(1, 'testkey-wrong-00000000000000'))
        wait_verify()
        self.assertEqual(len(bot.provider_list()), 1, '验证不过不能保存')
        self.assertTrue(any('验证没通过' in text for text in self.sent))
        self.assertIn(1, bot.PROVIDER_INPUT, '验证失败后仍可重发密钥')

    def test_plain_text_while_waiting_is_not_swallowed(self):
        bot.start_provider_input(1, 'url')
        self.assertFalse(bot.provider_input(1, '帮我看看 nginx 为什么 502'))
        bot.provider_input(1, 'https://api.other.com')
        self.assertFalse(bot.provider_input(1, '帮我看看 nginx 为什么 502'))
        self.assertFalse(bot.provider_input(1, 'short'))
        self.assertEqual(self.deleted, [], '普通消息不能被当成密钥删掉')

    def test_bad_url_is_rejected(self):
        bot.start_provider_input(1, 'url')
        bot.provider_input(1, 'http://evil.example.com')
        self.assertEqual(bot.PROVIDER_INPUT[1]['step'], 'url')
        self.assertTrue(any('https' in text for text in self.sent))

    def test_switch_changes_environment_model_and_session(self):
        item = self.add()
        session = {'engine_sessions': {'claude': 'old-session'}}
        self.assertEqual(bot.stored_session(session, 'claude', bot.session_channel('claude')), 'old-session')
        bot.atomic_json(bot.MODEL_STATE, {'model': 'install-model'})
        self.assertEqual(bot.switch_provider(item['id']), '')
        environment = bot.model_environment()
        self.assertEqual(environment['ANTHROPIC_BASE_URL'], 'https://api.other.com')
        self.assertEqual(environment['ANTHROPIC_API_KEY'], KEY)
        self.assertNotIn('ANTHROPIC_AUTH_TOKEN', environment, '旧令牌会被 CLI 优先使用，必须去掉')
        self.assertIsNone(bot.stored_session(session, 'claude', bot.session_channel('claude')),
                          '换供应商后不能续接旧会话')
        # 选了模型再切回去，各自记住自己的模型
        bot.atomic_json(bot.MODEL_STATE, {'model': 'model-b'})
        bot.remember_provider_model('model-b')
        self.assertEqual(bot.switch_provider(bot.BUILTIN_PROVIDER), 'install-model')
        self.assertEqual(bot.switch_provider(item['id']), 'model-b')

    def test_builtin_environment_untouched_until_key_replaced(self):
        environment = bot.model_environment()
        self.assertEqual(environment['ANTHROPIC_AUTH_TOKEN'], 'old-token')
        bot.start_provider_input(1, 'key', provider_id=bot.BUILTIN_PROVIDER)
        bot.INCOMING['message_id'] = 9
        self.assertTrue(bot.provider_input(1, NEW_KEY))
        wait_verify()
        environment = bot.model_environment()
        self.assertEqual(environment['ANTHROPIC_API_KEY'], NEW_KEY)
        self.assertEqual(environment['ANTHROPIC_BASE_URL'], 'https://install.example.com')
        self.assertEqual(bot.session_channel('claude'), 'gateway', '内置供应商换密钥不该丢会话')

    def test_account_channel_ignores_provider(self):
        item = self.add()
        bot.switch_provider(item['id'])
        bot.set_auth_channel('account', 'claude')
        environment = bot.model_environment()
        self.assertNotIn('ANTHROPIC_API_KEY', environment)
        self.assertEqual(bot.session_channel('claude'), 'account')

    def test_key_is_redacted_by_value_even_without_prefix(self):
        bare = 'abcdef0123456789ZYXWVUTSRQ'
        self.valid_keys.add(bare)
        self.add(key=bare)
        self.assertNotIn(bare, bot.redact('env: ANTHROPIC=' + bare + ' done'))

    def test_cancel_during_verification_does_not_save(self):
        bot.start_provider_input(1, 'url')
        bot.provider_input(1, 'https://api.other.com')
        gate = threading.Event()

        def slow(base_url, api_key):
            gate.wait(5)
            return MODELS
        with patch.object(bot, 'fetch_provider_models', side_effect=slow):
            bot.provider_input(1, KEY)
            bot.cancel_provider_input(1)
            gate.set()
            wait_verify()
        self.assertEqual(len(bot.provider_list()), 1, '验证期间取消了就不能保存')

    def test_corrupted_state_falls_back_to_builtin(self):
        bot.PROVIDER_STATE.write_text('{坏的', encoding='utf-8')
        self.assertEqual(bot.active_provider_id(), bot.BUILTIN_PROVIDER)
        bot.PROVIDER_STATE.write_text(json.dumps({'active': 'missing', 'items': {}}), encoding='utf-8')
        self.assertEqual(bot.active_provider_id(), bot.BUILTIN_PROVIDER)


if __name__ == '__main__':
    unittest.main()
