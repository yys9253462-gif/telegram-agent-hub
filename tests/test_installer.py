#!/usr/bin/env python3
import _root  # noqa: F401  仓库根目录进 sys.path
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = _root.ROOT

def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj

class InstallerTests(unittest.TestCase):
    def test_no_shell_interpretation(self):
        m = module('install')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'config.json'
            path.write_text('{"note": "$(touch /tmp/SHOULD_NOT_EXIST)"}')
            self.assertEqual(m.read_json(path)['note'], '$(touch /tmp/SHOULD_NOT_EXIST)')
            with self.assertRaises(m.InstallError):
                m.validate_url('http://example.com')
            with self.assertRaises(m.InstallError):
                m.env_quote('a\nb')

    def test_secret_environment(self):
        m = module('install')
        self.assertEqual(m.env_quote('x"y\\z'), '"x\\"y\\\\z"')
        self.assertTrue(m.INSTALLER_SHA256.get('claude'))
        self.assertTrue(m.INSTALLER_SHA256.get('codebuddy'))

    def test_archive_rejects_escape_and_links(self):
        m = module('bootstrap')
        for name, link in [('../escape', False), ('root/link', True)]:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w:gz') as t:
                item = tarfile.TarInfo(name)
                if link:
                    item.type = tarfile.SYMTYPE
                    item.linkname = '/etc/passwd'
                t.addfile(item)
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(m.BootstrapError):
                    m.extract_archive(buf.getvalue(), d)

    def test_redirect_drops_credentials(self):
        m = module('bootstrap')
        import urllib.request
        req = urllib.request.Request('https://api.github.com/repos/a/b/tarball/abc', headers={'Authorization':'Bearer FAKE_TEST_ONLY'})
        redir = m.SafeRedirect().redirect_request(req, None, 302, '', {}, 'https://codeload.github.com/a/b/legacy.tar.gz/abc')
        self.assertIsNone(redir.get_header('Authorization'))
        with self.assertRaises(m.BootstrapError):
            m.SafeRedirect().redirect_request(req, None, 302, '', {}, 'https://example.com/file')


def base_config(**overrides):
    """一份能通过校验的最小配置，只用于安装器逻辑测试，全部是明显的假值。"""
    c = {'telegram_bot_token': '123456:FAKE_FORMAT_EXAMPLE_ONLY', 'telegram_owner_id': '123456',
         'chat_base_url': 'https://gateway.example.com', 'chat_api_key': 'FAKE_FORMAT_EXAMPLE_ONLY',
         'default_model': 'your-model-id', 'enable_claude': True, 'enable_codebuddy': True,
         'voice_enabled': False, 'image_enabled': False, 'remote_enabled': False,
         'sudo_nopasswd': False, 'local_label': '本机'}
    c.update(overrides)
    return c


class CodexInstallerTests(unittest.TestCase):
    """安装器补 Codex 的回归测试。

    这些测试的重心不是「字符串出现了」，而是「行为真的对」：启用列表、默认值开关、
    至少启用一个的判定、provider 配置文件内容、npm 路线的调用参数。
    """

    def test_codex_is_a_known_engine(self):
        m = module('install')
        self.assertIn('codex', m.ENGINES_ORDER)
        # 顺序决定默认执行器：claude 必须仍在首位，老用户升级后默认行为不能变。
        self.assertEqual(m.ENGINES_ORDER[0], 'claude')

    def test_enabled_agents_follow_order(self):
        m = module('install')
        c = base_config(enable_claude=False, enable_codebuddy=True, enable_codex=True)
        env = m.runtime_environment(c, Path('/home/agentbot'))
        self.assertEqual(env['ENABLED_AGENTS'], 'codebuddy,codex')
        c['enable_claude'] = True
        env = m.runtime_environment(c, Path('/home/agentbot'))
        self.assertEqual(env['ENABLED_AGENTS'], 'claude,codebuddy,codex')
        c['enable_codex'] = False
        env = m.runtime_environment(c, Path('/home/agentbot'))
        self.assertEqual(env['ENABLED_AGENTS'], 'claude,codebuddy')

    def test_only_codex_enabled_is_allowed(self):
        m = module('install')
        # 只启用 Codex 是合法组合；旧实现「claude 与 codebuddy 都不启用就报错」会误拦。
        m.validate_config(base_config(enable_claude=False, enable_codebuddy=False, enable_codex=True))

    def test_all_disabled_still_rejected(self):
        m = module('install')
        with self.assertRaises(m.InstallError):
            m.validate_config(base_config(enable_claude=False, enable_codebuddy=False, enable_codex=False))

    def test_codex_flag_must_be_boolean(self):
        m = module('install')
        with self.assertRaises(m.InstallError):
            m.validate_config(base_config(enable_codex='yes'))

    def test_codex_disabled_by_default(self):
        """缺 enable_codex 字段时必须落到关闭，不能跟着「默认启用」那一组走。"""
        m = module('install')
        c = m.collect_config({k: v for k, v in base_config().items() if k != 'enable_codex'}, yes=True)
        self.assertIn('enable_codex', c, '缺字段时要补齐，否则后续读取会抛 KeyError')
        self.assertIs(c['enable_codex'], False, '老用户升级时不该被动多装一个 npm 依赖')

    def test_codex_enabled_stays_enabled(self):
        m = module('install')
        c = m.collect_config(base_config(enable_codex=True), yes=True)
        self.assertTrue(c['enable_codex'])

    def test_codex_config_points_at_gateway(self):
        m = module('install')
        text = m.codex_config_text('https://gateway.example.com', 'some-model')
        self.assertIn('base_url = "https://gateway.example.com/v1"', text)
        self.assertIn('wire_api = "responses"', text)
        self.assertIn('env_key = "OPENAI_API_KEY"', text)
        self.assertIn('requires_openai_auth = false', text)
        self.assertIn('model = "some-model"', text)
        self.assertIn('model_provider = "gateway"', text)
        # 凭据绝不能写进配置文件。
        self.assertNotIn('api_key', text.replace('env_key', ''))
        # /v1/responses 是错误写法：Codex 会自己拼 /responses。
        self.assertNotIn('/v1/responses', text)

    def test_codex_config_is_valid_toml(self):
        m = module('install')
        text = m.codex_config_text('https://gateway.example.com/', 'some-model')
        try:
            import tomllib
        except ImportError:
            self.skipTest('Python 3.11 以下没有 tomllib')
        parsed = tomllib.loads(text)
        self.assertEqual(parsed['model_provider'], 'gateway')
        provider = parsed['model_providers']['gateway']
        self.assertEqual(provider['base_url'], 'https://gateway.example.com/v1')
        self.assertFalse(provider['requires_openai_auth'])
        self.assertNotIn('api_key', provider)

    def test_codex_install_pins_version_and_verifies(self):
        m = module('install')
        calls = []

        def fake_run(command, **kwargs):
            calls.append(list(command))
            if command[0] == 'codex':
                return 'codex-cli ' + m.CODEX_VERSION + '\n'
            return ''

        with patch.object(m.shutil, 'which', return_value='/usr/bin/npm'), \
             patch.object(m, 'run', side_effect=fake_run):
            m.install_codex_cli()
        install_call = next(c for c in calls if c[:2] == ['npm', 'install'])
        target = install_call[-1]
        self.assertEqual(target, m.CODEX_PACKAGE + '@' + m.CODEX_VERSION)
        self.assertIn('--no-fund', install_call)
        self.assertIn('--no-audit', install_call)
        # 装完必须自检版本，不能装完就宣告成功。
        self.assertTrue(any(c[0] == 'codex' and '--version' in c for c in calls))

    def test_codex_install_rejects_wrong_version(self):
        m = module('install')

        def fake_run(command, **kwargs):
            return 'codex-cli 9.9.9\n' if command[0] == 'codex' else ''

        with patch.object(m.shutil, 'which', return_value='/usr/bin/npm'), \
             patch.object(m, 'run', side_effect=fake_run):
            with self.assertRaises(m.InstallError):
                m.install_codex_cli()

    def test_codex_install_without_npm_fails_loudly(self):
        m = module('install')
        with patch.object(m.shutil, 'which', return_value=None):
            with self.assertRaises(m.InstallError):
                m.install_codex_cli()

    @unittest.skipUnless(hasattr(os, 'getuid'), 'Windows 默认不允许建符号链接；此项由 Linux 上的真实安装覆盖')
    def test_shared_memory_wires_codex(self):
        m = module('install')
        source = Path(self.enterContext(tempfile.TemporaryDirectory()))
        home = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'agentbot'
        home.mkdir()
        # home/tasks 由 install() 负责创建，这里单独测 shared_memory 就得自己补上。
        (home / 'tasks').mkdir()
        m.shared_memory(source, home, uid=os.getuid(), gid=os.getgid())
        link = home / '.codex' / 'skills'
        self.assertTrue(link.is_symlink(), 'Codex 也要能读到共享技能目录')
        for directory in (home, home / 'tasks'):
            for name in ('CLAUDE.md', 'CODEBUDDY.md', 'AGENTS.md'):
                path = directory / name
                self.assertTrue(path.is_symlink() or path.exists(), '缺少指令文件链接：' + str(path))

    def test_shared_memory_mentions_codex_in_source(self):
        """符号链接用例在 Windows 上跳过，这里补一条与平台无关的结构检查。

        Codex 的指令文件名是 AGENTS.md，目录是 .codex；两者任一漏掉，Codex 就既读不到
        共享记忆也读不到共享技能。这里按源码里的真实集合断言，而不是找关键字。
        """
        import ast
        tree = ast.parse((ROOT / 'install.py').read_text(encoding='utf-8'))
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'shared_memory')
        loops = [n for n in ast.walk(func) if isinstance(n, ast.For)]
        collected = []
        for loop in loops:
            if isinstance(loop.iter, (ast.Tuple, ast.List)):
                values = [e.value for e in loop.iter.elts if isinstance(e, ast.Constant)]
                if values and all(isinstance(v, str) for v in values):
                    collected.append(set(values))
        flat = set().union(*collected) if collected else set()
        self.assertIn('.codex', flat, 'shared_memory 要为 Codex 建 ~/.codex 目录与技能链接')
        self.assertIn('AGENTS.md', flat, 'Codex 读 AGENTS.md，必须和 CLAUDE.md 一起链接')

    def test_dry_run_lists_codex(self):
        m = module('install')
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / 'c.json'
            cfg.write_text(json.dumps(base_config(enable_codex=True)), encoding='utf-8')
            out = io.StringIO()
            with patch.object(m, 'verify_telegram'), patch.object(sys, 'stdout', out):
                m.main(['--config', str(cfg), '--yes', '--dry-run'])
            self.assertIn('"codex"', out.getvalue())


if __name__ == '__main__':
    unittest.main()
