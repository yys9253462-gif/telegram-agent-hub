#!/usr/bin/env python3
"""运行中显示过程：每次工具调用一行，执行模型（子代理）的步骤带 ↳，失败标 ✗。

事件形状取自 2026-10-26 生产机实抓：子代理内部的 assistant/user 事件带 parent_tool_use_id。

运行：python3 tests/test_progress_steps.py
"""
import _root  # noqa: F401  仓库根目录进 sys.path
import unittest

import bot
from textfmt import render_reply


def state():
    return {'content': '', 'final': None, 'activity': '等待模型响应', 'failed': '', 'thinking': '', 'tool_name': ''}


def assistant(blocks, parent=None):
    return {'type': 'assistant', 'parent_tool_use_id': parent, 'message': {'content': blocks}}


class ProgressStepsTest(unittest.TestCase):
    def test_main_and_subagent_steps(self):
        s = state()
        bot.parse_claude_event(assistant([{'type': 'tool_use', 'name': 'Agent',
                                           'input': {'description': '盘点东京源码项目', 'prompt': '...'}}]), s)
        bot.parse_claude_event(assistant([{'type': 'tool_use', 'name': 'Bash',
                                           'input': {'command': "ssh tokyo 'ls /opt'"}}], parent='call_1'), s)
        bot.parse_claude_event({'type': 'user', 'parent_tool_use_id': 'call_1', 'message': {'content': [
            {'type': 'tool_result', 'is_error': True, 'content': 'Permission denied'}]}}, s)
        bot.parse_claude_event(assistant([{'type': 'tool_use', 'name': 'Read',
                                           'input': {'file_path': '/opt/app/main.go'}}], parent='call_1'), s)
        self.assertEqual(s['steps'], ['🤝 交给执行模型：盘点东京源码项目',
                                      "↳ 💻 ssh tokyo 'ls /opt' ✗",
                                      '↳ 📖 /opt/app/main.go'])
        self.assertTrue(s['activity'].startswith('执行模型正在：'))

    def test_preview_shows_recent_steps_in_quote(self):
        steps = ['💻 cmd ' + str(i) for i in range(9)]
        frame = bot.stream_preview('先盘点源码。', '', '正在执行工具：Bash', 30, steps)
        self.assertIn('…前面还有 3 步', frame)
        self.assertIn('💻 cmd 8', frame)
        self.assertNotIn('💻 cmd 2', frame)
        html = render_reply(frame)
        self.assertIn('<blockquote>', html)
        self.assertTrue(html.startswith('先盘点源码。'))
        self.assertIn('<i>⏳ 正在执行工具：Bash · 已运行30秒</i>', html)

    def test_long_command_is_shortened(self):
        line = bot.describe_tool('Bash', {'command': 'x' * 200})
        self.assertLess(len(line), 90)

    def test_codex_commands_become_steps(self):
        s = state()
        bot.parse_codex_event({'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'df -h /'}}, s)
        bot.parse_codex_event({'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'df -h /',
                                                                   'exit_code': 1}}, s)
        self.assertEqual(s['steps'], ['💻 df -h / ✗'])

    def test_no_steps_keeps_old_frame(self):
        self.assertEqual(bot.stream_preview('正文', '', '正在生成回复', 5),
                         bot.stream_preview('正文', '', '正在生成回复', 5, []))


if __name__ == '__main__':
    unittest.main()
