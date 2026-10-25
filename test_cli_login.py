"""官方 CLI 登录流程测试：只用内存替身，不接触任何真实账号。"""
import io
import subprocess
import threading
import unittest
from unittest.mock import patch

from github_auth import (LoginManager, codebuddy_login_prompt, codex_login_prompt,
                         login_clean_env)


class FakeProcess:
    def __init__(self, output=b'', returncode=0):
        self.stdout = io.BytesIO(output)
        self.stdin = io.BytesIO()
        self.returncode = returncode
        self.pid = 123456
        self.waited = False
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            raise subprocess.TimeoutExpired('官方CLI', timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    kill = terminate

    def send_signal(self, value):
        self.terminate()


class LoginPromptTests(unittest.TestCase):
    def test_codex_prompt_reads_official_url_and_code(self):
        text = '\x1b[32mhttps://auth.openai.com/codex/device\x1b[0m\nABCD-1234\n'
        self.assertEqual(codex_login_prompt(text),
                         {'url': 'https://auth.openai.com/codex/device', 'code': 'ABCD-1234'})

    def test_codex_prompt_rejects_other_hosts(self):
        for url in ('https://auth.openai.com.evil.test/codex/device',
                    'https://auth.openai.com@evil.test/codex/device',
                    'https://evil.test/codex/device',
                    'http://auth.openai.com/codex/device'):
            self.assertIsNone(codex_login_prompt(url + '\nABCD-1234'))

    def test_codex_prompt_hides_query_values(self):
        text = 'https://auth.openai.com/codex/device?state=secretvalue\nABCD-1234'
        prompt = codex_login_prompt(text)
        self.assertNotIn('secretvalue', prompt['url'])
        self.assertIn('state=[已隐藏]', prompt['url'])

    def test_codebuddy_prompt_uses_official_host_only(self):
        text = 'Browser didn\'t open? Use the url below to sign in:\n' \
               'https://copilot.tencent.com/login?platform=x&state=secret\n'
        prompt = codebuddy_login_prompt(text)
        self.assertEqual(prompt['url'],
                         'https://copilot.tencent.com/login?platform=x&state=[已隐藏]')
        self.assertIsNone(codebuddy_login_prompt('https://evil.test/login?state=x'))

    def test_clean_environment_keeps_home_and_drops_gateway(self):
        environment = login_clean_env({'HOME': '/home/account', 'PATH': '/bin',
                                       'OPENAI_API_KEY': 'secret', 'OPENAI_BASE_URL': 'secret',
                                       'ANTHROPIC_AUTH_TOKEN': 'secret', 'ANTHROPIC_BASE_URL': 'secret',
                                       'CODEBUDDY_API_KEY': 'secret'})
        self.assertEqual(environment, {'HOME': '/home/account', 'PATH': '/bin'})


class LoginManagerTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.done = threading.Event()

    def notify(self, event):
        self.events.append(event)
        if event['state'] in ('succeeded', 'failed', 'timeout', 'cancelled', 'authenticated'):
            self.done.set()

    def test_codex_uses_official_device_auth_and_reports_success(self):
        manager = LoginManager()
        process = FakeProcess(b'https://auth.openai.com/codex/device\nABCD-1234\nprivate-tail')
        with patch.object(manager, '_spawn', return_value=process) as spawn, \
                patch('builtins.open', side_effect=AssertionError('禁止读取凭据')):
            self.assertTrue(manager.start('codex', 12, '/bin/codex', {'HOME': '/account'}, self.notify)['ok'])
            self.assertTrue(self.done.wait(3))
        self.assertEqual(spawn.call_args.args[1], ['/bin/codex', 'login', '--device-auth'])
        self.assertEqual([event['state'] for event in self.events], ['ready', 'succeeded'])
        self.assertEqual(self.events[0]['code'], 'ABCD-1234')
        self.assertNotIn('private-tail', repr(self.events))

    def test_codex_nonzero_exit_reports_failure(self):
        manager = LoginManager()
        process = FakeProcess(b'', 1)
        with patch.object(manager, '_spawn', return_value=process):
            manager.start('codex', 12, '/bin/codex', {'HOME': '/account'}, self.notify)
            self.assertTrue(self.done.wait(3))
        self.assertEqual(self.events[-1]['state'], 'failed')

    def test_same_provider_allows_only_one_session(self):
        """同一执行器共用一个账号目录，两个聊天不能同时登录。"""
        manager = LoginManager()
        process = FakeProcess(returncode=None)
        with patch.object(manager, '_spawn', return_value=process), \
                patch('github_auth.os.killpg', side_effect=ProcessLookupError, create=True):
            self.assertTrue(manager.start('codex', 1, '/bin/codex', {}, self.notify)['ok'])
            self.assertEqual(manager.start('codex', 1, '/bin/codex', {}, self.notify)['state'],
                             'already_running')
            self.assertEqual(manager.start('codex', 2, '/bin/codex', {}, self.notify)['state'],
                             'already_running')
            self.assertEqual(manager.cancel('codex', 2)['state'], 'other_chat')
            self.assertTrue(manager.cancel('codex', 1)['ok'])
            self.assertTrue(self.done.wait(3))

    def test_cancel_marks_session_cancelled(self):
        manager = LoginManager()
        process = FakeProcess(returncode=None)
        with patch.object(manager, '_spawn', return_value=process), \
                patch('github_auth.os.killpg', side_effect=ProcessLookupError, create=True):
            manager.start('codex', 1, '/bin/codex', {}, self.notify)
            self.assertTrue(manager.cancel('codex', 1)['ok'])
            self.assertTrue(self.done.wait(3))
        self.assertEqual(self.events[-1]['state'], 'cancelled')
        self.assertTrue(process.terminated or process.waited)

    def test_timeout_stops_process_without_leaking_output(self):
        manager = LoginManager(timeout=0.05)
        process = FakeProcess(b'private-output', None)
        with patch.object(manager, '_spawn', return_value=process), \
                patch('github_auth.os.killpg', side_effect=lambda *args: process.terminate(), create=True):
            manager.start('codex', 1, '/bin/codex', {}, self.notify)
            self.assertTrue(self.done.wait(3))
        self.assertEqual(self.events[-1]['state'], 'timeout')
        self.assertNotIn('private-output', repr(self.events))

    def test_codebuddy_state_machine_confirms_official_prompts(self):
        """官方界面：先信任目录，再选登录方式，然后给出浏览器链接。"""
        manager = LoginManager()
        process = FakeProcess(
            b'Do you trust the files in this folder?\nSelect login method:\n'
            b'Log in via Chinese Site\nGenerating login URL...\n'
            b'Browser didn\'t open? Use the url below to sign in:\n'
            b'https://copilot.tencent.com/login?platform=x&state=secret\n'
            b'Waiting for auth...\n', None)
        writes = []
        with patch('github_auth.os.name', 'posix'), \
                patch('github_auth.os.killpg', side_effect=lambda *args: process.terminate(), create=True), \
                patch.object(manager, '_write', side_effect=lambda session, data: writes.append(data)), \
                patch.object(manager, '_spawn', return_value=process):
            self.assertTrue(manager.start('codebuddy', 7, '/bin/codebuddy', {}, self.notify)['ok'])
            ready = threading.Event()
            for _ in range(50):
                if any(event['state'] == 'ready' for event in self.events):
                    ready.set()
                    break
                threading.Event().wait(0.05)
            self.assertTrue(ready.is_set(), '官方链接出现后必须发出 ready 事件')
            manager.cancel('codebuddy', 7)
            self.done.wait(2)
        self.assertEqual(writes[:2], [b'\r', b'\r'])
        ready_event = next(event for event in self.events if event['state'] == 'ready')
        self.assertNotIn('secret', ready_event['url'])
        self.assertIn('copilot.tencent.com/login', ready_event['url'])

    def test_submit_is_rejected_for_both_providers(self):
        manager = LoginManager()
        for provider in ('codex', 'codebuddy'):
            result = manager.submit(provider, 1, 'ABCD-1234')
            self.assertEqual(result['state'], 'unsupported')
            self.assertIn('浏览器', result['message'])

    def test_codex_status_requires_official_account_hint(self):
        manager = LoginManager()
        completed = subprocess.CompletedProcess([], 0, 'Logged in using ChatGPT', '')
        with patch('github_auth.subprocess.run', return_value=completed) as run:
            result = manager.status('codex', '/bin/codex', {'HOME': '/account'})
        self.assertEqual(run.call_args.args[0], ['/bin/codex', 'login', 'status'])
        self.assertTrue(result['authenticated'])
        self.assertNotIn('ChatGPT', repr(result))

    def test_codex_status_rejects_api_key_only_login(self):
        manager = LoginManager()
        completed = subprocess.CompletedProcess([], 0, 'private-token', '')
        with patch('github_auth.subprocess.run', return_value=completed):
            result = manager.status('codex', '/bin/codex', {'HOME': '/account'})
        self.assertFalse(result['authenticated'])
        self.assertNotIn('private-token', repr(result))

    def test_codebuddy_status_uses_official_behaviour(self):
        manager = LoginManager()
        message = 'Authentication required. Please use /login command to sign in to your account'
        completed = subprocess.CompletedProcess([], 0, '', message)
        with patch('github_auth.subprocess.run', return_value=completed) as run:
            result = manager.status('codebuddy', '/bin/codebuddy', {'HOME': '/account'})
        self.assertFalse(result['authenticated'])
        self.assertEqual(run.call_args.args[0][1], '-p')

    def test_codebuddy_status_reports_authenticated_after_login(self):
        manager = LoginManager()
        completed = subprocess.CompletedProcess([], 0, 'normal', '')
        with patch('github_auth.subprocess.run', return_value=completed):
            result = manager.status('codebuddy', '/bin/codebuddy', {'HOME': '/account'})
        self.assertTrue(result['authenticated'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
