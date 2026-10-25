#!/usr/bin/env python3
"""Cloudflare API 令牌的校验与自省。

为什么不是 OAuth：Cloudflare 对第三方客户端只开放授权码流程，明确不支持
设备码，也不支持客户端凭据；授权码流程要求一个真实的 HTTPS 回调地址。
机器人跑在无公网回调的服务端，因此这里走「粘贴 API 令牌」这条路 ——
令牌本身是可撤销、可按站点限定权限的最小权限凭据，比复用账号密码安全得多。

本模块只做两件事：验证令牌活着、读出它能碰哪些资源。
"""
import json
import time
import urllib.error
import urllib.request

API_BASE = 'https://api.cloudflare.com/client/v4'
VERIFY_URL = API_BASE + '/user/tokens/verify'
ZONES_URL = API_BASE + '/zones?per_page=50'
ACCOUNTS_URL = API_BASE + '/accounts?per_page=20'


class CloudflareError(Exception):
    """面向机主的可读错误，不含令牌与响应原文。"""


def _get(url, token, timeout=20):
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token,
                                                  'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise CloudflareError('令牌无效或权限不足（HTTP ' + str(exc.code) + '）。') from None
        if exc.code == 429:
            raise CloudflareError('请求过于频繁（429），请稍后重试。') from None
        raise CloudflareError('Cloudflare 接口返回 HTTP ' + str(exc.code) + '。') from None
    except (urllib.error.URLError, OSError):
        raise CloudflareError('无法连接 Cloudflare。若本机位于受限网络，需要能直连 api.cloudflare.com；'
                              '请检查本机网络或代理设置。') from None
    except ValueError:
        raise CloudflareError('Cloudflare 返回内容不是合法 JSON，可能被中间设备拦截。') from None
    if not isinstance(body, dict):
        raise CloudflareError('Cloudflare 返回结构异常。')
    return body


def verify(token, timeout=20):
    """查令牌状态。返回 {status, id, expires_on}。

    注意：verify 通过只代表令牌本身是活的，不代表它对某个站点有权限。
    真正的权限要看后面 zones / accounts 的返回。
    """
    body = _get(VERIFY_URL, token, timeout=timeout)
    if not body.get('success'):
        raise CloudflareError('令牌校验未通过。')
    result = body.get('result') or {}
    status = str(result.get('status') or '').lower()
    if status and status != 'active':
        raise CloudflareError('令牌状态为 ' + status + '，不可用。')
    return {'status': status or 'unknown', 'id': result.get('id') or '',
            'expires_on': result.get('expires_on')}


def zones(token, timeout=20):
    """列出令牌能看见的站点。

    这里既是「权限探测」也是「给人看的作用范围」。令牌没有 Zone 权限时
    Cloudflare 会返回 success=false 或空列表，两种情况都当作「看不到站点」，
    不抛异常 —— 因为一个只做账号级操作（如 Workers）的令牌本来就没有 Zone 权限。
    """
    body = _get(ZONES_URL, token, timeout=timeout)
    if not body.get('success'):
        return []
    names = []
    for item in body.get('result') or []:
        if isinstance(item, dict) and item.get('name'):
            names.append({'name': item['name'], 'status': item.get('status') or '',
                          'id': item.get('id') or ''})
    return names


def accounts(token, timeout=20):
    """列出令牌能看见的账号。只取名字和 id，不取计费信息。"""
    body = _get(ACCOUNTS_URL, token, timeout=timeout)
    if not body.get('success'):
        return []
    result = []
    for item in body.get('result') or []:
        if isinstance(item, dict) and item.get('id'):
            result.append({'id': item['id'], 'name': item.get('name') or ''})
    return result


def judge(token, timeout=20):
    """一次性给出「这枚令牌能不能用、能碰什么」的结论。

    顺序有讲究：先 verify（判定死活），再列范围（判定权限）。
    verify 失败必须抛出，因为那说明令牌根本不能用；范围查询失败只降级为空。
    """
    started = time.monotonic()
    state = verify(token, timeout=timeout)
    scope = {'zones': zones(token, timeout=timeout), 'accounts': accounts(token, timeout=timeout)}
    return {'verify': state, 'scope': scope, 'elapsed': round(time.monotonic() - started, 2)}
