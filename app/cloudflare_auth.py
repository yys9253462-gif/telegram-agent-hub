#!/usr/bin/env python3
"""Cloudflare 授权：设备码流程 + API 令牌校验自省。

两条路并存，各有各的用处：

1) 设备码流程（默认）。
   Cloudflare 的 OIDC 发现文档里明确发布了 device_authorization_endpoint，
   官方支持 RFC 8628。第三方 OAuth 客户端走得通，但需要先注册客户端；
   wrangler 这个官方客户端的 client_id 是公开的，直接借用即可开箱即用。
   好处是把 30 多个权限勾选项压缩成一次「点链接 + 输码」，得到的是一枚
   OAuth 访问令牌（带 refresh_token，默认几天后过期，可续）。

2) 粘贴 API 令牌（备选）。
   完全按需勾权限，最小权限更彻底，且不会过期。适合只要读一个站点、
   或者不想把整账号授出去的情况。

无论走哪条路，最终都只在本机落一份 token，只以 CLOUDFLARE_API_TOKEN
注入任务进程。

⚠ 重要的现实坑：dash.cloudflare.com 前面挂着 Cloudflare 自己的机器人管理规则，
不带 User-Agent 的请求会直接吃 403「Attention Required!」拦截页（和 IP 无关，
本机与国内云主机都一样）。所以这里所有发往 dash 的请求都必须带 wrangler 的 UA
—— 我们借的正是 wrangler 的 client_id，用它的 UA 名正言顺。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = 'https://api.cloudflare.com/client/v4'
VERIFY_URL = API_BASE + '/user/tokens/verify'
ZONES_URL = API_BASE + '/zones?per_page=50'
ACCOUNTS_URL = API_BASE + '/accounts?per_page=20'

DEVICE_CODE_URL = 'https://dash.cloudflare.com/oauth2/device/auth'
TOKEN_URL = 'https://dash.cloudflare.com/oauth2/token'
GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:device_code'
# 这个 UA 不是伪装，是本模块的真实身份：我们就是拿 wrangler 的 client_id 在走流程。
# 顺带解决 dash.cloudflare.com 的机器人拦截（缺 UA 必定 403）。
CLIENT_UA = 'wrangler/4.42.0'
ACCEPT = 'application/json'
# 申请的权限：与官方 `wrangler login` 默认申请的清单完全一致（wrangler 4.x 源码里的
# DefaultScopes），这是这个 client_id 在授权页上确定能通过的全集。
#
# ⚠ 不要往里加清单外的名字：申请设备码那一步不校验权限名，什么都返回成功；
# 真正校验在机主打开的授权页上，一个未知名字整页报 Unknown oauth scope、授权失败
# （2026-10-26 线上踩过：加了 billing:read、dns_records:edit 等，授权页直接拒绝）。
#
# - offline_access 是拿 refresh_token 的前提，没有它访问令牌一过期就得重新授权；
# - user:read 必须包含在内，/user 接口只认它，少了校验会返回 403；
# - 这个客户端拿不到 DNS 记录写入、站点设置、防火墙/WAF、账号成员等权限。需要这些时，
#   用「手动填令牌」粘贴一枚自己在控制台创建的全权限 API 令牌。
DEFAULT_SCOPE = ' '.join((
    'offline_access', 'account:read', 'user:read', 'zone:read', 'ssl_certs:write',
    'workers:write', 'workers_kv:write', 'workers_routes:write', 'workers_scripts:write', 'workers_tail:read',
    'd1:write', 'pages:write', 'ai:write', 'queues:write', 'pipelines:write', 'secrets_store:write',
    'containers:write', 'cloudchamber:write', 'connectivity:admin'))


class CloudflareError(Exception):
    """面向机主的可读错误，不含令牌与响应原文。"""


class DeviceFlowError(CloudflareError):
    """设备码流程中的可预期失败。单独成类是为了让调用方区分
    「流程没走完」与「令牌校验不过」，两者给机主的提示完全不同。"""


def _dash(url, payload, timeout=20):
    """向 dash.cloudflare.com 发一次表单 POST，返回解析后的字典。

    这里单独走一套而不是复用 _get：域名不同（dash 走 WAF、api 不走）、
    方法不同（表单 POST 而不是带 Bearer 的 GET）、错误语义也不同
    （dash 的错误是 OAuth 的 {error, error_description}，不是 success=false）。

    ⚠ 轮询阶段最容易踩的坑：Cloudflare 把 authorization_pending 这种「还没授权完」
    当成 HTTP 400 返回（GitHub 是 200 + error 字段）。如果这里把 4xx 一律当异常抛，
    轮询就永远看不到 authorization_pending，每一轮都被判成「授权失败」，
    整个设备码流程必然走不通。所以 400/401 也要先把响应体解析出来交给调用方判断，
    只有真正的服务端错误与非 JSON 响应才抛。
    """
    data = urllib.parse.urlencode(payload).encode()
    request = urllib.request.Request(url, data=data, headers={
        'Accept': ACCEPT, 'Content-Type': 'application/x-www-form-urlencoded',
        'User-Agent': CLIENT_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # 先读响应体：OAuth 的错误是「状态码 + JSON 错误码」成对出现的，两者都要。
        try:
            body = json.loads(exc.read().decode('utf-8', 'replace'))
        except (ValueError, OSError, AttributeError):
            body = None
        if isinstance(body, dict) and body.get('error'):
            return body
        if exc.code in (403, 503):
            # 403 在 dash 这里有两种成因：client_id 不被接受，或撞上机器人拦截。
            # 两者的处置完全不同，提示必须把两条路都说出来，否则机主只会反复重试。
            raise DeviceFlowError('Cloudflare 拒绝了请求（HTTP ' + str(exc.code) + '）。'
                                  '要么这台机器的网络被 Cloudflare 的机器人防护拦截，'
                                  '要么该 client_id 不被接受。请稍后重试，或改用「粘贴API令牌」。') from None
        if exc.code == 429:
            raise DeviceFlowError('请求过于频繁（429），请稍等几分钟再试。') from None
        raise DeviceFlowError('Cloudflare 授权接口返回 HTTP ' + str(exc.code) + '，请稍后重试。') from None
    except (urllib.error.URLError, OSError):
        raise DeviceFlowError('无法连接 dash.cloudflare.com。若本机位于受限网络，'
                              '需要能直连该域名；请检查本机网络或代理设置。') from None
    except ValueError:
        raise DeviceFlowError('Cloudflare 返回内容不是合法 JSON，可能被中间设备拦截。') from None


def start(client_id, scope=DEFAULT_SCOPE):
    """申请设备码与用户码。

    返回字典含 device_code / user_code / verification_uri /
    verification_uri_complete / expires_in / interval。

    device_code 等同于半个令牌，绝不能再显示给机主 —— 会截图、会进聊天记录。
    Cloudflare 的用户码是 8 位大小写混合（不像 GitHub 的 XXXX-XXXX），
    有效期只有 5 分钟，比 GitHub 短得多，所以调用方要把握好提示语气。
    """
    if not client_id:
        raise DeviceFlowError('未配置 Cloudflare client_id，且内置默认值为空。')
    body = _dash(DEVICE_CODE_URL, {'client_id': client_id, 'scope': scope})
    if body.get('error'):
        raise DeviceFlowError(describe(body))
    for key in ('device_code', 'user_code', 'verification_uri'):
        if not body.get(key):
            raise DeviceFlowError('Cloudflare 返回的设备码信息不完整，请重试。')
    body.setdefault('interval', 5)
    body.setdefault('expires_in', 300)
    return body


def describe(body):
    """把 OAuth 错误码翻成人话。原始英文会直接出现在电报聊天里，不能透传。"""
    codes = {
        'authorization_pending': '用户尚未在 Cloudflare 上完成授权。',
        'slow_down': '轮询过快，Cloudflare 要求放慢节奏。',
        'expired_token': '设备码已过期，请重新发起授权。',
        'access_denied': '你拒绝了授权，这枚设备码已作废。',
        'invalid_client': 'client_id 不被接受。',
        'invalid_grant': '设备码无效或已使用。',
        'invalid_request': '请求参数不合法。',
        'invalid_scope': '申请的权限范围不被允许。',
        'unauthorized_client': '该客户端无权使用设备码流程。',
        'unsupported_grant_type': 'grant_type 参数不正确。',
    }
    code = str(body.get('error') or '').strip()
    detail = codes.get(code, 'Cloudflare 返回未知错误：' + (code or '空'))
    hint = str(body.get('error_description') or '').strip()
    # 只在描述是短句时附上：过长的英文原文对机主没用，还占满屏。
    if hint and len(hint) <= 160 and code not in codes:
        detail += '（' + hint + '）'
    return detail


def poll(client_id, device_code, interval=5, expires_in=300, sleep=time.sleep, timeout=None):
    """轮询换取访问令牌，成功后返回令牌响应（含 access_token / refresh_token）。

    必须遵守返回的 interval，否则会收到 slow_down 并被要求加长间隔。
    整个函数是阻塞的，调用方要放进后台线程，不能占住机器人主循环。
    """
    deadline = time.monotonic() + min(int(expires_in), 900)
    wait = max(5, int(interval))
    while time.monotonic() < deadline:
        if timeout is not None and time.monotonic() > timeout:
            raise DeviceFlowError('等待授权超时，请重新发起。')
        sleep(wait)
        body = _dash(TOKEN_URL, {'client_id': client_id, 'device_code': device_code,
                                 'grant_type': GRANT_TYPE})
        if body.get('access_token'):
            return body
        error = str(body.get('error') or '')
        if error == 'authorization_pending':
            continue
        if error == 'slow_down':
            # 规范要求：收到 slow_down 在原间隔上加 5 秒。
            wait += 5
            continue
        raise DeviceFlowError(describe(body))
    raise DeviceFlowError('设备码已过期，请重新发起授权。')


def refresh(client_id, refresh_token, timeout=20):
    """用 refresh_token 换一枚新的访问令牌。

    OAuth 访问令牌默认几天就过期，过期后任务里的 curl 会直接 401。
    这里保留续期能力，但本轮不做自动续期 —— 只在「授权状态」里给出提示，
    因为自动续期要在后台静默改写凭据文件，失败时很难被发现。
    """
    body = _dash(TOKEN_URL, {'client_id': client_id, 'refresh_token': refresh_token,
                             'grant_type': 'refresh_token'}, timeout=timeout)
    if not body.get('access_token'):
        raise DeviceFlowError(describe(body))
    return body


def whoami(token, timeout=20):
    """用令牌查一次身份，确认令牌真的有效，并记下登录的是谁。

    这是行为验证而不是看状态字段：OAuth 访问令牌不能走 /user/tokens/verify
    （那是给 API 令牌用的），所以真正可靠的判定就是真发一个请求看返回。

    /user 需要 user:read 权限。令牌没给这一项时它会返回 403，但那不代表令牌坏了 ——
    这时改用令牌有权访问的接口（/zones、/accounts）验证，验证通过就照常保存，
    只是拿不到账号邮箱。这两种情况的区别必须讲清楚，否则机主会以为令牌被撤销，
    白白重新授权一次。
    """
    request = urllib.request.Request(API_BASE + '/user', headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
        'User-Agent': 'telegram-agent-hub'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise CloudflareError('令牌已失效或被撤销，请重新授权。') from None
        if exc.code == 403:
            return _verify_without_identity(token, timeout)
        raise CloudflareError('令牌校验失败（HTTP ' + str(exc.code) + '），可能已撤销或权限不足。') from None
    except (urllib.error.URLError, OSError, ValueError):
        raise CloudflareError('无法连接 Cloudflare 校验令牌。') from None
    result = (body or {}).get('result') or {}
    return {'email': result.get('email') or '', 'id': result.get('id') or '',
            'identity': True}


def _verify_without_identity(token, timeout=20):
    """令牌没有 user:read 时，改用有权访问的接口证明它可用。

    只要有一个授权范围内的接口返回成功，就说明令牌本身有效，
    只是读不到账号信息 —— 这时应当把令牌保存下来继续用。
    """
    for url in (ZONES_URL, ACCOUNTS_URL):
        request = urllib.request.Request(url, headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
            'User-Agent': 'telegram-agent-hub'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise CloudflareError('令牌已失效或被撤销，请重新授权。') from None
            continue
        except (urllib.error.URLError, OSError, ValueError):
            raise CloudflareError('无法连接 Cloudflare 校验令牌。') from None
        if body.get('success'):
            return {'email': '', 'id': '', 'identity': False}
    raise CloudflareError('令牌能取到，但没有任何可用的访问权限，请重新授权并同意所需权限。')


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
