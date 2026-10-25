#!/usr/bin/env python3
"""Claude 账号登录与渠道切换的回归测试。

对应需求：机器人原本只能靠 API Key 走网关，无法登录 Claude 账号。
这里固化三点：账号模式下必须剔除网关变量、授权码判定不能误吞普通消息、
渠道状态损坏时要退回到可用的默认值。

运行：python test_auth_channel.py
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import bot


class AuthChannelStateTest(unittest.TestCase):
    """缺陷：渠道状态文件缺失或被改坏时，不能因此拒绝服务，
    必须退回网关渠道（那是默认且始终可用的）。"""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.original = bot.AUTH_STATE
        bot.AUTH_STATE = self.folder / 'auth-channel.json'

    def tearDown(self):
        bot.AUTH_STATE = self.original

    def test_default_is_gateway(self):
        self.assertEqual(bot.auth_channel(), 'gateway', '没有状态文件时应默认走接口密钥')

    def test_roundtrip(self):
        bot.set_auth_channel('account')
        self.assertEqual(bot.auth_channel(), 'account')
        bot.set_auth_channel('gateway')
        self.assertEqual(bot.auth_channel(), 'gateway')

    def test_corrupted_file_falls_back(self):
        bot.AUTH_STATE.write_text('{不是合法 JSON', encoding='utf-8')
        self.assertEqual(bot.auth_channel(), 'gateway', '状态文件损坏时必须退回默认渠道')

    def test_unknown_value_falls_back(self):
        bot.AUTH_STATE.write_text(json.dumps({'channel': '不存在'}), encoding='utf-8')
        self.assertEqual(bot.auth_channel(), 'gateway')

    def test_rejects_unknown_channel(self):
        with self.assertRaises(bot.MediaError):
            bot.set_auth_channel('随便写')


class ModelEnvironmentTest(unittest.TestCase):
    """缺陷：新版和旧版都没法在账号模式下工作。只要 ANTHROPIC_API_KEY /
    ANTHROPIC_BASE_URL 还留在环境变量里，CLI 就会优先走网关，
    机主登录的账号凭据永远用不上。"""

    def setUp(self):
        self.original_env = dict(os.environ)
        self.folder = Path(tempfile.mkdtemp())
        self.original_auth = bot.AUTH_STATE
        self.original_model_state = bot.MODEL_STATE
        bot.AUTH_STATE = self.folder / 'auth-channel.json'
        bot.MODEL_STATE = self.folder / 'selected-model.json'
        os.environ['ANTHROPIC_API_KEY'] = 'TEST-KEY-NOT-REAL'
        os.environ['ANTHROPIC_BASE_URL'] = 'https://example.invalid'
        os.environ['ANTHROPIC_MODEL'] = 'auto'

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)
        bot.AUTH_STATE = self.original_auth
        bot.MODEL_STATE = self.original_model_state

    def test_gateway_mode_keeps_credentials(self):
        bot.set_auth_channel('gateway')
        environment = bot.model_environment()
        self.assertEqual(environment.get('ANTHROPIC_API_KEY'), 'TEST-KEY-NOT-REAL')
        self.assertEqual(environment.get('ANTHROPIC_BASE_URL'), 'https://example.invalid')

    def test_account_mode_strips_credentials(self):
        bot.set_auth_channel('account')
        environment = bot.model_environment()
        self.assertNotIn('ANTHROPIC_API_KEY', environment, '账号模式必须移除 API Key，否则登录态永远不被使用')
        self.assertNotIn('ANTHROPIC_BASE_URL', environment, '账号模式必须移除网关地址')
        self.assertNotIn('ANTHROPIC_AUTH_TOKEN', environment)

    def test_account_mode_strips_model_overrides(self):
        bot.set_auth_channel('account')
        environment = bot.model_environment()
        # 网关的模型名（如 auto）在账号渠道下不存在，必须让 CLI 用自己的默认目录。
        for name in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
                     'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL'):
            self.assertNotIn(name, environment, name + ' 不应带到账号渠道')

    def test_autoupdater_still_disabled_in_account_mode(self):
        bot.set_auth_channel('account')
        self.assertEqual(bot.model_environment().get('DISABLE_AUTOUPDATER'), '1',
                         '切换渠道不应顺带打开自动更新')


class LoginCodeTest(unittest.TestCase):
    """缺陷：授权码要走普通消息通道回传。若不设判定，
    机主的正常任务文本可能被当成授权码提交，任务直接被吞掉。"""

    def test_accepts_typical_code(self):
        self.assertTrue(bot.looks_like_login_code('ac_' + 'x' * 60))

    def test_accepts_code_with_symbols(self):
        self.assertTrue(bot.looks_like_login_code('aB3.dEf-ghi_Jkl+mno/pqr=='))

    def test_rejects_chinese_task(self):
        self.assertFalse(bot.looks_like_login_code('帮我看看服务器为什么这么慢'))

    def test_rejects_text_with_spaces(self):
        self.assertFalse(bot.looks_like_login_code('这是 一段 带空格 的文本'))

    def test_rejects_short_text(self):
        self.assertFalse(bot.looks_like_login_code('abc123'))

    def test_rejects_empty(self):
        self.assertFalse(bot.looks_like_login_code(''))
        self.assertFalse(bot.looks_like_login_code(None))

    def test_rejects_overlong(self):
        self.assertFalse(bot.looks_like_login_code('x' * 3000))


if __name__ == '__main__':
    unittest.main(verbosity=2)
