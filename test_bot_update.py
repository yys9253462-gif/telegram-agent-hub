"""机器人更新交互测试，不访问网络或启动真实服务。"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bot


class BotUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.real_watch = bot.watch_update_result
        self.patches = [patch.object(bot, 'APP_DIR', self.root),
                        patch.object(bot, 'WORK', self.root),
                        patch.object(bot, 'OWNER', 123),
                        patch.object(bot, 'send'), patch.object(bot, 'send_options'),
                        patch.object(bot, 'watch_update_result')]
        for item in self.patches:
            item.start()
        self.controller = SimpleNamespace(lock=threading.RLock(), updating=False, update_baseline=None,
                                          running={}, data={'tasks': []})
        self.control_patch = patch.object(bot, 'CONTROL', self.controller)
        self.control_patch.start()
        bot.UPDATE_PENDING.clear()
        self.updater = SimpleNamespace(
            check=Mock(return_value={'current': 'a' * 40, 'latest': 'b' * 40,
                                     'update_available': True, 'local_modified': False}),
            load_install=Mock(return_value={'service': 'my-hub.service', 'python': '/usr/bin/python3',
                                           'app_dir': str(self.root), 'work_dir': str(self.root)}))
        self.module_patch = patch.object(bot, 'update_module', return_value=self.updater)
        self.module_patch.start()

    def tearDown(self):
        self.module_patch.stop()
        self.control_patch.stop()
        for item in reversed(self.patches):
            item.stop()
        bot.UPDATE_PENDING.clear()
        self.temp.cleanup()

    def prepare(self):
        bot.UPDATE_PENDING[123] = time.monotonic() + 300

    def test_check_runs_outside_queue_and_prepares_confirmation(self):
        finished = threading.Event()
        bot.send_options.side_effect = lambda *args: finished.set()
        bot.check_bot_version(123, prepare=True)
        self.assertTrue(finished.wait(2))
        with bot.UPDATE_LOCK:
            pass
        self.updater.check.assert_called_once_with(self.root)
        args = bot.send_options.call_args.args
        self.assertIn('aaaaaaaaaaaa', args[1])
        self.assertEqual(args[2], ['确认更新机器人', '检查版本'])
        self.assertGreater(bot.UPDATE_PENDING[123], time.monotonic())
        self.assertEqual(self.controller.data['tasks'], [])

    def test_local_changes_prevent_confirmation(self):
        finished = threading.Event()
        self.updater.check.return_value['local_modified'] = True
        self.prepare()
        bot.send.side_effect = lambda *args: finished.set()
        bot.check_bot_version(123, prepare=True)
        self.assertTrue(finished.wait(2))
        with bot.UPDATE_LOCK:
            pass
        self.assertNotIn(123, bot.UPDATE_PENDING)
        bot.send_options.assert_not_called()
        self.assertIn('本地修改', bot.send.call_args.args[1])

    def test_non_owner_does_not_check_or_notify(self):
        bot.check_bot_version(456, prepare=True)
        self.updater.check.assert_not_called()
        bot.send.assert_not_called()
        bot.send_options.assert_not_called()

    def test_repeated_check_is_throttled(self):
        with bot.UPDATE_LOCK:
            bot.check_bot_version(123)
        self.updater.check.assert_not_called()
        self.assertIn('稍后', bot.send.call_args.args[1])

    def test_confirm_requires_matching_chat_and_fresh_pending(self):
        self.prepare()
        with patch.object(bot.subprocess, 'run') as run:
            bot.confirm_bot_update(456)
            bot.UPDATE_PENDING[123] = time.monotonic() - 1
            bot.confirm_bot_update(123)
        run.assert_not_called()
        self.assertFalse(self.controller.updating)

    def test_active_tasks_prevent_update(self):
        for status in ('queued', 'running'):
            with self.subTest(status=status), patch.object(bot.subprocess, 'run') as run:
                self.prepare()
                self.controller.data['tasks'] = [{'status': status}]
                bot.confirm_bot_update(123)
                run.assert_not_called()
                self.assertFalse(self.controller.updating)

    def test_confirm_launches_fixed_transient_service(self):
        self.prepare()
        with patch.object(bot.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            bot.confirm_bot_update(123)
        self.assertEqual(run.call_args.args[0], [
            'sudo', '-n', 'systemd-run', '--unit=my-hub-updater', '--collect',
            '--property=Type=exec', '/usr/bin/python3', str(self.root / 'updater.py'),
            '--apply', '--app-dir', str(self.root)])
        self.assertEqual(run.call_args.kwargs, {'timeout': 15, 'capture_output': True})
        self.assertTrue(self.controller.updating)
        self.assertNotIn(123, bot.UPDATE_PENDING)

    def test_failed_launch_unblocks_enqueue_and_shows_admin_command(self):
        self.prepare()
        with patch.object(bot.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            bot.confirm_bot_update(123)
        self.assertFalse(self.controller.updating)
        self.assertIn('sudo systemd-run', bot.send.call_args.args[1])
        self.assertIn(str(self.root / 'updater.py'), bot.send.call_args.args[1])

    def test_invalid_metadata_unblocks_enqueue(self):
        self.prepare()
        self.updater.load_install.side_effect = ValueError('权限不安全')
        with patch.object(bot.subprocess, 'run') as run:
            bot.confirm_bot_update(123)
        run.assert_not_called()
        self.assertFalse(self.controller.updating)

    def test_enqueue_rejected_under_controller_lock(self):
        control = bot.TaskController(self.root)
        control.updating = True
        with self.assertRaisesRegex(bot.MediaError, '正在更新'):
            control.enqueue(123, '新任务')
        self.assertEqual(control.data['tasks'], [])

    def test_restart_notifies_owner_once_without_modifying_result(self):
        result = self.root / 'update-result.json'
        result.write_text(json.dumps({'success': True, 'current': 'a' * 40}), encoding='utf-8')
        original = result.read_bytes()
        bot.notify_update_result()
        bot.notify_update_result()
        bot.send.assert_called_once_with(123, '机器人更新成功。\n当前版本：aaaaaaaaaaaa')
        self.assertEqual(result.read_bytes(), original)
        self.assertTrue((self.root / 'update-notice.json').exists())

    def test_watch_new_failure_result_releases_gate_and_notifies(self):
        result = self.root / 'update-result.json'
        result.write_text(json.dumps({'success': False}), encoding='utf-8')
        self.controller.updating = True
        completed = threading.Event()
        real_notify = bot.notify_update_result
        def notify():
            value = real_notify()
            completed.set()
            return value
        with patch.object(bot, 'notify_update_result', side_effect=notify):
            self.real_watch('旧结果', self.controller)
            self.assertTrue(completed.wait(2))
        self.assertFalse(self.controller.updating)
        self.assertEqual(bot.send.call_args.args[0], 123)
        self.assertIn('失败', bot.send.call_args.args[1])

    def test_startup_freezes_only_when_updater_active(self):
        with patch.object(bot.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            bot.startup_update_watch()
        self.assertTrue(self.controller.updating)
        self.assertEqual(run.call_args.args[0],
                         ['systemctl', 'is-active', '--quiet', 'my-hub-updater.service'])
        bot.watch_update_result.assert_called_once_with(None, self.controller)
        bot.watch_update_result.reset_mock()
        self.controller.updating = False
        with patch.object(bot.subprocess, 'run', return_value=SimpleNamespace(returncode=3)):
            bot.startup_update_watch()
        self.assertFalse(self.controller.updating)
        bot.watch_update_result.assert_called_once_with()

    def test_manual_check_recovers_gate_after_late_result(self):
        self.controller.updating = True
        self.controller.update_baseline = '旧结果'
        (self.root / 'update-result.json').write_text(json.dumps({
            'success': False, 'message': '下载失败', 'backup': '/opt/backups/hub'}), encoding='utf-8')
        completed = threading.Event()
        bot.send.side_effect = lambda *args: completed.set() if '最新版本' in args[1] else None
        bot.check_bot_version(123)
        self.assertTrue(completed.wait(2))
        with bot.UPDATE_LOCK:
            pass
        self.assertFalse(self.controller.updating)
        notice = bot.send.call_args_list[0].args[1]
        self.assertIn('下载失败', notice)
        self.assertIn('/opt/backups/hub', notice)

    def test_same_old_result_does_not_release_gate(self):
        (self.root / 'update-result.json').write_text(json.dumps({'success': False}), encoding='utf-8')
        self.controller.updating = True
        baseline = bot.update_result_stamp()
        self.assertFalse(bot.reconcile_update_result(self.controller, baseline))
        self.assertTrue(self.controller.updating)

    def test_watch_waits_for_result_after_startup(self):
        completed = threading.Event()
        class ImmediateEvent:
            def wait(self, timeout):
                (self_root / 'update-result.json').write_text(
                    json.dumps({'success': True, 'commit': 'c' * 40}), encoding='utf-8')
        self_root = self.root
        real_notify = bot.notify_update_result
        def notify():
            value = real_notify()
            if value:
                completed.set()
            return value
        with patch.object(bot, 'notify_update_result', side_effect=notify), \
                patch.object(bot.threading, 'Event', return_value=ImmediateEvent()), \
                patch.object(bot.threading, 'Thread') as thread:
            self.real_watch()
            thread.call_args.kwargs['target']()
        self.assertTrue(completed.is_set())
        bot.send.assert_called_once_with(123, '机器人更新成功。\n当前版本：cccccccccccc')

    def test_menu_and_handle_text_do_not_enqueue(self):
        self.assertEqual(bot.MENU_GROUPS['设置']['版本更新'], ['检查版本', '更新机器人'])
        control = bot.TaskController(self.root)
        with patch.object(bot, 'check_bot_version') as check, patch.object(control, 'enqueue') as enqueue:
            control.handle_text(123, '检查版本')
        check.assert_called_once_with(123, prepare=False)
        enqueue.assert_not_called()


if __name__ == '__main__':
    unittest.main()
