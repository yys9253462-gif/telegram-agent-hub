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
        self.codex_auth = self.root / '.codex' / 'auth.json'
        self.cb_dir = self.root / 'codebuddy-auth'
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
            # 🔴 凭据路径必须一起重定向。退出流程会检查「凭据文件在不在」，
            # 不 patch 就会读到本机真实的 ~/.codex/auth.json 并把它删掉
            # （2026-10-26 真实踩过：test_logout_when_not_logged_in_is_refused
            # 删掉了开发机的 codex 登录凭据，靠 .logout-*.bak 备份才恢复）。
            patch.object(github_auth, 'CODEX_AUTH', self.codex_auth),
            patch.object(github_auth, 'CODEBUDDY_AUTH_DIR', self.cb_dir),
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
        self.assertEqual(labels, ['退出Codex账号（a@b.c）'], labels)

    def test_button_carries_the_identity(self):
        """按钮上要带身份：只写「退出X账号」，点的那一刻还得往上翻才想得起退的是谁。"""
        with patch.object(bot, 'account_authorized', return_value=(True, '已登录。')), \
             patch.object(bot, 'subscription_identity', return_value={
                 'email': '', 'name': '李静', 'plan': '', 'plan_label': '某教育公益版'}):
            self.press('退出账号')
        markup = bot.api.call_args.args[1]['reply_markup']['inline_keyboard']
        labels = [button['text'] for row in markup for button in row]
        self.assertTrue(any('李静' in label for label in labels),
                        '按钮上没有身份：' + str(labels))

    def test_button_has_no_silly_suffix_when_only_status_known(self):
        """只有一个「已登录」状态时，别渲染成「退出X账号（已登录）」。"""
        with patch.object(bot, 'account_authorized', return_value=(True, '已登录。')), \
             patch.object(bot, 'subscription_identity', return_value={
                 'email': '', 'name': '', 'plan': '', 'plan_label': '已登录'}):
            self.press('退出账号')
        markup = bot.api.call_args.args[1]['reply_markup']['inline_keyboard']
        labels = [button['text'] for row in markup for button in row]
        self.assertTrue(all('已登录' not in label for label in labels),
                        '按钮上出现了状态词：' + str(labels))

    def test_menu_without_any_login_explains_itself(self):
        with patch.object(bot, 'account_authorized', return_value=(False, '尚未登录。')), \
             patch.object(bot, 'subscription_identity', return_value=None):
            replies = self.press('退出账号')
        self.assertTrue(any('没有已登录的账号' in text for text in replies), replies)

    def test_unknown_engine_is_refused(self):
        replies = self.press('退出账号 不存在的执行器')
        self.assertTrue(any('未知执行器' in text for text in replies), replies)

    def test_stale_status_does_not_block_logout_when_credential_exists(self):
        """官方状态「假未登录」时，凭据还在就该允许退出。

        CodeBuddy 的状态靠跑一段临时对话来探，那一段会因为超时或网络抖动
        失败，被误判成没登录。这时若只信官方状态，凭据明明还在却退不掉。
        """
        path = make_codebuddy_auth(self.cb_dir)
        self.assertTrue(path.exists())
        with patch.object(bot, 'account_authorized', return_value=(False, '尚未登录或无法确认官方登录状态。')), \
             patch.object(bot.CLI_LOGINS, 'logout_target_present', return_value=True, create=True), \
             patch.object(bot.CLI_LOGINS, 'logout', return_value={'ok': True, 'state': 'logged_out', 'removed': '已清空'}) as logout:
            replies = self.press('退出账号 codebuddy')
        self.assertTrue(logout.called, '凭据还在却拒绝退出：' + '\n'.join(replies))
        self.assertFalse(any('没有确认的账号登录' in text for text in replies), replies)

    def test_no_credential_and_no_status_is_refused(self):
        """两边都没有凭据时仍应拒绝，别给一个假的「已退出」。"""
        with patch.object(bot, 'account_authorized', return_value=(False, '尚未登录。')), \
             patch.object(bot.CLI_LOGINS, 'logout_target_present', return_value=False, create=True), \
             patch.object(bot.CLI_LOGINS, 'logout', return_value={'ok': True, 'state': 'logged_out', 'removed': ''}) as logout:
            replies = self.press('退出账号 codebuddy')
        self.assertFalse(logout.called, '什么都没登录却执行了退出')
        self.assertTrue(any('没有确认的账号登录' in text for text in replies), replies)

    def test_logout_message_warns_about_gateway_switch(self):
        bot.set_auth_channel('account', 'codex')
        with patch.object(bot, 'account_authorized', return_value=(True, '已登录。')), \
             patch.object(bot.CLI_LOGINS, 'logout', return_value={'ok': True, 'state': 'logged_out', 'removed': '已删除 x'}):
            replies = self.press('退出账号 codex')
        joined = '\n'.join(replies)
        self.assertIn('接口密钥', joined)
        self.assertIn('已删除 x', joined)


class LogoutTargetPresentTest(LogoutBase):
    """logout_target_present 只回答「凭据在不在」，不读内容、不改文件。"""

    def test_codex_true_when_auth_file_exists(self):
        make_codex_auth(self.codex_auth)
        self.assertTrue(self.manager.logout_target_present('codex'))

    def test_codex_false_when_missing(self):
        self.assertFalse(self.manager.logout_target_present('codex'))

    def test_codebuddy_true_when_auth_section_present(self):
        make_codebuddy_auth(self.cb_dir)
        self.assertTrue(self.manager.logout_target_present('codebuddy'))

    def test_codebuddy_false_after_logout(self):
        make_codebuddy_auth(self.cb_dir)
        self.manager.logout('codebuddy', 'codebuddy', {})
        self.assertFalse(self.manager.logout_target_present('codebuddy'),
                         '退出后仍认为有凭据，会导致重复退出')

    def test_codebuddy_false_when_file_has_no_auth_section(self):
        self.cb_dir.mkdir(parents=True, exist_ok=True)
        (self.cb_dir / 'other.info').write_text(json.dumps({'other': {'keep': 'me'}}), encoding='utf-8')
        self.assertFalse(self.manager.logout_target_present('codebuddy'))

    def test_claude_has_no_fixed_credential_file(self):
        """Claude 走官方命令，没有固定凭据文件可查。"""
        self.assertFalse(self.manager.logout_target_present('claude'))

    def test_unknown_provider_is_false(self):
        self.assertFalse(self.manager.logout_target_present('nobody'))


class IdentityLabelTest(LogoutBase):
    """身份标签不能把状态词当身份，否则菜单会渲染成「已登录 · 已登录」。"""

    def test_status_word_is_not_used_as_plan(self):
        label = github_auth.account_identity_label(
            {'email': '', 'name': '', 'plan_label': '已登录'})
        self.assertEqual(label, '已登录', '渲染出了重复的状态词')

    def test_label_has_no_duplicate_segments(self):
        """「已登录 · 已登录」这种废话标签，逐段检查才能抓住。

        只断言整串等值不够：兜底值本身就叫「已登录」，两者撞在一起时
        整串看起来还是对的。必须拆开看有没有重复段。
        """
        cases = [
            {'email': '', 'name': '', 'plan_label': '已登录'},
            {'email': '', 'name': '李静', 'plan_label': '已登录'},
            {'email': 'a@b.c', 'name': '', 'plan_label': '登录'},
            {'email': '', 'name': '', 'plan_label': '订阅账号'},
        ]
        for identity in cases:
            label = github_auth.account_identity_label(identity, fallback='订阅账号')
            segments = [part.strip() for part in label.split('·')]
            self.assertEqual(len(segments), len(set(segments)),
                             '标签里有重复段：' + label)
            status_words = {'已登录', '登录'}
            self.assertFalse(status_words & set(segments) and len(segments) > 1,
                             '把状态词当身份段用了：' + label)

    def test_name_is_preferred_over_email(self):
        label = github_auth.account_identity_label(
            {'email': 'a@b.c', 'name': '李静', 'plan_label': 'WorkBuddy教育公益版'})
        self.assertEqual(label, '李静 · WorkBuddy教育公益版')

    def test_email_used_when_no_name(self):
        label = github_auth.account_identity_label(
            {'email': 'a@b.c', 'plan_label': 'Pro'})
        self.assertEqual(label, 'a@b.c · Pro')

    def test_empty_identity_falls_back(self):
        self.assertEqual(github_auth.account_identity_label(None), '已登录')

    def test_codebuddy_identity_reads_display_name(self):
        self.cb_dir.mkdir(parents=True, exist_ok=True)
        (self.cb_dir / 'Tencent-Cloud.coding-copilot.info').write_text(json.dumps({
            'account': {'displayName': '李静', 'enterpriseName': '某教育公益版',
                        'nickname': 'mex6tjwvq4cp'},
            'auth': {'accessToken': 'at-' + 'x' * 40},
        }), encoding='utf-8')
        identity = github_auth.codebuddy_identity()
        self.assertIsNotNone(identity, '有展示名却取不到身份')
        self.assertEqual(identity['name'], '李静')
        self.assertEqual(identity['plan_label'], '某教育公益版')

    def test_codebuddy_identity_ignores_random_nickname(self):
        """nickname 是随机串，拿来当身份只会让人更糊涂。"""
        self.cb_dir.mkdir(parents=True, exist_ok=True)
        (self.cb_dir / 'x.info').write_text(json.dumps({
            'account': {'nickname': 'mex6tjwvq4cp'},
            'auth': {'accessToken': 'at-' + 'x' * 40},
        }), encoding='utf-8')
        self.assertIsNone(github_auth.codebuddy_identity())

    def test_codebuddy_identity_never_leaks_token(self):
        self.cb_dir.mkdir(parents=True, exist_ok=True)
        secret = 'at-SECRET-THIS-MUST-NOT-LEAK'
        (self.cb_dir / 'x.info').write_text(json.dumps({
            'account': {'displayName': '机主'},
            'auth': {'accessToken': secret, 'refreshToken': secret},
        }), encoding='utf-8')
        identity = github_auth.codebuddy_identity()
        self.assertNotIn(secret, json.dumps(identity, ensure_ascii=False))

    def test_codebuddy_identity_missing_folder_is_none(self):
        self.assertIsNone(github_auth.codebuddy_identity())

    def test_codebuddy_identity_survives_corrupt_file(self):
        self.cb_dir.mkdir(parents=True, exist_ok=True)
        (self.cb_dir / 'broken.info').write_text('{not json', encoding='utf-8')
        (self.cb_dir / 'good.info').write_text(json.dumps({
            'account': {'displayName': '机主'}, 'auth': {'accessToken': 'x'}}), encoding='utf-8')
        identity = github_auth.codebuddy_identity()
        self.assertIsNotNone(identity, '一个坏文件不该让整份身份读不到')
        self.assertEqual(identity['name'], '机主')



class CredentialSafetyGuardTest(unittest.TestCase):
    """守卫：任何测试都不许让凭据路径指向真实 HOME。

    2026-10-26 的真实事故：退出流程新增「凭据文件在不在」的判据后，
    test_logout_when_not_logged_in_is_refused 没有重定向凭据路径，
    于是读到了本机真实的 ~/.codex/auth.json 并把它删掉。
    那次靠退出前的 .logout-*.bak 备份才恢复，但「靠备份兜底」不是设计。

    这个用例把「凭据路径必须落在临时目录」变成硬约束：
    在真实代码里跑一遍登录态查询与退出目标检查，路径若指向 HOME 就报红。
    """

    REAL_PATHS = (
        os.path.join(os.path.expanduser('~'), '.codex', 'auth.json'),
        os.path.join(os.path.expanduser('~'), '.local', 'share',
                     'CodeBuddyExtension', 'Data', 'Public', 'auth'),
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex_auth = self.root / '.codex' / 'auth.json'
        self.cb_dir = self.root / 'codebuddy-auth'
        # 先确认临时目录本身与真实 HOME 无关，免得这条守卫自己失效。
        for real in self.REAL_PATHS:
            self.assertFalse(str(self.root).startswith(os.path.dirname(real)),
                             '临时目录竟然落在真实凭据目录下：' + str(self.root))
        self.patches = [
            patch.object(github_auth, 'CODEX_AUTH', self.codex_auth),
            patch.object(github_auth, 'CODEBUDDY_AUTH_DIR', self.cb_dir),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_module_paths_point_into_temp_dir(self):
        self.assertTrue(str(github_auth.CODEX_AUTH).startswith(str(self.root)))
        self.assertTrue(str(github_auth.CODEBUDDY_AUTH_DIR).startswith(str(self.root)))

    def test_logout_writes_only_inside_temp_dir(self):
        """真跑一次退出，确认真实凭据路径一个字节都没动过。"""
        before = {}
        for real in self.REAL_PATHS:
            if os.path.exists(real):
                st = os.stat(real)
                before[real] = (st.st_size, st.st_mtime)
        make_codex_auth(self.codex_auth)
        make_codebuddy_auth(self.cb_dir)
        manager = github_auth.LoginManager()
        manager.logout('codex', 'codex', {})
        manager.logout('codebuddy', 'codebuddy', {})
        for real, snapshot in before.items():
            st = os.stat(real)
            self.assertEqual((st.st_size, st.st_mtime), snapshot,
                             '真实凭据被测试改动了：' + real)

if __name__ == '__main__':
    unittest.main(verbosity=2)
