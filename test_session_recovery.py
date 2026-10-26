"""会话失效自愈：CLI 说「找不到这个会话」时，应当清掉本地记录并告知用户重发。

线上现场（2026-10-26 生产实测）：
  codebuddy 在会话 ID 已失效时只吐一行
  {"type":"error","error":"No conversation found with session ID: ..."}
  然后**以退出码 0 结束**，正文为空。
  原来的代码不认识 error 事件，于是退出码 0 + 空正文 →
  用户只看到「任务未返回内容，请检查模型接口」，真因一个字都没有；
  更糟的是本地会话 ID 还在，重试必然再撞同一堵墙。

这里钉住三件事：
  1. error 事件必须被记成失败（否则判定不出错）；
  2. 只有「会话找不到」才允许清会话——凭据失效、额度耗尽不许动；
  3. 清会话必须真删键，而不是写个 None 占位。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


def fresh_state():
    return {'content': '', 'final': None, 'activity': '等待模型响应',
            'failed': '', 'thinking': ''}


class SessionErrorClassificationTest(unittest.TestCase):
    """分类判定：能自愈的会话失效 vs 清会话也没用的其他失败。"""

    def test_real_production_message_is_recoverable(self):
        # 生产原文，逐字抄的，不是照着印象写的。
        message = 'No conversation found with session ID: 01a17280-f9eb-7012-b79c-c521f53027c0'
        self.assertTrue(bot.session_error_is_recoverable(message))

    def test_variants_are_recoverable(self):
        for message in ('session not found',
                        'No conversation found',
                        'invalid session id: abc',
                        'unknown session 1234'):
            self.assertTrue(bot.session_error_is_recoverable(message),
                            '这类应当判为可自愈：' + message)

    def test_credential_failure_is_not_recoverable(self):
        # 清会话解决不了凭据问题，还会顺手抹掉本来好用的上下文。
        for message in ('invalid api key',
                        '401 Unauthorized',
                        'OAuth token has expired. Please log in again.',
                        'session token expired, please re-authenticate'):
            self.assertFalse(bot.session_error_is_recoverable(message),
                             '不该判为可自愈：' + message)

    def test_quota_failure_is_not_recoverable(self):
        # 「session limit reached」里带 session 一词，但那是额度不是会话记录。
        for message in ('session limit reached',
                        'usage limit exceeded for this session',
                        'rate limit exceeded'):
            self.assertFalse(bot.session_error_is_recoverable(message),
                             '额度类不该判为可自愈：' + message)

    def test_empty_message_is_not_recoverable(self):
        for message in ('', None, '   '):
            self.assertFalse(bot.session_error_is_recoverable(message))


class ErrorEventParsingTest(unittest.TestCase):
    """error 事件必须落进 state，且区分出会话失效。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # 🔴 save_session 会写真实 ~/tasks，不 patch 就会在开发机上建文件。
        self.patch = patch.object(bot, 'SESSION_STATE', self.root / 'agent-session.json')
        self.patch.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.patch.stop)

    def test_error_event_marks_failed(self):
        state = fresh_state()
        bot.parse_claude_event({'type': 'error',
                                'error': 'No conversation found with session ID: x'}, state)
        self.assertIn('No conversation found', state['failed'])
        self.assertTrue(state.get('session_invalid'))

    def test_error_event_with_credential_problem_does_not_flag_session(self):
        state = fresh_state()
        bot.parse_claude_event({'type': 'error', 'error': 'invalid api key'}, state)
        self.assertIn('invalid api key', state['failed'])
        self.assertFalse(state.get('session_invalid'),
                         '凭据错误被误判成会话失效，会白白清掉上下文')

    def test_plain_result_event_does_not_set_session_invalid(self):
        state = fresh_state()
        bot.parse_claude_event({'type': 'result', 'result': 'ok', 'session_id': 'abc'}, state)
        self.assertFalse(state.get('session_invalid'))


class ClearSessionTest(unittest.TestCase):
    """真删键，而不是写 None 占位。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = bot.TaskController(self.root)
        self.patch = patch.object(bot, 'CONTROL', self.controller)
        self.patch.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.patch.stop)

    def test_clear_removes_both_keys(self):
        bot.save_session('sess-abc')
        self.assertEqual(bot.session_id(), 'sess-abc')
        bot.clear_session()
        self.assertIsNone(bot.session_id())

    def test_clear_does_not_leave_none_placeholder(self):
        bot.save_session('sess-abc')
        bot.clear_session()
        session = self.controller.active_session()
        engine = bot.current_engine()
        self.assertNotIn(engine, session.get('engine_sessions', {}),
                         'engine_sessions 里留了空值占位，应当真删')
        self.assertNotIn('session_id', session,
                         'claude 的 session_id 应当真删，不是保留一条空值')

    def test_saving_other_engine_keeps_sessions_field_in_sync(self):
        """claude 被清空之后，给别的执行器存会话不能把 session_id 写成那个值。

        这个字段早于 engine_sessions 存在，界面和旧数据还在读它，语义是
        「claude 的会话」。原来的写法在引擎不是 claude 时什么都不做，
        所以这里的错法有两种：
          · 写成当前引擎的值 → claude 下次会拿 codebuddy 的 ID 去续接，
            撞「No conversation found」；
          · 留旧值不清 → 明明已经清掉了，还显示有会话。
        正确行为：它必须始终等于 engine_sessions['claude']（没有就是空）。
        """
        session = self.controller.data['sessions'][self.controller.data['active_session']]
        # 先模拟「claude 的会话已经被清掉」的现场
        session['engine_sessions'] = {'claude': None}
        session['session_id'] = 'stale-claude-sid'
        with patch.object(bot, 'current_engine', return_value='codebuddy'):
            bot.save_session('cb-sid')
        session = self.controller.active_session()
        self.assertIsNone(session.get('session_id'),
                          'claude 会话已清空，session_id 却还留着旧值或新引擎的值：'
                          + repr(session.get('session_id')))
        self.assertEqual(session['engine_sessions']['codebuddy'], 'cb-sid')
        self.assertEqual(session['engine_sessions']['claude'], None)

    def test_clearing_codebuddy_keeps_claude_session(self):
        """codebuddy 会话失效时清会话，不能把 claude 的也一起清掉。"""
        session = self.controller.data['sessions'][self.controller.data['active_session']]
        session['engine_sessions'] = {'claude': 'claude-sid', 'codebuddy': 'cb-sid'}
        session['session_id'] = 'claude-sid'
        with patch.object(bot, 'current_engine', return_value='codebuddy'):
            bot.clear_session()
        session = self.controller.active_session()
        self.assertNotIn('codebuddy', session['engine_sessions'])
        self.assertEqual(session['engine_sessions']['claude'], 'claude-sid')
        self.assertEqual(session['session_id'], 'claude-sid')
        # 各执行器的会话互不相干：清掉 codex 的，不能顺手清掉 claude 的。
        session = self.controller.data['sessions'][self.controller.data['active_session']]
        session.setdefault('engine_sessions', {}).update({'claude': 'keep-me', 'codex': 'drop-me'})
        session['session_id'] = 'keep-me'
        with patch.object(bot, 'current_engine', return_value='codex'):
            bot.clear_session()
        session = self.controller.active_session()
        self.assertNotIn('codex', session.get('engine_sessions', {}))
        self.assertEqual(session['engine_sessions'].get('claude'), 'keep-me')
        self.assertEqual(session.get('session_id'), 'keep-me')

    def test_clear_persists_to_disk(self):
        bot.save_session('sess-abc')
        bot.clear_session()
        reloaded = bot.TaskController(self.root)
        self.assertNotIn('/sess-abc', (self.root / 'tasks.json').read_text(encoding='utf-8')
                         if (self.root / 'tasks.json').exists() else '')
        self.assertIsNone(reloaded.active_session().get('session_id'))

    def test_clear_without_controller_nulls_session_state(self):
        with patch.object(bot, 'CONTROL', None), \
                patch.object(bot, 'SESSION_STATE', self.root / 'agent-session.json'):
            bot.save_session('sess-abc')
            bot.clear_session()
            self.assertIsNone(bot.session_id())


class RecoveryReplyTest(unittest.TestCase):
    """收尾回复里必须说清「已重置、请重发」，否则用户不知道下一步做什么。"""

    def test_run_stream_clears_and_tells_user(self):
        import inspect
        body = inspect.getsource(bot.run_stream)
        # 位置关系很重要：必须先清会话再拼回复，否则用户重试时仍带旧 ID。
        self.assertIn("state.get('session_invalid')", body)
        self.assertIn('clear_session()', body)
        self.assertIn('会话记录已失效', body)
        self.assertIn('刚才的要求再发一次', body)
        self.assertLess(body.index('clear_session()'), body.index('刚才的要求再发一次'),
                        '应当先清会话再告知用户')
        # 清理失败也得说清楚，不能让用户以为已经清好了。
        self.assertIn('/new', body)


if __name__ == '__main__':
    unittest.main()
