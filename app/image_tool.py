#!/usr/bin/env python3
"""供两种执行器调用的标准输入输出图片工具，密钥只由桥接程序读取。

协议约束：标准输出只能写 JSON-RPC 报文，任何日志、异常回显都必须改道标准错误。
"""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid

BASE_DIR = Path(__file__).resolve().parent


def image_config():
    return Path(os.environ.get('IMAGE_CONFIG_FILE', str(Path('/etc/telegram-agent-hub') / 'image.json')))


def generate(arguments):
    prompt = arguments.get('prompt', '')
    prompt = prompt.strip() if isinstance(prompt, str) else ''
    if not prompt:
        raise ValueError('图片描述不能为空')
    if len(prompt) > 12000:
        raise ValueError('图片描述最多一万二千字符')
    size = arguments.get('size') or '1024x1024'
    if size not in ('1024x1024', '1536x1024', '1024x1536'):
        raise ValueError('不支持的画幅')
    config_file = image_config()
    if not config_file.exists():
        raise RuntimeError('尚未配置生图通道')
    spec = importlib.util.spec_from_file_location('image_bridge', BASE_DIR / 'bot.py')
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    bridge.TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
    bridge.BASE = os.environ.get('TELEGRAM_API_BASE', 'https://api.telegram.org').rstrip('/') \
        + '/bot' + bridge.TOKEN + '/'
    task = {'id': uuid.uuid4().hex[:12], 'chat': int(os.environ['AGENT_IMAGE_CHAT_ID']),
            'text': prompt, 'payload': {**bridge.image_options(), 'size': size}}
    # 桥接程序内部会打印进度，全部改道标准错误，保证标准输出纯净。
    with contextlib.redirect_stdout(sys.stderr):
        bridge.generate_image(task)
    # 图片与说明已直接发送给机主，这里只回一句结果给模型，不回显路径以外的配置。
    return task.get('result') or '图片已生成并发送给机主'


def tool_definition():
    return {
        'name': 'generate_image',
        'description': (
            '使用机主已配置的私有接口生成图片，并直接回传到当前电报聊天。'
            '当用户以自然语言要求画图、生成图片、做海报、插画、头像、配图或“画一张…”时调用本工具。'
            '不需要向用户索取密钥或接口地址；不要改用旧的内置生图通道或第三方免费图库。'
            '成功时返回模型、画幅与文件路径；失败时如实报告失败，不得声称已经出图。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'prompt': {'type': 'string', 'description': '完整的图片描述，含主体、风格、构图、色调等细节'},
                'size': {'type': 'string', 'enum': ['1024x1024', '1536x1024', '1024x1536'],
                         'description': '画幅：方形、横图、竖图，缺省方形'},
            },
            'required': ['prompt'],
            'additionalProperties': False,
        },
    }


def handle(request):
    method = request.get('method')
    params = request.get('params') or {}
    if method == 'initialize':
        return {'protocolVersion': params.get('protocolVersion', '2024-11-05'),
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'private-image-channel', 'version': '1.1.0'}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [tool_definition()]}
    if method == 'tools/call':
        if params.get('name') != 'generate_image':
            raise ValueError('未知工具')
        arguments = params.get('arguments') or {}
        try:
            text = generate(arguments)
            return {'content': [{'type': 'text', 'text': text}], 'isError': False}
        except Exception as exc:
            # 只给异常类型，不回显请求体、响应体、请求头或配置内容。
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            return {'content': [{'type': 'text',
                                 'text': '图片未完成：' + reason + '。请如实告知机主失败，不要索取密钥或改用其他通道。'}],
                    'isError': True}
    raise ValueError('不支持的方法')


def main():
    # 行缓冲，避免批量输入时响应滞后。
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            print(json.dumps({'jsonrpc': '2.0', 'id': None,
                              'error': {'code': -32700, 'message': '报文不是合法JSON'}}), flush=True)
            continue
        if not isinstance(request, dict) or 'id' not in request:
            # 通知类报文无需响应。
            continue
        try:
            result = handle(request)
            reply = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
        except Exception as exc:
            reply = {'jsonrpc': '2.0', 'id': request['id'],
                     'error': {'code': -32603, 'message': str(exc) if isinstance(exc, ValueError) else '图片工具内部错误'}}
        print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
