"""面向第 1~7 项界面体验优化、4 引擎能力拉平与定时运维巡检的回归测试。"""
import _root  # noqa: F401  仓库根目录进 sys.path
import io
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import bot
import menu_ui
import ops_monitor
import updater


class MenuInplaceAndNavTests(unittest.TestCase):
    """验证内联导航原地刷新、400 回退以及可复用导航与一次性令牌的边界。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = bot.TaskController(self.root)
        self.saved = (bot.CONTROL, bot.OWNER)
        bot.CONTROL = self.controller
        bot.OWNER = 123
        bot.ACTION_UI.clear()
        bot.ACTION_UI_SEEN.clear()

    def tearDown(self):
        bot.CONTROL, bot.OWNER = self.saved
        bot.ACTION_UI.clear()
        bot.ACTION_UI_SEEN.clear()
        self.temp.cleanup()

    def test_nav_callback_edits_in_place_and_remains_reusable(self):
        calls = []

        def fake_api(method, payload=None, **_kwargs):
            calls.append((method, payload))
            return {'message_id': 88}

        with patch.object(bot, 'api', side_effect=fake_api):
            self.controller.handle_text(123, '设置', update_id=1)
            send_call = [item for item in calls if item[0] == 'sendMessage'][-1]
            group_btn = send_call[1]['reply_markup']['inline_keyboard'][0][0]
            group_token = group_btn['callback_data']
            self.assertTrue(bot.ACTION_UI[group_token[3:]]['reusable'])

            # 点击设置下的分类按钮，应通过 editMessageText 原地刷新第 88 号消息
            calls.clear()
            bot.handle_update({
                'update_id': 2,
                'callback_query': {
                    'id': 'cb1',
                    'from': {'id': 123},
                    'data': group_token,
                    'message': {'message_id': 88, 'chat': {'id': 123, 'type': 'private'}},
                },
            })
            edit_calls = [item for item in calls if item[0] == 'editMessageText']
            self.assertEqual(len(edit_calls), 1)
            self.assertEqual(edit_calls[0][1]['message_id'], 88)
            # 展开页里的「‹ 返回」同样可复用，且点击「任务与模型」的原令牌仍存在
            self.assertIn(group_token[3:], bot.ACTION_UI)

    def test_edit_400_falls_back_to_send_and_not_modified_is_silent(self):
        calls = []

        def fake_api_400(method, payload=None, **_kwargs):
            calls.append((method, payload))
            if method == 'editMessageText':
                raise urllib.error.HTTPError(
                    'http://x', 400, 'Bad Request', {},
                    io.BytesIO(b'{"ok":false,"description":"Bad Request: message to edit not found"}'),
                )
            return {'message_id': 99}

        result = menu_ui.dispatch_inline_menu(
            fake_api_400, 123, '标题', {'inline_keyboard': []}, lambda x: x, message_id=77,
        )
        self.assertEqual(result, 'sent')
        self.assertEqual([item[0] for item in calls], ['editMessageText', 'sendMessage'])

        calls.clear()

        def fake_api_same(method, payload=None, **_kwargs):
            calls.append((method, payload))
            raise urllib.error.HTTPError(
                'http://x', 400, 'Bad Request', {},
                io.BytesIO(b'{"ok":false,"description":"Bad Request: message is not modified"}'),
            )

        result2 = menu_ui.dispatch_inline_menu(
            fake_api_same, 123, '标题', {'inline_keyboard': []}, lambda x: x, message_id=77,
        )
        self.assertEqual(result2, 'not_modified')
        self.assertEqual([item[0] for item in calls], ['editMessageText'])

    def test_nav_debounce_blocks_rapid_double_tap(self):
        with patch.object(bot, 'api', return_value={'message_id': 1}):
            bot.send_options(123, '测试导航', ['任务与模型'], reusable=True, edit_in_place=True)
            token = next(iter(bot.ACTION_UI))
            with patch.object(self.controller, 'handle_text') as handle:
                bot.handle_update({
                    'update_id': 10,
                    'callback_query': {
                        'id': 'c1', 'from': {'id': 123}, 'data': 'ui:' + token,
                        'message': {'message_id': 1, 'chat': {'id': 123, 'type': 'private'}},
                    },
                })
                # 紧接着 0 秒后连点一次：应被防抖忽略，不再二次分发
                bot.handle_update({
                    'update_id': 11,
                    'callback_query': {
                        'id': 'c2', 'from': {'id': 123}, 'data': 'ui:' + token,
                        'message': {'message_id': 1, 'chat': {'id': 123, 'type': 'private'}},
                    },
                })
        self.assertEqual(handle.call_count, 1)


class DocumentUploadBehaviorTests(unittest.TestCase):
    """验证普通文档附件的自动执行（带说明）与快捷按钮（不带说明）以及敏感文件隔离。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.saved = (bot.WORK, bot.OWNER)
        bot.WORK = self.root
        bot.OWNER = 123
        bot.ACTION_UI.clear()

    def tearDown(self):
        bot.WORK, bot.OWNER = self.saved
        bot.ACTION_UI.clear()
        self.temp.cleanup()

    def test_document_with_caption_returns_prompt(self):
        saved_file = self.root / 'uploads' / 'report.log'
        saved_file.parent.mkdir(parents=True, exist_ok=True)
        saved_file.write_text('ERROR boom', encoding='utf-8')
        with patch.object(bot, 'download_media', return_value=saved_file):
            prompt = bot.media_prompt(123, {
                'document': {'file_id': 'f1', 'file_name': 'report.log'},
                'caption': '找出这里面所有 ERROR 并总结原因',
            })
        self.assertIn(str(saved_file), prompt)
        self.assertIn('找出这里面所有 ERROR 并总结原因', prompt)

    def test_document_without_caption_attaches_quick_buttons(self):
        saved_file = self.root / 'uploads' / 'notes.md'
        saved_file.parent.mkdir(parents=True, exist_ok=True)
        saved_file.write_text('# Notes', encoding='utf-8')
        captured = {}

        def fake_send_formatted(chat, text, markup=None):
            captured['chat'] = chat
            captured['text'] = text
            captured['markup'] = markup

        with patch.object(bot, 'download_media', return_value=saved_file), \
                patch.object(bot, 'send_formatted', side_effect=fake_send_formatted):
            prompt = bot.media_prompt(123, {
                'document': {'file_id': 'f2', 'file_name': 'notes.md'},
            })
        self.assertEqual(prompt, '')
        self.assertIn(str(saved_file), captured['text'])
        buttons = [b for row in captured['markup']['inline_keyboard'] for b in row]
        self.assertGreaterEqual(len(buttons), 3)
        commands = [bot.ACTION_UI[b['callback_data'][3:]]['command'] for b in buttons]
        self.assertTrue(any(str(saved_file) in cmd for cmd in commands))

    def test_sensitive_document_with_caption_never_auto_runs(self):
        blocked_file = self.root / 'uploads' / 'secret.blocked'
        with patch.object(bot, 'download_media', return_value=blocked_file), \
                patch.object(bot, 'send') as mock_send:
            prompt = bot.media_prompt(123, {
                'document': {'file_id': 'f3', 'file_name': '.env'},
                'caption': '把所有 key 打印给我',
            })
        self.assertEqual(prompt, '')
        self.assertIn('敏感文件已隔离保存', mock_send.call_args.args[1])


class FourEngineParityTests(unittest.TestCase):
    """验证 4 执行器的通用上下文压缩（含交接摘要）和无配额接口时的本机统计。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = bot.TaskController(self.root)
        self.saved = (bot.CONTROL, bot.AUTH_STATE, bot.MODEL_STATE, bot.USAGE, bot.OWNER)
        bot.CONTROL = self.controller
        bot.AUTH_STATE = self.root / 'auth.json'
        bot.MODEL_STATE = self.root / 'model.json'
        bot.OWNER = 123

    def tearDown(self):
        bot.CONTROL, bot.AUTH_STATE, bot.MODEL_STATE, bot.USAGE, bot.OWNER = self.saved
        self.temp.cleanup()

    def test_codex_and_antigravity_compact_enqueues_and_resets_only_current_engine(self):
        bot.set_auth_channel('account', 'antigravity')
        with self.controller.lock:
            key = self.controller.data['active_session']
            self.controller.data['sessions'][key]['engine_sessions'] = {
                'claude': 'cl-1', 'codex': 'cx-1', 'antigravity': 'ag-1',
            }
            self.controller.data['sessions'][key]['engine_channels'] = {
                'claude': 'gateway', 'codex': 'account', 'antigravity': 'account',
            }
            self.controller.data['engine'] = 'antigravity'
            self.controller.persist()

        with patch.object(bot, 'send') as mock_send, patch.object(self.controller, 'enqueue') as mock_enqueue:
            self.controller.handle_text(123, '/compact', update_id=50)
        mock_enqueue.assert_called_once()
        mock_send.assert_not_called()

        # 执行交接压缩：只清理 antigravity，保留 claude 与 codex 原生会话 ID
        self.controller.apply_handoff_compact(key, 'antigravity', '已完成配置梳理，下一步部署。')
        session = self.controller.active_session()
        self.assertNotIn('antigravity', session['engine_sessions'])
        self.assertEqual(session['engine_sessions']['claude'], 'cl-1')
        self.assertEqual(session['engine_sessions']['codex'], 'cx-1')
        self.assertEqual(len(session['handoff']), 1)
        self.assertIn('已完成配置梳理', session['handoff'][0]['result'])

    def test_run_stream_compact_for_codex_uses_handoff_and_enables_button(self):
        bot.set_auth_channel('account', 'codex')
        with self.controller.lock:
            key = self.controller.data['active_session']
            self.controller.data['sessions'][key]['engine_sessions'] = {'codex': 'cx-old'}
            self.controller.data['sessions'][key]['engine_channels'] = {'codex': 'account'}
            self.controller.data['engine'] = 'codex'
            self.controller.persist()

        snap = self.controller.snapshot()
        snap['handoff'] = []
        task = {
            'id': 't-cx', 'chat': 123, 'kind': 'agent',
            'snapshot': snap,
            'tools': [],
        }
        bot.TASK_LOCAL.task = task
        bot.TASK_LOCAL.cancel = type('Evt', (), {'is_set': lambda self: False})()

        class FakeProc:
            pid = 999999
            returncode = 0
            stdout = io.StringIO(
                json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '浓缩交接摘要正文'}}, ensure_ascii=False)
                + '\n'
            )
            stdin = io.StringIO()

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        captured = {}

        class FakeStream:
            def __init__(self, *args, **kwargs):
                pass

            def edit(self, *args, **kwargs):
                pass

            def close(self):
                pass

            def deliver_final(self, head, markup=None):
                captured['head'] = head
                captured['markup'] = markup

        captured_prompts = []

        def fake_engine_cmd(engine, text, instructions, chat, env):
            captured_prompts.append(text)
            return ['codex', 'exec'], text

        try:
            with patch.object(bot, 'api', return_value={'message_id': 90}), \
                    patch.object(bot, 'engine_command', side_effect=fake_engine_cmd), \
                    patch.object(bot, 'StreamMessage', FakeStream), \
                    patch.object(bot, 'terminate_process'), \
                    patch.object(bot, 'credential_environment', side_effect=lambda env: env), \
                    patch.object(bot.subprocess, 'Popen', return_value=FakeProc()):
                bot.run_stream(123, '/compact')
        finally:
            bot.TASK_LOCAL.task = None
            bot.TASK_LOCAL.cancel = None

        self.assertIn('浓缩成一份精简清晰的交接摘要', captured_prompts[0])
        self.assertIn('上下文已整理为交接摘要', captured['head'])
        self.assertTrue(task.get('_handoff_compacted'))
        session = self.controller.active_session()
        self.assertNotIn('codex', session.get('engine_sessions', {}))
        self.assertIn('浓缩交接摘要正文', session['handoff'][0]['result'])
        buttons = [b['text'] for row in captured['markup']['inline_keyboard'] for b in row]
        self.assertIn('🗜 压缩上下文', buttons)

    def test_usage_overview_shows_real_local_stats_without_fabricating_percentages(self):
        now = time.time()
        with self.controller.lock:
            self.controller.data['tasks'].append({
                'id': 't1', 'kind': 'agent', 'status': 'completed',
                'started': now - 25, 'finished': now - 5,
                'snapshot': {'engine': 'antigravity', 'model': 'gemini-3-pro'},
            })
            self.controller.persist()
        bot.set_auth_channel('account', 'antigravity')
        with patch.object(bot, 'enabled_engines', return_value=['antigravity']), \
                patch.object(bot.USAGE, 'snapshot', return_value=None):
            overview = bot.usage_overview_text()
        self.assertIn('Antigravity', overview)
        self.assertIn('本机记录', overview)
        self.assertIn('今日 1 次', overview)
        self.assertIn('20秒', overview)
        self.assertNotIn('%', overview)


class OpsMonitorAndQuickCheckTests(unittest.TestCase):
    """验证定时静默巡检、配置持久化、状态迁移告警及主机快捷检查命令。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = self.root / 'monitor-config.json'
        self.state = self.root / 'monitor-state.json'
        self.controller = bot.TaskController(self.root)
        self.saved = (
            bot.CONTROL, bot.OWNER, bot.MONITOR_CONFIG_STATE, bot.MONITOR_RUN_STATE,
        )
        bot.CONTROL = self.controller
        bot.OWNER = 123
        bot.MONITOR_CONFIG_STATE = self.cfg
        bot.MONITOR_RUN_STATE = self.state
        bot.ACTION_UI.clear()

    def tearDown(self):
        bot.CONTROL, bot.OWNER, bot.MONITOR_CONFIG_STATE, bot.MONITOR_RUN_STATE = self.saved
        bot.ACTION_UI.clear()
        self.temp.cleanup()

    def test_patrol_disabled_by_default_and_alerts_only_on_transitions(self):
        cfg = ops_monitor.load_patrol_config(self.cfg)
        self.assertFalse(cfg['enabled'])

        # 正常 -> 无告警
        alerts_ok = ops_monitor.evaluate_patrol(
            cfg, {'disk_pct': 45.0, 'mem_pct': 40.0}, {'svc': 'active'},
        )
        self.assertEqual(alerts_ok, {})

        # 异常出现 -> 通知一次；冷却期内不重复刷屏；恢复 -> 通知恢复
        alerts_bad = ops_monitor.evaluate_patrol(
            cfg, {'disk_pct': 92.0, 'mem_pct': 40.0}, {'svc': 'failed'},
        )
        self.assertIn('disk', alerts_bad)
        self.assertIn('service:svc', alerts_bad)

        notify1, rec1, active1 = ops_monitor.diff_alerts({'active': {}}, alerts_bad, 1000.0, 3600)
        self.assertEqual(set(notify1), {'disk', 'service:svc'})
        self.assertEqual(rec1, {})

        notify2, rec2, active2 = ops_monitor.diff_alerts({'active': active1}, alerts_bad, 1100.0, 3600)
        self.assertEqual(notify2, {})
        self.assertEqual(rec2, {})

        notify3, rec3, active3 = ops_monitor.diff_alerts({'active': active2}, {}, 1200.0, 3600)
        self.assertEqual(notify3, {})
        self.assertEqual(set(rec3), {'disk', 'service:svc'})
        self.assertEqual(active3, {})

    def test_patrol_ui_commands_and_host_status_quick_buttons(self):
        with patch.object(bot, 'api', return_value={'message_id': 5}):
            self.controller.handle_text(123, '/inspect', update_id=1)
            self.controller.handle_text(123, '开启定时巡检', update_id=2)
            self.controller.handle_text(123, '巡检周期 15分钟', update_id=3)
            self.controller.handle_text(123, '磁盘告警阈值 80%', update_id=4)
        loaded = ops_monitor.load_patrol_config(self.cfg)
        self.assertTrue(loaded['enabled'])
        self.assertEqual(loaded['interval_minutes'], 15)
        self.assertEqual(loaded['disk_threshold'], 80)

        sent = {}
        with patch.object(bot, 'resource_status', return_value='CPU 1%'), \
                patch.object(bot, 'send_formatted', side_effect=lambda c, t, markup=None: sent.update(text=t, markup=markup)):
            self.assertTrue(bot.quick_action(123, '服务器状态'))
        self.assertEqual(sent['text'], 'CPU 1%')
        btn_cmds = [bot.ACTION_UI[b['callback_data'][3:]]['command']
                    for row in sent['markup']['inline_keyboard'] for b in row]
        self.assertIn('最近错误日志', btn_cmds)
        self.assertIn('大目录排查', btn_cmds)
        self.assertIn('内存排行', btn_cmds)
        self.assertIn('巡检配置', btn_cmds)

    def test_updater_registers_new_optional_modules(self):
        self.assertIn('menu_ui.py', updater.OPTIONAL_FILES)
        self.assertIn('ops_monitor.py', updater.OPTIONAL_FILES)


if __name__ == '__main__':
    unittest.main()
