#!/usr/bin/env python3
"""会话绑定渠道、thinking 历史不兼容自愈、套餐到期推算。"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '1:test')
os.environ.setdefault('TELEGRAM_OWNER_ID', '1')
import github_auth as gh
import bot


class SessionChannelTest(unittest.TestCase):
    def test_legacy_session_is_gateway_only(self):
        session = {'engine_sessions': {'claude': 'abc'}}
        self.assertEqual(bot.stored_session(session, 'claude', 'gateway'), 'abc')
        self.assertIsNone(bot.stored_session(session, 'claude', 'account'))

    def test_recorded_channel_must_match(self):
        session = {'engine_sessions': {'claude': 'abc'}, 'engine_channels': {'claude': 'account'}}
        self.assertEqual(bot.stored_session(session, 'claude', 'account'), 'abc')
        self.assertIsNone(bot.stored_session(session, 'claude', 'gateway'))

    def test_thinking_error_detected(self):
        msg = 'API Error: 400 messages.2.content.0.thinking: each thinking block must contain non-whitespace thinking'
        self.assertTrue(bot.session_history_incompatible(msg))
        self.assertFalse(bot.session_history_incompatible('API Error: 429 rate limit'))


class InteractionTest(unittest.TestCase):
    def test_failure_hints_are_specific(self):
        self.assertIn('额度', bot.failure_hint('API Error: 429 rate limit exceeded'))
        self.assertIn('重新登录', bot.failure_hint('API Error: 401 authentication_error'))
        self.assertIn('换新会话', bot.failure_hint('each thinking block must contain non-whitespace thinking'))

    def test_answer_callback_swallows_expired_query(self):
        original = bot.api
        def boom(method, payload):
            raise RuntimeError('query is too old')
        bot.api = boom
        try:
            bot.answer_callback({'id': 'x'})
        finally:
            bot.api = original

    def test_api_retries_429(self):
        import io, urllib.error
        calls = []
        class Ok:
            def __enter__(self): return io.BytesIO(b'{"ok": true, "result": 1}')
            def __exit__(self, *a): return False
        def fake(request, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError('u', 429, 'Too Many', {},
                                             io.BytesIO(b'{"parameters": {"retry_after": 0}}'))
            return Ok()
        original, base = bot.urllib.request.urlopen, bot.BASE
        bot.urllib.request.urlopen, bot.BASE = fake, 'http://127.0.0.1/bot/'
        try:
            self.assertEqual(bot.api('sendMessage', {}), 1)
        finally:
            bot.urllib.request.urlopen, bot.BASE = original, base
        self.assertEqual(len(calls), 2)


class PlanTest(unittest.TestCase):
    def test_pro_renewal(self):
        now = datetime.datetime(2026, 10, 26, tzinfo=datetime.timezone.utc).timestamp()
        plan = gh.claude_plan_from_profile({
            'account': {'has_claude_pro': True, 'has_claude_max': False},
            'organization': {'organization_type': 'claude_pro', 'billing_type': 'stripe_subscription',
                             'subscription_status': 'active',
                             'subscription_created_at': '2026-09-22T19:02:58Z'}}, now)
        self.assertEqual(plan['plan'], 'Pro')
        renew = datetime.datetime.fromtimestamp(plan['renews_at'], datetime.timezone.utc)
        self.assertEqual((renew.month, renew.day), (11, 22))
        self.assertIn('Claude Pro', gh.usage_plan_text(plan))

    def test_month_end_clamps(self):
        created = datetime.datetime(2026, 1, 31, tzinfo=datetime.timezone.utc).timestamp()
        now = datetime.datetime(2026, 2, 5, tzinfo=datetime.timezone.utc).timestamp()
        renew = datetime.datetime.fromtimestamp(gh.next_monthly_renewal(created, now), datetime.timezone.utc)
        self.assertEqual((renew.month, renew.day), (2, 28))

    def test_unused_window_text(self):
        line = gh.usage_line({'label': '5小时', 'used_percent': 0.0, 'resets_at': None})
        self.assertIn('未开始计时', line)


if __name__ == '__main__':
    unittest.main()
