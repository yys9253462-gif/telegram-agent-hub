#!/usr/bin/env python3
"""五轮交互测试 · 第一轮：入口、按钮、群组与打字命令的路由。

测试视角是「机主真的按了一下」而不是「源码里有没有这个字符串」：
每一个顶层按钮、每一个二级按钮、每一个分组名、每一条快捷指令，
都真的喂给 handle_text，断言它要么给出回复、要么抛 MediaError，
绝不能出现「点了没反应也不报错」。

第二轮起依次覆盖队列与并发、渠道与执行器切换、授权与登录、边界与异常。
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bot

# 这些指令的回复由后台线程发出（版本检查要访问网络、登录要拉起官方 CLI），
# 断言时要等线程跑完，不能按同步返回去数。
BACKGROUND = {'检查版本', '更新机器人', '登录Claude', '登录CodeBuddy', '登录Codex',
              '授权GitHub', '授权Cloudflare'}
# 这些按钮在当前环境下必须被**明确拒绝**（账号没登录、走的是接口密钥渠道），
# 拒绝可以，但不能一声不吭——用户按了按钮却什么都没有，才是真缺陷。
EXPECTED_REFUSALS = {'账号模型', '使用账号 claude', '确认更新机器人'}


class InteractionBase(unittest.TestCase):
    """所有交互测试共用的替身环境：不联网、不落真实状态。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = bot.TaskController(self.root)
        self.sent = []
        self.enqueued = []
        # 把状态文件全部重定向到临时目录，避免污染本机真实配置。
        self.saved_paths = {}
        paths = ('WORK', 'STATE', 'SESSION_STATE', 'MODEL_STATE', 'MODEL_CACHE',
                 'STRATEGY_STATE', 'MENU_STATE', 'IMAGE_OPTIONS', 'AUTH_STATE', 'USAGE')
        for name in paths:
            if hasattr(bot, name):
                self.saved_paths[name] = getattr(bot, name)
        self.patches = [
            patch.object(bot, 'CONTROL', self.controller),
            patch.object(bot, 'OWNER', 123),
            patch.object(bot, 'send', side_effect=lambda chat, text: self.sent.append(text)),
            # 带按钮的回复走 api('sendMessage')，不进 send()。两条出口都要记，
            # 否则「有按钮的菜单」会被误判成没反应。
            patch.object(bot, 'api', side_effect=self._record),
            patch.object(bot, 'send_rich', side_effect=lambda chat, html: self.sent.append(html)),
            patch.object(bot, 'notify', side_effect=lambda chat, text: self.sent.append(text)),
            patch.object(bot, 'STRATEGY_STATE', self.root / 'strategy.json'),
            patch.object(bot, 'MENU_STATE', self.root / 'menu.json'),
            patch.object(bot, 'AUTH_STATE', self.root / 'auth.json'),
            patch.object(bot, 'MODEL_STATE', self.root / 'model.json'),
        ]
        for item in self.patches:
            item.start()
        bot.ACTION_UI.clear()
        bot.ACTION_UI_SEEN.clear()
        self.addCleanup(self._teardown)

    def _record(self, method, payload=None):
        """接管 api 出口，把发出的文案同样记进回复列表。"""
        if isinstance(payload, dict) and payload.get('text'):
            self.sent.append(payload['text'])
        return {'message_id': len(self.sent) + 1}

    def _teardown(self):
        for item in reversed(self.patches):
            item.stop()
        for name, value in self.saved_paths.items():
            setattr(bot, name, value)
        bot.ACTION_UI.clear()
        bot.ACTION_UI_SEEN.clear()
        bot.PENDING_TOKEN.update({'provider': '', 'chat': None, 'expires_at': 0})
        self.temp.cleanup()

    def press(self, text, chat=123):
        """模拟机主按下一个按钮或输入一条命令，返回它造成的回复列表。

        后台线程（版本检查、设备码轮询）不在本测试的调度范围内，
        见到就跳过并记账，由后面的轮次单独覆盖。
        """
        before = len(self.sent)
        error = None
        try:
            self.controller.handle_text(chat, text)
        except bot.MediaError as exc:
            # MediaError 是「用一句人话拒绝」的正常路径，收集起来供断言。
            error = str(exc)
        self.assertGreater(len(self.sent), before, '「' + text + '」没有任何回复')
        return self.sent[before:]

    def press_and_wait(self, text, chat=123):
        """按下之后等一句回复。用于回复由后台线程发出的指令。

        本机没装执行器时，登录类指令会当场抛 MediaError（「找不到 claude 命令」）。
        那是环境如实反馈，不是交互缺陷，照样算「有反应」。
        """
        before = len(self.sent)
        try:
            self.controller.handle_text(chat, text)
        except bot.MediaError as exc:
            # 拒绝也是一种回复：handle_update 的兜底分支会把原因发给机主。
            self.sent.append(str(exc))
            return self.sent[before:]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(self.sent) == before:
            time.sleep(0.02)
        self.assertGreater(len(self.sent), before, '「' + text + '」没有任何回复')
        return self.sent[before:]


class TopLevelButtonTest(InteractionBase):
    """顶层三行按钮逐个真按一遍。"""

    def labels(self):
        return [label for row in bot.KEYBOARD['keyboard'] for label in row]

    def test_every_top_button_answers(self):
        for label in self.labels():
            with self.subTest(button=label):
                # 顶层按钮用的是展示文案，必须能映射回内部命令。
                command = bot.ALIASES.get(label, label)
                self.press(command)

    def test_every_display_label_answers(self):
        """用户在手机键盘上看到的是 MENU_LABELS 里的文案，按的是文案本身。"""
        for label in self.labels():
            shown = bot.MENU_LABELS.get(label, label)
            with self.subTest(button=shown):
                self.press(bot.ALIASES.get(shown, shown))

    def test_unknown_text_becomes_task(self):
        """普通文字必须进任务队列，不能被某个分支误吞。

        入队后没有立即回复是设计如此：起始消息由执行器在任务真正启动时发出，
        所以这里断言的是「进了队列」，而不是「回了一句话」。
        """
        before = len(self.sent)
        try:
            self.controller.handle_text(123, '把 /tmp 下昨天生成的日志按大小列一下')
        except bot.MediaError as exc:  # pragma: no cover - 入队不应被拒绝
            self.fail('普通任务被拒绝：' + str(exc))
        self.assertEqual(len(self.sent), before, '入队不应额外发消息')
        with self.controller.lock:
            tasks = [item for item in self.controller.data['tasks'] if item['kind'] == 'agent']
        self.assertTrue(tasks, '普通文本没有进入任务队列')
        self.assertEqual(tasks[-1]['status'], 'queued')


class GroupButtonTest(InteractionBase):
    """二级分组里的每一个按钮都要有反应。"""

    def grouped(self):
        for owner, groups in bot.MENU_GROUPS.items():
            for name, items in groups.items():
                for item in items:
                    yield owner, name, item

    def test_every_grouped_button_answers(self):
        for owner, name, item in self.grouped():
            with self.subTest(group=owner + '/' + name, button=item):
                if item in BACKGROUND:
                    self.press_and_wait(item)
                elif item in EXPECTED_REFUSALS:
                    # 渠道是接口密钥时必须被明确拒绝，而且要说清去哪儿切渠道。
                    with self.assertRaises(bot.MediaError) as caught:
                        self.controller.handle_text(123, item)
                    self.assertIn('渠道切换', str(caught.exception),
                                  item + ' 的拒绝说明没告诉用户怎么办')
                else:
                    self.press(item)

    def test_group_owner_opens_menu(self):
        for owner in bot.MENU_GROUPS:
            with self.subTest(group=owner):
                replies = self.press(owner)
                # 二级入口显示的是 MENU_LABELS 里的文案（设置 → 模型与会话），
                # 所以比对展示名而不是内部命令名。
                shown = bot.MENU_LABELS.get(owner, owner)
                self.assertTrue(any(shown in text for text in replies),
                                owner + ' 打开的分组菜单没有带上入口名：' + repr(replies))

    def test_group_name_expands_not_treated_as_task(self):
        """分组名（如「任务与模型」）必须展开分组，不能被当成任务发给模型。"""
        names = [name for groups in bot.MENU_GROUPS.values() for name in groups]
        for name in names:
            with self.subTest(group=name):
                self.press(name)
        with self.controller.lock:
            kinds = [item['kind'] for item in self.controller.data['tasks']]
        self.assertNotIn('agent', kinds, '分组名被当成了任务：' + repr(kinds))


class QuickCommandTest(InteractionBase):
    """快捷指令与别名。"""

    def test_strategy_switch_takes_effect(self):
        for name in bot.STRATEGIES:
            with self.subTest(strategy=name):
                self.press(name)
                self.assertEqual(bot.strategy_name(), name)

    def test_unknown_strategy_rejected(self):
        """未知策略必须被明确拒绝。

        拒绝方式有两种：入队后由执行器报错，或当场报 MediaError。
        下一条消息既没改策略、也没排队，才是真正的「静默吞掉」。
        """
        before = bot.strategy_name()
        before_tasks = len(self.controller.data['tasks'])
        try:
            self.controller.handle_text(123, '自由发挥')
        except bot.MediaError:
            pass
        self.assertEqual(bot.strategy_name(), before, '未知策略不该改动当前策略')
        self.assertEqual(len(self.controller.data['tasks']), before_tasks + 1,
                         '未知策略应当作为任务正文排队，而不是既不改策略也不排队')

    def test_menu_hide_and_show(self):
        """收起与展开是一对，不能只改其中一个方向。"""
        self.press('收起菜单')
        self.assertFalse(bot.menu_visible())
        self.press('显示菜单')
        self.assertTrue(bot.menu_visible())

    def test_alias_targets_exist(self):
        """别名表里的每一项都要能落到一个真分支，不能指向已被删除的命令。"""
        for alias in bot.ALIASES:
            with self.subTest(alias=alias):
                command = bot.ALIASES.get(alias, alias)
                if command in BACKGROUND:
                    self.press_and_wait(command)
                else:
                    self.press(command)

    def test_empty_alias_target_is_not_self_mapping(self):
        dead = [key for key, value in bot.ALIASES.items() if key == value]
        self.assertFalse(dead, '别名表里有自我映射的死项：' + '、'.join(dead))


if __name__ == '__main__':
    unittest.main(verbosity=2)
