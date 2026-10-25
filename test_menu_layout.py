"""菜单结构测试。

核心目的不是数按钮，而是保证：顶层和各级分组里出现的每个按钮，
在 handle_text / quick_action 里都真有分支接住它。
前科：菜单项改版后按钮还在、处理分支被删，点下去毫无反应且不报错。
"""
import ast
import os
import unittest
from pathlib import Path

SOURCE = Path(__file__).with_name('bot.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)


def _load_constants():
    values = {}
    for node in TREE.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ('KEYBOARD', 'MENU_GROUPS'):
                values[target.id] = ast.literal_eval(node.value)
    missing = {'KEYBOARD', 'MENU_GROUPS'} - set(values)
    if missing:
        raise AssertionError('未能从 bot.py 解析出常量：' + '、'.join(sorted(missing)))
    return values['KEYBOARD'], values['MENU_GROUPS']


KEYBOARD, MENU_GROUPS = _load_constants()


def top_buttons():
    return [label for row in KEYBOARD['keyboard'] for label in row]


def grouped_buttons():
    """所有二级分组里出现的按钮。"""
    buttons = []
    for groups in MENU_GROUPS.values():
        for items in groups.values():
            buttons.extend(items)
    return buttons


def handled_labels():
    """扫描源码里被比较过的字符串字面量，当作「有分支接住」的证据。"""
    labels = set()
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Compare):
            continue
        for operand in [node.left, *node.comparators]:
            if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                labels.add(operand.value)
            elif isinstance(operand, (ast.Tuple, ast.List, ast.Set)):
                for item in operand.elts:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        labels.add(item.value)
        # 形如 text.startswith('发送文件 ', ...) 的前缀分支
        if isinstance(operand, ast.Call) and isinstance(operand.func, ast.Attribute):
            for argument in operand.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    labels.add(argument.value.strip())
    return labels


class TopLevelLayoutTest(unittest.TestCase):
    def test_display_buttons_resolve_to_original_commands(self):
        """展示文案美化后，点击按钮必须仍能进入原来的处理分支。"""
        import bot
        rendered = bot.menu_keyboard()
        for original, display in zip(KEYBOARD['keyboard'], rendered['keyboard']):
            self.assertEqual([bot.ALIASES.get(label, label) for label in display], original)
        for command, label in bot.MENU_LABELS.items():
            self.assertEqual(bot.ALIASES.get(label, label), command)

    def test_top_level_is_compact(self):
        """顶层最多三行，否则又会糊住聊天界面。"""
        self.assertLessEqual(len(KEYBOARD['keyboard']), 3,
                             '顶层不应超过三行')

    def test_top_level_contains_high_frequency_actions(self):
        """这些是真正天天按的，不能在精简中被误删。"""
        for label in ('新任务', '当前任务', '停止', '服务器状态'):
            self.assertIn(label, top_buttons(), label + ' 必须留在顶层')

    def test_top_level_has_two_group_entries(self):
        for entry in ('设置', '更多'):
            self.assertIn(entry, top_buttons(), entry + ' 必须在顶层作为二级入口')

    def test_no_blank_rows(self):
        for row in KEYBOARD['keyboard']:
            self.assertTrue(row, '顶层不能有空行')


class GroupMenuTest(unittest.TestCase):
    def test_every_group_is_non_empty(self):
        for owner, groups in MENU_GROUPS.items():
            self.assertTrue(groups, owner + ' 下没有分组')
            for name, items in groups.items():
                self.assertTrue(items, owner + ' › ' + name + ' 是空分组')

    def test_group_is_not_used_as_button(self):
        """分组名不能再被当成功能按钮，否则点下去只会再展开一次自己。"""
        grouped = grouped_buttons()
        for groups in MENU_GROUPS.values():
            for name in groups:
                self.assertNotIn(name, grouped, '分组名「' + name + '」不能被当成按钮')

    def test_grouped_buttons_are_unique(self):
        grouped = grouped_buttons()
        self.assertEqual(len(grouped), len(set(grouped)),
                         '同一个按钮不应出现在两个分组里')

    def test_top_and_grouped_do_not_overlap(self):
        """顶层已经有的按钮，不应再在二级菜单里重复出现。"""
        overlap = set(top_buttons()) & set(grouped_buttons())
        self.assertFalse(overlap, '重复出现：' + '、'.join(sorted(overlap)))

    def test_every_old_button_still_reachable(self):
        """改版不能弄丢功能：原来顶层的功能都要能在顶层或二级找到。"""
        legacy = {'新任务', '当前任务', '停止', '模型',
                  '选择智能体', '新建会话', '会话列表', '服务器状态'}
        reachable = set(top_buttons()) | set(grouped_buttons())
        missing = legacy - reachable
        self.assertFalse(missing, '功能入口丢失：' + '、'.join(sorted(missing)))


class HandlerCoverageTest(unittest.TestCase):
    def test_every_top_button_has_handler(self):
        labels = handled_labels()
        missing = [label for label in top_buttons() if label not in labels]
        self.assertFalse(missing, '顶层按钮没有处理分支：' + '、'.join(missing))

    def test_every_grouped_button_has_handler(self):
        labels = handled_labels()
        missing = [label for label in grouped_buttons() if label not in labels]
        self.assertFalse(missing, '二级按钮没有处理分支：' + '、'.join(missing))

    def test_removed_button_still_compatible(self):
        """「当前智能体」已从菜单移除，但旧键盘和习惯输入仍要能用。"""
        self.assertNotIn('当前智能体', top_buttons())
        self.assertIn('当前智能体', handled_labels())

    def test_group_owner_message_mentions_current_engine(self):
        """执行器现状直接写在入口标题里，所以不再需要单独的「当前智能体」按钮。"""
        self.assertIn('current_engine()', SOURCE)
        block = SOURCE.split("elif text in ('选择智能体', '/agent'):", 1)
        self.assertEqual(len(block), 2, '未找到选择智能体分支')
        self.assertIn('current_engine()', block[1].split('elif', 1)[0],
                      '选择智能体标题里应当显示当前执行器')


class DuplicateTailTest(unittest.TestCase):
    """消息内联按钮不应覆盖首页键盘或追加导航尾部。"""

    def test_no_placeholder_feature_in_menu(self):
        """「巡检配置」在通用版里只是「尚未启用」的占位回复，不能放进菜单误导用户。"""
        reachable = set(top_buttons()) | set(grouped_buttons())
        self.assertNotIn('巡检配置', reachable,
                         '占位功能不应出现在菜单里')

    def test_send_options_keeps_home_keyboard_without_navigation_tail(self):
        import bot
        from unittest.mock import patch
        with patch.object(bot, 'api') as api, patch.dict(bot.ACTION_UI, clear=True):
            bot.send_options(123, '功能', ['模型', '返回主菜单', '收起菜单'])
            markup = api.call_args.args[1]['reply_markup']
            self.assertEqual(set(markup), {'inline_keyboard'})
            buttons = [button for row in markup['inline_keyboard'] for button in row]
            self.assertEqual([button['text'] for button in buttons], ['模型设置'])

    def test_no_group_lists_the_same_button_twice(self):
        for owner, groups in MENU_GROUPS.items():
            for name, items in groups.items():
                dupes = [x for x in set(items) if items.count(x) > 1]
                self.assertFalse(dupes, owner + ' › ' + name + ' 有重复按钮：' + '、'.join(dupes))


if __name__ == '__main__':
    unittest.main(verbosity=2)
