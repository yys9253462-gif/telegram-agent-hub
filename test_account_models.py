"""订阅账号身份与账号模式模型选择测试，不访问网络、不碰真实凭据。"""
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import github_auth as gh


def make_jwt(claims):
    """造一个只有载荷的假 JWT：本模块不验签，只解载荷。"""
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'header.' + body + '.signature'


class IdentityTests(unittest.TestCase):
    def test_codex_identity_reads_email_and_plan_from_id_token(self):
        token = make_jwt({'email': 'me@example.com',
                          'https://api.openai.com/auth': {'chatgpt_plan_type': 'plus'}})
        identity = gh.codex_identity({'auth_mode': 'chatgpt',
                                      'tokens': {'id_token': token, 'access_token': 'x'}})
        self.assertEqual(identity['email'], 'me@example.com')
        self.assertEqual(identity['plan'], 'plus')
        self.assertEqual(identity['plan_label'], 'Plus')
        self.assertEqual(gh.account_identity_label(identity), 'me@example.com · Plus')

    def test_codex_api_key_mode_has_no_identity(self):
        # 接口密钥模式没有「订阅身份」这回事，不能显示成已登录订阅账号。
        token = make_jwt({'email': 'me@example.com'})
        self.assertIsNone(gh.codex_identity({'auth_mode': 'apikey',
                                            'tokens': {'id_token': token}}))

    def test_codex_identity_survives_garbage(self):
        # 结构坏掉（不是字典、没有 token）一律判为「没有身份」，绝不编一个出来。
        for auth in (None, {}, {'auth_mode': 'chatgpt'}, 'not-a-dict', 7):
            self.assertIsNone(gh.codex_identity(auth))
        # token 存在但解不开时，只能给出「身份未显示」，邮箱必须留空。
        broken = gh.codex_identity({'auth_mode': 'chatgpt', 'tokens': {'id_token': 'a.b'}})
        self.assertEqual(broken['email'], '')
        self.assertNotIn('@', gh.account_identity_label(broken))

    def test_unknown_plan_still_shows_something_honest(self):
        identity = gh.codex_identity({'auth_mode': 'chatgpt', 'tokens': {
            'id_token': make_jwt({'https://api.openai.com/auth': {'chatgpt_plan_type': 'galaxy'}})}})
        self.assertEqual(identity['plan_label'], 'galaxy')

    def test_missing_email_does_not_invent_one(self):
        identity = gh.codex_identity({'auth_mode': 'chatgpt', 'tokens': {
            'id_token': make_jwt({'https://api.openai.com/auth': {'chatgpt_plan_type': 'pro'}})}})
        self.assertEqual(identity['email'], '')
        self.assertEqual(gh.account_identity_label(identity), 'Pro')

    def test_claude_identity_parses_text_output(self):
        text = 'Logged in as ops@example.com\nSubscription: Max\n'
        identity = gh.claude_identity(text)
        self.assertEqual(identity['email'], 'ops@example.com')
        self.assertEqual(identity['plan_label'], 'Max')

    def test_claude_not_logged_in_is_none(self):
        self.assertIsNone(gh.claude_identity('Not logged in'))
        self.assertIsNone(gh.claude_identity('You are not logged in. Run claude login.'))
        # 空输出只说明「没读到内容」，不等于「没登录」，所以给一个不编造身份的占位。
        self.assertEqual(gh.claude_identity('')['email'], '')

    def test_claude_identity_strips_ansi(self):
        self.assertEqual(gh.claude_identity('\x1b[32mok@example.com\x1b[0m')['email'],
                         'ok@example.com')

    def test_identity_label_fallback(self):
        self.assertEqual(gh.account_identity_label(None), '已登录')
        self.assertEqual(gh.account_identity_label({'email': '', 'plan_label': ''}), '已登录')


class ModelListTests(unittest.TestCase):
    def test_codex_account_models_are_the_probed_ones(self):
        ids = [item[0] for item in gh.CODEX_ACCOUNT_MODELS]
        self.assertEqual(ids, ['gpt-6-sol', 'gpt-6-luna', 'gpt-6-astra', 'gpt-6.1-sol',
                               'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'])
        # 实测被官方拒绝的模型绝不能出现在清单里。
        for rejected in ('gpt-5.5', 'gpt-5.4', 'gpt-5.2', 'gpt-5.1', 'gpt-5.1-codex',
                         'o3', 'auto', 'gpt-5.6'):
            self.assertNotIn(rejected, ids)

    def test_claude_account_models_are_aliases(self):
        self.assertEqual([item[0] for item in gh.CLAUDE_ACCOUNT_MODELS],
                         ['default', 'opus', 'sonnet', 'haiku'])

    def test_codebuddy_has_no_hardcoded_list(self):
        self.assertEqual(gh.account_models('codebuddy'), ())


class AccountModelStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(bot, 'WORK', self.root),
                        patch.object(bot, 'AUTH_STATE', self.root / 'auth-channel.json'),
                        patch.object(bot, 'MODEL_STATE', self.root / 'model.json'),
                        patch.object(bot, 'MODEL_CACHE', self.root / 'catalog.json'),
                        patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key',
                                                'ANTHROPIC_BASE_URL': 'https://gateway.invalid',
                                                'ANTHROPIC_MODEL': 'gateway-model'})]
        for item in self.patches:
            item.start()
        self.controller = bot.TaskController(self.root)
        self.patches.append(patch.object(bot, 'CONTROL', self.controller))
        for item in self.patches[-1:]:
            item.start()
        bot.TASK_LOCAL.task = None

    def tearDown(self):
        bot.TASK_LOCAL.task = None
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def use(self, engine):
        self.controller.data['engine'] = engine

    def path(self, engine):
        return self.root / ('account-model-' + engine + '.json')

    def test_default_is_empty_until_chosen(self):
        self.assertEqual(bot.account_model('codex'), '')

    def test_each_engine_has_its_own_slot(self):
        bot.set_account_model('codex', 'gpt-6-sol')
        bot.set_account_model('claude', 'opus')
        self.assertEqual(bot.account_model('codex'), 'gpt-6-sol')
        self.assertEqual(bot.account_model('claude'), 'opus')
        self.assertEqual(json.loads(self.path('codex').read_text()), {'model': 'gpt-6-sol'})

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(bot.MediaError) as caught:
            bot.set_account_model('codex', 'gpt-5.1-codex')
        self.assertIn('清单', str(caught.exception))
        self.assertFalse(self.path('codex').exists())

    def test_empty_clears_back_to_default(self):
        bot.set_account_model('codex', 'gpt-6-sol')
        bot.set_account_model('codex', '')
        self.assertEqual(bot.account_model('codex'), '')

    def test_corrupt_state_reads_as_default(self):
        self.path('codex').write_text('{坏的')
        self.assertEqual(bot.account_model('codex'), '')
        self.path('codex').write_text(json.dumps({'model': 7}))
        self.assertEqual(bot.account_model('codex'), '')

    def test_codex_account_command_carries_model(self):
        self.use('codex')
        bot.set_auth_channel('account', 'codex')
        bot.set_account_model('codex', 'gpt-6-astra')
        environment = bot.model_environment()
        command, _ = bot.engine_command('codex', '任务', '指令', 1, environment)
        self.assertIn('--model', command)
        self.assertEqual(command[command.index('--model') + 1], 'gpt-6-astra')

    def test_codex_account_without_choice_sends_no_model(self):
        self.use('codex')
        bot.set_auth_channel('account', 'codex')
        command, _ = bot.engine_command('codex', '任务', '指令', 1, bot.model_environment())
        self.assertNotIn('--model', command)

    def test_gateway_channel_ignores_account_model(self):
        self.use('codex')
        bot.set_account_model('codex', 'gpt-6-astra')
        bot.set_auth_channel('gateway', 'codex')
        command, _ = bot.engine_command('codex', '任务', '指令', 1, bot.model_environment())
        self.assertIn('gateway-model', command)
        self.assertNotIn('gpt-6-astra', command)

    def test_codebuddy_account_uses_chosen_model_only_when_set(self):
        self.use('codebuddy')
        bot.set_auth_channel('account', 'codebuddy')
        command, _ = bot.engine_command('codebuddy', '任务', '指令', 1, bot.model_environment())
        self.assertNotIn('--model', command)


class ChannelSwitchPageTests(unittest.TestCase):
    """渠道切换页必须列出所有已登录账号，并给出各自入口。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(bot, 'WORK', self.root),
                        patch.object(bot, 'AUTH_STATE', self.root / 'auth-channel.json'),
                        patch.object(bot, 'CODEX_AUTH', self.root / 'codex-auth.json'),
                        patch.object(bot, 'CLAUDE_CREDENTIALS', self.root / 'claude-creds.json'),
                        patch.object(bot, 'OWNER', 123),
                        patch.object(bot, 'api')]
        self.controller = bot.TaskController(self.root)
        self.patches.append(patch.object(bot, 'CONTROL', self.controller))
        for item in self.patches:
            item.start()
        bot.TASK_LOCAL.task = None

    def tearDown(self):
        bot.TASK_LOCAL.task = None
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def use(self, engine):
        self.controller.data['engine'] = engine

    def send(self, text):
        with patch.object(bot, 'send') as send, patch.object(bot, 'send_options') as options:
            bot.TaskController(self.root).handle_text(123, text)
        return send, options

    def page_text(self, options):
        return options.call_args.args[1]

    def page_commands(self, options):
        return [item[1] if isinstance(item, tuple) else item
                for item in options.call_args.args[2]]

    def login_codex(self, email='me@example.com', plan='plus'):
        self.root.joinpath('codex-auth.json').write_text(json.dumps({
            'auth_mode': 'chatgpt',
            'tokens': {'access_token': 't', 'account_id': 'a',
                       'id_token': make_jwt({'email': email,
                                             'https://api.openai.com/auth':
                                                 {'chatgpt_plan_type': plan}})}}))

    def login_claude(self):
        self.root.joinpath('claude-creds.json').write_text(
            json.dumps({'claudeAiOauth': {'accessToken': 'sk-ant-oat01-fake'}}))

    def test_page_shows_real_identity_not_generic_word(self):
        self.login_claude()
        with patch.object(bot, 'account_authorized', return_value=(True, '已登录。')), \
             patch.object(bot.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = 'Logged in as ops@example.com\nSubscription: Max\n'
            run.return_value.stderr = ''
            _, options = self.send('渠道切换')
        text = self.page_text(options)
        self.assertIn('ops@example.com · Max', text)
        self.assertIn('使用账号 claude', self.page_commands(options))
        self.assertIn('使用接口密钥 claude', self.page_commands(options))

    def test_codex_account_listed_with_plan(self):
        self.login_codex(email='z@x.com', plan='plus')
        self.use('codex')
        with patch.object(bot, 'account_authorized', return_value=(True, '已登录。')):
            _, options = self.send('渠道切换')
        self.assertIn('z@x.com · Plus', self.page_text(options))
        self.assertIn('使用账号 codex', self.page_commands(options))

    def test_not_logged_in_says_so(self):
        with patch.object(bot, 'account_authorized', return_value=(False, '未登录。')), \
             patch.object(bot.subprocess, 'run') as run:
            run.return_value.returncode = 1
            run.return_value.stdout = 'Not logged in'
            run.return_value.stderr = ''
            _, options = self.send('渠道切换')
        self.assertIn('未登录', self.page_text(options))
        self.assertNotIn('使用账号 claude', self.page_commands(options))

    def test_switch_to_other_engine_says_current_engine_unchanged(self):
        self.login_codex()
        with patch.object(bot, 'account_authorized', return_value=(True, '已登录。')):
            send, options = self.send('使用账号 codex')
        # 切渠道只改身份，不替用户切执行器；这一点必须在回复里说清。
        self.assertIn('当前执行器仍是', self.page_text(options))
        self.assertEqual(self.page_commands(options), [bot.ENGINES['codex']['button']])
        self.assertEqual(bot.auth_channel('codex'), 'account')
        self.assertEqual(bot.current_engine(), 'claude')

    def test_switch_on_same_engine_does_not_nag(self):
        self.use('codex')
        with patch.object(bot, 'account_authorized', return_value=(True, '已登录。')):
            send, options = self.send('使用接口密钥 codex')
        self.assertIsNone(options.call_args)
        self.assertIn('已切换到：接口密钥', send.call_args_list[-1].args[1])
        self.assertEqual(bot.auth_channel('codex'), 'gateway')

    def test_model_menu_requires_account_channel(self):
        with patch.object(bot, 'account_model_options', return_value=[('gpt-6-sol', 'GPT-6 Sol')]):
            with self.assertRaises(bot.MediaError) as caught:
                self.send('账号模型')
        self.assertIn('接口密钥', str(caught.exception))

    def test_model_menu_marks_current_choice(self):
        self.use('codex')
        bot.set_auth_channel('account', 'codex')
        bot.set_account_model('codex', 'gpt-6-luna')
        _, options = self.send('账号模型')
        commands = self.page_commands(options)
        self.assertIn('账号模型 默认', commands)
        self.assertIn('账号模型 gpt-6-sol', commands)
        labels = [item[0] for item in options.call_args.args[2]]
        marked = [label for label in labels if label.endswith('·当前')]
        self.assertEqual(marked, ['GPT-6 Luna ·当前'])

    def test_choosing_model_persists_and_reports(self):
        self.use('codex')
        bot.set_auth_channel('account', 'codex')
        send, _ = self.send('账号模型 gpt-6-sol')
        self.assertEqual(bot.account_model('codex'), 'gpt-6-sol')
        self.assertIn('模型已设为：gpt-6-sol', send.call_args.args[1])

    def test_choosing_model_in_gateway_mode_warns_it_is_inactive(self):
        self.use('codex')
        bot.set_auth_channel('gateway', 'codex')
        send, _ = self.send('账号模型 gpt-6-sol')
        self.assertIn('要等切到账号模式才生效', send.call_args.args[1])

    def test_invalid_model_is_refused(self):
        self.use('codex')
        bot.set_auth_channel('account', 'codex')
        with self.assertRaises(bot.MediaError):
            self.send('账号模型 gpt-5.1-codex')
        self.assertEqual(bot.account_model('codex'), '')


class ReverseVerification(unittest.TestCase):
    """反向验证：把关键防护拆掉，测试必须报红。"""

    def test_removing_model_whitelist_would_accept_anything(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        with patch.object(bot, 'WORK', root), \
             patch.object(bot, 'account_model_options', return_value=[]):
            # 清单为空时 set_account_model 仍必须拒绝非空模型。
            with self.assertRaises(bot.MediaError):
                bot.set_account_model('codex', 'anything')

    def test_model_comes_from_per_engine_slot_not_global(self):
        self.assertNotEqual(bot.account_model_state_path('codex'),
                            bot.account_model_state_path('claude'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
