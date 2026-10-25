"""消息按钮与短期操作令牌测试，不访问网络。"""
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bot


class InlineMenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.controller = bot.TaskController(Path(self.temp.name))
        self.patches = [patch.object(bot, 'CONTROL', self.controller),
                        patch.object(bot, 'OWNER', 123), patch.object(bot, 'api'),
                        patch.object(bot, 'send')]
        for item in self.patches:
            item.start()
        bot.ACTION_UI.clear()
        bot.ACTION_UI_SEEN.clear()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        bot.ACTION_UI.clear()
        bot.ACTION_UI_SEEN.clear()
        self.temp.cleanup()

    def buttons(self):
        return [button for row in bot.api.call_args.args[1]['reply_markup']['inline_keyboard']
                for button in row]

    def update(self, data, update_id=1, chat=123, sender=123, kind='private'):
        return {'update_id': update_id, 'callback_query': {'id': str(update_id),
                'from': {'id': sender}, 'data': data,
                'message': {'chat': {'id': chat, 'type': kind}}}}

    def token(self, command='新建会话'):
        bot.send_options(123, '选择操作', [command])
        return self.buttons()[0]['callback_data']

    def test_inline_labels_keep_commands_and_home_keyboard(self):
        with patch.object(bot, 'CONTROL', None):
            bot.send_options(123, '选择', ['模型', '新建会话', '返回主菜单', '收起菜单'])
        markup = bot.api.call_args.args[1]['reply_markup']
        self.assertEqual(set(markup), {'inline_keyboard'})
        buttons = self.buttons()
        self.assertEqual([item['text'] for item in buttons], ['模型设置', '新建会话'])
        for button, command in zip(buttons, ['模型', '新建会话']):
            self.assertLessEqual(len(button['callback_data'].encode()), 64)
            self.assertEqual(bot.ACTION_UI[button['callback_data'][3:]]['command'], command)

    def test_settings_and_auth_are_flat(self):
        for owner, expected in [('设置', ['模型', '选择智能体', '新建会话', '会话列表', '登录Claude', '登录CodeBuddy', '登录Codex', '账号状态', '渠道切换', '账号模型', '检查版本', '更新机器人']),
                                ('更多', ['授权GitHub', '授权Cloudflare', '授权状态', '撤销授权'])]:
            bot.send_group_menu(123, '入口', bot.MENU_GROUPS[owner])
            self.assertEqual([bot.ACTION_UI[button['callback_data'][3:]]['command']
                              for button in self.buttons()], expected)

    def test_one_use_does_not_consume_sibling(self):
        bot.send_options(123, '选择', ['检查版本', '更新机器人'])
        first, second = [item['callback_data'] for item in self.buttons()]
        with patch.object(self.controller, 'handle_text') as handle:
            bot.handle_update(self.update(first))
            bot.handle_update(self.update(first, 2))
            bot.handle_update(self.update(second, 3))
        self.assertEqual(handle.call_args_list[0].args, (123, '检查版本', 1))
        self.assertEqual(handle.call_args_list[1].args, (123, '更新机器人', 3))
        self.assertEqual(handle.call_count, 2)
        self.assertEqual(self.controller.seen, {1, 2, 3})

    def test_owner_private_chat_binding(self):
        token = self.token()
        with patch.object(self.controller, 'handle_text') as handle:
            bot.handle_update(self.update(token, sender=456))
            bot.handle_update(self.update(token, kind='group'))
            bot.handle_update(self.update(token, chat=456))
            handle.assert_not_called()
            self.assertIn(token[3:], bot.ACTION_UI)
            bot.handle_update(self.update(token, 2))
            handle.assert_called_once_with(123, '新建会话', 2)

    def test_expired_restarted_and_forged_tokens(self):
        token = self.token()
        bot.ACTION_UI[token[3:]]['expires'] = time.monotonic() - 1
        with patch.object(self.controller, 'handle_text') as handle:
            bot.handle_update(self.update(token))
            token = self.token()
            bot.ACTION_UI.clear()
            bot.handle_update(self.update(token, 2))
            bot.handle_update(self.update('ui:停止任务 arbitrary', 3))
            bot.handle_update(self.update('停止任务 arbitrary', 4))
            handle.assert_not_called()
        self.assertIn('重新打开菜单', bot.send.call_args.args[1])

    def test_duplicate_update_is_acknowledged_once_each(self):
        token = self.token()
        with patch.object(self.controller, 'handle_text') as handle:
            bot.handle_update(self.update(token))
            bot.handle_update(self.update(token))
        handle.assert_called_once()
        acknowledgements = [call for call in bot.api.call_args_list if call.args[0] == 'answerCallbackQuery']
        self.assertEqual(len(acknowledgements), 2)

    def test_concurrent_double_click_only_dispatches_once(self):
        token = self.token()
        barrier = threading.Barrier(2)
        def click(number):
            barrier.wait()
            bot.handle_update(self.update(token, number))
        with patch.object(self.controller, 'handle_text') as handle:
            threads = [threading.Thread(target=click, args=(number,)) for number in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            handle.assert_called_once()

    def test_update_is_reserved_before_dispatch_consumes_sibling(self):
        bot.send_options(123, '选择', ['检查版本', '更新机器人'])
        first, second = [item['callback_data'] for item in self.buttons()]
        def reenter(*args):
            bot.handle_update(self.update(second))
        with patch.object(self.controller, 'handle_text', side_effect=reenter) as handle:
            bot.handle_update(self.update(first))
            handle.assert_called_once_with(123, '检查版本', 1)
        self.assertIn(second[3:], bot.ACTION_UI)

    def test_enqueue_still_receives_update_id(self):
        token = self.token('模型列表')
        bot.handle_update(self.update(token))
        self.assertEqual(len(self.controller.data['tasks']), 1)
        self.assertEqual(self.controller.data['tasks'][0]['kind'], 'models')
        self.assertIn(1, self.controller.seen)

    def test_capacity_expiry_and_long_command(self):
        command = '切换会话 ' + '会话' * 100
        token = self.token(command)
        self.assertEqual(bot.ACTION_UI[token[3:]]['command'], command)
        bot.ACTION_UI[token[3:]]['expires'] = 0
        for index in range(bot.ACTION_UI_LIMIT + 3):
            self.token(str(index))
        self.assertLessEqual(len(bot.ACTION_UI), bot.ACTION_UI_LIMIT)
        self.assertNotIn(token[3:], bot.ACTION_UI)

    def test_model_choose_and_paging_remain_valid(self):
        model = 'example-model'
        digest = hashlib.sha256(model.encode()).hexdigest()[:24]
        with patch.object(bot, 'read_model_cache', return_value=[{'id': model}]), \
                patch.object(bot, 'atomic_json') as save:
            bot.handle_update(self.update('choose:' + digest))
            save.assert_called_once_with(bot.MODEL_STATE, {'model': model})
        self.controller.ui['list'] = ['first', 'second']
        with patch.object(bot, 'show_model_items') as show:
            bot.handle_update(self.update('mp:list:1', 2))
            show.assert_called_once_with(123, [{'id': 'first'}, {'id': 'second'}], 1, key='list')

    def test_progress_and_stop_offer_per_task_commands(self):
        self.controller.running = {
            key: {'task': {'id': key, 'chat': 123}} for key in ('one', 'two')}
        for command in ('当前任务', '停止'):
            with patch.object(self.controller, 'task_summary', return_value='运行中'), \
                    patch.object(self.controller, 'stop') as stop:
                self.controller.handle_text(123, command)
                stop.assert_not_called()
            buttons = self.buttons()
            # 「查看进度」多带一个「刷新用量」按钮（用量是附加信息，随时可手动重取）；
            # 「停止」只列停止项，不该混进刷新按钮。两个命令的差异要显式断言，
            # 否则以后往其中一个加按钮会悄悄改变另一个的行为。
            expected = ['停止任务 one', '停止任务 two']
            if command == '当前任务':
                expected.append('刷新用量')
            self.assertEqual([bot.ACTION_UI[button['callback_data'][3:]]['command'] for button in buttons],
                             expected)
            with patch.object(self.controller, 'stop', return_value='已停止') as stop:
                bot.handle_update(self.update(buttons[1]['callback_data'], 10 if command == '当前任务' else 11))
                stop.assert_called_once_with('two')

    def test_tuple_label_preserves_complete_session_command(self):
        command = '切换会话 ' + 'a' * 32
        bot.send_options(123, '选择', [('当前 · 工作会话', command)])
        button = self.buttons()[0]
        self.assertEqual(button['text'], '当前 · 工作会话')
        with patch.object(self.controller, 'handle_text') as handle:
            bot.handle_update(self.update(button['callback_data']))
            handle.assert_called_once_with(123, command, 1)

    def test_recent_ten_sessions_switch_without_copying_id(self):
        self.controller.data['sessions'] = {
            'session-id-' + str(index): {'name': '会话' + str(index), 'session_id': None}
            for index in range(12)}
        self.controller.data['active_session'] = 'session-id-11'
        self.controller.handle_text(123, '会话列表')
        buttons = self.buttons()
        self.assertEqual(len(buttons), 10)
        self.assertTrue(buttons[0]['text'].startswith('当前 · 会话11'))
        commands = [bot.ACTION_UI[button['callback_data'][3:]]['command'] for button in buttons]
        self.assertEqual(commands, ['切换会话 session-id-' + str(index) for index in range(11, 1, -1)])
        bot.handle_update(self.update(buttons[1]['callback_data']))
        self.assertEqual(self.controller.data['active_session'], 'session-id-10')

    def test_short_stop_label_routes_complete_task_id(self):
        task_id = '12345678-long-complete-task-id'
        self.controller.running = {task_id: {'task': {'id': task_id, 'chat': 123}}}
        with patch.object(self.controller, 'task_summary', return_value='运行中'):
            self.controller.handle_text(123, '当前任务')
        button = self.buttons()[0]
        self.assertEqual(button['text'], '停止 12345678')
        with patch.object(self.controller, 'stop', return_value='已停止') as stop:
            bot.handle_update(self.update(button['callback_data']))
            stop.assert_called_once_with(task_id)

    def test_update_confirmation_keeps_existing_safety_checks(self):
        token = self.token('确认更新机器人')
        with patch.object(bot, 'confirm_bot_update') as confirm:
            bot.handle_update(self.update(token))
            confirm.assert_called_once_with(123)


if __name__ == '__main__':
    unittest.main(verbosity=2)
