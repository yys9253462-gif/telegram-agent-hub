#!/usr/bin/env python3
"""五轮交互测试 · 第五轮：边界条件与异常输入。

这一轮的问题都是「真实场景下会撞到、但平时手点不出来」的：
超长消息、畸形回调、状态文件被改坏、时钟异常、并发重入、附件异常。
判据统一是：**要么正常处理，要么给出一句人话，绝不能崩掉整个轮询循环。**
"""
import _root  # noqa: F401  仓库根目录进 sys.path
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


class EdgeBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = bot.TaskController(self.root)
        self.sent = []
        self.patches = [
            patch.object(bot, 'CONTROL', self.controller),
            patch.object(bot, 'OWNER', 123),
            patch.object(bot, 'send', side_effect=lambda chat, text: self.sent.append(text)),
            patch.object(bot, 'notify', side_effect=lambda chat, text: self.sent.append(text)),
            patch.object(bot, 'api', side_effect=lambda method, payload=None: (
                self.sent.append(payload.get('text')) if isinstance(payload, dict) and payload.get('text') else None)
                or {'message_id': 1}),
        ]
        for item in self.patches:
            item.start()
        bot.ACTION_UI.clear()
        bot.ACTION_UI_SEEN.clear()
        self.addCleanup(self._teardown)

    def _teardown(self):
        for item in reversed(self.patches):
            item.stop()
        bot.ACTION_UI.clear()
        bot.ACTION_UI_SEEN.clear()
        self.temp.cleanup()


class LongMessageTest(EdgeBase):
    """超长消息：切分要保内容、保结构、保上限。"""

    def test_very_long_plain_text_keeps_every_character(self):
        text = ('这是一段很长的说明。' * 3000)
        pieces = bot.split_message(text)
        self.assertGreater(len(pieces), 1)
        self.assertEqual(''.join(pieces), text)

    def test_each_piece_fits_telegram_limit(self):
        text = '中文与 ascii 混排 ' * 2000
        for piece in bot.split_message(text):
            self.assertLessEqual(len(bot.render_reply(piece)), bot.RENDER_LIMIT + 100,
                                 '某一片渲染后超过上限')

    def test_html_heavy_content_is_clamped(self):
        """& < > 渲染后会膨胀数倍，按原文字数切的文章会在这里炸。"""
        text = '&amp; <script> ' * 2000
        for piece in bot.split_message(text):
            self.assertLessEqual(len(bot.render_reply(piece)), bot.RENDER_LIMIT + 100)

    def test_single_giant_line_is_split(self):
        text = 'x' * 20000
        pieces = bot.split_message(text)
        self.assertEqual(''.join(pieces), text)
        self.assertTrue(all(len(piece) <= 1900 for piece in pieces))

    def test_unclosed_code_fence_is_closed_per_piece(self):
        text = '前文\n```python\n' + 'print(1)\n' * 400
        for index, piece in enumerate(bot.split_message(text)):
            fences = sum(1 for line in piece.splitlines() if line.lstrip().startswith('```'))
            self.assertEqual(fences % 2, 0, '第%d片代码围栏没闭合' % (index + 1))

    def test_empty_text_renders_something(self):
        self.assertTrue(bot.render_reply('').strip())

    def test_split_never_returns_empty_list(self):
        self.assertEqual(bot.split_message(''), [''])


class MalformedUpdateTest(EdgeBase):
    """畸形更新：缺字段、类型错、超长回调。每一条都只能被跳过，不能抛出去。"""

    def call(self, update):
        try:
            bot.handle_update(update)
        except Exception as exc:  # noqa: BLE001 - 本用例就是要抓「抛出来了」
            self.fail('畸形更新把异常抛到了轮询循环：' + type(exc).__name__ + ': ' + str(exc))

    def test_missing_update_id_is_skipped(self):
        self.call({'message': {'chat': {'id': 123, 'type': 'private'},
                               'from': {'id': 123}, 'text': '你好'}})

    def test_missing_chat_is_skipped(self):
        self.call({'update_id': 1, 'message': {'from': {'id': 123}, 'text': '你好'}})

    def test_non_string_text_is_skipped(self):
        self.call({'update_id': 2, 'message': {'chat': {'id': 123, 'type': 'private'},
                                               'from': {'id': 123}, 'text': {'a': 1}}})
        # 对象形态的正文不能被当成任务正文发给执行器。
        with self.controller.lock:
            texts = [item['text'] for item in self.controller.data['tasks']]
        self.assertFalse(any(isinstance(item, dict) for item in texts), texts)

    def test_empty_text_asks_for_content(self):
        self.call({'update_id': 3, 'message': {'chat': {'id': 123, 'type': 'private'},
                                               'from': {'id': 123}, 'text': '   '}})
        self.assertTrue(any('请发送' in text for text in self.sent), self.sent)

    def test_callback_without_data_is_skipped(self):
        self.call({'update_id': 4, 'callback_query': {'id': '1', 'from': {'id': 123},
                                                      'message': {'chat': {'id': 123, 'type': 'private'}}}})

    def test_callback_with_overlong_data_is_skipped(self):
        self.call({'update_id': 5, 'callback_query': {'id': '1', 'from': {'id': 123}, 'data': 'x' * 200,
                                                      'message': {'chat': {'id': 123, 'type': 'private'}}}})

    def test_stranger_is_ignored(self):
        with patch.object(bot, 'send') as send:
            self.call({'update_id': 6, 'message': {'chat': {'id': 999, 'type': 'private'},
                                                   'from': {'id': 999}, 'text': '你好'}})
        send.assert_not_called()

    def test_group_chat_is_ignored(self):
        with patch.object(bot, 'send') as send:
            self.call({'update_id': 7, 'message': {'chat': {'id': 123, 'type': 'group'},
                                                   'from': {'id': 123}, 'text': '你好'}})
        send.assert_not_called()

    def test_bad_page_number_does_not_crash(self):
        bot.CONTROL.ui['cursor'] = ['m1', 'm2']
        self.call({'update_id': 8, 'callback_query': {'id': '1', 'from': {'id': 123},
                                                      'data': 'mp:cursor:不是数字',
                                                      'message': {'chat': {'id': 123, 'type': 'private'}}}})
        self.assertTrue(any('分页参数无效' in text for text in self.sent), self.sent)

    def test_expired_page_cursor_is_reported(self):
        self.call({'update_id': 9, 'callback_query': {'id': '1', 'from': {'id': 123},
                                                      'data': 'mp:不存在的游标:0',
                                                      'message': {'chat': {'id': 123, 'type': 'private'}}}})
        self.assertTrue(any('已过期' in text for text in self.sent), self.sent)


class StateFileTest(unittest.TestCase):
    """状态文件损坏：宁可拒绝启动，也不能带着半份状态跑起来。"""

    def test_broken_json_refuses_to_start(self):
        root = Path(tempfile.mkdtemp())
        (root / 'bridge-state.json').write_text('{"tasks": [', encoding='utf-8')
        with self.assertRaises(ValueError):
            bot.TaskController(root)

    def test_top_level_array_is_reported_clearly(self):
        root = Path(tempfile.mkdtemp())
        (root / 'bridge-state.json').write_text('[]', encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            bot.TaskController(root)
        self.assertIn('顶层不是对象', str(caught.exception))

    def test_missing_fields_are_filled(self):
        root = Path(tempfile.mkdtemp())
        (root / 'bridge-state.json').write_text(
            json.dumps({'tasks': [{'id': 'abc'}], 'sessions': {'k': {}}}), encoding='utf-8')
        controller = bot.TaskController(root)
        self.assertEqual(controller.data['tasks'][0]['status'], 'interrupted')
        self.assertIn('snapshot', controller.data['tasks'][0])

    def test_running_task_on_restart_becomes_interrupted(self):
        """重启后把 running 标成 interrupted，不能自动重放。"""
        root = Path(tempfile.mkdtemp())
        (root / 'bridge-state.json').write_text(json.dumps({
            'tasks': [{'id': 'abc', 'status': 'running', 'snapshot': {}}],
            'sessions': {'k': {'name': 'k'}}, 'active_session': 'k'}), encoding='utf-8')
        controller = bot.TaskController(root)
        self.assertEqual(controller.data['tasks'][0]['status'], 'interrupted')
        self.assertIn('不会自动重放', controller.data['tasks'][0]['result'])

    def test_persisted_text_is_redacted(self):
        root = Path(tempfile.mkdtemp())
        controller = bot.TaskController(root)
        controller.enqueue(123, 'export GH_TOKEN=' + 'ghp_' + 'B2c3' * 8)
        controller.persist()
        disk = (root / 'bridge-state.json').read_text(encoding='utf-8')
        self.assertNotIn('ghp_B2c3', disk, '状态文件里留下了令牌原文')


class ClockAnomalyTest(EdgeBase):
    def test_footer_clamps_negative_duration(self):
        line = bot.execution_footer('claude', started=time.time() + 3600)
        self.assertIn('耗时：0秒', line)

    def test_task_summary_clamps_negative_duration(self):
        task = self.controller.enqueue(123, '任务')
        with self.controller.lock:
            self.controller.data['tasks'][0].update(started=time.time() + 100)
        summary = self.controller.task_summary(self.controller.data['tasks'][0])
        self.assertIn('耗时：0秒', summary)

    def test_future_expiry_does_not_trigger_renewal(self):
        """设备时钟偏快时，到期时间算出来是负数，不该被当成「必须立刻续」。"""
        from unittest.mock import patch as p
        with p.object(bot, 'credentials') as store:
            store.return_value.get.return_value = {'value': 'x', 'expires_at': time.time() + 99999}
            record, needed = bot.renewal_needed('cloudflare')
        self.assertFalse(needed)


class ReentrancyTest(EdgeBase):
    """同一会话连点两下同一个按钮，不能进两次。"""

    def test_double_click_on_inline_button_runs_once(self):
        bot.send_options(123, '选择', ['新建会话'])
        token = [button['callback_data'] for row in bot.api.call_args.args[1]['reply_markup']['inline_keyboard']
                 for button in row][0]
        update = {'callback_query': {'id': '1', 'from': {'id': 123}, 'data': token,
                                     'message': {'chat': {'id': 123, 'type': 'private'}}}}
        with patch.object(self.controller, 'handle_text') as handle:
            bot.handle_update(dict(update, update_id=100))
            bot.handle_update(dict(update, update_id=100))
        self.assertEqual(handle.call_count, 1, '同一个回调被执行了两次')

    def test_reused_token_second_time_is_reported(self):
        bot.send_options(123, '选择', ['新建会话'])
        token = [button['callback_data'] for row in bot.api.call_args.args[1]['reply_markup']['inline_keyboard']
                 for button in row][0]
        base = {'callback_query': {'id': '1', 'from': {'id': 123}, 'data': token,
                                   'message': {'chat': {'id': 123, 'type': 'private'}}}}
        bot.handle_update(dict(base, update_id=101))
        sent_before = len(self.sent)
        bot.handle_update(dict(base, update_id=102))
        self.assertTrue(any('已失效' in text for text in self.sent[sent_before:]),
                        '重放已用过的按钮没有提示')

    def test_expired_token_is_reported(self):
        bot.send_options(123, '选择', ['新建会话'])
        # send_options 内部会在锁里清理过期条目，构造完再改过期时间，
        # 顺便验证「清理之后点它」这条路径。
        token_name = next(key for key in bot.ACTION_UI if key)
        bot.ACTION_UI[token_name]['expires'] = time.monotonic() - 1
        bot.handle_update({'update_id': 103, 'callback_query': {
            'id': '1', 'from': {'id': 123}, 'data': 'ui:' + token_name,
            'message': {'chat': {'id': 123, 'type': 'private'}}}})
        self.assertTrue(any('已失效' in text for text in self.sent), self.sent)


class AttachmentTest(EdgeBase):
    def test_attachment_enqueues_media_task(self):
        bot.handle_update({'update_id': 200, 'message': {
            'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123},
            'document': {'file_id': 'f1', 'file_name': 'a.txt', 'file_size': 10}}})
        with self.controller.lock:
            kinds = [item['kind'] for item in self.controller.data['tasks']]
        self.assertIn('media', kinds)

    def test_attachment_without_caption_is_still_queued(self):
        bot.handle_update({'update_id': 201, 'message': {
            'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123},
            'photo': [{'file_id': 'p1', 'width': 10, 'height': 10}]}})
        with self.controller.lock:
            self.assertTrue(any(item['kind'] == 'media' for item in self.controller.data['tasks']))


class SensitivePathTest(EdgeBase):
    def test_private_key_is_blocked(self):
        self.assertTrue(bot.sensitive_path(self.root / 'id_rsa'))
        self.assertTrue(bot.sensitive_path(self.root / '.ssh' / 'id_ed25519'))

    def test_credential_files_are_blocked(self):
        for name in ('credentials.json', '.env', 'auth.json', '.netrc', 'sessions.json'):
            with self.subTest(name=name):
                self.assertTrue(bot.sensitive_path(self.root / name),
                                name + ' 是凭据文件，必须拦下')

    def test_normal_file_is_allowed(self):
        self.assertFalse(bot.sensitive_path(self.root / 'report.md'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
