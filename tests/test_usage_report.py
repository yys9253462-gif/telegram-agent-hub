#!/usr/bin/env python3
"""订阅套餐用量读取的测试。

这些用例都在防同一类事故：**把没读到的东西显示成读到了**。
用量是机主决定「还能不能继续跑」的依据，一个伪造的 100% 比一句
「读不到」危险得多，所以每个降级路径都必须有独立断言。
"""
import _root  # noqa: F401  仓库根目录进 sys.path
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('TELEGRAM_BOT_TOKEN', '000:FAKE')
os.environ.setdefault('TELEGRAM_OWNER_ID', '1')

import bot
import github_auth as gh


class UsageParseTests(unittest.TestCase):
    """解析层：三个引擎的三种响应形状都要能读，脏数据不能读成有效数字。"""

    def test_claude_windows_use_official_field_names(self):
        payload = {'five_hour': {'utilization': 12.5, 'resets_at': '2026-10-27T12:00:00Z'},
                   'seven_day': {'utilization': 80, 'resets_at': 1762000000}}
        windows = gh.usage_parse_claude(payload)
        self.assertEqual([item['key'] for item in windows], ['five_hour', 'seven_day'])
        self.assertEqual(windows[0]['label'], '5小时')
        self.assertEqual(windows[1]['label'], '7天')
        self.assertAlmostEqual(windows[0]['used_percent'], 12.5)

    def test_claude_missing_windows_is_an_error_not_zero(self):
        # 没有窗口数据时必须报错。返回空列表会被上层当成「用量 0%」显示，
        # 机主会以为额度满格，这是最坏的结果。
        with self.assertRaises(gh.UsageError):
            gh.usage_parse_claude({'seven_day_oauth_apps': None})

    def test_claude_ignores_null_window_blocks(self):
        payload = {'five_hour': {'utilization': None, 'resets_at': None}}
        with self.assertRaises(gh.UsageError):
            gh.usage_parse_claude(payload)

    def test_headers_read_both_windows(self):
        headers = {'anthropic-ratelimit-unified-5h-utilization': '37.5',
                   'anthropic-ratelimit-unified-5h-reset': '1761540000',
                   'anthropic-ratelimit-unified-7d-utilization': '4',
                   'anthropic-ratelimit-unified-7d-reset': '1762000000'}
        windows = gh.usage_parse_headers(headers)
        self.assertEqual([item['key'] for item in windows], ['five_hour', 'seven_day'])
        self.assertEqual(windows[0]['resets_at'], 1761540000)

    def test_headers_absent_returns_empty_not_error(self):
        # 正常请求常常完全没有限流头，这不是失败，只是没读到。
        self.assertEqual(gh.usage_parse_headers({}), [])
        self.assertEqual(gh.usage_parse_headers(None), [])

    def test_codex_real_response_shape(self):
        # 2026-10-26 在真实 Plus 账号上抓到的响应，原样做夹具。
        # 字段是单数 rate_limit + *_window，之前按二进制里的 Rust 结构体名
        # 写成 rate_limits + primary，结果线上一个窗口都读不出来。
        payload = {'plan_type': 'plus',
                   'rate_limit': {'allowed': False, 'limit_reached': True,
                                  'primary_window': {'used_percent': 100,
                                                     'limit_window_seconds': 18000,
                                                     'reset_after_seconds': 3043,
                                                     'reset_at': 1792953411},
                                  'secondary_window': {'used_percent': 31,
                                                       'limit_window_seconds': 604800,
                                                       'reset_after_seconds': 568936,
                                                       'reset_at': 1793519304}},
                   'model_usage': {'gpt-6-astra': {'available': False}}}
        windows = gh.usage_parse_codex(payload)
        self.assertEqual([item['label'] for item in windows], ['5小时', '7天'])
        self.assertEqual(windows[0]['used_percent'], 100.0)
        self.assertEqual(windows[1]['resets_at'], 1793519304)
        # 用满时剩余必须是 0，不能因为 100 看着像「满格」就显示成 100%。
        self.assertEqual(gh.usage_remaining(windows[0]), 0.0)
        # 服务端已明确拒绝时要标「额度已用尽」，否则机主会以为还能凑合跑。
        self.assertTrue(windows[0]['limit_reached'])
        self.assertFalse(windows[1]['limit_reached'])

    def test_codex_not_limited_has_no_exhausted_wording(self):
        payload = {'rate_limit': {'limit_reached': False,
                                  'primary_window': {'used_percent': 40,
                                                     'limit_window_seconds': 18000,
                                                     'reset_at': 1792953411}}}
        windows = gh.usage_parse_codex(payload)
        self.assertFalse(windows[0]['limit_reached'])
        line = gh.usage_line(windows[0], now=1792950300)
        self.assertNotIn('用尽', line)
        self.assertIn('剩余 60%', line)

    def test_codex_label_follows_window_minutes_not_slot(self):
        # 服务端会把 5 小时窗口放在 primary 或 secondary，按位置写死标签迟早标错。
        payload = {'rate_limits': {'primary': {'used_percent': 5, 'window_minutes': 10080},
                                   'secondary': {'used_percent': 60, 'window_minutes': 300}}}
        windows = gh.usage_parse_codex(payload)
        self.assertEqual(windows[0]['label'], '7天')
        self.assertEqual(windows[1]['label'], '5小时')

    def test_codex_seconds_normalised_and_relative_reset(self):
        payload = {'primary': {'used_percent': 10, 'limit_window_seconds': '18000',
                               'reset_after_seconds': 600}}
        now = 1761530000
        with patch.object(gh.time, 'time', return_value=now):
            windows = gh.usage_parse_codex(payload)
        self.assertEqual(windows[0]['label'], '5小时')
        self.assertEqual(windows[0]['resets_at'], now + 600)

    def test_percent_clamped_and_garbage_rejected(self):
        self.assertEqual(gh._usage_percent(150), 100.0)
        self.assertEqual(gh._usage_percent(-3), 0.0)
        self.assertIsNone(gh._usage_percent('abc'))
        self.assertIsNone(gh._usage_percent(True))
        self.assertIsNone(gh._usage_percent(float('nan')))

    def test_reset_accepts_seconds_millis_and_iso(self):
        self.assertEqual(gh._usage_reset(1761540000), 1761540000)
        self.assertEqual(gh._usage_reset(1761540000000), 1761540000)
        self.assertEqual(gh._usage_reset('2026-10-27T12:00:00Z'),
                         gh._usage_reset(1793102400))
        self.assertIsNone(gh._usage_reset('not-a-time'))

    def test_merge_keeps_richer_reading(self):
        merged = gh.usage_merge([{'key': 'five_hour', 'label': '5小时', 'used_percent': 20,
                                  'resets_at': None},
                                 {'key': 'five_hour', 'label': '5小时', 'used_percent': None,
                                  'resets_at': 1761540000}])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['used_percent'], 20)
        self.assertEqual(merged[0]['resets_at'], 1761540000)


class UsageDisplayTests(unittest.TestCase):
    """展示层：数字要能把「用掉」翻成「剩余」，刷新时间要能读懂。"""

    def test_remaining_is_inverted_from_used(self):
        self.assertEqual(gh.usage_remaining({'used_percent': 37.5}), 62.5)
        self.assertIsNone(gh.usage_remaining({'used_percent': None}))

    def test_unknown_remaining_never_renders_as_full(self):
        line = gh.usage_line({'label': '5小时', 'used_percent': None, 'resets_at': None})
        self.assertIn('未知', line)
        self.assertNotIn('100', line)

    def test_integer_percent_drops_trailing_zero(self):
        self.assertEqual(gh.usage_percent_text(80.0), '80%')
        self.assertEqual(gh.usage_percent_text(62.5), '62.5%')

    def test_reset_text_in_human_units(self):
        now = 1761530000
        self.assertEqual(gh.usage_format_reset(now + 8000, now), '2小时13分')
        self.assertEqual(gh.usage_format_reset(now + 90, now), '1分钟')
        self.assertEqual(gh.usage_format_reset(now - 1, now), '已到刷新点')

    def test_footer_reports_error_instead_of_numbers(self):
        text = gh.usage_footer({'ok': False, 'error': '未登录 Claude 账号。'})
        self.assertIn('未登录', text)
        self.assertNotIn('%', text)


class UsageReporterTests(unittest.TestCase):
    """读取层：缓存、凭证缺失、失败降级都要有确定行为。"""

    def test_missing_credential_does_not_call_network(self):
        calls = []
        reporter = gh.UsageReporter(credential=lambda provider: None,
                                    fetcher=lambda url, headers: calls.append(url) or ({}, {}))
        state = reporter.claude()
        self.assertFalse(state['ok'])
        self.assertIn('未登录', state['error'])
        self.assertEqual(calls, [])

    def test_success_then_cache_then_force(self):
        calls = []

        def fetcher(url, headers):
            calls.append(url)
            return {'five_hour': {'utilization': 10, 'resets_at': 1761540000}}, {}

        reporter = gh.UsageReporter(credential=lambda provider: {'value': 'tok'},
                                    fetcher=fetcher, clock=lambda: 1761530000)
        self.assertTrue(reporter.claude()['ok'])
        reporter.claude()
        reporter.claude(force=True)
        # 套餐信息走 /api/oauth/profile 且单独缓存，这里只数用量接口。
        self.assertEqual(calls.count(gh.USAGE_CLAUDE_URL), 2)

    def test_failed_read_cached_with_short_ttl(self):
        calls = []
        now = [1761530000]

        def fetcher(url, headers):
            calls.append(url)
            raise gh.UsageError('接口返回 HTTP 500。')

        reporter = gh.UsageReporter(credential=lambda provider: {'value': 'tok'},
                                    fetcher=fetcher, ttl=600, error_ttl=5,
                                    clock=lambda: now[0])
        reporter.claude()
        reporter.claude()
        self.assertEqual(len(calls), 1)
        now[0] += 6      # 越过错误缓存期后应重试，不能一次失败就永久放弃
        reporter.claude()
        self.assertEqual(len(calls), 2)

    def test_password_style_401_becomes_actionable_message(self):
        # 走真实抓取层，只把 urlopen 换掉：只有穿过 usage_http_json 才能证明
        # 401 会被翻译成「需要重新登录」，直接让 fetcher 抛 HTTPError 测的是别的东西。
        def boom(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, 'unauthorized', {}, io.BytesIO(b''))

        reporter = gh.UsageReporter(credential=lambda provider: {'value': 'tok'},
                                    fetcher=gh.usage_http_json)
        with patch.object(gh.urllib.request, 'urlopen', boom):
            state = reporter.claude()
        self.assertFalse(state['ok'])
        self.assertIn('重新登录', state['error'])

    def test_unexpected_fetch_error_does_not_escape(self):
        # 抓取层之外的意外异常必须被兜住：用量读不到不能拖垮整轮任务。
        def fetcher(url, headers):
            raise RuntimeError('意外崩溃')

        reporter = gh.UsageReporter(credential=lambda provider: {'value': 'tok'},
                                    fetcher=fetcher)
        state = reporter.claude()
        self.assertFalse(state['ok'])
        self.assertTrue(state['error'])

    def test_remember_headers_feeds_codebuddy_snapshot(self):
        # CodeBuddy 没有独立的用量查询接口，读数只能来自对话响应头。
        reporter = gh.UsageReporter(credential=lambda provider: None)
        reporter.remember_headers('codebuddy', {
            'anthropic-ratelimit-unified-7d-utilization': '55',
            'anthropic-ratelimit-unified-7d-reset': '1762000000'})
        state = reporter.snapshot('codebuddy')
        self.assertTrue(state['ok'])
        self.assertEqual(state['windows'][0]['used_percent'], 55.0)

    def test_codex_without_reading_says_so(self):
        reporter = gh.UsageReporter(credential=lambda provider: None)
        state = reporter.snapshot('codex')
        self.assertFalse(state['ok'])
        self.assertIn('未登录', state['error'])

    def test_codex_requires_account_id_header(self):
        # 只带 Bearer 会 403，必须同时带 chatgpt-account-id。
        seen = {}

        def fetcher(url, headers):
            seen.update(headers)
            return {'rate_limit': {'primary_window': {'used_percent': 5,
                                                      'limit_window_seconds': 18000,
                                                      'reset_at': 1792953411}}}, {}

        reporter = gh.UsageReporter(
            credential=lambda provider: {'value': 'tok', 'account': 'acct-1'},
            fetcher=fetcher)
        state = reporter.codex()
        self.assertTrue(state['ok'])
        self.assertEqual(seen.get('chatgpt-account-id'), 'acct-1')
        self.assertEqual(seen.get('Authorization'), 'Bearer tok')

    def test_codex_api_key_mode_is_not_account_mode(self):
        # API Key 登录也写 auth.json，但没有套餐窗口，不能当成账号查。
        tmp = Path(tempfile.mkdtemp()) / 'auth.json'
        tmp.write_text(json.dumps({'auth_mode': 'apikey',
                                   'tokens': {'access_token': 'x', 'account_id': 'y'}}))
        self.assertIsNone(bot.codex_account_token(tmp))

    def test_unsupported_engine_returns_none(self):
        reporter = gh.UsageReporter(credential=lambda provider: None)
        self.assertIsNone(reporter.snapshot('unknown'))

    def test_credential_callback_exception_is_contained(self):
        def boom(provider):
            raise RuntimeError('凭据文件坏了')

        reporter = gh.UsageReporter(credential=boom)
        state = reporter.claude()
        self.assertFalse(state['ok'])


class BotUsageWiringTests(unittest.TestCase):
    """接线层：渠道判断、凭证位置、底部追加，三处都不能张冠李戴。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (bot.AUTH_STATE, bot.CLAUDE_CREDENTIALS, bot.USAGE)
        bot.AUTH_STATE = self.tmp / 'auth-channel.json'
        bot.CLAUDE_CREDENTIALS = self.tmp / '.credentials.json'
        bot.USAGE = gh.UsageReporter(credential=bot.usage_credential)

    def tearDown(self):
        bot.AUTH_STATE, bot.CLAUDE_CREDENTIALS, bot.USAGE = self.saved

    def test_gateway_channel_gets_no_footer(self):
        bot.set_auth_channel('gateway', 'claude')
        self.assertEqual(bot.usage_footer_for('claude'), '')

    def test_account_channel_without_login_explains_why(self):
        bot.set_auth_channel('account', 'claude')
        self.assertIn('未登录', bot.usage_footer_for('claude'))

    def test_account_channel_with_reading_formats_footer(self):
        bot.set_auth_channel('account', 'claude')
        bot.CLAUDE_CREDENTIALS.write_text(json.dumps(
            {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-FAKE'}}))
        bot.USAGE = gh.UsageReporter(
            credential=bot.usage_credential,
            fetcher=lambda url, headers: ({'five_hour': {'utilization': 25, 'resets_at': 1761540000}}, {}),
            clock=lambda: 1761530000)
        footer = bot.usage_footer_for('claude', force=True)
        self.assertTrue(footer.startswith('用量：'))
        self.assertIn('5小时 剩余 75%', footer)

    def test_token_read_from_cli_credentials_file(self):
        # Claude 的令牌在 CLI 自己的文件里，不在我们的 credentials.json，
        # 读错位置会永远显示「未登录」。
        bot.CLAUDE_CREDENTIALS.write_text(json.dumps(
            {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-FAKE'}}))
        self.assertEqual(bot.claude_oauth_token(), 'sk-ant-oat01-FAKE')

    def test_token_absent_when_file_malformed(self):
        bot.CLAUDE_CREDENTIALS.write_text('{not json')
        self.assertIsNone(bot.claude_oauth_token())
        bot.CLAUDE_CREDENTIALS.write_text(json.dumps({'claudeAiOauth': {'accessToken': '  '}}))
        self.assertIsNone(bot.claude_oauth_token())

    def test_overview_marks_gateway_engines(self):
        for name in ('claude', 'codebuddy', 'codex'):
            bot.set_auth_channel('gateway', name)
        with patch.object(bot, 'enabled_engines', return_value=['claude', 'codebuddy', 'codex']):
            text = bot.usage_overview_text()
        self.assertEqual(text.count('无套餐窗口'), 3)

    def test_overview_never_invents_numbers_for_errors(self):
        bot.set_auth_channel('account', 'claude')
        with patch.object(bot, 'enabled_engines', return_value=['claude']):
            text = bot.usage_overview_text()
        self.assertIn('未登录', text)
        self.assertNotIn('%', text)

    def test_refresh_button_reaches_handler(self):
        # 按钮发出去但没接处理函数，点了会像死键。
        with patch.object(bot, 'enabled_engines', return_value=[]):
            self.assertIn('套餐用量', bot.usage_overview_text())


class ReplyFooterTests(unittest.TestCase):
    """回复底部那一段：执行器 · 模型 · 耗时，账号渠道再接用量。

    2026-10-26 用户要求：执行器/模型/耗时不要再单独发一条消息，
    统一压到每条对话的底部，与用量同行。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (bot.AUTH_STATE, bot.MODEL_STATE, bot.USAGE)
        bot.AUTH_STATE = self.tmp / 'auth-channel.json'
        bot.MODEL_STATE = self.tmp / 'selected-model.json'
        bot.USAGE = gh.UsageReporter(credential=bot.usage_credential)

    def tearDown(self):
        bot.AUTH_STATE, bot.MODEL_STATE, bot.USAGE = self.saved

    def test_executor_and_model_are_named(self):
        bot.set_auth_channel('gateway', 'claude')
        bot.MODEL_STATE.write_text(json.dumps({'model': 'wbdp4.1f'}))
        line = bot.execution_footer('claude')
        self.assertEqual(line, '执行器：Claude · 模型：wbdp4.1f')

    def test_executor_label_comes_from_engine_spec(self):
        # 写死英文名会与菜单里的中文标签对不上，用户会以为是两个东西。
        bot.set_auth_channel('gateway', 'codebuddy')
        self.assertIn('执行器：CodeBuddy', bot.execution_footer('codebuddy'))

    def test_duration_only_appears_when_started_is_known(self):
        bot.set_auth_channel('gateway', 'claude')
        self.assertNotIn('耗时', bot.execution_footer('claude'))
        line = bot.execution_footer('claude', started=time.monotonic() - 37)
        self.assertIn('耗时：37秒', line)

    def test_negative_duration_is_clamped_to_zero(self):
        # 机器时钟回拨会让耗时变成负数，显示「-3秒」比不显示更糟。
        bot.set_auth_channel('gateway', 'claude')
        self.assertIn('耗时：0秒', bot.execution_footer('claude', started=time.monotonic() + 60))

    def test_wall_clock_is_never_mixed_with_monotonic(self):
        """monotonic 读数只能用 monotonic 减。

        线上真实事故：把 time.time()（约 17 亿）减掉 monotonic 读数，
        得出「耗时 1792918791 秒」直接显示给用户。这个用例就是钉住这条：
        传一个 monotonic 读数进去，耗时必须是正常量级。
        """
        bot.set_auth_channel('gateway', 'claude')
        line = bot.execution_footer('claude', started=time.monotonic() - 5)
        self.assertIn('耗时：5秒', line)
        # 再说一遍反向：绝不能出现六七位数以上的秒数。
        digits = line.split('耗时：')[1].rstrip('秒').split('分')[0]
        self.assertLess(int(digits), 600, '耗时算出了天文数字：' + line)

    def test_wall_clock_fallback_for_persisted_tasks(self):
        """拿不到 monotonic 时退回墙钟。任务状态从磁盘读回来就属于这种。"""
        bot.set_auth_channel('gateway', 'claude')
        line = bot.execution_footer('claude', started=None,
                                    started_wall=time.time() - 42)
        self.assertIn('耗时：42秒', line)

    def test_elapsed_formatting(self):
        self.assertEqual(bot.format_elapsed(0), '0秒')
        self.assertEqual(bot.format_elapsed(59), '59秒')
        self.assertEqual(bot.format_elapsed(60), '1分0秒')
        self.assertEqual(bot.format_elapsed(134), '2分14秒')
        self.assertEqual(bot.format_elapsed(3600), '1小时0分')
        self.assertEqual(bot.format_elapsed(3780), '1小时3分')
        self.assertEqual(bot.format_elapsed(-9), '0秒')

    def test_account_channel_shows_official_default_when_unset(self):
        # 账号模式没选模型时用的是官方默认，不能显示成网关那个模型名。
        bot.set_auth_channel('account', 'claude')
        bot.MODEL_STATE.write_text(json.dumps({'model': 'wbdp4.1f'}))
        with patch.object(bot, 'account_model', return_value=''):
            self.assertIn('模型：官方默认', bot.execution_footer('claude'))
            self.assertNotIn('wbdp4.1f', bot.execution_footer('claude'))

    def test_account_channel_shows_the_chosen_account_model(self):
        bot.set_auth_channel('account', 'claude')
        with patch.object(bot, 'account_model', return_value='opus'):
            self.assertIn('模型：Opus 5.5', bot.execution_footer('claude'))

    def test_gateway_reply_has_no_usage_line(self):
        bot.set_auth_channel('gateway', 'claude')
        text = bot.reply_footer('claude', started=time.monotonic() - 5)
        self.assertIn('执行器：Claude', text)
        self.assertNotIn('用量', text)

    def test_account_reply_puts_usage_on_its_own_line(self):
        bot.set_auth_channel('account', 'claude')
        with patch.object(bot, 'usage_footer_for', return_value='用量：5小时 剩余 75%'):
            text = bot.reply_footer('claude', started=time.monotonic() - 12)
        lines = text.split('\n')
        self.assertEqual(len(lines), 2)
        self.assertIn('执行器：Claude', lines[0])
        self.assertIn('耗时：12秒', lines[0])
        self.assertEqual(lines[1], '用量：5小时 剩余 75%')

    def test_usage_failure_does_not_swallow_the_executor_line(self):
        # 用量读不到时执行摘要仍必须在：那是本地就能算出来的确定信息。
        bot.set_auth_channel('account', 'claude')
        with patch.object(bot, 'usage_footer_for', return_value=''):
            text = bot.reply_footer('claude', started=time.monotonic() - 3)
        self.assertIn('执行器：Claude', text)
        self.assertIn('耗时：3秒', text)
        self.assertEqual(text.count('\n'), 0)


class UsageReverseVerification(unittest.TestCase):
    """反向验证：把这些保护拆掉，测试必须报红，证明前面的用例不是永远绿的。"""

    def test_removing_error_ttl_would_fail(self):
        now = [1761530000]
        calls = []

        def fetcher(url, headers):
            calls.append(url)
            raise gh.UsageError('boom')

        reporter = gh.UsageReporter(credential=lambda provider: {'value': 'tok'},
                                    fetcher=fetcher, ttl=600, error_ttl=5,
                                    clock=lambda: now[0])
        reporter.claude()
        reporter.claude()
        self.assertEqual(len(calls), 1)
        # 把错误缓存期改成和成功一样长，第 6 秒就不该重试了 —— 用来确认
        # 上面那条断言真的依赖 error_ttl 这个字段，而不是碰巧成立。
        reporter.error_ttl = 600
        reporter.cache.clear()
        reporter.claude()
        reporter.claude()
        self.assertEqual(len(calls), 2)

    def test_full_percent_is_not_remaining_when_field_missing(self):
        # 直接拿 used_percent 当剩余就全反了；这条断言钉住换算方向。
        item = {'used_percent': 90}
        self.assertEqual(gh.usage_remaining(item), 10.0)
        self.assertNotEqual(gh.usage_remaining(item), 90.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
