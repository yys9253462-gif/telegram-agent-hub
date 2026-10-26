#!/usr/bin/env python3
"""流式输出的解析与刷新节奏测试。

背景：用户反馈「流式回复没有流式输出」。排查时没有靠猜，而是在生产机上
抓了真实事件流（CodeBuddy + --include-partial-messages），拿到两个事实：

1. **thinking 与 text 是两种不同的 delta**：
     {"type":"stream_event","event":{"type":"content_block_delta",
       "index":0,"delta":{"type":"thinking_delta","thinking":"用户"}}}
     {"type":"stream_event","event":{"type":"content_block_delta",
       "index":1,"delta":{"type":"text_delta","text":"1"}}}
   content_block 只出现在 content_block_start 里，delta 事件里没有这个字段。

2. **一段正文往往只持续零点几秒**（实测 0.36s / 0.49s / 4.88s，中间隔着
   几秒工具执行）。原来的固定 1 秒节流让前两段一次都没刷新过，界面上就是
   整块蹦出来。

下面的用例直接拿真实的 JSON 事件（不是自己编的简化版）喂给解析器，
再验证刷新节奏确实会把「短短一段」也拆成多次。
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


def stream_event(inner):
    return {'type': 'stream_event', 'event': inner}


def block_start(index, kind, **extra):
    payload = {'type': kind}
    payload.update(extra)
    return stream_event({'type': 'content_block_start', 'index': index,
                         'content_block': payload})


def block_stop(index):
    return stream_event({'type': 'content_block_stop', 'index': index})


def text_delta(index, text):
    return stream_event({'type': 'content_block_delta', 'index': index,
                         'delta': {'type': 'text_delta', 'text': text}})


def thinking_delta(index, text):
    return stream_event({'type': 'content_block_delta', 'index': index,
                         'delta': {'type': 'thinking_delta', 'thinking': text}})


def fresh_state():
    # 与 run_stream 里的初始字典保持一致，包括 tool_name：
    # 少了它，input_json_delta 阶段就只能显示兜底词。
    return {'content': '', 'final': None, 'activity': '等待模型响应',
            'failed': '', 'thinking': '', 'tool_name': ''}


class ParseTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(bot, 'SESSION_STATE', self.root / 'session.json')]
        for item in self.patches:
            item.start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def feed(self, events, state=None):
        state = state if state is not None else fresh_state()
        for event in events:
            bot.parse_claude_event(event, state)
        return state


class ThinkingDeltaTest(ParseTestBase):
    """思考内容不进正文，但不能让界面看起来卡住。"""

    def test_thinking_delta_does_not_enter_content(self):
        state = self.feed([block_start(0, 'thinking'),
                           thinking_delta(0, '用户在问'),
                           block_stop(0)])
        self.assertEqual(state['content'], '',
                         '思考内容混进了正文，最终答案会被推理摘要污染')

    def test_thinking_delta_is_recorded_for_display(self):
        state = self.feed([block_start(0, 'thinking'),
                           thinking_delta(0, '用户在问'),
                           thinking_delta(0, '路径问题')])
        self.assertIn('用户在问', state['thinking'])
        self.assertIn('路径问题', state['thinking'])

    def test_thinking_sets_activity(self):
        state = self.feed([block_start(0, 'thinking')])
        self.assertEqual(state['activity'], '正在思考')
        state = self.feed([thinking_delta(0, '嗯')], state)
        self.assertEqual(state['activity'], '正在思考')

    def test_thinking_is_capped(self):
        """思考越长越没用，只留最近一小段，别把内存撑大。"""
        state = fresh_state()
        for _ in range(200):
            bot.parse_claude_event(thinking_delta(0, 'x' * 50), state)
        self.assertLessEqual(len(state['thinking']), 600)


class TextDeltaTest(ParseTestBase):
    def test_text_delta_accumulates(self):
        state = self.feed([block_start(1, 'text'),
                           text_delta(1, '第一段'),
                           text_delta(1, '继续'),
                           block_stop(1)])
        self.assertEqual(state['content'], '第一段继续')
        self.assertEqual(state['activity'], '正在生成回复')

    def test_two_text_blocks_are_separated(self):
        """工具调用前后的两段正文要分开，不能黏成一句。"""
        state = self.feed([
            block_start(1, 'text'), text_delta(1, '先看目录。'), block_stop(1),
            block_start(2, 'tool_use', name='Bash'), block_stop(2),
            block_start(1, 'text'), text_delta(1, '再看磁盘。'), block_stop(1),
        ])
        self.assertEqual(state['content'], '先看目录。\n\n再看磁盘。')

    def test_text_block_start_alone_adds_nothing(self):
        """只开块、没内容时不该往正文里塞空行（这条曾经导致 153 个连续换行）。"""
        state = self.feed([block_start(1, 'text')])
        self.assertEqual(state['content'], '')

    def test_real_production_sequence(self):
        """照生产抓包原样走一遍：thinking → tool → text ×3 段。

        期望得到完整正文，且三段之间有段落分隔。
        """
        events = [
            block_start(0, 'thinking'), thinking_delta(0, '先 ls'), block_stop(0),
            block_start(2, 'tool_use', name='Bash'), block_stop(2),
            block_start(0, 'thinking'), block_stop(0),
            block_start(1, 'text'), text_delta(1, '`/opt` 下只有'),
            text_delta(1, '一个目录。'), block_stop(1),
            block_start(2, 'tool_use', name='Bash'), block_stop(2),
            block_start(1, 'text'), text_delta(1, '根分区 40G，'), block_stop(1),
        ]
        state = self.feed(events)
        self.assertEqual(state['content'], '`/opt` 下只有一个目录。\n\n根分区 40G，')

    def test_content_block_absent_from_delta_is_tolerated(self):
        """delta 事件里没有 content_block 字段，解析不能因此崩。"""
        event = {'type': 'stream_event', 'event': {
            'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'x'}}}
        state = self.feed([event])
        self.assertEqual(state['content'], 'x')

    def test_unknown_delta_types_are_ignored(self):
        state = self.feed([stream_event({'type': 'content_block_delta', 'index': 0,
                                         'delta': {'type': 'something_new', 'x': 'y'}})])
        self.assertEqual(state['content'], '')
        self.assertEqual(state['thinking'], '')

    def test_message_start_and_stop_do_not_add_text(self):
        state = self.feed([stream_event({'type': 'message_start', 'message': {}}),
                           stream_event({'type': 'message_stop'})])
        self.assertEqual(state['content'], '')


class ToolNameTest(ParseTestBase):
    """工具名只在 content_block_start 里出现，必须记下来。

    漏记的后果是参数阶段显示「正在准备工具参数：工具」——兜底词本身没意义，
    用户看不出到底在准备哪个工具（2026-10-26 生产实测踩到）。
    """

    def test_tool_name_is_remembered_from_block_start(self):
        state = self.feed([block_start(2, 'tool_use', name='Bash')])
        self.assertEqual(state['tool_name'], 'Bash')

    def test_input_json_delta_shows_real_tool_name(self):
        state = self.feed([block_start(2, 'tool_use', name='Bash'),
                           stream_event({'type': 'content_block_delta', 'index': 2,
                                         'delta': {'type': 'input_json_delta',
                                                   'partial_json': '{"command"'}})])
        self.assertEqual(state['activity'], '正在准备工具参数：Bash')

    def test_missing_tool_name_degrades_without_placeholder(self):
        # 上游省略了 content_block_start 时，宁可少一个词，也别冒出「工具」这种废字。
        state = self.feed([stream_event({'type': 'content_block_delta', 'index': 2,
                                         'delta': {'type': 'input_json_delta',
                                                   'partial_json': '{'}})])
        self.assertEqual(state['activity'], '正在准备工具参数')
        self.assertNotIn('：工具', state['activity'])


class FinalAnswerSelectionTest(unittest.TestCase):
    """最终回复该用「累积正文」还是「CLI 收尾答复」。

    线上真实事故（2026-10-26 生产复现）：让 agent 依次执行三条命令、
    每条说一句话，流式过程中三段都正确显示了，收尾却只剩最后一句。
    原因是 `answer = result or content` —— result 永远非空，
    于是累积正文被整体丢弃。
    """

    def test_keeps_accumulated_body_when_longer(self):
        content = '第一段说明。\n\n第二段说明。\n\n第三段总结。'
        final = '第三段总结。'
        self.assertEqual(bot.pick_final_answer(final, content), content,
                         '累积正文更长时必须用它，否则中间段落全丢')

    def test_falls_back_to_final_when_no_content(self):
        self.assertEqual(bot.pick_final_answer('只有收尾答复', ''), '只有收尾答复')

    def test_falls_back_to_final_when_final_is_richer(self):
        # 有些执行器的 result 是整理过的完整答复，比碎片式累积更完整。
        self.assertEqual(bot.pick_final_answer('完整答复正文', '零碎'), '完整答复正文')

    def test_placeholder_when_both_empty(self):
        self.assertIn('未返回内容', bot.pick_final_answer('', ''))

    def test_reproduces_the_production_case(self):
        """复现生产现场：三段正文 + 一句话收尾，结果必须保留三段。"""
        content = '`/opt` 下只有一个目录：`claude-telegram`。\n\n根分区 40G，已用 5.6G，剩余 32G，使用率 15%。\n\n负载很低，机器空闲。'
        final = '负载很低，机器空闲。'
        answer = bot.pick_final_answer(final, content)
        self.assertIn('claude-telegram', answer)
        self.assertIn('根分区', answer)
        self.assertIn('机器空闲', answer)

    def test_run_stream_uses_the_helper(self):
        import inspect
        body = inspect.getsource(bot.run_stream)
        self.assertIn('pick_final_answer', body)
        # 反向钉住：不能再退回「result 优先」的写法。
        self.assertNotIn("answer = (final or {}).get('result') or content", body)


class EditPacingTest(ParseTestBase):
    """刷新节奏：一段只持续零点几秒的正文，也必须被拆成多次刷新。

    这里的断言盯两个层面：
      - 常量本身在合理区间（太慢就退回整块蹦）；
      - **主循环真的用这两个常量**，而不是又写回一个固定值。
        只测 simulate 的参数没意义——那只证明「我传进去的值能算对」，
        抓不到代码里节流又变回 1 秒的情况。
    """

    def test_main_loop_uses_the_adaptive_step(self):
        import inspect
        source = inspect.getsource(bot.run_stream)
        self.assertIn('stream_interval(now - started, grew)', source,
                      '主循环没有按「有没有新正文」切换刷新间隔，'
                      '短段落会退回整块蹦出')
        self.assertNotIn("interval = 1.0 if activity", source,
                         '又写回了固定 1 秒的节流')
        # 开头快节奏，之后降到电报能长期接受的频率（否则长任务会被限流卡住）。
        self.assertEqual(bot.stream_interval(5, True), bot.STREAM_EDIT_MIN)
        self.assertEqual(bot.stream_interval(5, False), bot.STREAM_EDIT_IDLE)
        self.assertGreaterEqual(bot.stream_interval(300, True), 1.0)
        self.assertGreaterEqual(bot.stream_interval(300, False), 3.0)

    def test_block_stop_triggers_immediate_settle(self):
        """段落收尾要立刻结算，否则最后几个字会拖到下一段才显示。"""
        import inspect
        source = inspect.getsource(bot.run_stream)
        self.assertIn('content_block_stop', source)
        self.assertIn('settled', source)

    def test_thinking_is_surfaced_while_content_is_empty(self):
        """思考阶段正文还是空的，界面不能只留一行干巴巴的进度。"""
        import inspect
        source = inspect.getsource(bot.run_stream)
        self.assertIn("state.get('thinking')", source,
                      '思考阶段没有把思考内容显示出来，用户会觉得卡住了')

    def simulate(self, segments, interval_min, interval_idle):
        """按主循环的判定逻辑重放，返回每次刷新的正文长度。

        segments 是 [(秒数, [字...]), ...]：每段的持续时长与逐字内容。
        用虚拟时钟，不真的等。
        """
        state = fresh_state()
        clock = 0.0
        last_edit = 0.0
        last_len = 0
        block_finished = False
        lengths = []
        for duration, chars in segments:
            step = duration / max(1, len(chars))
            bot.parse_claude_event(block_start(1, 'text'), state)
            for ch in chars:
                bot.parse_claude_event(text_delta(1, ch), state)
                clock += step
                grew = len(state['content']) > last_len
                interval = interval_min if grew else interval_idle
                if clock - last_edit >= interval:
                    last_edit = clock
                    last_len = len(state['content'])
                    lengths.append(last_len)
            bot.parse_claude_event(block_stop(1), state)
            block_finished = True
            if block_finished and len(state['content']) > last_len:
                last_edit = clock
                last_len = len(state['content'])
                lengths.append(last_len)
                block_finished = False
        return lengths

    def test_short_segment_is_still_streamed(self):
        """0.4 秒的一段正文，用旧逻辑（1 秒节流）一次都不会刷。"""
        segments = [(0.4, list('这是很短的一段回答'))]
        old = self.simulate(segments, 1.0, 2.0)
        new = self.simulate(segments, bot.STREAM_EDIT_MIN, bot.STREAM_EDIT_IDLE)
        self.assertEqual(len(old), 1, '旧逻辑本来就该只刷 1 次（段落收尾那次）')
        self.assertGreater(len(new), 1,
                          '新的自适应节流仍然把短段落当成一整块，没有拆开')

    def test_pacing_constants_are_sane(self):
        # 太快会撞 Telegram 限流，太慢就退回「整块蹦」。250ms 是实测出来的值。
        self.assertLessEqual(bot.STREAM_EDIT_MIN, 0.35)
        self.assertGreaterEqual(bot.STREAM_EDIT_MIN, 0.2)
        self.assertGreater(bot.STREAM_EDIT_IDLE, bot.STREAM_EDIT_MIN)

    def test_progressive_lengths_not_big_jumps(self):
        """刷新出来的正文长度应当是渐进的，而不是从 0 直接跳到整段。"""
        segments = [(0.5, list('第一段回答内容'))]
        lengths = self.simulate(segments, bot.STREAM_EDIT_MIN, bot.STREAM_EDIT_IDLE)
        self.assertGreater(len(lengths), 1, str(lengths))
        # 第一次刷新不该已经是全文
        self.assertLess(lengths[0], sum(len(c) for _, c in segments), str(lengths))


class FinalDeliveryTest(unittest.TestCase):
    """最终答案必达：限流退避期内也不能被跳过。"""

    def test_final_answer_waits_out_backoff(self):
        sent = []
        with patch.object(bot, 'api', side_effect=lambda m, p, retries=None: sent.append(p['text'])):
            stream = bot.StreamMessage(1, 2, '', '')
            stream.hold_until = time.monotonic() + 0.3
            stream.edit('中间帧')
            stream.close()
            stream.deliver_final('最终答案')
        self.assertTrue(sent and '最终答案' in sent[-1], sent)

    def test_final_answer_falls_back_to_new_message(self):
        import io, urllib.error
        def api(method, payload, retries=None):
            raise urllib.error.HTTPError('u', 400, 'bad', {}, io.BytesIO(b'{"description": "message to edit not found"}'))
        fallback = []
        with patch.object(bot, 'api', side_effect=api),              patch.object(bot, 'send_formatted', side_effect=lambda chat, text: fallback.append(text)):
            stream = bot.StreamMessage(1, 2, '', '')
            stream.close()
            stream.deliver_final('最终答案')
        self.assertEqual(fallback, ['最终答案'])


class BackgroundSenderTest(unittest.TestCase):
    """编辑不能再阻塞主循环：一次 edit 是一次 HTTP 往返。"""

    def test_run_stream_uses_a_sender_thread(self):
        """edit 必须是「登记最新帧」，不能在里面同步等网络。"""
        import inspect
        source = inspect.getsource(bot.run_stream)
        self.assertIn('StreamMessage(', source, '流式编辑没有走后台发送器，网络往返会阻塞事件处理')
        # 「只留最新一帧」：连续登记多帧，网络卡住期间只有最后一帧会被发出。
        sent, gate = [], threading.Event()
        def api(method, payload, retries=None):
            gate.wait(2)
            sent.append(payload['text'])
            return {}
        with patch.object(bot, 'api', side_effect=api):
            stream = bot.StreamMessage(1, 2, '', '')
            for value in ('第一帧', '第二帧', '第三帧'):
                stream.edit(value)
            gate.set()
            stream.close()
        self.assertLessEqual(len(sent), 2, sent)
        self.assertIn('第三帧', sent[-1])


class RealFixtureReplayTest(ParseTestBase):
    """用生产抓包回放，验证整条链路的累积结果。

    fixtures/stream_claude_codebuddy.jsonl 是在生产机上跑
    「依次执行 ls /opt、df -h /、uptime」时用
    `codebuddy -p ... --output-format stream-json --include-partial-messages`
    抓下来的，只去掉了 _meta/uuid 这类与解析无关的大字段。

    它比手写的简化事件可靠：真实链路里 thinking 与 text 交错、
    工具块夹在中间、index 会重复使用，这些坑只有在真数据上才踩得到。
    """

    FIXTURE = Path(__file__).parent / 'fixtures' / 'stream_claude_codebuddy.jsonl'

    def load(self):
        self.assertTrue(self.FIXTURE.exists(), '夹具文件缺失：' + str(self.FIXTURE))
        events = []
        for line in self.FIXTURE.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return events

    def test_content_matches_the_three_segments(self):
        events = self.load()
        state = self.feed(events)
        content = state['content']
        # 三段正文，各自对应一条命令的结果。
        self.assertIn('claude-telegram', content)
        self.assertIn('40G', content)
        self.assertIn('负载', content)
        # 段与段之间必须有分隔，不能黏成一整句。
        self.assertGreaterEqual(content.count('\n\n'), 2, content)
        # 思考内容不能漏进正文。
        self.assertNotIn('用户要求', content)
        self.assertNotIn('thinking', content.lower())

    def test_thinking_was_captured(self):
        state = self.feed(self.load())
        self.assertTrue(state['thinking'], '整份抓包里没留下任何思考内容')
        self.assertLessEqual(len(state['thinking']), 600)

    def test_tool_calls_are_recorded(self):
        events = self.load()
        with patch.object(bot, 'record_tool') as recorder:
            self.feed(events)
        self.assertTrue(recorder.called, '抓包里有三次 Bash 调用，工具记录却是空的')
        self.assertTrue(all(call.args[0] == 'Bash' for call in recorder.call_args_list),
                        recorder.call_args_list)

    def test_final_result_is_present(self):
        state = self.feed(self.load())
        self.assertIsNotNone(state['final'], '没有拿到 result 事件，收尾会退回用累积正文')
        self.assertEqual((state['final'] or {}).get('subtype'), 'success')

    def test_no_runaway_blank_lines(self):
        """正文里不能出现成片的空行（这条曾经出过 153 个连续换行）。"""
        content = self.feed(self.load())['content']
        self.assertNotIn('\n\n\n', bot.tidy_stream_text(content))


class StreamTextTidyTest(unittest.TestCase):
    def test_excess_blank_lines_are_compressed(self):
        self.assertEqual(bot.tidy_stream_text('a\n\n\n\n\nb'), 'a\n\nb')

    def test_leading_and_trailing_blank_lines_removed(self):
        self.assertEqual(bot.tidy_stream_text('\n\na\n\n'), 'a')

    def test_normal_paragraph_spacing_preserved(self):
        self.assertEqual(bot.tidy_stream_text('a\n\nb'), 'a\n\nb')


if __name__ == '__main__':
    unittest.main(verbosity=2)
