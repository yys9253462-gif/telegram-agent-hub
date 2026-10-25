#!/usr/bin/env python3
"""五轮交互测试 · 第三轮：执行器与渠道切换的交互一致性。

这一轮盯三类事：
1. 渠道是**按执行器独立**的，切 Claude 的渠道不能顺手改掉 CodeBuddy 的。
2. 切执行器与切渠道是两件事，不能互相偷偷带动。
3. 不管当前处于什么组合，回复底部那行「执行器 · 模型」都不能自相矛盾。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


class ChannelBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = bot.TaskController(self.root)
        self.sent = []
        self.patches = [
            patch.object(bot, 'CONTROL', self.controller),
            patch.object(bot, 'OWNER', 123),
            patch.object(bot, 'send', side_effect=lambda chat, text: self.sent.append(text)),
            patch.object(bot, 'api', side_effect=lambda method, payload=None: (
                self.sent.append(payload['text']) if isinstance(payload, dict) and payload.get('text') else None)
                or {'message_id': 1}),
            patch.object(bot, 'AUTH_STATE', self.root / 'auth-channel.json'),
            # 账号模型的存放路径由 WORK 派生，必须一起重定向，
            # 否则会往真实的 ~/tasks 里写文件。
            patch.object(bot, 'WORK', self.root),
            patch.object(bot, 'MODEL_STATE', self.root / 'selected-model.json'),
            patch.object(bot, 'STRATEGY_STATE', self.root / 'strategy.json'),
            patch.dict(bot.os.environ, {'ENABLED_AGENTS': 'claude,codebuddy,codex'}, clear=False),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def press(self, text):
        before = len(self.sent)
        try:
            self.controller.handle_text(123, text)
        except bot.MediaError as exc:
            self.sent.append(str(exc))
        return self.sent[before:]

    def engine(self):
        return bot.current_engine()


class ChannelIsolationTest(ChannelBase):
    def test_channels_are_per_engine(self):
        bot.set_auth_channel('account', 'claude')
        bot.set_auth_channel('gateway', 'codebuddy')
        self.assertEqual(bot.auth_channel('claude'), 'account')
        self.assertEqual(bot.auth_channel('codebuddy'), 'gateway')

    def test_switching_one_does_not_touch_the_other(self):
        bot.set_auth_channel('gateway', 'claude')
        bot.set_auth_channel('gateway', 'codex')
        bot.set_auth_channel('account', 'claude')
        self.assertEqual(bot.auth_channel('codex'), 'gateway')

    def test_legacy_single_channel_file_is_migrated_to_claude(self):
        """老版本只有一个 channel 字段，不能因为升级就把它当成「谁都没设」。"""
        (self.root / 'auth-channel.json').write_text(
            json.dumps({'channel': 'account'}), encoding='utf-8')
        self.assertEqual(bot.auth_channel('claude'), 'account')
        # 其它执行器不该被这条遗留设置带跑。
        self.assertEqual(bot.auth_channel('codebuddy'), 'gateway')

    def test_corrupt_channel_file_falls_back_to_gateway(self):
        (self.root / 'auth-channel.json').write_text('{坏掉的 json', encoding='utf-8')
        self.assertEqual(bot.auth_channel('claude'), 'gateway')

    def test_unknown_channel_value_is_rejected(self):
        (self.root / 'auth-channel.json').write_text(
            json.dumps({'channels': {'claude': '乱写'}}), encoding='utf-8')
        self.assertEqual(bot.auth_channel('claude'), 'gateway')


class EngineSwitchTest(ChannelBase):
    def test_switching_engine_keeps_channels(self):
        bot.set_auth_channel('account', 'claude')
        bot.set_auth_channel('gateway', 'codebuddy')
        self.press('使用CodeBuddy')
        self.assertEqual(self.engine(), 'codebuddy')
        self.assertEqual(bot.auth_channel('claude'), 'account', '切执行器把 Claude 的渠道改了')
        self.assertEqual(bot.auth_channel('codebuddy'), 'gateway')

    def test_switching_engine_does_not_switch_channel(self):
        """当前执行器在账号渠道，切到另一个走接口密钥的执行器后，
        渠道必须跟着执行器走，而不是沿用一个跟它无关的值。"""
        bot.set_auth_channel('account', 'claude')
        bot.set_auth_channel('gateway', 'codebuddy')
        self.press('使用CodeBuddy')
        self.assertEqual(bot.auth_channel(), 'gateway')

    def test_disabled_engine_is_refused(self):
        with patch.dict(bot.os.environ, {'ENABLED_AGENTS': 'claude'}, clear=False):
            replies = self.press('使用Codex')
        self.assertTrue(any('未安装' in text for text in replies), replies)
        self.assertEqual(self.engine(), 'claude', '被拒绝的切换却改了当前执行器')

    def test_already_running_task_keeps_its_engine(self):
        """任务入队时把执行器写进快照，之后切换不能反过来改变已在跑的任务。"""
        task = self.controller.enqueue(123, '一件事')
        self.press('使用CodeBuddy')
        with self.controller.lock:
            stored = self.controller.data['tasks'][0]
        self.assertEqual(stored['snapshot']['engine'], task['snapshot']['engine'])


class ChannelSwitchMessageTest(ChannelBase):
    def test_cross_engine_switch_warns_that_executor_is_unchanged(self):
        """给另一个执行器切渠道时，要说清「当前执行器没变，还用不上」。"""
        bot.set_auth_channel('gateway', 'claude')
        bot.set_auth_channel('gateway', 'codebuddy')
        replies = self.press('使用接口密钥 codebuddy')
        joined = '\n'.join(replies)
        self.assertIn('当前执行器仍是', joined)
        self.assertIn('Claude', joined)

    def test_same_engine_switch_has_no_such_warning(self):
        replies = self.press('使用接口密钥 claude')
        joined = '\n'.join(replies)
        self.assertNotIn('当前执行器仍是', joined)

    def test_account_channel_requires_login(self):
        with patch.object(bot, 'account_authorized', return_value=(False, '尚未登录')):
            replies = self.press('使用账号 claude')
        self.assertTrue(any('还没有确认' in text for text in replies), replies)
        self.assertEqual(bot.auth_channel('claude'), 'gateway', '没登录却把渠道切过去了')

    def test_unknown_engine_in_command_is_rejected(self):
        replies = self.press('使用账号 不存在的执行器')
        self.assertTrue(any('未知执行器' in text for text in replies), replies)


class AccountModelTest(ChannelBase):
    def test_account_model_refused_on_gateway_channel(self):
        replies = self.press('账号模型')
        joined = '\n'.join(replies)
        self.assertIn('接口密钥', joined)
        self.assertIn('渠道切换', joined, '拒绝时没有告诉用户去哪儿切渠道')

    def test_account_model_choice_requires_valid_id(self):
        bot.set_auth_channel('account', 'claude')
        with patch.object(bot, 'account_model_options', return_value=[('model-a', 'A')]):
            with self.assertRaises(bot.MediaError):
                bot.set_account_model('claude', 'model-z')

    def test_account_model_default_clears_choice(self):
        with patch.object(bot, 'account_model_options', return_value=[]):
            bot.set_account_model('claude', '')
        self.assertEqual(bot.account_model('claude'), '')

    def test_choice_is_remembered_per_engine(self):
        with patch.object(bot, 'account_model_options', return_value=[('model-a', 'A')]):
            bot.set_account_model('claude', 'model-a')
        self.assertEqual(bot.account_model('claude'), 'model-a')
        self.assertEqual(bot.account_model('codex'), '', '账号模型被写到了别的执行器上')

    def test_setting_model_on_gateway_channel_says_it_is_not_active(self):
        bot.set_auth_channel('gateway', 'claude')
        with patch.object(bot, 'account_model_options', return_value=[('model-a', 'A')]):
            replies = self.press('账号模型 model-a')
        joined = '\n'.join(replies)
        self.assertIn('要等切到账号模式才生效', joined)


class FooterConsistencyTest(ChannelBase):
    """底部那行是机主判断「这轮到底用了谁」的唯一依据，不能自相矛盾。"""

    def test_gateway_shows_gateway_model(self):
        bot.set_auth_channel('gateway', 'claude')
        bot.atomic_json(bot.MODEL_STATE, {'model': 'wbdp4.1f'})
        line = bot.execution_footer('claude')
        self.assertIn('执行器：Claude', line)
        self.assertIn('模型：wbdp4.1f', line)

    def test_account_shows_official_default_when_unset(self):
        bot.set_auth_channel('account', 'claude')
        with patch.object(bot, 'account_model', return_value=''):
            self.assertIn('官方默认', bot.execution_footer('claude'))

    def test_account_shows_chosen_model(self):
        bot.set_auth_channel('account', 'claude')
        with patch.object(bot, 'account_model', return_value='claude-sonnet-5'):
            self.assertIn('模型：claude-sonnet-5', bot.execution_footer('claude'))

    def test_footer_follows_the_current_engine(self):
        self.press('使用CodeBuddy')
        self.assertIn('执行器：CodeBuddy', bot.execution_footer())

    def test_unknown_engine_name_falls_back_without_crashing(self):
        """旧状态文件里可能残留已下线的执行器名。"""
        line = bot.execution_footer('某个已下线的执行器')
        self.assertTrue(line.startswith('执行器：'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
