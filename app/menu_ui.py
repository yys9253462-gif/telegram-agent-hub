"""内联菜单与工作台文案渲染辅助模块。"""
import json
import urllib.error


def format_home_menu_text(keyboard, home_labels, descriptions, model, strategy, has_session):
    """渲染工作台首页正文，展示当前运行环境与底部快捷按钮说明。"""
    legend = [
        '• ' + home_labels.get(cmd, cmd) + '：' + descriptions[cmd]
        for row in keyboard['keyboard']
        for cmd in row
        if cmd in descriptions
    ]
    return ('🤖 智能体工作台\n'
            '────────────────\n'
            '当前大脑  ·  ' + model + '\n'
            '执行策略  ·  ' + strategy + '\n'
            '当前对话  ·  ' + ('继续当前会话' if has_session else '准备开始新会话') + '\n'
            '────────────────\n'
            '直接发送文字、语音、图片或文件就能派发任务。\n\n'
            '📌 底部快捷按钮说明：\n' + '\n'.join(legend))


def format_group_title(menu_groups, menu_labels, group_icons, group_descriptions, owner, group, summary=''):
    """渲染设置主目录或入口分类页正文，附带每组功能职责说明。"""
    if owner == '设置':
        groups = menu_groups.get('设置', {})
        legend = [
            '• ' + group_icons.get(name, name) + '：' + group_descriptions.get(name, '')
            for name in groups
        ]
        parts = [menu_labels.get(group, group)]
        if summary:
            parts.append(summary)
        parts.append('请选择设置类别（各按钮职责）：\n' + '\n'.join(legend))
        return '\n\n'.join(parts)
    intro = {'更多': '直接连接平台，查看或撤销授权。'}
    return menu_labels.get(group, group) + '\n' + intro.get(owner, '选择下面的功能。')


def format_group_expand(menu_groups, menu_labels, group_icons, group_descriptions, descriptions, text):
    """匹配二级分组并返回展开后的正文与带「‹ 返回」的按钮列表。"""
    for owner, groups in menu_groups.items():
        items = groups.get(text)
        if items:
            back = [('‹ 返回', owner)] if len(groups) > 1 else []
            header = menu_labels.get(owner, owner) + ' / ' + group_icons.get(text, text)
            desc = group_descriptions.get(text, '选择下面的功能。')
            details = '\n'.join(
                '• ' + menu_labels.get(item, item) + '：' + descriptions[item]
                for item in items if item in descriptions
            )
            body = header + '\n' + desc + ('\n\n按钮功能说明：\n' + details if details else '')
            return body, list(items) + back
    return None


def _http_error_description(exc):
    """尽量读取界面接口返回的错误描述，读不到时返回空串。"""
    try:
        raw = exc.read()
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='replace')
        data = json.loads(raw)
        return str(data.get('description') or '')
    except (OSError, ValueError, AttributeError, TypeError):
        return ''


def dispatch_inline_menu(api_fn, chat, text, inline_markup, redact_fn, message_id=None, swallow_fn=None):
    """发送或原地编辑一条内联菜单消息。

    当存在 message_id 时先调用 editMessageText 原地刷新当前菜单，避免每次点按钮都刷出新气泡；
    若原文未变更则静默成功；若消息不可编辑或触发 400，则自动回退到 sendMessage。
    """
    safe_text = redact_fn(text)
    if message_id is not None:
        try:
            api_fn('editMessageText', {
                'chat_id': chat,
                'message_id': message_id,
                'text': safe_text,
                'reply_markup': inline_markup,
            })
            return 'edited'
        except urllib.error.HTTPError as exc:
            desc = _http_error_description(exc).lower()
            if 'message is not modified' in desc:
                return 'not_modified'
            if swallow_fn is not None:
                swallow_fn('原地更新内联菜单', exc)
        except Exception as exc:
            if swallow_fn is not None:
                swallow_fn('原地更新内联菜单', exc)
    api_fn('sendMessage', {
        'chat_id': chat,
        'text': safe_text,
        'reply_markup': inline_markup,
    })
    return 'sent'
