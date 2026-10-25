#!/usr/bin/env python3
"""退出账号（退出登录）测试。

背景：三家 CLI 的退出机制完全不同，全部在生产机上实测过：
- Claude 有官方 `claude auth logout`。
- Codex **没有 logout 子命令**，登录态是 ~/.codex/auth.json 整份文件；
  只把内部字段置空（tokens=null、auth_mode=null）官方仍报已登录，
  必须整份移除。
- CodeBuddy 也没有 logout，登录态在 *.info 的 auth 段，清空即退出。

这些差异一旦写错，症状很隐蔽：界面说「已退出」，实际还登录着。
所以下面每个用例都盯「文件真的没了 / 真的清了」，而不只是看返回值。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import github_auth


def make_codex_auth(path, with_api_key=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {'auth_mode': 'chatgpt',
            'last_refresh': '2026-10-25T17:07:54Z',
            'tokens': {'access_token': 'at-1234', 'refresh_token': 'rt-5678',
                       'id_token': 'id-9012', 'account_id': 'acc-1'},
            'OPENAI_API_KEY': 'sk-gateway-placeholder' if with_api_key else None}
    path.write_text(json.dumps(data), encoding='utf-8')
    os.chmod(path, 0o600)


def make_codebuddy_auth(folder, name='Tencent-Cloud.coding-copilot.info'):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    data = {'account': {'uid': 'u-1', 'nickname': '机主', 'lastLogin': True},
            'auth': {'accessToken': 'at-' + 'x' * 40, 'refreshToken': 'rt-' + 'y' * 40,
                     'sessionState': 's-1', 'expiresAt': 1795375235983},
            'other': {'keep': 'me'}}
    path.write_text(json.dumps(data), encoding='utf-8')
    os.chmod(path, 0o600)
    return path


class LogoutBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex_auth = self.root / '.codex' / 'auth.json'
        self.cb_dir = self.root / 'codebuddy-auth'
        self.patches = [
            patch.object(github_auth, 'CODEX_AUTH', self.codex_auth),
            patch.object(github_auth, 'CODEBUDDY_AUTH_DIR', self.cb_dir),
        ]
        for item in self.patches:
            item.start()
        self.manager = github_auth.LoginManager()
        self.addCleanup(self._teardown)

    def _teardown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()


class CodexLogoutTest(LogoutBase):
    def test_auth_file_is_removed(self):
        make_codex_auth(self.codex_auth)
        result = self.manager.logout('codex', 'codex', {})
        self.assertTrue(result['ok'], result)
        self.assertFalse(self.codex_auth.exists(), 'auth.json 还在，官方仍会判定为已登录')

    def test_hollowing_out_the_file_is_not_enough(self):
        """这条用例是给以后的自己看的：置空字段不等于退出。

        生产实测：tokens=null、auth_mode=null 之后，`codex login status`
        照样输出「Logged in using ChatGPT」，因为它只看文件在不在。
        """
        make_codex_auth(self.codex_auth)
        data = json.loads(self.codex_auth.read_text())
        data['tokens'] = None
        data['auth_mode'] = None
        self.codex_auth.write_text(json.dumps(data))
        self.assertTrue(self.codex_auth.exists())
        # 现实现必须整份删除，所以调用之后文件一定不存在。
        self.manager.logout('codex', 'codex', {})
        self.assertFalse(self.codex_auth.exists())

    def test_backup_is_kept(self):
        """同一个文件里可能有网关密钥，删之前必须留一份。"""
        make_codex_auth(self.codex_auth, with_api_key=True)
        result = self.manager.logout('codex', 'codex', {})
        self.assertTrue(result['ok'], result)
        backups = list(self.codex_auth.parent.glob('auth.json.logout-*.bak'))
        self.assertEqual(len(backups), 1, '没有留下备份')
        saved = json.loads(backups[0].read_text())
        self.assertTrue(saved['tokens']['access_token'], '备份里应当保留原始内容')

    def test_backup_permissions_are_private(self):
        """备份里带的是令牌原文，权限必须收紧。

        Windows 的 chmod 只能表达只读位，模拟不了 600，所以这条只在 POSIX 上判权限，
        其它平台改为断言「确实调用了 chmod」，保证收紧动作没被删掉。
        """
        make_codex_auth(self.codex_auth)
        with patch.object(github_auth.os, 'chmod', wraps=os.chmod) as chmod:
            self.manager.logout('codex', 'codex', {})
        targets = [call.args for call in chmod.call_args_list]
        self.assertTrue(any(mode == 0o600 for _, mode in targets),
                        '备份没有经过 chmod 600：' + repr(targets))
        if os.name == 'posix':
            backup = list(self.codex_auth.parent.glob('auth.json.logout-*.bak'))[0]
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_already_logged_out_is_reported_not_faked(self):
        result = self.manager.logout('codex', 'codex', {})
        self.assertTrue(result['ok'])
        self.assertEqual(result['state'], 'not_logged_in')

    def test_removed_detail_mentions_backup(self):
        make_codex_auth(self.codex_auth)
        result = self.manager.logout('codex', 'codex', {})
        self.assertIn('备份', result['removed'])


class CodeBuddyLogoutTest(LogoutBase):
    def test_auth_section_is_cleared(self):
        path = make_codebuddy_auth(self.cb_dir)
        result = self.manager.logout('codebuddy', 'codebuddy', {})
        self.assertTrue(result['ok'], result)
        data = json.loads(path.read_text())
        self.assertNotIn('auth', data, 'auth 段还在，官方仍判定为已登录')
        self.assertEqual(data.get('account'), {}, '账号信息没清掉')

    def test_other_settings_are_preserved(self):
        path = make_codebuddy_auth(self.cb_dir)
        self.manager.logout('codebuddy', 'codebuddy', {})
        data = json.loads(path.read_text())
        self.assertEqual(data.get('other'), {'keep': 'me'}, '不该动与登录无关的设置')

    def test_file_is_not_deleted(self):
        """CodeBuddy 那个文件是它的本地存储，直接删会连带丢设置。"""
        path = make_codebuddy_auth(self.cb_dir)
        self.manager.logout('codebuddy', 'codebuddy', {})
        self.assertTrue(path.exists(), '文件被删了，其他设置一起丢了')

    def test_multiple_account_files_are_all_cleared(self):
        make_codebuddy_auth(self.cb_dir, 'Tencent-Cloud.coding-copilot.info')
        make_codebuddy_auth(self.cb_dir, 'Other.coding-copilot.info')
        result = self.manager.logout('codebuddy', 'codebuddy', {})
        self.assertTrue(result['ok'], result)
        for path in self.cb_dir.glob('*.info'):
            self.assertNotIn('auth', json.loads(path.read_text()), path.name + ' 没清干净')

    def test_unrelated_info_file_is_left_alone(self):
        odd = self.cb_dir / 'plugin.info'
        self.cb_dir.mkdir(parents=True, exist_ok=True)
        odd.write_text(json.dumps({'productFeatures': {}}), encoding='utf-8')
        make_codebuddy_auth(self.cb_dir)
        self.manager.logout('codebuddy', 'codebuddy', {})
        self.assertEqual(json.loads(odd.read_text()), {'productFeatures': {}})

    def test_corrupt_file_does_not_abort_the_rest(self):
        self.cb_dir.mkdir(parents=True, exist_ok=True)
        broken = self.cb_dir / 'Broken.info'
        broken.write_text('{不是 json', encoding='utf-8')
        good = make_codebuddy_auth(self.cb_dir)
        result = self.manager.logout('codebuddy', 'codebuddy', {})
        self.assertTrue(result['ok'], result)
        self.assertNotIn('auth', json.loads(good.read_text()))

    def test_missing_folder_reports_not_logged_in(self):
        result = self.manager.logout('codebuddy', 'codebuddy', {})
        self.assertTrue(result['ok'])
        self.assertEqual(result['state'], 'not_logged_in')


class LogoutGuardTest(LogoutBase):
    def test_unknown_provider_is_refused(self):
        result = self.manager.logout('claude', 'claude', {})
        self.assertIn(result['state'], ('unsupported', 'failed'))

    def test_running_login_blocks_logout(self):
        """登录进行到一半时退出，登录进程最后一步会把凭据写回来。"""
        make_codex_auth(self.codex_auth)
        session = github_auth._LoginSession('codex', 123, lambda event: None)
        self.manager._sessions['codex'] = session
        result = self.manager.logout('codex', 'codex', {})
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'login_running')
        self.assertTrue(self.codex_auth.exists(), '被拦下的退出仍然删了文件')

    def test_claude_uses_official_command(self):
        completed = type('R', (), {'returncode': 0, 'stdout': 'ok', 'stderr': ''})()
        with patch.object(github_auth.subprocess, 'run', return_value=completed) as run:
            result = self.manager.logout('claude', '/usr/bin/claude', {})
        self.assertTrue(result['ok'], result)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/claude', 'auth', 'logout'])

    def test_claude_failure_is_surfaced(self):
        completed = type('R', (), {'returncode': 1, 'stdout': '', 'stderr': 'boom'})()
        with patch.object(github_auth.subprocess, 'run', return_value=completed):
            result = self.manager.logout('claude', '/usr/bin/claude', {})
        self.assertFalse(result['ok'])
        self.assertIn('boom', result['message'])

    def test_claude_missing_binary_is_handled(self):
        with patch.object(github_auth.subprocess, 'run', side_effect=OSError('no such file')):
            result = self.manager.logout('claude', '/nonexistent/claude', {})
        self.assertFalse(result['ok'])
        self.assertIn('无法执行', result['message'])


class ChannelAfterLogoutTest(unittest.TestCase):
    """退出账号后的渠道处理：账号渠道已经没有凭据了，必须切回接口密钥。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sent = []
        self.controller = bot.TaskController(self.root)
        self.patches = [
            patch.object(bot, 'CONTROL', self.controller),
            patch.object(bot, 'OWNER', 123),
            patch.object(bot, 'send', side_effect=lambda chat, text: self.sent.append(text)),
            patch.object(bot, 'api', side_effect=lambda method, payload=None: (
                self.sent.append(payload['text']) if isinstance(payload, dict) and payload.get('text') else None)
                or {'message_id': 1}),
            patch.object(bot, 'AUTH_STATE', self.root / 'auth-channel.json'),
            patch.object(bot, 'WORK', self.root),
            patch.dict(bot.os.environ, {'ENABLED_AGENTS': 'claude,codebuddy,codex'}, clear=False),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def press(self, text):
        before = len(self.sent)
        try:
            self.controller.handle_text(123, text)
        except bot.MediaError as exc:
            self.sent.append(str(exc))
        return self.sent[before:]

    def test_account_channel_falls_back_to_gateway(self):
        bot.set_auth_channel('account', 'codex')
        with patch.object(bot, 'account_authorized', return_value=(True, '已登录。')), \
             patch.object(bot.CLI_LOGINS, 'logout', return_value={'ok': True, 'state': 'logged_out', 'removed': ''}):
            self.press('退出账号 codex')
        self.assertEqual(bot.auth_channel('codex'), 'gateway',
                         '账号已退出却还留在账号渠道，任务会拿不到凭据')

    def test_gateway_channel_is_untouched(self):
        bot.set_auth_channel('gateway', 'codex')
        with patch.object(bot, 'account_authorized', return_value=(True, '已登录。')), \
             patch.object(bot.CLI_LOGINS, 'logout', return_value={'ok': True, 'state': 'logged_out', 'removed': ''}):
            self.press('退出账号 codex')
        self.assertEqual(bot.auth_channel('codex'), 'gateway')

    def test_other_engines_channel_is_untouched(self):
        bot.set_auth_channel('account', 'claude')
        bot.set_auth_channel('account', 'codex')
        with patch.object(bot, 'account_authorized', return_value=(True, '已登录。')), \
             patch.object(bot.CLI_LOGINS, 'logout', return_value={'ok': True, 'state': 'logged_out', 'removed': ''}):
            self.press('退出账号 codex')
        self.assertEqual(bot.auth_channel('claude'), 'account', '退出 Codex 把 Claude 的渠道也改了')

    def test_logout_when_not_logged_in_is_refused(self):
        with patch.object(bot, 'account_authorized', return_value=(False, '尚未登录。')):
            replies = self.press('退出账号 codex')
        self.assertTrue(any('没有确认的账号登录' in text for text in replies), replies)

    def test_menu_lists_only_logged_in_engines(self):
        def fake(engine=None):
            return (True, '已登录。') if engine in ('codex',) else (False, '尚未登录。')
        with patch.object(bot, 'account_authorized', side_effect=fake), \
             patch.object(bot, 'subscription_identity', side_effect=lambda engine: {'email': 'a@b.c', 'plan': '', 'plan_label': '订阅'} if engine == 'codex' else None):
            self.press('退出账号')
        markup = bot.api.call_args.args[1]['reply_markup']['inline_keyboard']
        labels = [button['text'] for row in markup for button in row]
        self.assertEqual(labels, ['退出Codex账号'], labels)

    def test_menu_without_any_login_explains_itself(self):
        with patch.object(bot, 'account_authorized', return_value=(False, '尚未登录。')), \
             patch.object(bot, 'subscription_identity', return_value=None):
            replies = self.press('退出账号')
        self.assertTrue(any('没有已登录的账号' in text for text in replies), replies)

    def test_unknown_engine_is_refused(self):
        replies = self.press('退出账号 不存在的执行器')
        self.assertTrue(any('未知执行器' in text for text in replies), replies)

    def test_logout_message_warns_about_gateway_switch(self):
        bot.set_auth_channel('account', 'codex')
        with patch.object(bot, 'account_authorized', return_value=(True, '已登录。')), \
             patch.object(bot.CLI_LOGINS, 'logout', return_value={'ok': True, 'state': 'logged_out', 'removed': '已删除 x'}):
            replies = self.press('退出账号 codex')
        joined = '\n'.join(replies)
        self.assertIn('接口密钥', joined)
        self.assertIn('已删除 x', joined)


if __name__ == '__main__':
    unittest.main(verbosity=2)
