"""外部平台授权（GitHub / Cloudflare）的测试。

每个用例都对应一处真实风险，注释里写明「不这么做会怎样」：
凭据泄漏、误吞正常任务、轮询过快被封、撤销后又被写回、未授权时捡到全局令牌。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import credentials
import cloudflare_auth
import github_auth
from credentials import CredentialStore, fingerprint, looks_like
import bot


FAKE_GH = 'gho_' + 'A1b2C3d4E5f6G7h8I9j0' + 'K1L2M3'
FAKE_CF = 'cfut_' + 'Xy9' * 8
FAKE_CF_OLD = 'a' * 40


class TempStoreMixin(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='cred-test-'))
        self.store = CredentialStore(self.temp / 'credentials.json')

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)


class CredentialShapeTest(unittest.TestCase):
    """缺陷：把正常任务描述当令牌存进去，或把格式不对的令牌存进去后静默失败。"""

    def test_github_tokens_accepted(self):
        for value in (FAKE_GH, 'ghp_' + 'b' * 36, 'github_pat_' + 'c' * 30):
            self.assertTrue(looks_like('github', value), value)

    def test_github_rejects_lookalikes(self):
        for value in ('这是一段任务描述', 'gho_短', 'gho_ ' + 'a' * 30, 'https://github.com/x'):
            self.assertFalse(looks_like('github', value), value)

    def test_cloudflare_both_formats(self):
        self.assertTrue(looks_like('cloudflare', FAKE_CF))
        self.assertTrue(looks_like('cloudflare', FAKE_CF_OLD))

    def test_cloudflare_rejects_short_input(self):
        """太短的一律不是令牌。注意 39/40 位不带前缀的串仍会被接受 ——
        因为设备码换来的 OAuth 令牌长度不固定，不能再用「必须正好 40 位」当判据；
        真令牌会因为长度区间（20~2000）之外而被拦。"""
        self.assertTrue(looks_like('cloudflare', 'a' * 40))
        self.assertFalse(looks_like('cloudflare', 'a' * 12))
        self.assertFalse(looks_like('cloudflare', 'cfut_' + 'a' * 5))

    def test_cloudflare_overlong_rejected(self):
        """上限要拦住整段粘贴（比如复制了半页页面），但必须放过真实令牌。"""
        self.assertTrue(looks_like('cloudflare', 'a' * 300))
        self.assertFalse(looks_like('cloudflare', 'a' * 2001))

    def test_whitespace_rejected(self):
        """手机复制常带尾随换行，但中间有空格一定是复制错了。"""
        self.assertFalse(looks_like('github', 'gho_' + 'a' * 20 + ' b'))

    def test_unknown_provider_never_matches(self):
        self.assertFalse(looks_like('gitlab', FAKE_GH))


class StoreRoundTripTest(TempStoreMixin):
    """缺陷：凭据文件权限过宽，或摘要把令牌原文带出去。"""

    def test_save_and_read(self):
        self.store.save('github', FAKE_GH, scope='repo')
        record = self.store.get('github')
        self.assertEqual(record['value'], FAKE_GH)
        self.assertEqual(record['scope'], 'repo')

    def test_file_mode_is_0600(self):
        """缺陷：凭据文件权限过宽，同机其他用户可读。

        Windows 上 os.chmod 只控制只读位，永远得到 666，所以在 Windows 上改为
        用 AST 检查「建文件时确实传了 0o600」—— 否则这条测试在开发机上永远红、
        在服务器上才绿，会被当成环境噪声忽略掉，最终失去防护意义。

        刻意不用「源码里出现 0o600 字样」来判断：那种断言把参数改成 0o644 时
        仍然能看到 0o600 出现在别处（如后续 chmod 兜底），属于永远绿的假测试。
        """
        self.store.save('github', FAKE_GH)
        if os.name == 'posix':
            mode = self.store.path.stat().st_mode & 0o777
            self.assertEqual(oct(mode), '0o600', '凭据文件必须是 600，否则同机其他用户可读')
        else:
            import ast
            tree = ast.parse(Path(credentials.__file__).read_text(encoding='utf-8'))
            modes = []
            for node in ast.walk(tree):
                # 只看 os.open(...) 这个调用建文件时给的 mode，不看 chmod 的兜底值。
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'open'
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == 'os'):
                    for argument in node.args:
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, int):
                            modes.append(argument.value)
            self.assertIn(0o600, modes,
                          'os.open 建凭据文件时必须显式传 0600，实测参数：' + repr(modes))
            self.assertNotIn(0o644, modes, '不能以 644 建凭据文件')
            self.assertTrue(self.store.path.exists())

    def test_write_uses_exclusive_create(self):
        """缺陷：先建普通文件再改权限，中间存在一个 644 的窗口。"""
        source = Path(credentials.__file__).read_text(encoding='utf-8')
        self.assertIn('O_EXCL', source, '必须用排他创建，避免跟随已存在的恶意软链接')
        self.assertIn('os.fdopen', source)

    def test_summary_never_leaks_value(self):
        self.store.save('github', FAKE_GH)
        summary = self.store.summary('github')
        blob = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(FAKE_GH, blob, '摘要里不能出现令牌原文')
        self.assertEqual(summary['fingerprint'], fingerprint(FAKE_GH))

    def test_masked_only_shows_prefix(self):
        masked = credentials.masked(FAKE_GH)
        self.assertTrue(masked.startswith(FAKE_GH[:4]))
        self.assertNotIn(FAKE_GH[8:], masked)

    def test_corrupt_file_degrades_to_empty(self):
        """缺陷：配置文件损坏时抛异常，每次查授权状态都失败。"""
        self.store.path.write_text('{ 不是 JSON', encoding='utf-8')
        self.assertIsNone(self.store.get('github'))
        self.assertFalse(self.store.summary('github')['authorized'])

    def test_wrong_top_level_type(self):
        self.store.path.write_text('[1,2,3]', encoding='utf-8')
        self.assertIsNone(self.store.get('github'))

    def test_no_temp_leftover(self):
        self.store.save('github', FAKE_GH)
        leftovers = [p for p in self.temp.iterdir() if p.suffix == '.tmp']
        self.assertFalse(leftovers, '写入不能留下临时文件')

    def test_drop_removes_only_target(self):
        self.store.save('github', FAKE_GH)
        self.store.save('cloudflare', FAKE_CF)
        self.assertTrue(self.store.drop('github'))
        self.assertIsNone(self.store.get('github'))
        self.assertIsNotNone(self.store.get('cloudflare'))
        self.assertFalse(self.store.drop('github'), '重复撤销应返回 False')


class EnvironmentInjectionTest(TempStoreMixin):
    """缺陷：未授权时服务器上残留的 GH_TOKEN 被任务静默用掉，用的是别人的凭据。"""

    def setUp(self):
        super().setUp()
        self.original = bot.CREDENTIALS
        bot.CREDENTIALS = self.temp / 'credentials.json'
        self.saved_env = dict(os.environ)

    def tearDown(self):
        bot.CREDENTIALS = self.original
        os.environ.clear()
        os.environ.update(self.saved_env)
        super().tearDown()

    def test_authorized_token_injected(self):
        self.store.save('github', FAKE_GH)
        environment = bot.credential_environment()
        self.assertEqual(environment.get('GH_TOKEN'), FAKE_GH)

    def test_unauthorized_clears_inherited_vars(self):
        os.environ['GH_TOKEN'] = 'ghp_leaked_from_server_env_000000000'
        os.environ['GITHUB_TOKEN'] = 'ghp_also_leaked_00000000000000000'
        environment = bot.credential_environment()
        self.assertNotIn('GH_TOKEN', environment)
        self.assertNotIn('GITHUB_TOKEN', environment)

    def test_both_providers_together(self):
        self.store.save('github', FAKE_GH)
        self.store.save('cloudflare', FAKE_CF)
        environment = bot.credential_environment()
        self.assertEqual(environment['GH_TOKEN'], FAKE_GH)
        self.assertEqual(environment['CLOUDFLARE_API_TOKEN'], FAKE_CF)

    def test_old_cloudflare_global_key_removed(self):
        """Global API Key 是全权限老方案，绝不能作为替代混进来。"""
        os.environ['CLOUDFLARE_API_KEY'] = 'global-key-should-be-removed'
        self.store.save('cloudflare', FAKE_CF)
        environment = bot.credential_environment()
        self.assertNotIn('CLOUDFLARE_API_KEY', environment)

    def test_merge_does_not_resurrect_cleared_vars(self):
        """合并语义陷阱：{**a, **b} 只能增改不能删除。

        早先 credential_environment() 自己返回 {**os.environ}，调用处写
        {**environment, **credential_environment()}，被 pop 掉的 GH_TOKEN 会被
        左边那份 environment 原样带回来，剔除逻辑形同虚设。
        这里复刻「基础环境 → 应用授权」的真实链路，断言泄漏必须消失。
        """
        os.environ['GH_TOKEN'] = 'ghp_leaked_from_server_env_000000000'
        os.environ['CLOUDFLARE_API_KEY'] = 'global-key-should-be-removed'
        base = {'PATH': '/usr/bin', 'GH_TOKEN': 'ghp_leaked_again_000000000000',
                'CLOUDFLARE_API_KEY': 'another-global-key'}
        environment = bot.credential_environment(base)
        self.assertNotIn('GH_TOKEN', environment)
        self.assertNotIn('CLOUDFLARE_API_KEY', environment)
        self.assertEqual(environment.get('PATH'), '/usr/bin', '无关变量不能被误删')

    def test_authorized_token_survives_merge(self):
        """已授权时令牌必须出现在最终环境里，不能被剔除逻辑误伤。"""
        self.store.save('github', FAKE_GH)
        environment = bot.credential_environment({'PATH': '/usr/bin'})
        self.assertEqual(environment.get('GH_TOKEN'), FAKE_GH)

    def test_run_stream_passes_environment_into_helper(self):
        """调用处必须是 credential_environment(environment)，不能事后合并。

        为什么必须查源码而不是查行为：上面两个用例即使把调用处改回
        {**environment, **credential_environment()}，函数本身依然正确，测试照样全绿。
        泄漏只发生在「合并」这一步，所以只能盯住调用处的写法本身。
        """
        import ast
        source = Path(bot.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if 'environment' not in targets:
                continue
            calls = [n for n in ast.walk(node.value)
                     if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'credential_environment']
            if calls:
                found.append(calls[0])
        self.assertTrue(found, '没找到调用 credential_environment 的地方，文件名或函数名变了？')
        for call in found:
            self.assertEqual(len(call.args), 1,
                             'credential_environment 必须接收基础环境参数，否则剔除结果会被合并覆盖')

    def test_run_stream_has_no_dict_merge_with_helper(self):
        """反向兜底：禁止再出现 {**environment, **credential_environment()} 这种写法。

        只看真实代码，不看注释与文档字符串：函数说明里会引用这个错误写法当例子，
        用纯文本搜索会把它误判成违规（自身就踩过一次）。
        """
        import ast
        tree = ast.parse(Path(bot.__file__).read_text(encoding='utf-8'))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                # {**x} 写法里 key 为 None，value 是展开的对象
                if key is None and isinstance(value, ast.Call) \
                        and getattr(value.func, 'id', '') == 'credential_environment':
                    offenders.append(node.lineno)
        self.assertFalse(offenders, '字典解包无法删除键，这种写法会让未授权令牌重新出现在任务环境里；'
                                    f'出现在第 {offenders} 行')


class DeviceFlowPollTest(unittest.TestCase):
    """缺陷：不遵守 interval 会被 GitHub 判 slow_down；把 pending 当失败会立刻放弃授权。"""

    def run_poll(self, replies, **kwargs):
        calls = []

        def fake_request(url, payload, timeout=20):
            calls.append(payload)
            return replies.pop(0)

        original = github_auth._request
        github_auth._request = fake_request
        sleeps = []
        options = {'interval': 5, 'expires_in': 900}
        options.update(kwargs)
        try:
            result = github_auth.poll('cid', 'dev', sleep=lambda seconds: sleeps.append(seconds),
                                      **options)
        finally:
            github_auth._request = original
        return result, calls, sleeps

    def test_pending_then_success(self):
        result, calls, sleeps = self.run_poll([
            {'error': 'authorization_pending'},
            {'error': 'authorization_pending'},
            {'access_token': FAKE_GH, 'scope': 'repo'},
        ])
        self.assertEqual(result['access_token'], FAKE_GH)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [5, 5, 5], '每次轮询都必须先按间隔等待')

    def test_slow_down_increases_interval(self):
        result, calls, sleeps = self.run_poll([
            {'error': 'slow_down'},
            {'access_token': FAKE_GH},
        ])
        self.assertEqual(result['access_token'], FAKE_GH)
        self.assertEqual(sleeps, [5, 10], '收到 slow_down 后间隔要加 5 秒')

    def test_access_denied_raises(self):
        with self.assertRaises(github_auth.DeviceFlowError) as caught:
            self.run_poll([{'error': 'access_denied'}])
        self.assertIn('取消', str(caught.exception))

    def test_expired_token_raises(self):
        with self.assertRaises(github_auth.DeviceFlowError) as caught:
            self.run_poll([{'error': 'expired_token'}])
        self.assertIn('过期', str(caught.exception))

    def test_device_flow_disabled_message_is_actionable(self):
        """最容易漏的配置：没勾 Enable Device Flow 时报错必须指向那一步。"""
        with self.assertRaises(github_auth.DeviceFlowError) as caught:
            self.run_poll([{'error': 'device_flow_disabled'}])
        self.assertIn('Enable Device Flow', str(caught.exception))

    def test_deadline_stops_polling(self):
        """缺陷：expires_in 已过还继续轮询，等于永久占住一个线程。"""
        with self.assertRaises(github_auth.DeviceFlowError) as caught:
            self.run_poll([{'error': 'authorization_pending'}] * 10, expires_in=0)
        self.assertIn('过期', str(caught.exception))


class StartFlowGuardTest(unittest.TestCase):
    """缺陷：没有 client_id 时不报错，而是拿着空串去请求 GitHub。"""

    def test_missing_client_id_raises_readable_error(self):
        original = bot.GITHUB_CLIENT_ID
        bot.GITHUB_CLIENT_ID = ''
        try:
            with self.assertRaises(github_auth.DeviceFlowError) as caught:
                github_auth.start('')
            self.assertIn('client_id', str(caught.exception))
        finally:
            bot.GITHUB_CLIENT_ID = original

    def test_client_id_has_usable_default(self):
        """开箱即用：不配环境变量时也必须有一个非空 client_id。

        原实现默认空串，导致机主必须先自己去注册 OAuth App 才能用，
        点了按钮只得到一句「未配置」。设备码流程只需要 client_id、不需要 secret，
        且 client_id 本身是公开标识，所以内置一个默认值是安全的。
        """
        import subprocess
        source = Path(bot.__file__).read_text(encoding='utf-8')
        self.assertIn("os.environ.get('GITHUB_CLIENT_ID') or 'Iv1.", source,
                      'GITHUB_CLIENT_ID 必须有内置默认值，否则又要机主先注册 App')

    def test_running_flow_is_reused_not_restarted(self):
        """缺陷：重复发起会打满 GitHub 每小时 50 次的设备码上限。"""

        class Alive:
            def is_alive(self):
                return True

        bot.DEVICE_FLOW.update({'provider': 'github', 'thread': Alive(),
                                'verification_uri': 'https://github.com/login/device',
                                'user_code': 'ABCD-1234', 'expires_at': time.time() + 600})
        original = bot.GITHUB_CLIENT_ID
        bot.GITHUB_CLIENT_ID = 'cid'
        try:
            text = bot.start_github_device_flow(1)
        finally:
            bot.GITHUB_CLIENT_ID = original
            bot.DEVICE_FLOW.update({'provider': '', 'thread': None})
        self.assertIn('ABCD-1234', text)
        self.assertIn('CD-1234', text)
        self.assertIn('设备码还是', text)


class DeviceCodeMessageTest(unittest.TestCase):
    """机主要的是「点开就能授权」，所以消息必须是可点链接 + 可点复制的码。"""

    def render(self, seconds=900, **kwargs):
        return bot.device_code_message('https://github.com/login/device', 'WDJB-MJHT',
                                       seconds, **kwargs)

    def test_link_is_clickable_anchor(self):
        text = self.render()
        self.assertIn('<a href="https://github.com/login/device">', text)

    def test_code_is_tappable_to_copy(self):
        """<code> 包起来点一下就复制；裸文本在手机上只能长按选中。"""
        text = self.render()
        self.assertIn('<code>WDJB-MJHT</code>', text)

    def test_uses_html_parse_mode(self):
        """必须走 HTML 发送，纯文本发出的标签会原样显示成一堆尖括号。"""
        self.assertIn("'parse_mode': 'HTML'", Path(bot.__file__).read_text(encoding='utf-8'))
        self.assertIn("'disable_web_page_preview': True", Path(bot.__file__).read_text(encoding='utf-8'))

    def test_expiry_is_shown_in_minutes(self):
        self.assertIn('15 分钟', self.render())

    def test_zero_seconds_still_reads_as_one_minute(self):
        """剩余时间为 0 或负数时不能显示「0 分钟有效」，那看着像已经过期。"""
        self.assertIn('1 分钟内有效', self.render(seconds=0))

    def test_redact_keeps_html_tags(self):
        """redact 会先把消息过一遍脱敏，它不能把标签也抹掉，否则链接和码就废了。"""
        cleaned = bot.redact(self.render())
        self.assertIn('<a href="https://github.com/login/device">', cleaned)
        self.assertIn('<code>WDJB-MJHT</code>', cleaned)

    def test_falls_back_to_plain_text_when_html_rejected(self):
        """缺陷：HTML 解析失败时整条消息丢掉，机主拿不到设备码，授权彻底卡死。"""
        calls = []

        def fake_api(method, payload):
            calls.append(dict(payload))
            if len(calls) == 1:
                raise RuntimeError('Bad Request: can not parse entities')

        original = bot.api
        bot.api = fake_api
        try:
            bot.send_rich(1, '<b>标题</b>\n<code>WDJB-MJHT</code>')
        finally:
            bot.api = original
        self.assertEqual(len(calls), 2, '失败后必须重发一次纯文本')
        self.assertIsNone(calls[1].get('parse_mode'))
        self.assertIn('WDJB-MJHT', calls[1]['text'])
        self.assertNotIn('<b>', calls[1]['text'])


class TokenIntakeTest(TempStoreMixin):
    """缺陷：等待令牌期间，普通任务描述被当成令牌吞掉，或者令牌被当任务派给模型。"""

    def setUp(self):
        super().setUp()
        self.original = bot.CREDENTIALS
        bot.CREDENTIALS = self.temp / 'credentials.json'
        self.sent = []
        self.original_send = bot.send
        bot.send = lambda chat, text: self.sent.append(text)

    def tearDown(self):
        bot.CREDENTIALS = self.original
        bot.send = self.original_send
        bot.PENDING_TOKEN.update({'provider': '', 'chat': None, 'expires_at': 0})
        super().tearDown()

    def arm(self, chat=1, ttl=300):
        bot.PENDING_TOKEN.update({'provider': 'cloudflare', 'chat': chat,
                                  'expires_at': time.time() + ttl})

    def test_normal_text_not_swallowed_when_not_armed(self):
        self.assertFalse(bot.await_token_input(1, '这是一段普通任务描述'))

    def test_normal_text_rejected_politely_when_armed(self):
        self.arm()
        handled = bot.await_token_input(1, '帮我看看服务器磁盘')
        self.assertTrue(handled)
        self.assertTrue(any('不像' in text for text in self.sent))
        # 等待态要保留，让机主能重发正确的令牌。
        self.assertTrue(bot.pending_token(1))

    def test_expired_window_ignored(self):
        self.arm(ttl=-1)
        self.assertFalse(bot.pending_token(1))
        self.assertFalse(bot.await_token_input(1, FAKE_CF))

    def test_other_chat_not_affected(self):
        self.arm(chat=1)
        self.assertFalse(bot.await_token_input(999, FAKE_CF))

    def test_window_cleared_after_use(self):
        self.arm()
        # 校验会真的请求网络，这里替换掉，只验证状态流转。
        original = bot.cloudflare_auth.judge
        bot.cloudflare_auth.judge = lambda token: {'verify': {'status': 'active'},
                                                  'scope': {'zones': [], 'accounts': []},
                                                  'elapsed': 0}
        try:
            bot.await_token_input(1, FAKE_CF)
        finally:
            bot.cloudflare_auth.judge = original
        self.assertFalse(bot.pending_token(1), '用过一次就要关窗，不能连续吃多条消息')
        self.assertIsNotNone(bot.credentials().get('cloudflare'))


class StatusTextTest(TempStoreMixin):
    """缺陷：状态页把令牌原文显示出来，等于每次查状态都在聊天里贴一次凭据。"""

    def setUp(self):
        super().setUp()
        self.original = bot.CREDENTIALS
        bot.CREDENTIALS = self.temp / 'credentials.json'

    def tearDown(self):
        bot.CREDENTIALS = self.original
        bot.DEVICE_FLOW.update({'provider': '', 'user_code': ''})
        super().tearDown()

    def test_unauthorized_lists_both(self):
        text = bot.auth_status_text()
        self.assertIn('GitHub：未授权', text)
        self.assertIn('Cloudflare：未授权', text)

    def test_authorized_hides_secret(self):
        self.store.save('github', FAKE_GH, scope='repo')
        text = bot.auth_status_text()
        self.assertIn('GitHub：已授权', text)
        self.assertNotIn(FAKE_GH, text)
        self.assertNotIn(FAKE_GH[10:], text)

    def test_password_storage_claim_present(self):
        text = bot.auth_status_text()
        self.assertIn('不进任务记录', text)


class RevokeTest(TempStoreMixin):
    """缺陷：撤销授权时不清内存里的设备码等待态，
    轮询线程随后把新拿到的令牌又写回文件，撤销等于没做。"""

    def setUp(self):
        super().setUp()
        self.original = bot.CREDENTIALS
        bot.CREDENTIALS = self.temp / 'credentials.json'
        self.sent = []
        self.original_send = bot.send
        self.original_options = bot.send_options
        bot.send = lambda chat, text: self.sent.append(text)
        bot.send_options = lambda chat, title, labels: self.sent.append(title + '|' + '、'.join(labels))

    def tearDown(self):
        bot.CREDENTIALS = self.original
        bot.send = self.original_send
        bot.send_options = self.original_options
        bot.DEVICE_FLOW.update({'provider': '', 'device_code': '', 'user_code': '', 'thread': None})
        bot.CLOUDFLARE_FLOW.update({'thread': None, 'device_code': '', 'user_code': ''})
        super().tearDown()

    def handle(self, text):
        controller = bot.TaskController.__new__(bot.TaskController)
        controller.lock = __import__('threading').RLock()
        controller.data = {'tasks': [], 'favorites': [], 'engine': 'claude'}
        controller.persist = lambda: None
        controller.handle_text(1, text)

    def test_revoke_clears_device_flow_state(self):
        self.store.save('github', FAKE_GH)
        bot.DEVICE_FLOW.update({'provider': 'github', 'device_code': 'dc-secret',
                                'user_code': 'ABCD-1234', 'thread': None})
        self.handle('撤销GitHub')
        self.assertEqual(bot.DEVICE_FLOW['device_code'], '', '撤销后必须清掉设备码，否则轮询会把令牌写回来')
        self.assertEqual(bot.DEVICE_FLOW['user_code'], '')
        self.assertIsNone(bot.credentials().get('github'))

    def test_revoke_cloudflare_keeps_github(self):
        self.store.save('github', FAKE_GH)
        self.store.save('cloudflare', FAKE_CF)
        self.handle('撤销Cloudflare')
        self.assertIsNotNone(bot.credentials().get('github'))
        self.assertIsNone(bot.credentials().get('cloudflare'))

    def test_revoke_cloudflare_clears_cloudflare_flow(self):
        """缺陷：撤销 Cloudflare 后不清它自己的设备码状态，轮询线程随后
        把新拿到的令牌写回文件，撤销等于没做 —— 而机主以为权限已经收回。"""
        self.store.save('cloudflare', FAKE_CF)
        bot.CLOUDFLARE_FLOW.update({'thread': None, 'device_code': 'cf-dc-secret',
                                    'user_code': 'iw3QpfiT'})
        self.handle('撤销Cloudflare')
        self.assertEqual(bot.CLOUDFLARE_FLOW['device_code'], '')
        self.assertEqual(bot.CLOUDFLARE_FLOW['user_code'], '')
        self.assertIsNone(bot.credentials().get('cloudflare'))

    def test_revoke_menu_offers_both(self):
        self.handle('撤销授权')
        self.assertTrue(any('撤销GitHub' in text and '撤销Cloudflare' in text for text in self.sent))

    def test_revoke_mentions_platform_side(self):
        """本地删了不等于平台侧撤销，必须明说，否则机主以为已经收回权限。"""
        self.store.save('github', FAKE_GH)
        self.handle('撤销GitHub')
        self.assertTrue(any('平台' in text for text in self.sent))

    def test_revoke_without_credential_is_safe(self):
        self.handle('撤销GitHub')
        self.assertTrue(any('本来就没有' in text for text in self.sent))


class RedactNewPatternTest(unittest.TestCase):
    """缺陷：新令牌前缀没进脱敏规则，模型把令牌贴进正文时原样进聊天记录。"""

    def test_cloudflare_token_masked(self):
        self.assertNotIn(FAKE_CF, bot.redact('令牌是 ' + FAKE_CF + ' 请收好'))

    def test_github_token_still_masked(self):
        self.assertNotIn(FAKE_GH, bot.redact('令牌 ' + FAKE_GH))

    def test_normal_hash_untouched(self):
        """40 位十六进制提交号不能被误抹，否则所有提交摘要都变成占位符。"""
        sha = 'a1b2c3d4' * 5
        self.assertIn(sha, bot.redact('提交 ' + sha))


class MenuEntryTest(unittest.TestCase):
    """缺陷：菜单里有按钮但没分支，点下去毫无反应。"""

    def test_provider_group_present(self):
        self.assertIn('平台授权', bot.MENU_GROUPS['更多'])
        for label in ('授权GitHub', '授权Cloudflare', '授权状态', '撤销授权'):
            self.assertIn(label, bot.MENU_GROUPS['更多']['平台授权'])

    def test_cloudflare_paste_entry_present(self):
        """粘贴令牌这条路必须仍然可达，否则只想要单站点最小权限的机主无路可走。"""
        self.assertEqual(bot.ALIASES['手动填令牌'], '云flare粘贴令牌')
        self.assertIn("elif text == '云flare粘贴令牌':", Path(bot.__file__).read_text(encoding='utf-8'))

    def test_aliases_point_to_real_buttons(self):
        labels = set(bot.MENU_GROUPS['更多']['平台授权'])
        for alias in ('/gh', '/cf', '/auth'):
            self.assertIn(bot.ALIASES[alias], labels)


class CloudflareDeviceFlowTest(unittest.TestCase):
    """Cloudflare 改走设备码。每个用例都对应一处会让功能整体失效的坑。"""

    def test_client_id_has_usable_default(self):
        """缺陷：把 client_id 做成必填项，等于把注册 OAuth 客户端的负担丢给机主。"""
        source = Path(bot.__file__).read_text(encoding='utf-8')
        self.assertIn("os.environ.get('CLOUDFLARE_CLIENT_ID')", source)
        self.assertTrue(bot.CLOUDFLARE_CLIENT_ID)

    def test_dash_requests_carry_wrangler_ua(self):
        """缺陷（实测踩过）：不带 User-Agent 请求 dash.cloudflare.com 必定吃 403
        「Attention Required!」拦截页，与 IP 无关。少了这个头，服务器上必然失败。"""
        source = Path(github_auth.__file__).with_name('cloudflare_auth.py').read_text(encoding='utf-8')
        self.assertIn("'User-Agent': CLIENT_UA", source)
        self.assertTrue(cloudflare_auth.CLIENT_UA.startswith('wrangler/'))

    def test_scope_asks_for_refresh_token(self):
        """没有 offline_access 就没有 refresh_token，令牌几天后到期只能重新授权。"""
        self.assertIn('offline_access', cloudflare_auth.DEFAULT_SCOPE)

    def test_token_endpoint_payload_matches_spec(self):
        """缺陷：grant_type 写错会被 Cloudflare 判 unsupported_grant_type，轮询永远失败。"""
        self.assertEqual(cloudflare_auth.GRANT_TYPE,
                         'urn:ietf:params:oauth:grant-type:device_code')
        self.assertEqual(cloudflare_auth.TOKEN_URL,
                         'https://dash.cloudflare.com/oauth2/token')

    def test_poll_honours_slow_down(self):
        """缺陷：收到 slow_down 仍按原间隔猛轮，会被一直惩罚到过期。"""
        waits = []

        def fake_sleep(seconds):
            waits.append(seconds)

        responses = [{'error': 'authorization_pending'},
                     {'error': 'slow_down'},
                     {'access_token': 'cf-oauth-token'}]
        original = cloudflare_auth._dash
        cloudflare_auth._dash = lambda url, payload, timeout=20: responses.pop(0)
        try:
            body = cloudflare_auth.poll('cid', 'dc', interval=5, expires_in=60, sleep=fake_sleep)
        finally:
            cloudflare_auth._dash = original
        self.assertEqual(body['access_token'], 'cf-oauth-token')
        self.assertEqual(waits, [5, 5, 10], '第一次 slow_down 后间隔必须加 5 秒')

    def test_poll_raises_on_access_denied(self):
        original = cloudflare_auth._dash
        cloudflare_auth._dash = lambda url, payload, timeout=20: {'error': 'access_denied'}
        try:
            with self.assertRaises(cloudflare_auth.DeviceFlowError) as caught:
                cloudflare_auth.poll('cid', 'dc', interval=1, expires_in=60, sleep=lambda s: None)
        finally:
            cloudflare_auth._dash = original
        self.assertIn('拒绝了授权', str(caught.exception))

    def test_pending_returned_as_http_400_is_still_pending(self):
        """★ 实测踩过的坑，最致命的一个：
        Cloudflare 把 authorization_pending 用 HTTP 400 返回（GitHub 是 200）。
        若 _dash 把 4xx 一律当异常抛，轮询每一轮都会变成「授权失败」，
        设备码流程永远走不完 —— 而机主在页面上明明已经点了允许。
        所以这里必须真的造一个 400 响应，确认轮询能继续而不是抛错。"""
        original = cloudflare_auth.urllib.request.urlopen
        calls = []

        def boom(request, timeout=None):
            calls.append(1)
            body = (b'{"error":"authorization_pending","error_description":'
                    b'"The authorization request is still pending."}')
            raise cloudflare_auth.urllib.error.HTTPError(
                request.full_url, 400, 'Bad Request', {}, __import__('io').BytesIO(body))

        cloudflare_auth.urllib.request.urlopen = boom
        try:
            with self.assertRaises(cloudflare_auth.DeviceFlowError) as caught:
                # expires_in=2 让轮询在两秒内自然到期，于是以「过期」收尾，
                # 而不是以「HTTP 400 请稍后重试」收尾。
                cloudflare_auth.poll('cid', 'dc', interval=1, expires_in=2,
                                     sleep=lambda s: None)
        finally:
            cloudflare_auth.urllib.request.urlopen = original
        self.assertTrue(calls, '轮询必须真的发出了请求')
        message = str(caught.exception)
        self.assertNotIn('HTTP 400', message,
                         '400 + authorization_pending 不能被当成错误抛出去')
        self.assertIn('过期', message)

    def test_real_error_body_on_400_is_surfaced(self):
        """反过来也要成立：400 里带的是真错误（比如 access_denied）时不能吞掉。"""
        original = cloudflare_auth.urllib.request.urlopen

        def boom(request, timeout=None):
            body = b'{"error":"access_denied","error_description":"User denied."}'
            raise cloudflare_auth.urllib.error.HTTPError(
                request.full_url, 400, 'Bad Request', {}, __import__('io').BytesIO(body))

        cloudflare_auth.urllib.request.urlopen = boom
        try:
            with self.assertRaises(cloudflare_auth.DeviceFlowError) as caught:
                cloudflare_auth.poll('cid', 'dc', interval=1, expires_in=60,
                                     sleep=lambda s: None)
        finally:
            cloudflare_auth.urllib.request.urlopen = original
        self.assertIn('拒绝了授权', str(caught.exception))

    def test_403_message_offers_the_paste_fallback(self):
        """403 有两种成因（机器人拦截 / client_id 不被接受），提示必须同时给出退路，
        否则机主只会对着同一句报错反复重试。"""
        original = cloudflare_auth.urllib.request.urlopen

        def boom(request, timeout=None):
            raise cloudflare_auth.urllib.error.HTTPError(
                request.full_url, 403, 'Forbidden', {}, None)

        cloudflare_auth.urllib.request.urlopen = boom
        try:
            with self.assertRaises(cloudflare_auth.DeviceFlowError) as caught:
                cloudflare_auth.start('cid')
        finally:
            cloudflare_auth.urllib.request.urlopen = original
        self.assertIn('粘贴API令牌', str(caught.exception))

    def test_whoami_uses_user_endpoint_not_token_verify(self):
        """缺陷：OAuth 访问令牌走 /user/tokens/verify 会被判无效，
        于是「授权成功」变成「校验失败」，功能名义上做了实际用不了。"""
        original = cloudflare_auth.urllib.request.urlopen
        seen = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"result": {"email": "a@b.c", "id": "123"}}'

        def spy(request, timeout=None):
            seen.append(request.full_url)
            return Response()

        cloudflare_auth.urllib.request.urlopen = spy
        try:
            who = cloudflare_auth.whoami('token')
        finally:
            cloudflare_auth.urllib.request.urlopen = original
        self.assertEqual(who['email'], 'a@b.c')
        self.assertTrue(seen[0].endswith('/client/v4/user'), seen)

    def test_message_prefers_complete_uri(self):
        """有 verification_uri_complete 就必须用它：自带码，机主少一步手抄。"""
        text = bot.cloudflare_device_code_message(self.flow(), 299)
        self.assertIn('<a href="https://dash.cloudflare.com/oauth2/device/verify?user_code=iw3QpfiT">', text)
        self.assertIn('<code>iw3QpfiT</code>', text)

    def test_message_says_five_minutes_not_fifteen(self):
        """Cloudflare 只有 5 分钟（GitHub 15 分钟）。照抄 GitHub 的文案会让机主
        以为还有十分钟可用，实际早过期了。所以这里断言的是 Cloudflare 版独有的
        措辞（「分钟有效（比 GitHub 短得多）」），而不是数字 ——
        只断言数字的话，把文案改回 GitHub 那版照样能通过。"""
        text = bot.cloudflare_device_code_message(self.flow(), 299)
        self.assertIn('比 GitHub 短得多', text)
        self.assertIn('4 分钟', text)

    def test_message_keeps_plain_uri_as_fallback(self):
        """电报内置浏览器偶尔会丢掉查询参数，那时还得能手输码。"""
        text = bot.cloudflare_device_code_message(self.flow(), 299)
        self.assertIn('https://dash.cloudflare.com/oauth2/device/verify<', text)

    def test_message_falls_back_when_complete_uri_missing(self):
        flow = self.flow()
        flow['verification_uri_complete'] = ''
        text = bot.cloudflare_device_code_message(flow, 299)
        self.assertIn('<a href="https://dash.cloudflare.com/oauth2/device/verify">', text)

    @staticmethod
    def flow():
        return {'verification_uri': 'https://dash.cloudflare.com/oauth2/device/verify',
                'verification_uri_complete':
                    'https://dash.cloudflare.com/oauth2/device/verify?user_code=iw3QpfiT',
                'user_code': 'iw3QpfiT'}


class CloudflareFlowIsolationTest(unittest.TestCase):
    """缺陷：两个平台共用一份设备码状态，后发起的把先发起的覆盖掉，
    先那轮就永远换不到令牌（而机主看起来两轮都在「等待中」）。"""

    def test_two_flows_are_separate_dicts(self):
        self.assertIsNot(bot.DEVICE_FLOW, bot.CLOUDFLARE_FLOW)
        bot.CLOUDFLARE_FLOW['user_code'] = 'iw3QpfiT'
        self.assertNotEqual(bot.DEVICE_FLOW.get('user_code'), 'iw3QpfiT')
        bot.CLOUDFLARE_FLOW['user_code'] = ''

    def test_reusing_live_flow_does_not_request_new_code(self):
        """有效期内重复点按钮必须复用，不能重新申请：既浪费配额，也让手上那个码失效。"""
        class Alive:
            def is_alive(self):
                return True

        bot.CLOUDFLARE_FLOW.update({'thread': Alive(), 'user_code': 'iw3QpfiT',
                                    'verification_uri': 'https://dash.cloudflare.com/oauth2/device/verify',
                                    'verification_uri_complete': 'https://dash.cloudflare.com/oauth2/device/verify?user_code=iw3QpfiT',
                                    'expires_at': time.time() + 200})
        original = cloudflare_auth.start
        calls = []
        cloudflare_auth.start = lambda *a, **k: calls.append(1) or {}
        try:
            text = bot.start_cloudflare_device_flow(1)
        finally:
            cloudflare_auth.start = original
            bot.CLOUDFLARE_FLOW.update({'thread': None, 'user_code': ''})
        self.assertEqual(calls, [], '有效期内不应再次申请设备码')
        self.assertIn('iw3QpfiT', text)


class OAuthTokenShapedCredentialTest(unittest.TestCase):
    """缺陷：凭据形态只认 cfut_ 与 40 位旧格式，设备码换来的 OAuth 令牌
    在保存时被自己的校验拦下，整条流程白跑。"""

    def test_oauth_access_token_accepted(self):
        token = 'A' * 64
        self.assertTrue(looks_like('cloudflare', token))

    def test_long_base64url_token_accepted(self):
        token = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r'
        self.assertTrue(looks_like('cloudflare', token))

    def test_task_sentence_still_rejected(self):
        """放开形态不能把正常任务描述也放进来 —— 那会在等待态里被误吞成令牌。"""
        for value in ('这是一句普通的任务描述', '幫我把这个仓库的 CI 修一下', 'please deploy it now'):
            self.assertFalse(looks_like('cloudflare', value), value)


class SensitiveFileTest(unittest.TestCase):
    """缺陷：模型用「发文件」把凭据文件原样传回聊天。"""

    def test_credentials_file_blocked(self):
        self.assertTrue(bot.sensitive_path(Path('/tmp/credentials.json')))

    def test_credentials_in_state_blacklist(self):
        self.assertIn('credentials.json', Path(bot.__file__).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
