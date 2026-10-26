#!/usr/bin/env python3
"""回复排版分层：标题、正文、进度、执行信息要能一眼分开。

运行：python3 tests/test_reply_style.py
"""
import _root  # noqa: F401  仓库根目录进 sys.path
import unittest

import textfmt
from textfmt import FOOTER_MARK, PROGRESS_MARK, TITLE_MARK, clean_reply, render_reply


class ReplyStyleTest(unittest.TestCase):
    def test_task_title_is_bold_with_pin(self):
        self.assertEqual(render_reply(TITLE_MARK + '任务 abc · 你好'), '<b>📌 任务 abc · 你好</b>')

    def test_heading_levels_have_distinct_icons(self):
        self.assertEqual(render_reply('# 结论'), '🟦 <b>结论</b>')
        self.assertEqual(render_reply('### 细节'), '🔹 <b>细节</b>')

    def test_progress_line_is_italic(self):
        self.assertEqual(render_reply(PROGRESS_MARK + '正在执行 Bash · 已运行12秒'), '<i>⏳ 正在执行 Bash · 已运行12秒</i>')

    def test_footer_goes_into_expandable_quote(self):
        rendered = render_reply('正文\n\n' + FOOTER_MARK + '\n执行器：Claude · 耗时：3秒\n用量：5小时 剩余 90%')
        self.assertTrue(rendered.startswith('正文'))
        self.assertTrue(rendered.endswith('<blockquote expandable>执行器：Claude · 耗时：3秒\n用量：5小时 剩余 90%</blockquote>'))

    def test_markdown_quote_and_list(self):
        self.assertEqual(render_reply('> 注意\n> 第二行'), '<blockquote>注意\n第二行</blockquote>')
        self.assertEqual(render_reply('- 一\n  - 二'), '• 一\n  • 二')

    def test_code_block_with_language_is_highlighted(self):
        self.assertEqual(render_reply('```python\nprint(1)\n```'),
                         '<pre><code class="language-python">print(1)</code></pre>')
        self.assertEqual(render_reply('```\nls\n```'), '<pre>ls</pre>')

    def test_marks_never_leak_into_plain_text(self):
        text = TITLE_MARK + '任务 abc\n' + PROGRESS_MARK + '进度\n' + FOOTER_MARK + '\n用量'
        plain = clean_reply(text)
        for mark in textfmt.MARKS:
            self.assertNotIn(mark, plain)
        self.assertNotIn('title', plain)

    def test_html_is_still_escaped(self):
        self.assertEqual(render_reply('# <b>x</b>'), '🟦 <b>&lt;b&gt;x&lt;/b&gt;</b>')
        self.assertEqual(render_reply(FOOTER_MARK + '\n<script>'), '<blockquote expandable>&lt;script&gt;</blockquote>')


if __name__ == '__main__':
    unittest.main()
