#!/usr/bin/env python3
"""GitHub / Cloudflare 授权凭据的存取模块。

设计边界（都是有意为之，改动前请先读）：
- 凭据只落在 0600 的 credentials.json 里，不进 git、不进任务状态文件、不进日志。
- 读出来的凭据只用于「注入子进程环境变量」，绝不回传给模型或电报聊天。
- 对外只提供脱敏摘要（前缀 4 位 + 长度 + 指纹），够判断「是哪一枚」而不够复原。
- 任何异常一律降级成「未授权」，不向调用方抛，避免一处配置损坏拖死主循环。
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path

# 允许的提供方与它们各自的凭据形态。新增提供方只在这里补一条。
PROVIDERS = {
    'github': {
        'label': 'GitHub',
        'env': 'GH_TOKEN',
        # 同一族变量必须一起清。GitHub 的 CLI 与 git 会按优先级依次尝试这些名字，
        # 只清 GH_TOKEN 而留着 GITHUB_TOKEN，任务就会静默用到另一枚令牌。
        'aliases': ('GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'),
        'pattern': r'^(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})$',
        'hint': '形如 gho_ / ghp_ / ghu_ / github_pat_ 开头的令牌',
    },
    'cloudflare': {
        'label': 'Cloudflare',
        'env': 'CLOUDFLARE_API_TOKEN',
        'aliases': ('CF_API_TOKEN', 'CLOUDFLARE_API_KEY', 'CLOUDFLARE_EMAIL'),
        # 三种合法形态，都见过实物：
        #   cfut_ 前缀 = 控制台创建的 API 令牌（新格式）
        #   40 位无前缀 = 老格式 API 令牌
        #   设备码流程换来的 OAuth 访问令牌 = 长度不固定的一串 base64url 风格字符
        # 不能只认前两种：设备码授权成功后会被自己的校验拦下来，功能等于白做。
        # 这里保持「无空白 + 长度区间 + 字符集受限」，误判风险仍很低。
        'pattern': r'^(?:cfut_[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{40}|[A-Za-z0-9._~+/-]{20,})$',
        'hint': '形如 cfut_ 开头的 API 令牌、40 位旧格式令牌，或一次成功的设备码授权',
    },
}

# 凭据最短长度。低于这个长度的输入基本都是误粘贴，直接拒绝而不是存进去等调用失败。
MIN_LENGTH = 20
# Cloudflare 的 OAuth 访问令牌实测长 64~300 字符，留足余量到 2000。
# 上限大了会不会误吞任务描述？不会 —— 还有「无空白 + 字符集受限」两道闸。
MAX_LENGTH = 2000


def fingerprint(value):
    """给别人看的指纹。sha256 前 12 位，不可逆，但同一枚令牌永远得出同一指纹。

    用途是「换没换」的对照：机主看到指纹变了就知道是另一枚令牌，
    不必把令牌原文再翻出来比对。
    """
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def masked(value):
    """脱敏显示。只露前 4 位与长度，中间一律省略。"""
    if not value:
        return '未授权'
    return value[:4] + '…（共 ' + str(len(value)) + ' 字符）'


def looks_like(provider, value):
    """判断一段文本是否像该提供方的凭据。

    这里刻意严格：宁可让机主重发一次，也不要把正常的任务描述当令牌存进去。
    反向也重要 —— 只有「像令牌」的输入才会被当授权处理，否则机主要发一句
    普通的任务描述，就可能被误吞成凭据。
    """
    spec = PROVIDERS.get(provider)
    if not spec or not isinstance(value, str):
        return False
    candidate = value.strip()
    if not MIN_LENGTH <= len(candidate) <= MAX_LENGTH:
        return False
    if any(character.isspace() for character in candidate):
        return False
    return bool(re.fullmatch(spec['pattern'], candidate))


class CredentialStore:
    """credentials.json 的读写封装。

    文件结构：
        {"github": {"value": "...", "scope": "...", "saved_at": 1234567890,
                    "expires_at": null, "refresh": "..."}, "cloudflare": {...}}
    """

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            # 文件缺失或损坏都当成「什么都没授权」。损坏时不能抛，
            # 否则每次查询授权状态都会让机器人操作失败。
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, provider):
        """取某提供方的完整记录，含明文凭据。仅供注入子进程用，不得对外回显。"""
        record = self._load().get(provider)
        if not isinstance(record, dict):
            return None
        if not isinstance(record.get('value'), str) or not record['value']:
            return None
        return record

    def save(self, provider, value, scope='', refresh=None, expires_in=None):
        """写入凭据。scope 与过期时间只作记录，判断可用性要看真实接口返回。"""
        if provider not in PROVIDERS:
            raise ValueError('未知的授权提供方')
        value = (value or '').strip()
        if not looks_like(provider, value):
            raise ValueError(PROVIDERS[provider]['hint'])
        data = self._load()
        now = int(time.time())
        data[provider] = {
            'value': value,
            'scope': str(scope or ''),
            'saved_at': now,
            'expires_at': (now + int(expires_in)) if expires_in else None,
            'refresh': refresh or None,
            'fingerprint': fingerprint(value),
        }
        self._write(data)
        return data[provider]

    def update_refresh(self, provider, value, refresh, expires_in):
        """刷新令牌后替换凭据，保留原有的 scope 等信息。"""
        data = self._load()
        record = data.get(provider)
        if not isinstance(record, dict):
            return None
        now = int(time.time())
        record.update({'value': (value or '').strip(),
                       'refresh': refresh or record.get('refresh'),
                       'saved_at': now,
                       'expires_at': (now + int(expires_in)) if expires_in else None,
                       'fingerprint': fingerprint((value or '').strip())})
        data[provider] = record
        self._write(data)
        return record

    def drop(self, provider):
        """删除某提供方的凭据。返回是否真的删掉了。"""
        data = self._load()
        if provider not in data:
            return False
        data.pop(provider)
        self._write(data)
        return True

    def _write(self, data):
        temp = self.path.with_suffix(self.path.suffix + '.' + os.urandom(6).hex() + '.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 用 os.open 直接带 0600 建文件，而不是先建再 chmod。
            # 先建后改之间有一个短暂窗口文件是 644；更关键的是 Windows 上
            # os.chmod 只作用于只读位，得到的是 666 —— 所以这里用 open 的
            # mode 参数（POSIX 生效），并在替换之后对目标文件再 chmod 一次兜底。
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(self.path)
            # replace 之后权限跟着文件走，但显式再设一次，避免某些文件系统
            # 在 rename 时按 umask 重算权限。
            os.chmod(self.path, 0o600)
        finally:
            if temp.exists():
                temp.unlink()

    def summary(self, provider):
        """对外可见的摘要：有没有、指纹、什么时候存的。绝不含凭据原文。"""
        spec = PROVIDERS.get(provider)
        if not spec:
            return {'provider': provider, 'authorized': False, 'reason': '未知提供方'}
        record = self.get(provider)
        if not record:
            return {'provider': provider, 'label': spec['label'], 'authorized': False}
        return {'provider': provider, 'label': spec['label'], 'authorized': True,
                'fingerprint': record.get('fingerprint') or fingerprint(record['value']),
                'scope': record.get('scope') or '',
                'saved_at': record.get('saved_at'),
                'expires_at': record.get('expires_at')}

    def environment(self):
        """生成要注入子进程的环境变量。

        只注入已授权的提供方；未授权的变量名会从继承来的环境里主动剔除，
        避免服务器上某个全局配置意外泄漏到任务里。
        """
        overrides = {}
        for name, spec in PROVIDERS.items():
            record = self.get(name)
            if record:
                overrides[spec['env']] = record['value']
        return overrides


def expired(record, now=None):
    """凭据是否已过记录中的过期时间。

    只有「已知过期时间且已越过」才算过期。expires_at 为空表示不过期。
    这里不能倒过来用「没过期时间就当成有效」去判断可用性 ——
    真正的可用性只能靠真实接口探测，记录只是提示。
    """
    if not isinstance(record, dict):
        return False
    expires_at = record.get('expires_at')
    if not expires_at:
        return False
    return int(expires_at) <= int(now if now is not None else time.time())
