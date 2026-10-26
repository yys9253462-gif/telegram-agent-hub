#!/usr/bin/env python3
"""五轮交互测试 · 第四轮：授权、令牌回填与凭据注入的状态机。

这一轮的判据都是「凭据会不会泄漏 / 会不会被错误地静默使用」：
1. 等待令牌期间发普通任务，不能被当成令牌吞掉。
2. 撤销授权之后，轮询线程不能把令牌写回来。
3. 未授权时同名环境变量必须被剔除，不能让任务捡到全局令牌。
4. 续期只静默成功、只在真失效时提醒。
"""
import _root  # noqa: F401  仓库根目录进 sys.path
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import credentials

FAKE_CF = 'cfut_' + 'Xy9' * 8
FAKE_GH = 'ghp_' + 'A1b2' * 9


class AuthBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sent = []
        self.patches = [
            patch.object(bot, 'OWNER', 123),
            patch.object(bot, 'send', side_effect=lambda chat, text: self.sent.append(text)),
            patch.object(bot, 'notify', side_effect=lambda chat, text: self.sent.append(text)),
            patch.object(bot, 'api', side_effect=lambda method, payload=None: (
                self.sent.append(payload['text']) if isinstance(payload, dict) and payload.get('text') else None)
                or {'message_id': 1}),
            patch.object(bot, 'CREDENTIALS', self.root / 'credentials.json'),
            patch.object(bot, 'CREDENTIAL_RENEW_WARNED', {}),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        for item in reversed(self.patches):
            item.stop()
        bot.PENDING_TOKEN.update({'provider': '', 'chat': None, 'expires_at': 0})
        bot.DEVICE_FLOW.update({'provider': '', 'device_code': '', 'user_code': '', 'thread': None})
        bot.CLOUDFLARE_FLOW.update({'thread': None, 'device_code': '', 'user_code': ''})
        self.temp.cleanup()

    def store(self):
        return credentials.CredentialStore(bot.CREDENTIALS)


class PendingTokenTest(AuthBase):
    """粘贴令牌的两种交互：真令牌要收下，普通任务要放行。"""

    def arm(self):
        bot.PENDING_TOKEN.update({'provider': 'cloudflare', 'chat': 123,
                                  'expires_at': time.time() + 300})

    def test_valid_token_is_consumed(self):
        self.arm()
        with patch.object(bot, 'submit_cloudflare_token') as submit:
            handled = bot.await_token_input(123, FAKE_CF)
        self.assertTrue(handled)
        submit.assert_called_once()
        self.assertEqual(bot.PENDING_TOKEN['provider'], '', '收下令牌后没有清掉等待态')

    def test_normal_task_is_passed_through(self):
        """等待期间发一句任务描述，必须能正常入队，不能被当成令牌吞掉。"""
        self.arm()
        with patch.object(bot, 'submit_cloudflare_token') as submit:
            handled = bot.await_token_input(123, '帮我看看磁盘还剩多少')
        # 会是 True（提示「不像令牌」），但绝不能真的去校验或吞掉任务。
        submit.assert_not_called()
        self.assertEqual(bot.PENDING_TOKEN['provider'], 'cloudflare', '等待态不该被普通文本清掉')

    def test_expired_window_stops_intercepting(self):
        bot.PENDING_TOKEN.update({'provider': 'cloudflare', 'chat': 123,
                                  'expires_at': time.time() - 1})
        self.assertFalse(bot.await_token_input(123, '随便一句话'))

    def test_other_chat_is_not_affected(self):
        self.arm()
        self.assertFalse(bot.await_token_input(999, '别的会话的任务'))

    def test_password_like_text_is_not_a_token(self):
        self.arm()
        with patch.object(bot, 'submit_cloudflare_token') as submit:
            bot.await_token_input(123, 'my password is hunter2')
        submit.assert_not_called()

    def test_cancel_clears_the_window(self):
        self.arm()
        bot.PENDING_TOKEN.update({'provider': '', 'chat': None, 'expires_at': 0})
        self.assertFalse(bot.pending_token(123))


class RevokeTest(AuthBase):
    def test_revoke_removes_credential(self):
        store = self.store()
        store.save('cloudflare', FAKE_CF, scope='站点')
        bot.PENDING_TOKEN.update({'provider': '', 'chat': None, 'expires_at': 0})
        import json
        with patch.object(bot, 'send', side_effect=lambda chat, text: self.sent.append(text)):
            bot.CONTROL = None
            self.run_revoke()
        self.assertIsNone(store.get('cloudflare'))

    def run_revoke(self):
        """直接走撤销分支：它不需要 controller，只改内存态与凭据文件。"""
        controller = bot.TaskController(self.root)
        with patch.object(bot, 'CONTROL', controller):
            controller.handle_text(123, '撤销Cloudflare')

    def test_revoke_clears_inflight_device_flow(self):
        bot.CLOUDFLARE_FLOW.update({'device_code': 'still-waiting', 'user_code': 'ABCD'})
        store = self.store()
        store.save('cloudflare', FAKE_CF, scope='站点')
        self.run_revoke()
        self.assertEqual(bot.CLOUDFLARE_FLOW['device_code'], '',
                         '撤销后设备码还留在内存里，轮询线程会把令牌写回来')

    def test_revoke_without_credential_is_reported(self):
        self.run_revoke()
        self.assertTrue(any('本来就没有' in text for text in self.sent), self.sent)


class CredentialEnvironmentTest(AuthBase):
    """未授权时必须剔除同名变量，这是「静默用了别人的令牌」的唯一防线。"""

    def test_unauthorized_vars_are_removed(self):
        base = {'GH_TOKEN': 'leaked-from-global', 'CLOUDFLARE_API_TOKEN': 'leaked',
                'PATH': '/usr/bin'}
        with patch.object(bot, 'renew_credential', return_value=None):
            environment = bot.credential_environment(base)
        self.assertNotIn('GH_TOKEN', environment)
        self.assertNotIn('CLOUDFLARE_API_TOKEN', environment)
        self.assertEqual(environment['PATH'], '/usr/bin')

    def test_authorized_var_is_injected(self):
        self.store().save('cloudflare', FAKE_CF, scope='站点')
        with patch.object(bot, 'renew_credential', return_value=None):
            environment = bot.credential_environment({})
        for spec in bot.PROVIDERS.values():
            if spec['env'] in environment:
                self.assertEqual(environment[spec['env']], FAKE_CF)

    def test_call_site_does_not_reintroduce_removed_vars(self):
        """字典解包只能增改、不能删除。调用处若写 {**environment, **credential_environment()}，
        被剔除的变量会被左边那份原样带回来，剔除等于没做。"""
        import ast
        source = Path(bot.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict) or len(node.keys) != 2:
                continue
            for value in node.values:
                if isinstance(value, ast.Call) and getattr(value.func, 'id', '') == 'credential_environment':
                    offenders.append(node.lineno)
        self.assertFalse(offenders, '第 ' + '、'.join(map(str, offenders))
                         + ' 行又把剔除结果合并回去了')

    def test_function_accepts_base_and_returns_copy(self):
        base = {'PATH': '/usr/bin'}
        with patch.object(bot, 'renew_credential', return_value=None):
            environment = bot.credential_environment(base)
        self.assertEqual(base, {'PATH': '/usr/bin'}, 'credential_environment 改了调用方的字典')


class RenewPolicyTest(AuthBase):
    def save(self, expires_in, refresh='refresh-value', provider='cloudflare'):
        value = FAKE_CF if provider == 'cloudflare' else FAKE_GH
        self.store().save(provider, value, scope='站点', refresh=refresh, expires_in=expires_in)

    def test_far_from_expiry_sends_nothing(self):
        self.save(7200)
        self.assertFalse(bot.renew_check_once('cloudflare'))
        self.assertEqual(self.sent, [])

    def test_successful_renewal_is_silent(self):
        self.save(60)
        with patch.object(bot, 'renew_credential', return_value='new-token'):
            self.assertFalse(bot.renew_check_once('cloudflare'))
        self.assertEqual(self.sent, [], '续期成功却发了消息')

    def test_failed_renewal_alerts_once(self):
        self.save(60)
        with patch.object(bot, 'renew_credential', return_value=None):
            self.assertTrue(bot.renew_check_once('cloudflare'))
            self.assertFalse(bot.renew_check_once('cloudflare'), '同一小时内重复提醒')
        self.assertEqual(len(self.sent), 1)

    def test_missing_refresh_token_alerts(self):
        self.save(60, refresh=None)
        self.assertTrue(bot.renew_check_once('cloudflare'))
        self.assertIn('没有可用于自动续期', self.sent[0])

    def test_alert_repeats_after_the_gap(self):
        self.save(60)
        with patch.object(bot, 'renew_credential', return_value=None):
            bot.renew_check_once('cloudflare')
            bot.CREDENTIAL_RENEW_WARNED['cloudflare']['at'] -= bot.CREDENTIAL_RENEW_ALERT_GAP + 1
            self.assertTrue(bot.renew_check_once('cloudflare'))
        self.assertEqual(len(self.sent), 2)

    def test_expiry_unknown_does_not_trigger(self):
        self.store().save('cloudflare', FAKE_CF, scope='站点')
        self.assertFalse(bot.renew_check_once('cloudflare'))
        self.assertEqual(self.sent, [])


class LoginCodeDetectionTest(unittest.TestCase):
    """授权码识别：宁可漏判也不能把普通任务误吞。"""

    def test_short_text_is_not_a_code(self):
        self.assertFalse(bot.looks_like_login_code('你好'))

    def test_text_with_space_is_not_a_code(self):
        self.assertFalse(bot.looks_like_login_code('帮我 部署 一下'))

    def test_chinese_task_is_not_a_code(self):
        self.assertFalse(bot.looks_like_login_code('把服务器上的日志清理一下'))

    def test_typical_code_is_recognised(self):
        self.assertTrue(bot.looks_like_login_code('ac_' + 'x' * 60))

    def test_empty_is_not_a_code(self):
        self.assertFalse(bot.looks_like_login_code(''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
