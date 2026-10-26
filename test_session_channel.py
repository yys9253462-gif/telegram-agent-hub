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
