#!/usr/bin/env python3
"""订阅套餐用量与套餐信息：读取 Claude / Codex 的 5 小时与 7 天窗口、Pro/Max 与续费日。

原来放在 github_auth.py 里（那个文件早已不只管 GitHub 授权）。github_auth
仍原样转出这里的全部名字，旧的 `github_auth.usage_*` 调用不受影响。
"""
import datetime
import json
import threading
import time
import urllib.error
import urllib.request

# ==== 订阅套餐用量读取 ====
#
# 已安装机器的 updater 用固定白名单 CODE_FILES 投递代码，所以这个文件先作为
# 可选文件登记进 updater.py（OPTIONAL_FILES），之后才把代码从 github_auth.py
# 挪过来。以后再新增模块也要照这个顺序分两版发布。
#
# 数据来源全部来自官方 CLI 自己用的通道，不猜、不爬网页：
# - Claude：GET https://api.anthropic.com/api/oauth/usage，Bearer 走 credentials.json
#   里的 OAuth 访问令牌。响应里 five_hour / seven_day 各带 utilization(0-100) 与
#   resets_at(ISO 8601)。
# - Claude 每次对话的响应头里还有 anthropic-ratelimit-unified-5h-utilization /
#   -5h-reset / -7d-utilization / -7d-reset，流式跑一次就能顺手拿一份。
# - Codex：ChatGPT 账号走 wham 的 rate_limits 结构，同样有 used_percent、
#   window_minutes、resets_at 三个字段。
# - CodeBuddy：客户端把服务端返回的 anthropic-ratelimit-unified 头转成
#   _meta["codebuddy.ai/requestCredits"] 进度，没有独立的用量查询接口。
#
# 拿不到就如实说拿不到：三个引擎都可能因为未登录、令牌过期、接口改版而失败，
# 失败必须带明确原因返回，绝不用估算数字糊过去。

USAGE_CLAUDE_URL = 'https://api.anthropic.com/api/oauth/usage'
USAGE_CLAUDE_BETA = 'oauth-2025-04-20'
# 套餐身份：has_claude_max / has_claude_pro、subscription_status、subscription_created_at。
# 官方不给到期日，按订阅创建日逐月推算下一个续费日（Stripe 月付以创建日为锚点）。
USAGE_CLAUDE_PROFILE_URL = 'https://api.anthropic.com/api/oauth/profile'
# Codex 账号用量。必须同时带 chatgpt-account-id，只有 Bearer 会 403。
# 同域的 /backend-api/codex/usage 是 403、/backend-api/wham/rate-limits 是 404，
# 别拿它们试（实测）。
USAGE_CODEX_URL = 'https://chatgpt.com/backend-api/wham/usage'
USAGE_TIMEOUT = 8
USAGE_WINDOWS = (
    ('five_hour', '5小时', 300),
    ('seven_day', '7天', 10080),
    ('seven_day_opus', '7天 Opus', 10080),
    ('seven_day_sonnet', '7天 Sonnet', 10080),
)


class UsageError(Exception):
    """用量读取失败，message 直接面向机主。"""


def usage_http_json(url, headers, timeout=USAGE_TIMEOUT):
    """抓一次 JSON。只负责把 HTTP 失败翻译成人能看懂的原因。"""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response), dict(response.headers)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise UsageError('授权失效或被拒（HTTP ' + str(exc.code) + '），需要重新登录。') from None
        if exc.code == 429:
            raise UsageError('查询太频繁（HTTP 429），请稍后再看。') from None
        raise UsageError('接口返回 HTTP ' + str(exc.code) + '。') from None
    except (urllib.error.URLError, OSError):
        raise UsageError('连不上用量接口，请检查本机网络。') from None
    except ValueError:
        raise UsageError('接口返回的不是合法 JSON。') from None
    except Exception:
        # urlopen 在不同实现下可能抛别的异常；一律归成可读原因，
        # 保证调用方只需处理 UsageError 一种失败。
        raise UsageError('读取用量时出错。') from None


def _usage_percent(value):
    """把各种写法的百分比归一成 0-100 的浮点；不合法一律返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:            # NaN
        return None
    return min(100.0, max(0.0, number))


def _usage_reset(value):
    """归一到秒级时间戳。同时接受 ISO 8601 字符串与秒/毫秒数字。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        # 毫秒时间戳比秒大三个数量级，这里按量级判断，不做魔法猜测。
        return int(stamp / 1000) if stamp > 1e11 else int(stamp)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _usage_reset(int(text))
    # Python 3.11 之前 fromisoformat 不认结尾的 Z，先换掉。
    text = text.replace('Z', '+00:00')
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp())


def usage_window(key, label, percent, reset, reached=False):
    """构造一个窗口读数；字段名固定，调用方不必记住来源差异。"""
    return {'key': key, 'label': label,
            'used_percent': _usage_percent(percent),
            'resets_at': _usage_reset(reset),
            'limit_reached': bool(reached)}


def usage_parse_claude(payload):
    """解析 /api/oauth/usage 的响应体。"""
    if not isinstance(payload, dict):
        raise UsageError('用量响应结构不认识（不是对象）。')
    windows = []
    for key, label, _minutes in USAGE_WINDOWS:
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        reading = usage_window(key, label, block.get('utilization'), block.get('resets_at'))
        if reading['used_percent'] is None and reading['resets_at'] is None:
            continue
        windows.append(reading)
    if not windows:
        raise UsageError('用量响应里没有窗口数据（可能账号没有套餐）。')
    return windows


def usage_parse_headers(headers):
    """从响应头里取 Claude 的窗口读数。

    头名是 anthropic-ratelimit-unified-<5h|7d>-<utilization|reset>，
    reset 是秒级时间戳。只有状态码为拒绝时头才会带着限流信息，
    正常请求也可能完全没有这些头，这时返回空列表而不是报错。
    """
    lowered = {}
    for key, value in (headers or {}).items():
        lowered[str(key).lower()] = value
    windows = []
    for prefix, key, label in (('5h', 'five_hour', '5小时'), ('7d', 'seven_day', '7天')):
        percent = lowered.get('anthropic-ratelimit-unified-' + prefix + '-utilization')
        reset = lowered.get('anthropic-ratelimit-unified-' + prefix + '-reset')
        if percent is None and reset is None:
            continue
        reading = usage_window(key, label, percent, reset)
        if reading['used_percent'] is None and reading['resets_at'] is None:
            continue
        windows.append(reading)
    return windows


def usage_parse_codex(payload):
    """解析 Codex 账号用量（GET /backend-api/wham/usage）。

    真实响应形状（2026-10 实测）：
        {"plan_type": "plus",
         "rate_limit": {"limit_reached": true,
                        "primary_window":   {"used_percent": 100,
                                             "limit_window_seconds": 18000,
                                             "reset_after_seconds": 3056,
                                             "reset_at": 1792953411},
                        "secondary_window": {"used_percent": 31, ...}}}

    ⚠ 字段名是单数 rate_limit + *_window，不是 Codex 二进制里那份
    Rust 结构体名的 rate_limits + primary/secondary —— 两者都保留兼容，
    但判断顺序要以前者（实测过的线上形状）优先。
    """
    if not isinstance(payload, dict):
        raise UsageError('用量响应结构不认识（不是对象）。')
    block = None
    for name in ('rate_limit', 'rate_limits'):
        candidate = payload.get(name)
        if isinstance(candidate, dict):
            block = candidate
            break
    if block is None:
        block = payload
    windows = []
    # limit_reached 是整份响应的结论（服务端拒绝调用），不是单窗口的；
    # 照实传给「谁已经用满」的那个窗口，让展示能明确说「额度已用尽」。
    reached = bool(block.get('limit_reached'))
    for slot, fallback in (('primary', '5小时'), ('secondary', '7天')):
        item = block.get(slot) or block.get(slot + '_window')
        if not isinstance(item, dict):
            continue
        minutes = item.get('window_minutes') or item.get('limit_window_minutes')
        if minutes is None and item.get('limit_window_seconds'):
            try:
                minutes = int(item['limit_window_seconds']) // 60
            except (TypeError, ValueError):
                minutes = None
        # 窗口时长优先用作名字：服务端会把 5 小时窗口挪到 primary 或 secondary，
        # 按位置写死标签迟早标错。
        label = _usage_window_label(minutes, fallback)
        # reset_at 是绝对秒级时间戳；resets_at 是它在部分版本里的叫法。
        reset = item.get('reset_at')
        if reset is None:
            reset = item.get('resets_at')
        if reset is None:
            reset = item.get('reset_after_seconds')
            if reset is not None:
                try:
                    reset = time.time() + float(reset)
                except (TypeError, ValueError):
                    reset = None
        reading = usage_window(str(slot), label, item.get('used_percent'), reset,
                               reached and (item.get('used_percent') or 0) >= 99)
        if reading['used_percent'] is None and reading['resets_at'] is None:
            continue
        windows.append(reading)
    if not windows:
        raise UsageError('用量响应里没有窗口数据（可能账号没有套餐）。')
    return windows


def _usage_window_label(minutes, fallback):
    """按窗口时长给出中文标签；拿不到时长才退回位置默认值。"""
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return fallback
    if value <= 0:
        return fallback
    if value % 1440 == 0:
        return str(value // 1440) + '天'
    if value % 60 == 0:
        return str(value // 60) + '小时'
    return str(value) + '分钟'


def next_monthly_renewal(created, now=None):
    """从订阅创建时间逐月往后推，返回第一个晚于 now 的续费时间戳。"""
    if created is None:
        return None
    now = time.time() if now is None else now
    start = datetime.datetime.fromtimestamp(created, datetime.timezone.utc)
    for months in range(0, 1200):
        total = start.month - 1 + months
        year, month = start.year + total // 12, total % 12 + 1
        day = start.day
        while True:
            try:
                candidate = start.replace(year=year, month=month, day=day)
                break
            except ValueError:
                day -= 1
        if candidate.timestamp() > now:
            return int(candidate.timestamp())
    return None


def claude_plan_from_profile(payload, now=None):
    """解析 /api/oauth/profile，得出套餐名、订阅状态与预计续费日。读不懂返回 None。"""
    if not isinstance(payload, dict):
        return None
    account = payload.get('account') if isinstance(payload.get('account'), dict) else {}
    org = payload.get('organization') if isinstance(payload.get('organization'), dict) else {}
    kind = str(org.get('organization_type') or '')
    if account.get('has_claude_max') or 'max' in kind:
        plan = 'Max'
    elif account.get('has_claude_pro') or 'pro' in kind:
        plan = 'Pro'
    elif kind:
        plan = kind
    else:
        return None
    status = str(org.get('subscription_status') or '')
    renews = None
    if status == 'active' and org.get('billing_type') == 'stripe_subscription':
        renews = next_monthly_renewal(_usage_reset(org.get('subscription_created_at')), now)
    return {'plan': plan, 'status': status, 'renews_at': renews,
            'trial_ends_at': _usage_reset(org.get('claude_code_trial_ends_at'))}


def usage_plan_text(plan):
    """「套餐：Claude Pro · 订阅有效 · 预计 2026-11-22 续费」。"""
    if not plan:
        return ''
    status = {'active': '订阅有效', 'past_due': '扣费失败待补缴', 'canceled': '已取消',
              'cancelled': '已取消', 'trialing': '试用中'}.get(plan.get('status'), plan.get('status') or '')
    parts = ['套餐：Claude ' + plan.get('plan', '?')]
    if status:
        parts.append(status)
    if plan.get('renews_at'):
        parts.append('预计 ' + time.strftime('%Y-%m-%d', time.localtime(plan['renews_at'])) + ' 续费（按月推算）')
    if plan.get('trial_ends_at'):
        parts.append('试用到 ' + time.strftime('%Y-%m-%d', time.localtime(plan['trial_ends_at'])))
    return ' · '.join(parts)


def usage_merge(windows):
    """同一 key 出现多次时保留信息更全的那条（先到的优先，后到的补空字段）。"""
    merged = {}
    order = []
    for item in windows or []:
        key = item.get('key') or item.get('label') or '?'
        if key not in merged:
            merged[key] = dict(item, key=key)
            order.append(key)
            continue
        current = merged[key]
        for field in ('used_percent', 'resets_at'):
            if current.get(field) is None and item.get(field) is not None:
                current[field] = item[field]
    return [merged[key] for key in order]


def usage_remaining(item):
    """剩余百分比；没有读数时返回 None，不假装是 100%。"""
    percent = (item or {}).get('used_percent')
    if percent is None:
        return None
    return round(max(0.0, 100.0 - percent), 1)


def usage_percent_text(value):
    """87.5 → '87.5%'；87.0 → '87%'。整数不带小数点更省地方。"""
    if value is None:
        return '未知'
    if abs(value - round(value)) < 0.05:
        return str(int(round(value))) + '%'
    return str(round(value, 1)) + '%'


def usage_format_reset(stamp, now=None):
    """把刷新时间写成「还剩 2小时13分」这种相对时长。"""
    if stamp is None:
        return ''
    now = time.time() if now is None else now
    left = int(stamp - now)
    if left <= 0:
        return '已到刷新点'
    if left >= 86400:
        days, rest = divmod(left, 86400)
        return str(days) + '天' + str(rest // 3600) + '小时'
    if left >= 3600:
        hours, rest = divmod(left, 3600)
        return str(hours) + '小时' + str(rest // 60) + '分'
    return str(max(1, left // 60)) + '分钟'


def usage_line(item, now=None):
    """一行窗口读数，例如「5小时 剩余 62.5%（2小时13分后刷新）」。"""
    if not item:
        return ''
    text = item.get('label', '窗口') + ' 剩余 ' + usage_percent_text(usage_remaining(item))
    left = usage_format_reset(item.get('resets_at'), now)
    if item.get('limit_reached'):
        # 服务端已经明确拒绝（limit_reached=true）时，光写「剩余 0%」不够，
        # 机主会以为还能凑合跑，实际发任务会直接被拒。
        if left and left != '已到刷新点':
            return text + '（额度已用尽，' + left + '后恢复）'
        return text + '（额度已用尽）'
    if left == '已到刷新点':
        return text + '（已到刷新点，重新拉取即可看到新窗口）'
    if left:
        return text + '（' + left + '后刷新）'
    if not item.get('used_percent'):
        # 窗口没被用过时官方不给 resets_at：计时从下一次请求才开始，并不是「未知」。
        return text + '（未开始计时）'
    return text + '（刷新时间未知）'


def usage_leaking_engine(provider):
    """用量读取只对这三个执行器有意义。"""
    return provider in ('claude', 'codex', 'codebuddy')


class UsageReporter:
    """按执行器读取订阅用量，带短缓存与失败降级。

    设计取舍：
    - 缓存不设太长。机主要看的是「还能不能继续跑」，缓一分钟和实时区别不大，
      但能挡住连发多条消息时的重复请求。
    - 失败也要缓存，但时间更短。接口挂了没必要每轮都撞一次，也不该让机主
      以为数据是新的。
    - 只接受调用方传入的「读令牌」回调，本类不直接碰凭证文件，
      方便单测注入假数据，也避免把凭证散布到多处。
    """

    def __init__(self, credential=None, fetcher=None, ttl=60, error_ttl=15, clock=None):
        self.credential = credential
        self.fetcher = fetcher or usage_http_json
        self.ttl = ttl
        self.error_ttl = error_ttl
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self.cache = {}

    def _cached(self, key):
        entry = self.cache.get(key)
        if not entry:
            return None
        if entry['expires'] <= self.clock():
            return None
        return entry['value']

    def _store(self, key, value):
        ttl = self.ttl if value.get('ok') else self.error_ttl
        with self.lock:
            self.cache[key] = {'value': value, 'expires': self.clock() + ttl}
        return value

    def _credentials(self, provider='claude'):
        if self.credential is None:
            return None
        try:
            record = self.credential(provider)
        except Exception:
            return None
        return record if isinstance(record, dict) else None

    def claude(self, force=False):
        """Claude 用量：优先官方 /api/oauth/usage，失败再退回凭证里的最后读数。"""
        key = 'claude'
        if not force:
            hit = self._cached(key)
            if hit is not None:
                return hit
        record = self._credentials('claude')
        token = (record or {}).get('value')
        if not token:
            return self._store(key, {'provider': 'claude', 'ok': False, 'windows': [],
                                     'error': '未登录 Claude 账号。', 'at': self.clock()})
        headers = {'Authorization': 'Bearer ' + token,
                   'anthropic-beta': USAGE_CLAUDE_BETA,
                   'Accept': 'application/json'}
        try:
            payload, _headers = self.fetcher(USAGE_CLAUDE_URL, headers)
            windows = usage_parse_claude(payload)
        except UsageError as exc:
            return self._store(key, {'provider': 'claude', 'ok': False, 'windows': [],
                                     'error': str(exc), 'at': self.clock()})
        except Exception:
            # 用量是附加信息。抓取层抛出的任何意外异常都不能冒到调用方，
            # 否则一轮已经跑完的任务会因为「多看一行数字」而整体失败。
            return self._store(key, {'provider': 'claude', 'ok': False, 'windows': [],
                                     'error': '读取用量时出错。', 'at': self.clock()})
        return self._store(key, {'provider': 'claude', 'ok': True, 'windows': windows,
                                 'error': '', 'at': self.clock(), 'plan': self.claude_plan(headers)})

    def claude_plan(self, headers):
        """套餐信息一小时才变一次也嫌多，单独缓存；读不到不影响用量。"""
        hit = self._cached('claude-plan')
        if hit is not None:
            return hit.get('plan')
        try:
            payload, _headers = self.fetcher(USAGE_CLAUDE_PROFILE_URL, headers)
            plan = claude_plan_from_profile(payload, self.clock())
        except Exception:
            plan = None
        with self.lock:
            self.cache['claude-plan'] = {'value': {'plan': plan},
                                         'expires': self.clock() + (3600 if plan else self.error_ttl)}
        return plan

    def codex(self, force=False):
        """Codex 账号用量：GET /backend-api/wham/usage。

        凭证来自 codex 自己的 ~/.codex/auth.json，与 Claude 同理只读不写。
        必须同时带 chatgpt-account-id 头，缺了会被 403。
        """
        key = 'codex'
        if not force:
            hit = self._cached(key)
            if hit is not None:
                return hit
        record = self._credentials('codex') if self.credential else None
        token = (record or {}).get('value')
        account = (record or {}).get('account')
        if not token:
            return self._store(key, {'provider': 'codex', 'ok': False, 'windows': [],
                                     'error': '未登录 Codex 账号。', 'at': self.clock()})
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
        if account:
            headers['chatgpt-account-id'] = account
        try:
            payload, _headers = self.fetcher(USAGE_CODEX_URL, headers)
            windows = usage_parse_codex(payload)
        except UsageError as exc:
            return self._store(key, {'provider': 'codex', 'ok': False, 'windows': [],
                                     'error': str(exc), 'at': self.clock()})
        except Exception:
            return self._store(key, {'provider': 'codex', 'ok': False, 'windows': [],
                                     'error': '读取用量时出错。', 'at': self.clock()})
        return self._store(key, {'provider': 'codex', 'ok': True, 'windows': windows,
                                 'error': '', 'at': self.clock()})

    def remember_headers(self, provider, headers):
        """把某一轮对话响应头里的窗口读数记进缓存。

        这条路径完全免费（数据本来就在响应里），所以覆盖缓存，
        下次展示直接用，避免再打一次用量接口。
        """
        windows = usage_parse_headers(headers)
        if not windows:
            return None
        return self._store(provider, {'provider': provider, 'ok': True,
                                      'windows': windows, 'error': '', 'at': self.clock(),
                                      'from': 'headers'})

    def snapshot(self, provider, force=False):
        """给指定执行器出一份用量快照；不支持的执行器直接返回 None。"""
        if not usage_leaking_engine(provider):
            return None
        if provider == 'claude':
            return self.claude(force=force)
        if provider == 'codex':
            return self.codex(force=force)
        # CodeBuddy 没有公开的用量查询接口，只有对话响应头这一条免费通道，
        # 所以只读缓存，不主动发请求。
        hit = self._cached(provider)
        if hit is not None:
            return hit
        return {'provider': provider, 'ok': False, 'windows': [],
                'error': '尚无读数，跑一轮账号模式任务后即可显示。', 'at': self.clock()}


def usage_footer(state, now=None):
    """把用量快照压成会话底部的一行；失败时说明原因，绝不写假数字。"""
    if not state:
        return ''
    if not state.get('ok'):
        reason = state.get('error') or '读取失败'
        return '用量：' + reason
    lines = [usage_line(item, now) for item in usage_merge(state.get('windows'))]
    lines = [line for line in lines if line]
    if not lines:
        return '用量：暂无可读窗口数据。'
    text = '用量：' + '；'.join(lines)
    plan = usage_plan_text(state.get('plan'))
    return text + '\n' + plan if plan else text
