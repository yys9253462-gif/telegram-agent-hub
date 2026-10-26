"""官方 CLI 登录流程测试：只用内存替身，不接触任何真实账号。"""
import _root  # noqa: F401  仓库根目录进 sys.path
import io
import os
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

import github_auth
from github_auth import (LoginManager, codebuddy_login_prompt, codex_login_prompt,
                         login_clean_env, login_relevant)


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

    def test_codebuddy_prompt_keeps_state_so_the_link_actually_works(self):
        """state 是 CLI 会话标识，抹掉它链接就废了：能打开，但登完回不来。
        2026-10-26 真实反馈：用户拿到的链接是 state=[已隐藏]，无法登录。"""
        text = 'Browser didn\'t open? Use the url below to sign in:\n' \
               'https://copilot.tencent.com/login?platform=x&state=7bc0f169-8f51-464d-9e9e-421000fdd18d&version=2.176.0\n'
        prompt = codebuddy_login_prompt(text)
        self.assertEqual(prompt['url'],
                         'https://copilot.tencent.com/login?platform=x'
                         '&state=7bc0f169-8f51-464d-9e9e-421000fdd18d&version=2.176.0')
        self.assertIsNone(codebuddy_login_prompt('https://evil.test/login?state=x'))

    def test_codebuddy_prompt_still_hides_real_secrets(self):
        text = 'https://copilot.tencent.com/login?platform=x&state=abc-123&code=SECRETCODE&access_token=SECRETTOKEN'
        url = codebuddy_login_prompt(text)['url']
        self.assertIn('state=abc-123', url)
        self.assertNotIn('SECRETCODE', url)
        self.assertNotIn('SECRETTOKEN', url)

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

    def test_codex_failure_is_not_rescued_by_a_logged_in_codebuddy(self):
        """Codex 登录失败不能因为机器上装着已登录的 CodeBuddy 就被说成成功。

        2026-10-26 服务器上真实踩到：为了修「登录成功却报已取消」，
        让退出码非 0 时去问官方命令要结论，但那段没限定执行器，
        于是 Codex 设备码流程一失败就被这台机的 CodeBuddy 登录态顶成 succeeded。
        Windows 上测不出这条，因为 Windows 会把 CodeBuddy 登录直接拒掉。
        """
        manager = LoginManager()
        process = FakeProcess(b'', 1)
        with patch.object(manager, '_spawn', return_value=process), \
                patch.object(manager, '_codebuddy_authenticated', return_value=True):
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
            b'https://copilot.tencent.com/login?platform=x&state=SESSION-STATE-1\n'
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
        # state 必须完整送到用户手上，否则浏览器登完 CLI 认不回来。
        self.assertIn('state=SESSION-STATE-1', ready_event['url'])
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


class LoginSpinnerFloodTests(unittest.TestCase):
    """2026-10-26 真实故障：已登录的机器上，CodeBuddy 界面不给登录链接，
    而是直接进「Syncing…」旋转动画，每 0.1 秒一帧，九秒填满 16KB 尾部缓冲。
    结果是链接被挤出窗口、界面又不退出，最后报「登录已取消」，用户明明已经登录成功。"""

    def setUp(self):
        self.events = []
        self.done = threading.Event()

    def notify(self, event):
        self.events.append(event)
        if event['state'] in ('succeeded', 'failed', 'timeout', 'cancelled'):
            self.done.set()

    def test_normal_prompts_are_not_eaten_by_the_spinner_rules(self):
        """新加的「半截动画行」判据靠特征词识别，不能误伤官方提示文本。

        真实界面会给到这些行：信任目录、登录方式、链接、等待授权、成功提示。
        只要有一行被当成动画丢掉，用户就看不到本该看到的提示或链接。
        """
        normal = ('Do you trust the files in this folder?\n'
                  '  /root/.codebuddy\n'
                  '1. Yes, proceed\n'
                  '2. No, exit\n'
                  'Select login method:\n'
                  '1. Log in via Chinese Site\n'
                  '2. Log in via International Site\n'
                  '3. Enterprise\n'
                  "Browser didn't open? Use the url below to sign in:\n"
                  'https://copilot.tencent.com/login?platform=CLI&state=abc-1&version=2.176.0\n'
                  'Waiting for auth... (Press ESC to cancel)\n'
                  'You are logged in as user@example.com')
        kept = login_relevant(normal)
        for line in normal.splitlines():
            self.assertIn(line, kept, '官方正常提示被当成动画丢掉了：' + line)

    def test_chopped_spinner_lines_are_recognised(self):
        """块边界会把动画行切成半截，行首没了也认得出。"""
        chopped = ['streaming · ↓ 83 tokens)', 'reaming · ↓ 83 tokens)ns)',
                   'kens)tokens)5s · streaming · ↓ 83 tokens)', '· ↓ 83 tokens)']
        for line in chopped:
            self.assertTrue(github_auth._is_spinner_line(line), '没认出半截动画行：' + line)
        for line in ('Waiting for auth... (Press ESC to cancel)',
                     'https://copilot.tencent.com/login?platform=CLI&state=abc-1&version=2.176.0',
                     'Select login method:'):
            self.assertFalse(github_auth._is_spinner_line(line), '误伤正常行：' + line)

    def test_spinner_frames_are_dropped_before_buffer(self):
        flood = ''.join('✷ Syncing… (5s · streaming · ↓ 83 tokens)\n' for _ in range(500))
        kept = login_relevant(flood)
        self.assertNotIn('Syncing…', kept)
        self.assertLess(len(kept), 50)

    def test_real_lines_survive_the_filter(self):
        text = ('Do you trust the files in this folder?\n'
                '✷ Syncing… (1s)\n'
                'Select login method:\n'
                'https://copilot.tencent.com/login?platform=CLI&state=abc-1&version=2.176.0\n')
        kept = login_relevant(text)
        self.assertIn('Do you trust the files in this folder?', kept)
        self.assertIn('Select login method:', kept)
        self.assertIn('state=abc-1', kept)

    # —— 以下两条都是「反向验证驱动」的用例 ——
    # 2026-10-26 教训：只测 login_relevant() 函数本身，把主循环里的过滤拆掉照样全绿，
    # 等于没测。所以这里必须走真实 _run，并且断言的量是「主循环有没有真的过滤」。
    #
    # 又一轮教训：断言 ready 事件还不够。链接一旦解析成功就存进了 session.payload，
    # 后面缓冲被动画挤爆也不影响 ready，于是拆掉过滤它照样绿。
    # 能真正区分「过滤 / 不过滤」的判据，只有缓冲自身的末尾状态。

    def _write_lines(self, write_end, text, line_bytes=1024):
        """按整行切片喂给管道，模拟真实 pty 的吐字方式。

        真实伪终端是按「整帧整行」输出的；而普通管道读端随读随返，
        块边界会落在任意字节位置，把动画行切成半截，
        半截行首字符对不上旋转动画的判定，过滤自然失效——那是替身的伪影，
        不是被测代码的行为。这里把块边界对齐到换行，让替身贴近真实。
        """
        piece = ''
        for line in text.splitlines(keepends=True):
            if piece and len(piece.encode()) + len(line.encode()) > line_bytes:
                os.write(write_end, piece.encode())
                piece = ''
            piece += line
        if piece:
            os.write(write_end, piece.encode())

    def _run_codebuddy_with_real_pipe(self, pieces, wait_for):
        """走真实主循环跑一次 CodeBuddy 登录，返回 (事件, 收尾时缓冲末段, 收尾时缓冲长度)。

        用真 os.pipe() 构造进程替身：读线程必须走真实 read()，
        这样缓冲的累积、上限截断、动画涌入的挤压都是真的。
        """
        manager = LoginManager()
        read_end, write_end = os.pipe()
        process = FakeProcess(returncode=None)
        process.stdout = os.fdopen(read_end, 'rb', buffering=0)

        def terminate():
            # 模拟真实进程被中止：关写端让读线程拿到 EOF，退出码记为被信号杀死。
            process.terminated = True
            try:
                os.close(write_end)
            except OSError:
                pass
            process.returncode = -15

        process.terminate = terminate
        process.kill = terminate
        with patch('github_auth.os.name', 'posix'), \
                patch.object(manager, '_spawn', return_value=process), \
                patch.object(manager, '_codebuddy_authenticated', return_value=False), \
                patch('github_auth.os.killpg', side_effect=ProcessLookupError, create=True):
            manager.start('codebuddy', 1, '/bin/codebuddy', {}, self.notify)
            for piece, wait_state in pieces:
                if piece:
                    self._write_lines(write_end, piece)
                if wait_state:
                    # 按截止时间等，不能用 self.done.wait(0.05) 当睡眠：done 一旦被置位，
                    # wait 立刻返回，整个循环零等待就结束了。CI 机器慢，处理几十 KB 动画要好几秒，
                    # 这正是这个用例偶发失败的原因。条件满足就提前退出，平时不变慢。
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if any(e['state'] == wait_state for e in self.events):
                            break
                        time.sleep(0.05)
            # 给主循环一点时间把最后一片数据读进缓冲再收尾。
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with manager._lock:
                    session = manager._sessions.get('codebuddy')
                if session is not None:
                    if wait_for and wait_for in session.buffer:
                        break
                    if not wait_for and len(session.buffer) > 0:
                        break
                time.sleep(0.05)
            with manager._lock:
                session = manager._sessions.get('codebuddy')
                whole = session.buffer if session else ''
                size = len(whole)
            manager.cancel('codebuddy', 1)
            self.done.wait(3)
        return list(self.events), whole, size

    def test_link_tail_survives_when_flood_arrives_in_one_chunk(self):
        """一批里链接在前、几十 KB 动画在后：写进缓冲前必须滤掉动画。

        不过滤时 16KB 尾部窗口里只剩动画，链接所在的文字直接被挤出去。
        所以要断言的是「收尾时缓冲里还能看到链接」——这是唯一能把
        『主循环有没有过滤』区分开的判据（ready 事件有 payload 兜底，测不出来）。
        """
        flood = ''.join('✷ Syncing… (5s · streaming · ↓ 83 tokens)\n' for _ in range(1200))
        payload = ('Select login method:\n'
                   'https://copilot.tencent.com/login?platform=CLI&state=tail-keep-1&version=2.176.0\n'
                   + flood)
        self.assertGreater(len(payload), 16384, '载荷必须超过缓冲上限，否则测不出挤压')
        events, whole, size = self._run_codebuddy_with_real_pipe([(payload, 'ready')],
                                                                wait_for='state=tail-keep-1')
        ready = [e for e in events if e['state'] == 'ready']
        self.assertTrue(ready, '链接没被解析出来，ready 事件没发出来')
        self.assertIn('state=tail-keep-1', ready[0]['url'])
        self.assertIn('state=tail-keep-1', whole,
                      '缓冲里已经看不到链接——动画挤掉了有效内容，说明写缓冲前没过滤')
        self.assertLess(size, 12000, '缓冲被动画撑到接近上限，说明动画帧进了缓冲')
        # 被块边界切碎的半截动画行也必须滤掉，否则每块都会漏一点进去。
        self.assertNotIn('tokens)', whole, '半截动画行漏进了缓冲')

    def test_link_tail_survives_continuous_flood(self):
        """界面先给链接、随后持续刷动画：尾部窗口里也得留着链接。

        真实时序就是这样：登录链接出现后界面立刻开始刷「Syncing…」，
        实测九秒内就把 16KB 缓冲刷满。
        """
        head = ('Do you trust the files in this folder?\nSelect login method:\n'
                'https://copilot.tencent.com/login?platform=CLI&state=loop-keep-1&version=2.176.0\n')
        flood = ''.join('✷ Syncing… (5s · streaming · ↓ 83 tokens)\n' for _ in range(800))
        events, whole, size = self._run_codebuddy_with_real_pipe(
            [(head, 'ready'), (flood, None)], wait_for='state=loop-keep-1')
        ready = [e for e in events if e['state'] == 'ready']
        self.assertTrue(ready, '链接没被解析出来，ready 事件没发出来')
        self.assertIn('state=loop-keep-1', ready[0]['url'])
        self.assertIn('state=loop-keep-1', whole,
                      '持续动画把链接挤出了缓冲，说明写缓冲前没过滤')
        self.assertLess(size, 12000, '缓冲被动画撑到接近上限，说明动画帧进了缓冲')
        self.assertNotIn('tokens)', whole, '半截动画行漏进了缓冲')

    def test_already_logged_in_reports_success_without_waiting_for_link(self):
        """已登录时界面永远不给链接，必须靠官方命令确认真实状态，而不是等到超时。"""
        manager = LoginManager()
        process = FakeProcess(returncode=1)
        # CodeBuddy 登录依赖伪终端，Windows 上会被直接拒绝；这里只验状态机。
        with patch('github_auth.os.name', 'posix'), \
                patch.object(manager, '_spawn', return_value=process), \
                patch.object(manager, '_codebuddy_authenticated', return_value=True):
            manager.start('codebuddy', 1, '/bin/codebuddy', {}, self.notify)
            self.assertTrue(self.done.wait(3))
        self.assertEqual(self.events[-1]['state'], 'succeeded')
        self.assertNotIn('cancelled', [event['state'] for event in self.events])

    def test_not_logged_in_without_link_is_failure_not_success(self):
        manager = LoginManager()
        process = FakeProcess(returncode=1)
        with patch('github_auth.os.name', 'posix'), \
                patch.object(manager, '_spawn', return_value=process), \
                patch.object(manager, '_codebuddy_authenticated', return_value=False):
            manager.start('codebuddy', 1, '/bin/codebuddy', {}, self.notify)
            self.assertTrue(self.done.wait(3))
        self.assertEqual(self.events[-1]['state'], 'failed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
