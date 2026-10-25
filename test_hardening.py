#!/usr/bin/env python3
"""五轮系统排查中修复行为的回归测试。

每个用例都对应一处真实缺陷，注释写明缺陷现象，便于以后判断能否删除。
运行：python test_hardening.py
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import bot


class SplitMessageTest(unittest.TestCase):
    """缺陷：send_formatted 按 1800 字符硬切，把 ** 和 ``` 劈成两半，
    用户看到裸露的星号，代码块渲染错乱。"""

    def test_bold_marker_not_split(self):
        text = 'a' * 1795 + '**重要结论**' + 'b' * 100
        old_head = text[:1800]
        self.assertEqual(old_head.count('**') % 2, 1, '旧实现应当劈开标记，否则本用例失去意义')
        pieces = bot.split_message(text)
        for index, piece in enumerate(pieces):
            self.assertEqual(piece.count('**') % 2, 0, '第%d片仍有未闭合的加粗标记' % (index + 1))

    def test_bold_renders_completely(self):
        text = 'a' * 1795 + '**重要结论**' + 'b' * 100
        joined = ''.join(bot.render_reply(piece) for piece in bot.split_message(text))
        self.assertIn('<b>重要结论</b>', joined)
        self.assertNotIn('**', joined)

    def test_code_fence_balanced(self):
        text = 'x' * 1700 + '\n```python\nprint(1)\n```\n' + 'y' * 300
        for piece in bot.split_message(text):
            fences = sum(1 for line in piece.splitlines() if line.lstrip().startswith('```'))
            self.assertEqual(fences % 2, 0, '代码围栏未成对')

    def test_no_content_lost(self):
        text = 'z' * 5000
        joined = ''.join(bot.split_message(text)).replace('\n', '')
        self.assertEqual(len(joined), len(text))


class RedactTest(unittest.TestCase):
    """缺陷：URL 内嵌密码、AWS/GitHub 令牌完全没脱敏；
    且 authorization 规则先于 Bearer 执行，只抹掉 "Bearer" 而令牌本体留在原地。"""

    def test_bearer_token_fully_masked(self):
        output = bot.redact('Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def')
        self.assertNotIn('eyJhbGciOiJIUzI1NiJ9', output)

    def test_url_embedded_password(self):
        output = bot.redact('https://user:RealPassword123@example.com/path')
        self.assertNotIn('RealPassword123', output)

    def test_database_url_with_at_in_password(self):
        output = bot.redact('postgres://admin:P@ssw0rd@db:5432/x')
        self.assertNotIn('P@ssw0rd', output)

    def test_platform_tokens(self):
        for secret in ('AKIAIOSFODNN7EXAMPLE', 'ghp_abcdefghijklmnopqrstuvwxyz01'):
            self.assertNotIn(secret, bot.redact('值：' + secret))

    def test_normal_text_untouched(self):
        safe = ['访问 https://example.com/docs', 'md5 = 3b1181039773b329737c5f438d9ed76f',
                'user@example.com 已登录', '端口 127.0.0.1:18443 已释放', '内存占用 62.3%']
        for text in safe:
            self.assertEqual(bot.redact(text), text)


class CredentialBlockTest(unittest.TestCase):
    """缺陷：文件外发只按文件名拦截，改个名字就能把含密码的连接串发出去。"""

    PATTERNS = (
        r'-----BEGIN [^-]*PRIVATE KEY-----',
        r'(?i:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|token|credential)\s*["\x27]?\s*[:=]\s*[^\s]',
        r'(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@',
        r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|(?:AKIA|ASIA)[0-9A-Z]{16})\b',
    )

    def blocked(self, content):
        import re
        return any(re.search(pattern, content) for pattern in self.PATTERNS)

    def test_credentials_blocked(self):
        for content in ('DB_PASSWORD=abc123', 'url: postgres://u:P@ss@h/db',
                        'token: eyJhbGciOiJIUzI1NiJ9', '-----BEGIN RSA PRIVATE KEY-----\nx'):
            self.assertTrue(self.blocked(content), content)

    def test_plain_content_allowed(self):
        for content in ('service started on port 8080', '# 安装说明\n运行 bash get.sh',
                        'def hello():\n    return "world"', '本工具使用 token 做鉴权，格式见文档。'):
            self.assertFalse(self.blocked(content), content)


class StateNormalizeTest(unittest.TestCase):
    """缺陷：状态文件缺 session_id 时 snapshot() 抛 KeyError，
    而它每次入队都会调用，等于机器人彻底无法接任务。"""

    def controller(self, payload):
        folder = Path(tempfile.mkdtemp())
        (folder / 'bridge-state.json').write_text(json.dumps(payload), encoding='utf-8')
        return bot.TaskController(folder)

    def test_missing_session_id_does_not_crash(self):
        control = self.controller({'tasks': [], 'sessions': {'k': {'name': 'n'}}, 'active_session': 'k'})
        snapshot = control.snapshot()
        self.assertEqual(snapshot['session_key'], 'k')
        self.assertIsNone(snapshot['session_id'])

    def test_dangling_active_session_repaired(self):
        control = self.controller({'tasks': [], 'sessions': {'a': {'name': 'x'}}, 'active_session': '不存在'})
        self.assertIn(control.data['active_session'], control.data['sessions'])

    def test_running_task_marked_interrupted(self):
        control = self.controller({'tasks': [{'id': 't', 'status': 'running', 'snapshot': {}}],
                                   'sessions': {'k': {'name': 'n'}}, 'active_session': 'k'})
        self.assertEqual(control.data['tasks'][0]['status'], 'interrupted')

    def test_normal_state_preserved(self):
        original = {'tasks': [{'id': 't1', 'status': 'completed', 'kind': 'agent', 'text': '原文',
                               'payload': {}, 'created': 1.0, 'tools': ['Read'], 'result': '结果', 'chat': 1,
                               'snapshot': {'model': 'gpt-x', 'target': '远程', 'strategy': '深入分析',
                                            'engine': 'codebuddy', 'session_key': 'sk1', 'session_id': 'id-1'}}],
                    'sessions': {'sk1': {'name': '会话', 'session_id': 'id-1', 'engine_sessions': {'codebuddy': 'c1'}}},
                    'active_session': 'sk1', 'target': '远程', 'favorites': ['gpt-x'], 'seen': [1]}
        control = self.controller(original)
        task = control.data['tasks'][0]
        self.assertEqual(task['text'], '原文')
        self.assertEqual(task['snapshot']['engine'], 'codebuddy')
        self.assertEqual(control.data['sessions']['sk1']['engine_sessions'], {'codebuddy': 'c1'})


class PruneTest(unittest.TestCase):
    """缺陷：tasks 只 append 从不删除，每条 result 最多 24000 字符，
    1000 条即 23MB，而每次 persist 都全量 deepcopy 后重写。"""

    def build(self, finished, running=0):
        tasks = [{'id': 'f%d' % i, 'status': 'completed', 'text': '', 'payload': {},
                  'result': 'x' * 24000} for i in range(finished)]
        tasks += [{'id': 'r%d' % i, 'status': 'running', 'text': '', 'payload': {}, 'result': ''} for i in range(running)]
        return tasks

    def test_keeps_recent_and_running(self):
        control = bot.TaskController.__new__(bot.TaskController)
        control.data = {'tasks': self.build(300, 1)}
        control._prune()
        kept = control.data['tasks']
        self.assertEqual(sum(1 for t in kept if t['status'] in control.TERMINAL), control.KEEP_FINISHED)
        self.assertEqual(sum(1 for t in kept if t['status'] == 'running'), 1)
        self.assertEqual(kept[0]['id'], 'f100', '应丢弃最早的 100 条')

    def test_no_prune_when_under_limit(self):
        control = bot.TaskController.__new__(bot.TaskController)
        control.data = {'tasks': self.build(50)}
        control._prune()
        self.assertEqual(len(control.data['tasks']), 50)


class StreamBlankLineTest(unittest.TestCase):
    """缺陷：旧代码在每次 message_start 时无条件追加 '\\n\\n'，而 agent 每完成
    一次工具调用都会产生一个 message_start，连续工具调用期间没有任何正文插入，
    空行便不断叠加（线上实测出现过 153 个连续换行，气泡被撑到极高）。"""

    def test_repeated_tool_calls_do_not_accumulate(self):
        buffer = '初始正文'
        for _ in range(20):
            buffer += bot.start_text_block(buffer)
        self.assertEqual(buffer.count('\n'), 2, '连续 20 次工具调用后应只保留一个段落分隔')

    def test_old_behavior_really_accumulates(self):
        # 反向验证：证明旧写法确实会膨胀，否则本组用例失去意义。
        old = '初始正文'
        for _ in range(20):
            old += '\n\n'
        self.assertEqual(old.count('\n'), 40, '旧实现应当累积 40 个换行')

    def test_no_separator_without_content(self):
        self.assertEqual(bot.start_text_block(''), '', '空缓冲区不应补分隔符')

    def test_no_separator_when_already_spaced(self):
        self.assertEqual(bot.start_text_block('正文\n\n'), '', '末尾已有空行时不应再叠加')

    def test_tidy_collapses_excess_newlines(self):
        self.assertEqual(bot.tidy_stream_text('a' + '\n' * 153 + 'b'), 'a\n\nb')
        self.assertEqual(bot.tidy_stream_text('\n\nabc\n\n'), 'abc')

    def test_tidy_keeps_normal_paragraph(self):
        self.assertEqual(bot.tidy_stream_text('abc\n\ndef'), 'abc\n\ndef', '正常段落分隔必须保留')


class AtomicJsonTest(unittest.TestCase):
    """缺陷：5 处重复的 write_text + replace 没有 fsync、权限、排他创建；
    统一走 atomic_json 后 offset 必须仍是纯文本，不能变成 JSON。"""

    def test_offset_stays_plain_text(self):
        target = Path(tempfile.mkdtemp()) / 'telegram-offset'
        bot.atomic_json(target, '123', raw=True)
        self.assertEqual(target.read_text(), '123')
        self.assertEqual(int(target.read_text()), 123)

    def test_json_roundtrip(self):
        target = Path(tempfile.mkdtemp()) / 'state.json'
        bot.atomic_json(target, {'visible': False})
        self.assertEqual(json.loads(target.read_text()), {'visible': False})

    def test_no_temp_leftover(self):
        folder = Path(tempfile.mkdtemp())
        bot.atomic_json(folder / 'x.json', {'a': 1})
        self.assertEqual([f.name for f in folder.iterdir() if f.name.endswith('.tmp')], [])


class RenderedLengthTest(unittest.TestCase):
    """缺陷：split_message 按原文 1800 字符切片，但 Telegram 的 4096 上限
    算的是**渲染后**长度。HTML 转义把 & 撑到 5 倍、< 撑到 4 倍，
    一片 1800 字的原文能渲染成 9000 字符，sendMessage 直接 400，整条结果送不出去。"""

    def test_html_escape_inflation_is_real(self):
        # 先证明这个风险真实存在，否则后面的断言毫无意义。
        self.assertEqual(len(bot.render_reply('&' * 1800)), 9000)

    def test_no_piece_exceeds_render_limit(self):
        for label, text in (
            ('全与号', '&' * 5000),
            ('全小于号', '<' * 5000),
            ('代码内与号', '`' + '&' * 4000 + '`'),
            ('普通代码块', '    if a < b and c > d: return x & y\n' * 200),
        ):
            for index, piece in enumerate(bot.split_message(text)):
                rendered = len(bot.render_reply(piece))
                self.assertLessEqual(rendered, 4096,
                                     label + ' 第%d片渲染后 %d 字符，超过 Telegram 上限' % (index + 1, rendered))

    def test_content_preserved_after_clamping(self):
        # 拆分只允许在原文层面切开放，内容必须逐字保留。
        for text in ('&' * 5000, '中文内容' * 2000, 'a<&>b\n' * 800):
            self.assertEqual(''.join(bot.split_message(text)), text)


class RedactBacktrackingTest(unittest.TestCase):
    """缺陷：URL 内嵌凭证规则写成 ([a-z][a-z0-9+.-]*://)([^\\s/:@]+):([^\\s/]+)@，
    在「有 :// 但没有 @」的长文本上二次方回溯。实测 4 万字符耗时 7.9 秒，
    而 redact 对每条消息、每条结果都会跑。"""

    def test_long_text_without_at_is_fast(self):
        import time
        text = 'sk-' * 20000
        start = time.monotonic()
        bot.redact(text)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5, '脱敏耗时 %.2f 秒，说明正则回溯又退化了' % elapsed)

    def test_credentials_still_masked(self):
        # 收紧正则不能把功能一起弄丢。
        self.assertIn('[已隐藏]', bot.redact('https://user:pass@host/x'))
        self.assertIn('[已隐藏]', bot.redact('postgres://u:p@db:5432/x'))
        # 不含凭证的 URL 必须原样保留。
        self.assertEqual(bot.redact('https://example.com/a:b'), 'https://example.com/a:b')


class TerminateProcessTest(unittest.TestCase):
    """缺陷：killpg 抛 ProcessLookupError 时原实现直接 return，跳过了 wait()。
    进程已退出但未回收时正是这个状态，于是它变成僵尸常驻。
    另外末尾的 wait(timeout=5) 自己也会超时，它位于调用方的 finally 里，
    抛出来会把真正的异常盖掉。"""

    class FakeOS:
        name = 'posix'

        def __init__(self, raiser):
            self.raiser = raiser

        def killpg(self, pid, sig):
            self.raiser()

    class FakeSignal:
        SIGTERM = 15
        SIGKILL = 9

    class FakeProcess:
        pid = 4242

        def __init__(self, always_timeout=False):
            self.waits = 0
            self.always_timeout = always_timeout

        def wait(self, timeout=None):
            self.waits += 1
            if self.always_timeout:
                raise subprocess.TimeoutExpired('x', timeout)
            return 0

    def run_with(self, killpg_raiser, process):
        original_os, original_signal = bot.os, bot.signal
        bot.os = self.FakeOS(killpg_raiser)
        bot.signal = self.FakeSignal
        try:
            bot.terminate_process(process)
        finally:
            bot.os, bot.signal = original_os, original_signal

    def test_process_reaped_when_group_already_gone(self):
        def missing():
            raise ProcessLookupError()
        process = self.FakeProcess()
        self.run_with(missing, process)
        self.assertGreater(process.waits, 0, '进程组已消失时仍必须 wait 回收，否则留下僵尸')

    def test_wait_timeout_does_not_escape(self):
        def ok():
            pass
        process = self.FakeProcess(always_timeout=True)
        try:
            self.run_with(ok, process)
        except subprocess.TimeoutExpired:
            self.fail('terminate_process 不允许向外抛异常，它会盖掉调用方 finally 里的真实原因')


class StateFileTypeTest(unittest.TestCase):
    """缺陷：状态文件是合法 JSON 但顶层类型不对（[] / "str" / null）时，
    _normalize 会以 AttributeError 炸开，机器人直接起不来，
    而错误信息完全指不出根因。"""

    def test_wrong_toplevel_type_raises_clear_error(self):
        for payload in ('[]', '"字符串"', '123', 'null'):
            folder = Path(tempfile.mkdtemp())
            (folder / 'bridge-state.json').write_text(payload, encoding='utf-8')
            with self.assertRaises(ValueError) as caught:
                bot.TaskController(folder)
            self.assertIn('顶层不是对象', str(caught.exception))

    def test_valid_object_still_boots(self):
        folder = Path(tempfile.mkdtemp())
        (folder / 'bridge-state.json').write_text(
            '{"tasks": [], "sessions": {"a": {"name": "x"}}, "active_session": "a"}', encoding='utf-8')
        controller = bot.TaskController(folder)
        self.assertEqual(controller.data['tasks'], [])


class TaskLengthLimitTest(unittest.TestCase):
    """缺陷：任务正文无上限，而执行器把正文作为独立 argv 传给 CLI。
    Linux 单个 argv 上限约 128KB，超长任务会以 E2BIG 启动失败。"""

    def test_overlong_text_rejected(self):
        controller = bot.TaskController(Path(tempfile.mkdtemp()))
        with self.assertRaises(bot.MediaError):
            controller.enqueue(1, '任' * (bot.TaskController.MAX_TASK_CHARS + 1))

    def test_normal_text_accepted(self):
        controller = bot.TaskController(Path(tempfile.mkdtemp()))
        task = controller.enqueue(1, '正常长度的任务')
        self.assertEqual(task['status'], 'queued')


class StreamContentCapTest(unittest.TestCase):
    """缺陷：解析器对 state['content'] 无上限累加，只在最后才截断到 24000。
    一轮 80 次工具调用、每次输出几万字时，单个任务就能吃掉几十兆内存。"""

    def test_cap_enforced(self):
        state = {'content': '', 'final': None, 'activity': '', 'failed': ''}
        for _ in range(3000):
            bot.append_content(state, '输出片段' * 200)
        self.assertLessEqual(len(state['content']), bot.STREAM_CONTENT_CAP)

    def test_tail_is_kept(self):
        state = {'content': '', 'final': None, 'activity': '', 'failed': ''}
        for _ in range(3000):
            bot.append_content(state, '片段' * 100)
        self.assertIn('前面内容过长已省略', state['content'])
        self.assertTrue(state['content'].endswith('片段'))


class SeenThrottleTest(unittest.TestCase):
    """缺陷：每收到一条消息就原子写一次状态文件（满负荷时约 13ms 且持锁），
    而 seen 的唯一作用是重启后不重复执行。改成节流后要保证：
    已处理的 update_id 仍然全部记进内存集合。"""

    def test_all_ids_recorded(self):
        controller = bot.TaskController(Path(tempfile.mkdtemp()))
        for index in range(100):
            controller.note_seen(5000 + index)
        for index in range(100):
            self.assertIn(5000 + index, controller.seen)

    def test_writes_are_throttled(self):
        controller = bot.TaskController(Path(tempfile.mkdtemp()))
        writes = {'count': 0}
        original = bot.atomic_json

        def counting(path, value, raw=False):
            writes['count'] += 1
            return original(path, value, raw)

        bot.atomic_json = counting
        try:
            for index in range(100):
                controller.note_seen(7000 + index)
        finally:
            bot.atomic_json = original
        self.assertLess(writes['count'], 100, '每条都落盘等于没做节流')


class GroupMenuTest(unittest.TestCase):
    """缺陷：分组展开逻辑在 quick_action 与 handle_text 里各写一份，
    文案改动只改一处就会漂移。统一走 expand_group 后要保证两处入口行为一致。"""

    def test_expand_group_returns_bool(self):
        captured = []
        original = bot.send_options
        bot.send_options = lambda chat, title, items: captured.append((title, list(items)))
        try:
            self.assertTrue(bot.expand_group(1, '任务与模型'))
            self.assertFalse(bot.expand_group(1, '根本不存在'))
        finally:
            bot.send_options = original
        self.assertTrue(captured[0][0].startswith('工作设置 / 任务与模型'))
        self.assertIn(bot.GROUP_DESCRIPTIONS['任务与模型'], captured[0][0])
        self.assertEqual(captured[0][1], bot.MENU_GROUPS['设置']['任务与模型'])

    def test_title_helper_covers_both_owners(self):
        self.assertTrue(bot.group_title('设置', '设置').startswith('工作设置\n'))
        self.assertTrue(bot.group_title('更多', '更多').startswith('更多功能\n'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
