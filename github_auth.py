#!/usr/bin/env python3
"""GitHub OAuth 设备码流程（Device Authorization Grant）的客户端实现。

为什么选设备码：机器人跑在无浏览器的服务器上，设备码流程是 GitHub 官方
为无头程序提供的路径 —— 只要 client_id（无 secret），不需要回调地址，
也不需要公网可达。整个流程只有三步：申请码 → 机主在别处输码 → 轮询换令牌。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

DEVICE_CODE_URL = 'https://github.com/login/device/code'
TOKEN_URL = 'https://github.com/login/oauth/access_token'
USER_URL = 'https://api.github.com/user'
# 设备码请求只接受 JSON 响应；不带这个头拿到的是表单编码文本，解析会失败。
ACCEPT = 'application/json'
GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:device_code'


class DeviceFlowError(Exception):
    """设备码流程中的可预期失败，message 直接面向机主，不含敏感信息。"""


def _request(url, payload, timeout=20):
    data = urllib.parse.urlencode(payload).encode()
    request = urllib.request.Request(url, data=data, headers={'Accept': ACCEPT,
                                                             'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # 404 基本只有一个原因：client_id 不存在或应用被删。
        # 这种错误提示成「稍后重试」会让人一直重试一个永远失败的操作，
        # 必须直接指向真正要改的东西。
        if exc.code == 404:
            raise DeviceFlowError('GitHub 不认这个 client_id（404）。'
                                  '请确认 OAuth App 仍然存在、client_id 复制完整，'
                                  '并在应用设置里勾选 Enable Device Flow。') from None
        if exc.code == 403:
            raise DeviceFlowError('GitHub 拒绝了请求（403），通常是设备码申请频率超限（每小时 50 次），'
                                  '请等一段时间再试。') from None
        raise DeviceFlowError('GitHub 接口返回 ' + str(exc.code) + '，请稍后重试。') from None
    except (urllib.error.URLError, OSError):
        raise DeviceFlowError('无法连接 GitHub。若本机位于受限网络，需要能直连 github.com；'
                              '请检查本机网络或代理设置。') from None
    except ValueError:
        raise DeviceFlowError('GitHub 返回内容不是合法 JSON，可能被中间设备拦截。') from None


def start(client_id, scope='repo read:org gist'):
    """申请设备码与用户码。

    返回字典含 device_code / user_code / verification_uri / expires_in / interval。
    device_code 是换取令牌的凭据，绝不能再显示给机主（会截图、会进聊天记录）。
    这个接口对同一 OAuth App 有每小时 50 次的上限，所以调用方要拦重复发起。
    """
    if not client_id:
        raise DeviceFlowError('未配置 GitHub client_id，请先在安装器或配置里填写。')
    body = _request(DEVICE_CODE_URL, {'client_id': client_id, 'scope': scope})
    if body.get('error'):
        raise DeviceFlowError(describe(body))
    for key in ('device_code', 'user_code', 'verification_uri'):
        if not body.get(key):
            raise DeviceFlowError('GitHub 返回的设备码信息不完整，请重试。')
    body.setdefault('interval', 5)
    body.setdefault('expires_in', 900)
    return body


def describe(body):
    """把 GitHub 的错误码翻成人话。

    这些错误码是文档里明列的固定集合，写死在这里比透传原始英文更好，
    因为原始文本会直接出现在电报聊天里。
    """
    codes = {
        'authorization_pending': '用户尚未在 GitHub 上完成授权。',
        'slow_down': '轮询过快，GitHub 要求放慢节奏。',
        'expired_token': '设备码已过期，请重新发起授权。',
        'access_denied': '你取消了授权，这枚设备码已作废。',
        'incorrect_client_credentials': 'client_id 不正确。',
        'incorrect_device_code': '设备码无效。',
        'device_flow_disabled': '该 OAuth App 未启用设备码流程，请到应用设置里勾选 Enable Device Flow。',
        'unsupported_grant_type': 'grant_type 参数不正确。',
    }
    code = str(body.get('error') or '').strip()
    return codes.get(code, 'GitHub 返回未知错误：' + (code or '空'))


def poll(client_id, device_code, interval=5, expires_in=900, sleep=time.sleep, timeout=None):
    """轮询换取访问令牌，成功后返回令牌响应。

    必须遵守 GitHub 给的最小间隔，否则会收到 slow_down 并追加 5 秒惩罚。
    这整个函数是阻塞的，调用方要放在后台线程里，不能占用机器人主循环。
    """
    deadline = time.monotonic() + min(int(expires_in), 900)
    wait = max(5, int(interval))
    while time.monotonic() < deadline:
        if timeout is not None and time.monotonic() > timeout:
            raise DeviceFlowError('等待授权超时，请重新发起。')
        sleep(wait)
        body = _request(TOKEN_URL, {'client_id': client_id, 'device_code': device_code,
                                    'grant_type': GRANT_TYPE})
        if body.get('access_token'):
            return body
        error = str(body.get('error') or '')
        if error == 'authorization_pending':
            continue
        if error == 'slow_down':
            # 文档明确：收到 slow_down 要在原间隔上再加 5 秒。
            wait += 5
            continue
        raise DeviceFlowError(describe(body))
    raise DeviceFlowError('设备码已过期，请重新发起授权。')


def whoami(token, timeout=15):
    """用令牌查一次当前账号，用来确认令牌真的有效、并记下登录的是谁。

    这是「行为验证」而不是「看状态字段」：GitHub 的令牌没有自检接口，
    唯一可靠的判定就是真发一个请求看返回。
    """
    request = urllib.request.Request(USER_URL, headers={'Accept': ACCEPT,
                                                       'Authorization': 'Bearer ' + token,
                                                       'User-Agent': 'telegram-agent-hub'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        raise DeviceFlowError('令牌校验失败（HTTP ' + str(exc.code) + '），可能已撤销或权限不足。') from None
    except (urllib.error.URLError, OSError, ValueError):
        raise DeviceFlowError('无法连接 GitHub 校验令牌。') from None
    return {'login': body.get('login') or '', 'name': body.get('name') or '',
            'id': body.get('id')}
