#!/usr/bin/env python3
"""Antigravity（agy）执行器接入测试。

覆盖注册信息、命令行拼装与环境变量清理、流式事件解析、
谷歌授权登录链路、状态检测与退出登录，以及安装器开关校验。
"""
import _root  # noqa: F401  仓库根目录进 sys.path
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import github_auth as gh


def _load_install():
    spec = importlib.util.spec_from_file_location('install', _root.ROOT / 'install.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AntigravityRegistryTests(unittest.TestCase):
    def test_antigravity_registered_and_in_menus(self):
        self.assertIn('antigravity', bot.ENGINES)
        spec = bot.ENGINES['antigravity']
        self.assertEqual(spec['kind'], 'antigravity')
        self.assertEqual(spec['binary'], 'agy')
        self.assertEqual(spec['button'], '使用Antigravity')
        self.assertIn('登录Antigravity', bot.MENU_GROUPS['设置']['账号登录'])
        self.assertEqual(bot.MENU_LABELS['登录Antigravity'], '登录Antigravity账号')
        self.assertEqual(bot.EFFORT_LEVELS['antigravity'], ('low', 'medium', 'high', 'xhigh', 'max'))

    def test_account_models_catalog_valid(self):
        models = gh.account_models('antigravity')
        self.assertTrue(models)
        ids = [item[0] for item in models]
        self.assertIn('gemini-4-argon-high', ids)
        self.assertIn('gemini-3.8-flash-medium', ids)
        for model_id, display in models:
            self.assertNotEqual(model_id, display)


class AntigravityCommandTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.saved = {name: getattr(bot, name) for name in (
            'WORK', 'AUTH_STATE', 'MODEL_STATE', 'AGENT_HOME', 'CONTROL')}
        bot.WORK = self.folder
        bot.AUTH_STATE = self.folder / 'auth-channel.json'
        bot.MODEL_STATE = self.folder / 'selected-model.json'
        bot.AGENT_HOME = self.folder / 'home'
        bot.CONTROL = bot.TaskController(self.folder)

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(bot, name, value)

    def test_command_flags_and_account_environment(self):
        bot.set_auth_channel('account', 'antigravity')
        bot.set_account_model('antigravity', 'gemini-4-argon-high')
        bot.set_effort('antigravity', 'high')
        env = {
            'HOME': str(self.folder),
            'PATH': '/usr/bin',
            'ANTHROPIC_API_KEY': 'gateway-key',
            'GEMINI_API_KEY': 'gemini-gateway-key',
            'GOOGLE_API_KEY': 'google-key',
            '_IMAGE_SERVER': '1',
            '_IMAGE_CONFIG': '/tmp/img.json',
        }
        with patch.object(bot, 'session_id', return_value='conv-abc-123'):
            cmd, stdin_text = bot.engine_command('antigravity', '检查目录', '请保持简短', 1001, env)

        self.assertIsNone(stdin_text)
        self.assertIn('-p', cmd)
        prompt_arg = cmd[cmd.index('-p') + 1]
        self.assertIn('请保持简短', prompt_arg)
        self.assertIn('检查目录', prompt_arg)
        self.assertEqual(cmd[cmd.index('--output-format') + 1], 'stream-json')
        self.assertIn('--dangerously-skip-permissions', cmd)
        self.assertEqual(cmd[cmd.index('--model') + 1], 'gemini-4-argon-high')
        self.assertEqual(cmd[cmd.index('--effort') + 1], 'high')
        self.assertEqual(cmd[cmd.index('--conversation') + 1], 'conv-abc-123')
        for forbidden in ('--append-system-prompt', '--max-turns', '--verbose',
                          '--include-partial-messages', '--resume', '--mcp-config', '--agents'):
            self.assertNotIn(forbidden, cmd)
        for stripped in ('ANTHROPIC_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY',
                         '_IMAGE_SERVER', '_IMAGE_CONFIG'):
            self.assertNotIn(stripped, env)

    def test_gateway_mode_ignores_foreign_default_model(self):
        bot.set_auth_channel('gateway', 'antigravity')
        env = {'HOME': str(self.folder), '_IMAGE_SERVER': '1', '_IMAGE_CONFIG': '/tmp/x.json'}
        with patch.object(bot, 'current_model', return_value='claude-sonnet-4-6'), \
             patch.object(bot, 'session_id', return_value=None):
            cmd, _ = bot.engine_command('antigravity', '你好', '', 1, env)
        self.assertNotIn('--model', cmd)
        with patch.object(bot, 'current_model', return_value='gemini-3.8-flash-high'), \
             patch.object(bot, 'session_id', return_value=None):
            cmd2, _ = bot.engine_command('antigravity', '你好', '', 1, dict(env))
        self.assertEqual(cmd2[cmd2.index('--model') + 1], 'gemini-3.8-flash-high')


class AntigravityStreamParseTests(unittest.TestCase):
    def _empty_state(self):
        return {
            'content': '', 'thinking': '', 'activity': '等待模型响应',
            'steps': [], 'tools': {}, 'final': None, 'failed': '',
            'session_invalid': False, 'context_used': 0, 'context_window': 0,
        }

    def test_stream_parse_delta_tools_usage_and_result(self):
        state = self._empty_state()
        saved = []
        with patch.object(bot, 'save_session', side_effect=saved.append):
            bot.parse_antigravity_event({'event': 'init', 'conversation_id': 'cid-001'}, state)
            bot.parse_antigravity_event({
                'event': 'step_update',
                'step_update': {'step_index': 1, 'step_type': 'agent_response',
                                'state': 'ACTIVE', 'text_delta': '正在'}
            }, state)
            bot.parse_antigravity_event({
                'event': 'step_update',
                'step_update': {'step_index': 1, 'step_type': 'agent_response',
                                'state': 'ACTIVE', 'text_delta': '检查。'}
            }, state)
            bot.parse_antigravity_event({
                'event': 'step_update',
                'step_update': {'step_index': 2, 'step_type': 'tool', 'state': 'ACTIVE',
                                'tool_name': 'run_command',
                                'tool_info': {'parameters': {'CommandLine': 'ls -la'}}}
            }, state)
            bot.parse_antigravity_event({
                'event': 'step_update',
                'step_update': {'step_index': 2, 'step_type': 'tool', 'state': 'DONE',
                                'status': 'ERROR', 'tool_name': 'run_command',
                                'tool_info': {'is_error': True, 'parameters': {'CommandLine': 'ls -la'}}}
            }, state)
            bot.parse_antigravity_event({
                'event': 'step_update',
                'step_update': {'step_index': 3, 'step_type': 'agent_response',
                                'state': 'DONE', 'text_delta': '完成。',
                                'usage': {'input_tokens': 1200, 'cache_read_tokens': 300, 'output_tokens': 40}}
            }, state)
            bot.parse_antigravity_event({
                'event': 'result',
                'result': {'conversation_id': 'cid-001', 'status': 'SUCCESS', 'response': '正在检查。\n\n完成。'}
            }, state)

        self.assertEqual(saved, ['cid-001', 'cid-001'])
        self.assertEqual(state['content'], '正在检查。\n\n完成。')
        self.assertEqual(len(state['steps']), 1)
        self.assertIn('ls -la', state['steps'][0])
        self.assertTrue(state['steps'][0].endswith(' ✗'))
        self.assertEqual(state['context_used'], 1500)
        self.assertFalse(state['final']['is_error'])

    def test_stream_failure_and_recoverable_error(self):
        state = self._empty_state()
        bot.parse_antigravity_event({
            'event': 'result',
            'result': {'status': 'FAILED', 'error': 'upstream error'}
        }, state)
        self.assertTrue(state['final']['is_error'])
        self.assertEqual(state['failed'], 'upstream error')

        state2 = self._empty_state()
        bot.parse_antigravity_event({'event': 'error', 'error': 'No conversation found with ID x'}, state2)
        self.assertIn('No conversation found', state2['failed'])
        self.assertTrue(state2['session_invalid'])


class AntigravityAuthFlowTests(unittest.TestCase):
    def test_extracts_google_oauth_url_keeping_challenge(self):
        sample = (
            '\x1b[32mYour browser should open automatically. If not:\x1b[0m\n\n'
            ' \x1b]8;;https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=107'
            '&code_challenge=XYZ123&code_challenge_method=S256'
            '&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&response_type=code\x1b\\'
            'https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=107'
            '&code_challenge=XYZ123&code_challenge_method=S256'
            '&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&response_type=code'
            '\x1b]8;;\x1b\\\n'
        )
        prompt = gh.antigravity_login_prompt(sample)
        self.assertIsNotNone(prompt)
        self.assertIn('code_challenge=XYZ123', prompt['url'])
        self.assertIn('redirect_uri=', prompt['url'])
        self.assertNotIn(']8;;', prompt['url'])
        self.assertNotIn('\x1b', prompt['url'])

    def test_run_loop_success_hint_does_not_overwrite_with_cancelled(self):
        """匹配到登录成功提示并置位停止标志时，绝不能在下一轮循环被覆盖成 cancelled。"""
        import io, queue
        manager = gh.LoginManager(timeout=10)
        events = []
        ready_gate = queue.Queue()

        def capture(ev):
            events.append(ev)
            if ev.get('state') == 'ready':
                ready_gate.put(True)

        session = gh._LoginSession('antigravity', 77, capture)

        class ChunkedStdout:
            def __init__(self):
                self.step = 0

            def read(self, _n):
                if self.step == 0:
                    self.step = 1
                    return (b'Select login method:\n'
                            b'https://accounts.google.com/o/oauth2/auth?client_id=107&state=S1\n')
                if self.step == 1:
                    self.step = 2
                    ready_gate.get(timeout=2)
                    return b'OAuth: authenticated successfully\n'
                return b''

        class FakeProc:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = ChunkedStdout()

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

        with patch.object(manager, '_antigravity_authenticated', return_value=False), \
             patch.object(manager, '_spawn', return_value=FakeProc()), \
             patch.object(manager, '_terminate'):
            manager._run(session, '/bin/agy', {'HOME': '/tmp'})

        states = [e['state'] for e in events]
        self.assertIn('ready', states)
        self.assertEqual(states[-1], 'succeeded')
        self.assertNotIn('cancelled', states)

    def test_status_detects_signed_in_and_signed_out(self):
        manager = gh.LoginManager()
        unauth = subprocess.CompletedProcess([], 0, 'Welcome to Antigravity CLI. Please sign in first.', '')
        with patch('github_auth.subprocess.run', return_value=unauth) as run:
            res = manager.status('antigravity', '/bin/agy', {'HOME': '/tmp', 'GEMINI_API_KEY': 'secret'})
        self.assertEqual(run.call_args.args[0], ['/bin/agy', 'models'])
        self.assertNotIn('GEMINI_API_KEY', run.call_args.kwargs['env'])
        self.assertTrue(res['ok'])
        self.assertFalse(res['authenticated'])

        authed = subprocess.CompletedProcess([], 0, 'gemini-4-argon-high\ngemini-3.8-flash-medium\n', '')
        with patch('github_auth.subprocess.run', return_value=authed):
            res2 = manager.status('antigravity', '/bin/agy', {'HOME': '/tmp'})
        self.assertTrue(res2['authenticated'])

    def test_submit_writes_code_to_active_pty_and_checks_chat(self):
        manager = gh.LoginManager()
        session = gh._LoginSession('antigravity', 42, lambda _e: None)
        session.master = 99
        manager._sessions['antigravity'] = session

        mismatch = manager.submit('antigravity', 99, '4/0A-auth-code')
        self.assertEqual(mismatch['state'], 'other_chat')

        written = []
        with patch.object(manager, '_write', side_effect=lambda s, data: written.append(data)):
            ok = manager.submit('antigravity', 42, '  4/0A-auth-code  ')
        self.assertTrue(ok['ok'])
        self.assertEqual(ok['state'], 'submitted')
        self.assertEqual(written, [b'4/0A-auth-code\r'])
        self.assertTrue(session.submitted.is_set())

    def test_advance_selects_default_google_oauth_once(self):
        manager = gh.LoginManager()
        session = gh._LoginSession('antigravity', 1, lambda _e: None)
        writes = []
        with patch.object(manager, '_write', side_effect=lambda s, data: writes.append(data)):
            step = manager._advance_antigravity(session, 'Select login method:\n > 1. Google OAuth', 0)
            step = manager._advance_antigravity(session, 'Select login method:\n > 1. Google OAuth', step)
        self.assertEqual(step, 1)
        self.assertEqual(writes, [b'\r'])

    def test_logout_returns_not_logged_in_when_unauthenticated(self):
        manager = gh.LoginManager()
        unauth = subprocess.CompletedProcess([], 0, 'You are currently not signed in.', '')
        with patch('github_auth.subprocess.run', return_value=unauth):
            res = manager.logout('antigravity', '/bin/agy', {'HOME': '/tmp'})
        self.assertTrue(res['ok'])
        self.assertEqual(res['state'], 'not_logged_in')


class AntigravityInstallerTests(unittest.TestCase):
    def test_installer_registers_antigravity_disabled_by_default(self):
        inst = _load_install()
        self.assertIn('antigravity', inst.ENGINES_ORDER)
        cfg = inst.collect_config({
            'telegram_bot_token': '123456:FAKE_FORMAT_EXAMPLE_ONLY',
            'telegram_owner_id': '123456',
            'chat_base_url': 'https://gateway.example.com',
            'chat_api_key': 'FAKE_FORMAT_EXAMPLE_ONLY',
            'default_model': 'your-model-id',
            'enable_claude': True,
            'enable_codebuddy': True,
        }, yes=True)
        self.assertIs(cfg['enable_antigravity'], False)
        cfg['enable_antigravity'] = True
        env = inst.runtime_environment(cfg, Path('/home/agentbot'))
        self.assertTrue(env['ENABLED_AGENTS'].endswith('antigravity'))


if __name__ == '__main__':
    unittest.main()
