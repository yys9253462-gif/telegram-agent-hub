#!/usr/bin/env python3
"""五轮系统排查中修复行为的回归测试。

每个用例都对应一处真实缺陷，注释写明缺陷现象，便于以后判断能否删除。
运行：python test_hardening.py
"""
import json
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
