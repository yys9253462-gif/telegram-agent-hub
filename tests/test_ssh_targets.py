#!/usr/bin/env python3
"""「明明有权限，机器人却自己给自己下禁令」的回归测试。

2026-10-26 真实事故：机主问「为什么还要我去授权」。
实际情况是免密 sudo 全开、SSH 免密也通、密钥与 config 都在，
只是 `REMOTE_ALIAS` 环境变量没设，于是注入给模型的指令写着
「当前未配置远程SSH目标，不得声称可以访问远程服务器」，
模型看到 tokyo 的配置也不敢连，只交了一份历史报告。

教训：判断「我有没有这个能力」要看**实际配置**，不能只看某个环境变量。
变量是安装器写的意图，配置是机器上真实存在的能力，两者缺一都不能当成没有。
"""
import _root  # noqa: F401  仓库根目录进 sys.path
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('TELEGRAM_BOT_TOKEN', '000:FAKE')
os.environ.setdefault('TELEGRAM_OWNER_ID', '1')

import bot

CONFIG_HEAD = 'Host tokyo\n  HostName 57.181.25.17\n  User root\n  IdentityFile ~/.ssh/x\n'


class SshTargetTests(unittest.TestCase):
    """ssh_targets()：判定「有没有可用远程主机」的唯一入口。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / '.ssh').mkdir()
        self.saved = (bot.AGENT_HOME, bot.REMOTE_ALIAS)

    def tearDown(self):
        bot.AGENT_HOME, bot.REMOTE_ALIAS = self.saved

    def write_config(self, text):
        (self.home / '.ssh' / 'config').write_text(text, encoding='utf-8')

    def test_config_host_is_found_when_env_var_is_empty(self):
        # 这就是本次事故的现场：变量空、配置在。
        bot.REMOTE_ALIAS = ''
        bot.AGENT_HOME = self.home
        self.write_config(CONFIG_HEAD)
        self.assertEqual([name for name, _ in bot.ssh_targets()], ['tokyo'])

    def test_env_var_wins_when_present(self):
        # 安装器显式配置的意图最明确，优先于 config 里的其它条目。
        bot.REMOTE_ALIAS = 'agent-remote'
        bot.AGENT_HOME = self.home
        self.write_config(CONFIG_HEAD)
        self.assertEqual([name for name, _ in bot.ssh_targets()], ['agent-remote'])

    def test_wildcard_block_is_skipped(self):
        bot.REMOTE_ALIAS = ''
        bot.AGENT_HOME = self.home
        self.write_config(CONFIG_HEAD + '\nHost *\n  ServerAliveInterval 30\n')
        self.assertEqual([name for name, _ in bot.ssh_targets()], ['tokyo'])

    def test_multiple_hosts_all_listed(self):
        bot.REMOTE_ALIAS = ''
        bot.AGENT_HOME = self.home
        self.write_config(CONFIG_HEAD + '\nHost us2\n  HostName 3.140.122.74\n')
        self.assertEqual([name for name, _ in bot.ssh_targets()], ['tokyo', 'us2'])

    def test_host_pointing_at_this_machine_is_excluded(self):
        # 「Host tencent → 本机公网地址」不是远程目标，列出来会让机器人 ssh 自己。
        bot.REMOTE_ALIAS = ''
        bot.AGENT_HOME = self.home
        self.write_config(CONFIG_HEAD + '\nHost tencent\n  HostName 43.155.161.217\n')
        with patch.object(bot, 'local_addresses', return_value={'43.155.161.217', 'localhost'}):
            self.assertEqual([name for name, _ in bot.ssh_targets()], ['tokyo'])

    def test_alias_equal_to_own_hostname_is_excluded(self):
        bot.REMOTE_ALIAS = ''
        bot.AGENT_HOME = self.home
        self.write_config('Host localhost\n  HostName 127.0.0.1\n' + CONFIG_HEAD)
        with patch.object(bot, 'local_addresses', return_value={'localhost', '127.0.0.1'}):
            self.assertEqual([name for name, _ in bot.ssh_targets()], ['tokyo'])

    def test_missing_config_is_empty_not_an_error(self):
        bot.REMOTE_ALIAS = ''
        bot.AGENT_HOME = self.home
        self.assertEqual(bot.ssh_targets(), [])

    def test_unreadable_config_is_empty_not_an_error(self):
        bot.REMOTE_ALIAS = ''
        bot.AGENT_HOME = self.home
        (self.home / '.ssh' / 'config').write_bytes(b'\xff\xfe\x00bad')
        self.assertEqual(bot.ssh_targets(), [])


class InstructionWordingTests(unittest.TestCase):
    """注入给模型的文案：有主机就说能用，没主机才说不许声称。"""

    def setUp(self):
        self.saved = bot.REMOTE_ALIAS

    def tearDown(self):
        bot.REMOTE_ALIAS = self.saved

    def test_available_host_is_offered_without_asking_permission(self):
        bot.REMOTE_ALIAS = 'tokyo'
        text = bot.agent_instructions()
        self.assertIn('ssh tokyo', text)
        # 关键断言：不能要求机主再授权一次。
        self.assertIn('不要因为目标是「远程」就停下来征求同意', text)

    def test_missing_host_says_so_but_does_not_forbid_the_whole_tool(self):
        bot.REMOTE_ALIAS = ''
        with patch.object(bot, 'ssh_targets', return_value=[]):
            text = bot.agent_instructions()
        self.assertIn('没有可用的 SSH 主机配置', text)
        # 旧文案是「不得声称可以访问远程服务器」，措辞容易被模型理解成
        # 「所有远程操作都不许做」，因此改成只约束「不要谎称已连上」。
        self.assertNotIn('不得声称可以访问远程服务器', text)

    def test_extra_hosts_are_mentioned(self):
        bot.REMOTE_ALIAS = 'tokyo'
        with patch.object(bot, 'ssh_targets', return_value=[('tokyo', '远程服务器 tokyo'),
                                                            ('us2', '远程服务器 us2')]):
            text = bot.agent_instructions()
        self.assertIn('其它可用主机：us2', text)

    def test_authorized_capability_is_used_directly(self):
        # 机主的原话是「应该所有 agent 都获取最高权限，而不是要我去授权」。
        bot.REMOTE_ALIAS = 'tokyo'
        text = bot.agent_instructions()
        self.assertIn('已授权的能力直接用', text)


class ReverseVerification(unittest.TestCase):
    """反向验证：把判据退回只看环境变量，上面必须报红。"""

    def test_env_only_judgement_would_lose_the_host(self):
        home = Path(tempfile.mkdtemp())
        (home / '.ssh').mkdir()
        (home / '.ssh' / 'config').write_text(CONFIG_HEAD, encoding='utf-8')
        saved = (bot.AGENT_HOME, bot.REMOTE_ALIAS)
        try:
            bot.AGENT_HOME = home
            bot.REMOTE_ALIAS = ''
            # 新判据能看见 tokyo
            self.assertTrue(bot.ssh_targets())
            # 旧判据（只看环境变量）只能得到空列表 —— 这正是事故成因。
            legacy = [(bot.REMOTE_ALIAS, bot.REMOTE_LABEL)] if bot.REMOTE_ALIAS else []
            self.assertEqual(legacy, [])
        finally:
            bot.AGENT_HOME, bot.REMOTE_ALIAS = saved


if __name__ == '__main__':
    unittest.main(verbosity=2)
