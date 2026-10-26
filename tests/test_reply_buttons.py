import unittest

import _root  # noqa: F401
import bot


class ReplyButtonsTest(unittest.TestCase):
    def test_extract_suggestions(self):
        body, items = bot.extract_suggestions('结论在这里。\n\n【建议】把修复部署上线｜回滚到旧版 | 再查一遍日志')
        self.assertEqual(body, '结论在这里。')
        self.assertEqual(items, ['把修复部署上线', '回滚到旧版', '再查一遍日志'])

    def test_no_suggestion_line_unchanged(self):
        text = '正文提到【建议】但不在最后。\n第二行'
        self.assertEqual(bot.extract_suggestions(text), (text, []))

    def test_at_most_three(self):
        _, items = bot.extract_suggestions('x\n**【建议】：** a｜b｜c｜d')
        self.assertEqual(items, ['a', 'b', 'c'])

    def test_context_bar(self):
        self.assertEqual(bot.context_bar(62000, 200000), '上下文 ▰▰▰▱▱▱▱▱▱▱ 31% · 62k / 200k')
        self.assertIn('/ 1M', bot.context_bar(250000, 1000000))
        self.assertEqual(bot.context_bar(0, 200000), '')

    def test_buttons_have_compact_last(self):
        markup = bot.reply_buttons(1, ['部署上线'])
        rows = markup['inline_keyboard']
        self.assertEqual(rows[0][0]['text'], '👉 部署上线')
        self.assertEqual(rows[-1][0]['text'], '🗜 压缩上下文')
        token = rows[-1][0]['callback_data'].split(':')[1]
        self.assertEqual(bot.ACTION_UI[token]['command'], '压缩上下文')
        self.assertLessEqual(len(rows[0][0]['callback_data'].encode()), 64)

    def test_parse_usage_and_compact(self):
        s = {'content': '', 'final': None, 'activity': '', 'failed': '', 'thinking': '', 'tool_name': ''}
        bot.save_session = lambda value: None
        bot.parse_claude_event({'type': 'assistant', 'message': {'content': [], 'usage': {
            'input_tokens': 2, 'cache_read_input_tokens': 50000, 'cache_creation_input_tokens': 1000}}}, s)
        bot.parse_claude_event({'type': 'assistant', 'parent_tool_use_id': 'x', 'message': {'content': [], 'usage': {
            'input_tokens': 9}}}, s)
        self.assertEqual(s['context_used'], 51002)
        bot.parse_claude_event({'type': 'result', 'modelUsage': {
            'small': {'inputTokens': 5, 'contextWindow': 128000},
            'main': {'cacheReadInputTokens': 90000, 'contextWindow': 1000000}}}, s)
        self.assertEqual(s['context_window'], 1000000)
        bot.parse_claude_event({'type': 'system', 'subtype': 'compact_boundary',
                                'compact_metadata': {'pre_tokens': 25157, 'post_tokens': 7822}}, s)
        self.assertEqual(s['compacted']['post_tokens'], 7822)

    def test_prompt_mentions_format(self):
        self.assertIn('【建议】', bot.SUGGEST_PROMPT)


if __name__ == '__main__':
    unittest.main()
