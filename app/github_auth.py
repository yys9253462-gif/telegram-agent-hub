#!/usr/bin/env python3
"""GitHub OAuth 设备码流程（Device Authorization Grant）的客户端实现。

为什么选设备码：机器人跑在无浏览器的服务器上，设备码流程是 GitHub 官方
为无头程序提供的路径 —— 只要 client_id（无 secret），不需要回调地址，
也不需要公网可达。整个流程只有三步：申请码 → 机主在别处输码 → 轮询换令牌。
"""
import base64
import binascii
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


def renew(client_id, refresh_token, client_secret=None, timeout=20):
    """用刷新令牌换一枚新的访问令牌。

    GitHub App 的用户令牌默认 8 小时过期，刷新令牌最长 6 个月。
    官方说明：设备码流程拿到的令牌，续期时不需要 client_secret。

    ⚠ 刷新令牌是一次性的：用掉之后旧的刷新令牌和旧的访问令牌都作废，
    所以响应里返回的新刷新令牌必须立刻存下来，否则下一次续期就无从下手。
    """
    payload = {'client_id': client_id, 'refresh_token': refresh_token,
               'grant_type': 'refresh_token'}
    if client_secret:
        payload['client_secret'] = client_secret
    body = _request(TOKEN_URL, payload, timeout=timeout)
    if not body.get('access_token'):
        raise DeviceFlowError(describe(body))
    return body


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
"""官方 CLI 的远程登录：Codex 设备码流程与 CodeBuddy 官方交互登录。

两类流程都只在本进程内暂存终端输出：凭据不落盘、不写日志、不回传原始输出。
Codex 走官方 `login --device-auth`，用户在官方网页输入设备码即可。
CodeBuddy 官方只提供浏览器登录链接并在本地等待，没有授权码回填入口，
因此这里实现的是「给出链接 + 等待官方界面确认」，不伪造回填输入框。
"""
import codecs
import hashlib
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

LOGIN_PROVIDERS = ('codex', 'codebuddy')

# 退出登录要动的两处路径。都跟随 HOME，测试里改 HOME 即可重定向。
CODEX_AUTH = Path(os.environ.get('CODEX_AUTH_FILE')
                  or os.path.join(os.path.expanduser('~'), '.codex', 'auth.json'))
# CodeBuddy 把账号凭据放在这个目录下的 *.info 的 auth 段里。
CODEBUDDY_AUTH_DIR = Path(os.environ.get('CODEBUDDY_AUTH_DIR')
                          or os.path.join(os.path.expanduser('~'), '.local', 'share',
                                          'CodeBuddyExtension', 'Data', 'Public', 'auth'))
LOGIN_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
LOGIN_URL = re.compile(r'https://[^\s<>"\']+')
LOGIN_DEVICE_CODE = re.compile(r'(?<![A-Za-z0-9_-])([A-Z0-9]{4,6}-[A-Z0-9]{4,6})(?![A-Za-z0-9_-])')
LOGIN_CODEX_HOSTS = ('auth.openai.com', 'chatgpt.com')
LOGIN_CODEBUDDY_HOST = 'copilot.tencent.com'
LOGIN_SECRET_QUERY = re.compile(r'(^|[?&])(state|code|token|access_token)=([^&\s]*)', re.IGNORECASE)
# CodeBuddy 的登录链接必须整条给用户，`state` 是 CLI 生成的会话标识（UUID），
# 用来把浏览器里的登录结果对回本机等待的那个进程，本身不是凭证也不是权限。
# 抹掉它等于把链接废掉：用户点进去登不回来（2026-10-26 真实反馈）。
# 所以这里只过滤真正像秘密的参数，`state` 保留。
CODEBUDDY_KEEP_QUERY = re.compile(r'(^|[?&])(code|token|access_token)=([^&\s]*)', re.IGNORECASE)
LOGIN_GATEWAY_PREFIX = ('OPENAI_', 'ANTHROPIC_', 'CODEBUDDY_')
LOGIN_TRUST_PROMPT = 'Do you trust the files in this folder?'
LOGIN_METHOD_PROMPT = 'Select login method:'
LOGIN_SUCCESS_HINTS = ('logged in', 'login success', 'successfully', 'success',
                       '登录成功', '授权成功', '欢迎回来')
# 官方界面的旋转动画帧字符。这些行只有动画与耗时，写进缓冲只会挤掉真正的信息。
LOGIN_SPINNER_CHARS = ('✷', '✶', '✸', '✹', '✺', '✻', '✳', '·',
                       '⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')
# 动画帧里的特征词汇。块边界把动画行切成半截后，行首字符就没了，
# 只能靠这些词认出来。实测完整帧形如：
#   ✷ Syncing… (5s · streaming · ↓ 83 tokens)
# 选的词都只在动画帧里出现，正常提示行（信任目录 / 选择登录方式 / 登录链接 /
# 等待授权）一个都不会命中。
LOGIN_SPINNER_HINTS = ('syncing', 'streaming', 'tokens)', '· ↓', '↓ ')


def login_clean_env(env):
    """保留 HOME、PATH 等运行环境，去掉会覆盖账号授权的网关变量。"""
    return {key: value for key, value in env.items()
            if not key.upper().startswith(LOGIN_GATEWAY_PREFIX)}


def login_sanitize_url(url, pattern=LOGIN_SECRET_QUERY):
    """只保留官方主机与路径，登录状态参数按给定规则遮盖。

    默认把 state/code/token 全遮掉（设备码流程的保守做法）；
    需要把 state 交给用户去点链接的流程（CodeBuddy）传 CODEBUDDY_KEEP_QUERY。
    """
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.netloc:
        return None
    query = pattern.sub(
        lambda found: found.group(1) + found.group(2) + '=[已隐藏]', parsed.query)
    return parsed.scheme + '://' + parsed.netloc + parsed.path + (('?' + query) if query else '')


def codex_login_prompt(text):
    """从官方输出里取设备码链接与用户码。"""
    clean = LOGIN_ANSI.sub('', text)
    for match in LOGIN_URL.finditer(clean):
        url = match.group(0).rstrip('.,);')
        parsed = urlsplit(url)
        if parsed.netloc in LOGIN_CODEX_HOSTS and parsed.path == '/codex/device':
            code = LOGIN_DEVICE_CODE.search(clean)
            if not code:
                return None
            return {'url': login_sanitize_url(url), 'code': code.group(1)}
    return None


def codebuddy_login_prompt(text):
    """从官方界面取浏览器登录链接。

    链接里的 state 必须原样保留：它是 CLI 会话标识，
    用户拿这条链接在浏览器里完成登录，CLI 靠它把结果对回本机进程。
    抹掉 state 后链接照样能打开，但登完回不来，等于白登一次。
    """
    clean = LOGIN_ANSI.sub('', text)
    for match in LOGIN_URL.finditer(clean):
        url = match.group(0).rstrip('.,);')
        parsed = urlsplit(url)
        if parsed.netloc == LOGIN_CODEBUDDY_HOST and parsed.path == '/login':
            return {'url': login_sanitize_url(url, CODEBUDDY_KEEP_QUERY)}
    return None


def login_relevant(text):
    """只保留与登录进展有关的行，丢掉动画刷屏。

    CodeBuddy 在已登录状态下会直接进「Syncing…」旋转动画，每 0.1 秒输出一帧，
    九秒就能填满 16KB 的尾部缓冲，把真正的链接挤出去（2026-10-26 实测）。
    所以在写进缓冲之前先滤一遍：纯动画帧行、连续重复行都不留。

    🔴 不能只判行首：pty 与管道都不保证这一块数据恰好落在换行处，
    动画行会被块边界切成半截，切剩下的那半截行首是个普通字符，
    只判行首就会原样放进缓冲（实测某次只滤掉一部分，缓冲末段全是
    「treaming · ↓ 83 tokens)ns)kens)」这类碎片）。所以按「整行是否只是动画」判。
    """
    kept = []
    seen = set()
    for raw in (text or '').split('\n'):
        line = raw.strip()
        if not line:
            kept.append(raw)
            continue
        if _is_spinner_line(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        kept.append(raw)
    return '\n'.join(kept).strip('\n')


def _is_spinner_line(line):
    """判断一行是不是（可能是被块边界切碎的）旋转动画帧。"""
    if line[:1] in LOGIN_SPINNER_CHARS:
        return True
    # 被切碎的半截行：行首掉了，但动画的特征词汇还在，且整行短得不像正经提示。
    low = line.lower()
    for hint in LOGIN_SPINNER_HINTS:
        if hint in low and len(line) <= 120:
            return True
    return False


class _LoginSession:
    """一次登录会话。会话按执行器划分，因为同一执行器共用一个账号目录。"""

    def __init__(self, provider, chat, notify):
        self.provider = provider
        self.chat = chat
        self.notify = notify
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.process = None
        self.master = None
        self.state = 'starting'
        self.buffer = ''
        self.thread = None
        # 已经解析出来的登录信息（链接 / 设备码）。
        # 官方界面在等待授权时会持续刷新动画，几 KB 的尾部缓冲会被新动画挤满，
        # 光靠 buffer 里再搜一次会搜不到，所以一旦找到就单独记住，不再依赖缓冲区。
        self.payload = None


class LoginManager:
    """start 立即返回，登录进度通过 notify 回调送达。

    同一执行器同时只允许一个登录会话：两个聊天共用同一个账号目录，
    并发登录会互相覆盖登录结果。取消只允许发起者操作。
    """

    def __init__(self, timeout=600, status_timeout=90, buffer_limit=16384):
        self.timeout = timeout
        self.status_timeout = status_timeout
        self.buffer_limit = buffer_limit
        self._sessions = {}
        self._lock = threading.RLock()

    def start(self, provider, chat, binary, env, notify):
        if provider not in LOGIN_PROVIDERS:
            return {'ok': False, 'state': 'unsupported'}
        if provider == 'codebuddy' and os.name != 'posix':
            return {'ok': False, 'state': 'unsupported',
                    'message': 'CodeBuddy 登录依赖伪终端，仅支持 Linux 服务器。'}
        with self._lock:
            if provider in self._sessions:
                return {'ok': False, 'state': 'already_running'}
            session = _LoginSession(provider, chat, notify)
            self._sessions[provider] = session
            session.thread = threading.Thread(
                target=self._run, args=(session, binary, login_clean_env(dict(env))),
                daemon=True, name='cli-login-' + provider)
            session.thread.start()
        return {'ok': True, 'state': 'starting'}

    def cancel(self, provider, chat):
        with self._lock:
            session = self._sessions.get(provider)
        if session is None:
            return {'ok': False, 'state': 'not_running'}
        if chat is not None and session.chat != chat:
            return {'ok': False, 'state': 'other_chat'}
        session.stopped.set()
        self._terminate(session)
        return {'ok': True, 'state': 'cancelled'}

    def submit(self, provider, chat, code):
        """官方没有授权码回填入口，明确拒绝而不是假装支持。"""
        return {'ok': False, 'state': 'unsupported',
                'message': '官方登录流程不提供授权码回填，请在浏览器完成授权。'}

    def logout(self, provider, binary, env):
        """退出某个执行器的账号登录。

        三家 CLI 的退出机制完全不同，全部实测过，不能套用同一套：

        - Claude：官方给了 `claude auth logout`，直接调，最干净。
        - Codex：`codex login` 只有 status 子命令，**没有 logout**。
          登录态是 ~/.codex/auth.json 整份文件（受控实验：只把文件改名，
          `codex login status` 立刻变成 Not logged in；恢复即回来）。
          所以必须整份移除，**不能只把里面的 tokens 置空** ——
          实测置空 tokens、甚至把 auth_mode 也写成 null，状态照样报
          「Logged in using ChatGPT」，只看文件在不在。
          移除前先备份，因为同一个文件里可能还有网关的 OPENAI_API_KEY。
        - CodeBuddy：也没有 logout。登录态在
          ~/.local/share/CodeBuddyExtension/Data/Public/auth/*.info 的 auth 段
          （accessToken / refreshToken / sessionState）。实测清空 auth 段后
          官方命令立刻回「Authentication required」，恢复即正常。
          这里只清 auth 与 account 两段，保留同一个文件里的其它设置。

        三种做法都只影响账号登录态，不动网关密钥与任务数据。
        """
        # 注意这里不能用 LOGIN_PROVIDERS：那张表是「走本管理器登录流程」的执行器，
        # 只有 codex 与 codebuddy。Claude 的登录由 bot.py 自己起进程（它要拿到
        # 授权链接让机主回填），但**退出**走的是官方 `claude auth logout`，
        # 完全可以在这里统一收口。用错表会让 Claude 的退出永远进不来。
        if provider not in ('claude', 'codex', 'codebuddy'):
            return {'ok': False, 'state': 'unsupported',
                    'message': '这个执行器没有账号登录。'}
        with self._lock:
            if provider in self._sessions:
                # 登录进行到一半就退出，两件事会互相覆盖：登录进程最后一步
                # 会把刚拿到的凭据再写回去，用户看到「已退出」却还是登录着的。
                return {'ok': False, 'state': 'login_running',
                        'message': '这个执行器正在登录中，请先取消登录再退出账号。'}
        if provider == 'claude':
            return self._claude_logout(binary, env)
        if provider == 'codex':
            return self._codex_logout()
        return self._codebuddy_logout()

    def logout_target_present(self, provider):
        """退出要动的凭据还在不在 —— 只回答「在不在」，不读内容。

        用来和官方状态互相校验。官方状态有时会「假未登录」：
        CodeBuddy 的状态是靠跑一段临时对话探的，那一段会因为超时、
        网络抖动或模型侧排队而失败，被误判成没登录；Codex 的状态
        也可能因为二进制缺失等环境问题读不出来。这时凭据其实还在，
        若只信官方状态，退出会被错误地拦下。

        Claude 不在此列：它没有固定凭据文件，退出走官方 auth logout，
        官方说没登录就是不没登录。
        """
        if provider == 'codex':
            return CODEX_AUTH.exists()
        if provider == 'codebuddy':
            folder = CODEBUDDY_AUTH_DIR
            try:
                for path in folder.glob('*.info'):
                    try:
                        data = json.loads(path.read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        continue
                    if isinstance(data, dict) and (data.get('auth') or data.get('account')):
                        return True
            except OSError:
                return False
            return False
        return False

    def _claude_logout(self, binary, env):
        try:
            result = subprocess.run([binary, 'auth', 'logout'], capture_output=True,
                                    text=True, timeout=self.status_timeout,
                                    env=login_clean_env(dict(env)))
        except (OSError, subprocess.SubprocessError):
            return {'ok': False, 'state': 'failed',
                    'message': '无法执行 claude auth logout。'}
        if result.returncode != 0:
            detail = ((result.stdout or '') + (result.stderr or '')).strip()[:200]
            return {'ok': False, 'state': 'failed',
                    'message': '官方退出命令失败' + ('：' + detail if detail else '。')}
        return {'ok': True, 'state': 'logged_out', 'removed': '官方 auth logout'}

    def _codex_logout(self):
        path = CODEX_AUTH
        if not path.exists():
            return {'ok': True, 'state': 'not_logged_in', 'removed': ''}
        backup = self._backup(path)
        try:
            path.unlink()
        except OSError as exc:
            return {'ok': False, 'state': 'failed',
                    'message': '删除登录凭据失败：' + type(exc).__name__}
        detail = '已删除 ' + str(path)
        if backup:
            detail += '（原文件备份在 ' + str(backup) + '，确认不需要可自行删除）'
        return {'ok': True, 'state': 'logged_out', 'removed': detail}

    def _codebuddy_logout(self):
        folder = CODEBUDDY_AUTH_DIR
        if not folder.is_dir():
            return {'ok': True, 'state': 'not_logged_in', 'removed': ''}
        cleared = []
        for path in sorted(folder.glob('*.info')):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                # 读不出来的文件不属于我们该动的东西，跳过并如实报告。
                continue
            if not isinstance(data, dict) or ('auth' not in data and 'account' not in data):
                continue
            backup = self._backup(path)
            data.pop('auth', None)
            data['account'] = {}
            try:
                temp = path.with_suffix(path.suffix + '.tmp')
                with temp.open('w', encoding='utf-8') as handle:
                    json.dump(data, handle, ensure_ascii=False)
                os.chmod(temp, 0o600)
                temp.replace(path)
            except OSError as exc:
                return {'ok': False, 'state': 'failed',
                        'message': '清理登录态失败：' + type(exc).__name__}
            cleared.append(path.name + ('（备份 ' + str(backup) + '）' if backup else ''))
        if not cleared:
            return {'ok': True, 'state': 'not_logged_in', 'removed': ''}
        return {'ok': True, 'state': 'logged_out',
                'removed': '已清空 ' + '、'.join(cleared)}

    def _backup(self, path):
        """退出前留一份备份。删掉的东西里可能混着网关密钥，得能拿回来。"""
        stamp = time.strftime('%Y%m%d-%H%M%S')
        target = path.with_name(path.name + '.logout-' + stamp + '.bak')
        try:
            shutil.copy2(path, target)
            # copy2 会连权限一起复制。源文件若因为历史原因就是 666，
            # 备份里那枚令牌也就跟着人人可读，所以这里强制收紧。
            os.chmod(target, 0o600)
            return target
        except OSError:
            return None

    def status(self, provider, binary, env):
        """查询官方登录状态。只执行官方命令，不读凭据文件。"""
        if provider == 'codex':
            return self._codex_status(binary, env)
        if provider == 'codebuddy':
            return self._codebuddy_status(binary, env)
        return {'ok': False, 'state': 'unsupported', 'authenticated': False}

    def _codex_status(self, binary, env):
        try:
            result = subprocess.run([binary, 'login', 'status'], capture_output=True,
                                    text=True, timeout=self.status_timeout,
                                    env=login_clean_env(dict(env)))
        except (OSError, subprocess.SubprocessError):
            return {'ok': False, 'state': 'failed', 'authenticated': False}
        output = ((result.stdout or '') + (result.stderr or '')).lower()
        # 退出码为 0 也可能是接口令牌登录，必须看到官方账号提示才算账号登录。
        account = result.returncode == 0 and 'chatgpt' in output
        return {'ok': True, 'state': 'authenticated' if account else 'unauthenticated',
                'authenticated': account}

    def _codebuddy_authenticated(self, binary, env):
        """官方命令的真实行为：未登录时会明确要求先登录。"""
        result = self.status('codebuddy', binary, env)
        return bool(result.get('authenticated'))

    def _codebuddy_status(self, binary, env):
        """用官方命令的真实行为判断：未登录时会明确要求先登录。"""
        try:
            result = subprocess.run(
                [binary, '-p', '只回复两个字：正常', '--dangerously-skip-permissions'],
                capture_output=True, text=True, timeout=self.status_timeout,
                env=login_clean_env(dict(env)))
        except (OSError, subprocess.SubprocessError):
            return {'ok': False, 'state': 'failed', 'authenticated': False}
        output = ((result.stdout or '') + (result.stderr or '')).lower()
        if 'authentication required' in output or 'use /login' in output:
            return {'ok': True, 'state': 'unauthenticated', 'authenticated': False}
        return {'ok': True, 'state': 'authenticated', 'authenticated': True}

    def _emit(self, session, state, **fields):
        session.state = state
        event = {'provider': session.provider, 'chat': session.chat, 'state': state, **fields}
        try:
            session.notify(event)
        except Exception:
            # 通知失败不能影响子进程清理，也不记录可能含凭据的异常。
            pass

    def _spawn(self, session, command, env):
        options = {'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE,
                   'stderr': subprocess.STDOUT, 'env': env, 'bufsize': 0}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options['start_new_session'] = True
        if session.provider == 'codebuddy':
            import fcntl
            import pty
            import struct
            import termios
            master, slave = pty.openpty()
            session.master = master
            options['stdin'] = slave
            options['stdout'] = slave
            options['stderr'] = slave
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 50, 220, 0, 0))
            process = subprocess.Popen(command, **options)
            os.close(slave)
            return process
        return subprocess.Popen(command, **options)

    def _read(self, session, output):
        source = session.master if session.master is not None else session.process.stdout
        # 读到的字节可能把一个多字节字符从中间切开（pty 与管道都不保证读完整个字符），
        # 用「允许末尾残留」的解码器把半个字符留到下一块再拼，避免落成乱码。
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while not session.stopped.is_set():
                if session.master is not None:
                    import select
                    readable, _, _ = select.select([source], [], [], 0.1)
                    if not readable:
                        continue
                    chunk = os.read(source, 4096)
                else:
                    chunk = source.read(1024)
                if not chunk:
                    break
                if isinstance(chunk, bytes):
                    chunk = decoder.decode(chunk)
                    if not chunk:
                        # 这一块整个都是半个字符，等下一块补齐再处理。
                        continue
                try:
                    output.put_nowait(chunk)
                except queue.Full:
                    try:
                        output.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        output.put_nowait(chunk)
                    except queue.Full:
                        pass
        except (OSError, ValueError):
            pass
        finally:
            try:
                output.put_nowait(None)
            except queue.Full:
                pass

    def _run(self, session, binary, env):
        reader = None
        final_state = 'failed'
        try:
            with session.lock:
                if session.stopped.is_set():
                    final_state = 'cancelled'
                    return
                if session.provider == 'codebuddy':
                    # 官方交互界面只在伪终端下正常显示。
                    env = dict(env)
                    env['TERM'] = 'xterm-256color'
                    env['COLUMNS'] = '220'
                    session.process = self._spawn(session, [binary, '/login'], env)
                else:
                    session.process = self._spawn(
                        session, [binary, 'login', '--device-auth'], env)
            output = queue.Queue(maxsize=32)
            reader = threading.Thread(target=self._read, args=(session, output), daemon=True)
            reader.start()
            deadline = time.monotonic() + self.timeout
            ready = False
            eof = False
            step = 0
            next_probe = time.monotonic() + 20
            # 先问一次官方命令：已经登录好的机器上，界面不会给登录链接，
            # 而是直接进「Syncing…」动画。这时若还傻等链接，就只能等到超时，
            # 最后把「已登录」说成超时或失败（2026-10-26 真实反馈）。
            already = (session.provider == 'codebuddy'
                       and self._codebuddy_authenticated(binary, env))
            if already:
                final_state = 'succeeded'
            while not already:
                if session.stopped.is_set():
                    final_state = 'cancelled'
                    break
                if time.monotonic() >= deadline:
                    final_state = 'timeout'
                    break
                try:
                    chunk = output.get(timeout=0.1)
                    if chunk is None:
                        eof = True
                    else:
                        # 官方界面会持续刷新，只保留尾部内容即可；
                        # 这里不能因为输出变长就中断登录流程。
                        # 但在写进缓冲前要先滤掉旋转动画，否则几秒就被刷屏填满，
                        # 真正的链接会被挤出窗口（2026-10-26 实测：9 秒即达 16KB 上限）。
                        session.buffer = (session.buffer + login_relevant(chunk))[-self.buffer_limit:]
                        text = LOGIN_ANSI.sub('', session.buffer)
                        if session.provider == 'codebuddy':
                            # 成功提示必须排在动画判断之前：官方界面自报登录成功时，
                            # 紧跟其后的就是「Syncing…」动画，先读它才不会把结果误判成失败。
                            if any(hint in text.lower() for hint in LOGIN_SUCCESS_HINTS):
                                final_state = 'succeeded'
                                session.stopped.set()
                                continue
                            step = self._advance_codebuddy(session, text, step)
                            if session.payload is None:
                                prompt = codebuddy_login_prompt(session.buffer)
                                if prompt:
                                    session.payload = prompt
                                    ready = True
                                    self._emit(session, 'ready', **prompt)
                            if ready and time.monotonic() >= next_probe:
                                # 官方界面不一定退出，用官方命令的真实行为核实登录是否生效。
                                next_probe = time.monotonic() + 20
                                if self._codebuddy_authenticated(binary, env):
                                    final_state = 'succeeded'
                                    session.stopped.set()
                        else:
                            if session.payload is None:
                                prompt = codex_login_prompt(session.buffer)
                                if prompt:
                                    session.payload = prompt
                                    ready = True
                                    self._emit(session, 'ready', **prompt)
                except queue.Empty:
                    pass
                returncode = session.process.poll()
                if returncode is not None and (eof or not reader.is_alive()) and output.empty():
                    if returncode == 0:
                        final_state = 'succeeded'
                    elif session.provider == 'codebuddy':
                        # 界面什么都没给出就退出，往往是一开始就已经登录好了，
                        # 此时不能凭退出码断言失败，要问官方命令要真实结论。
                        # 只对 CodeBuddy 这么做：Codex 走的是设备码流程，退出码非 0
                        # 就是真的失败，套用同一个探测逻辑会把失败说成成功
                        # （服务器上装了 codebuddy 且已登录时实测复现）。
                        final_state = ('succeeded' if self._codebuddy_authenticated(binary, env)
                                       else 'failed')
                    else:
                        final_state = 'failed'
                    break
        except Exception:
            final_state = 'cancelled' if session.stopped.is_set() else 'failed'
        finally:
            session.stopped.set()
            try:
                self._terminate(session)
            except (OSError, subprocess.TimeoutExpired):
                final_state = 'failed'
            finally:
                if reader:
                    reader.join(timeout=1)
                session.buffer = ''
                self._emit(session, final_state)
                with self._lock:
                    if self._sessions.get(session.provider) is session:
                        del self._sessions[session.provider]

    def _advance_codebuddy(self, session, text, step):
        """官方界面每一步只按一次回车，避免重复触发。

        同一次输出里可能同时出现「信任目录」和「选择登录方式」，
        因此要循环推进到没有新进展为止，否则会停在第一个提示上。
        """
        for _ in range(4):
            if step == 0:
                if LOGIN_TRUST_PROMPT in text:
                    self._write(session, b'\r')
                    step = 1
                    continue
                if LOGIN_METHOD_PROMPT in text:
                    self._write(session, b'\r')
                    step = 2
                    continue
            elif step == 1 and LOGIN_METHOD_PROMPT in text:
                self._write(session, b'\r')
                step = 2
                continue
            break
        return step

    def _write(self, session, data):
        if session.master is None:
            return
        try:
            os.write(session.master, data)
        except OSError:
            pass

    def _terminate(self, session):
        with session.lock:
            process = session.process
            if process is None:
                return
            if process.poll() is None:
                if session.provider == 'codebuddy' and session.master is not None:
                    # 官方界面支持 ESC 取消，优先用它收尾。
                    self._write(session, b'\x1b')
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        if os.name == 'nt':
                            process.kill()
                        else:
                            os.killpg(process.pid, getattr(signal, 'SIGKILL', signal.SIGTERM))
                    except (OSError, ProcessLookupError):
                        process.kill()
                    process.wait(timeout=2)
            else:
                process.wait(timeout=1)
            if session.master is None:
                for stream in (process.stdin, process.stdout):
                    if stream:
                        try:
                            stream.close()
                        except OSError:
                            pass
            if session.master is not None:
                try:
                    os.close(session.master)
                except OSError:
                    pass
                session.master = None


# ==== 订阅套餐用量读取：已拆到 usage.py，这里原样转出 ====
from usage import *  # noqa: F401,F403
from usage import _usage_percent, _usage_reset  # noqa: F401


# ==== 订阅账号身份 与 账号模式可选模型 ====
#
# 渠道切换要能列出「所有登录的订阅账号」，就必须知道每个执行器背后
# 到底是哪个账号、什么套餐。这里只取身份与套餐这类非敏感字段：
# 令牌、refresh_token、id_token 原文一律不出这个模块。

CODEX_PLAN_LABELS = {
    'free': '免费版',
    'plus': 'Plus',
    'pro': 'Pro',
    'team': 'Team',
    'business': 'Business',
    'enterprise': 'Enterprise',
    'edu': 'Edu',
}
# Claude 的 --model 接受别名，比写死版本号稳：官方换版本时别名照样有效。
# Claude 账号模式可选模型（2026-10-26 在真实 Pro 账号上逐个实测，别名会跟着官方最新版走）。
# fable 在 Pro 上要额外用量额度，未开通时官方直接拒绝，所以标注出来而不是藏掉。
# 不放 opusplan：它只在交互式规划模式下才切到 Opus，机器人用 -p 非交互运行，实测全程只用
# Sonnet。「主模型规划、另一个模型执行」改由「规划执行」模式（executor 子代理）实现。
CLAUDE_ACCOUNT_MODELS = (
    ('opus', 'Opus 5.5'),
    ('opus[1m]', 'Opus 5.5 · 1M 上下文'),
    ('sonnet', 'Sonnet 5.5'),
    ('sonnet[1m]', 'Sonnet 5.5 · 1M 上下文'),
    ('haiku', 'Haiku 5.5'),
    ('fable', 'Fable 5.1（Pro 需额外额度）'),
)
# Codex 账号模式真实可用的模型（2026-10-26 在真实 Plus 账号上逐个实测）。
# 判定依据：返回 "Stream must be set to true" 说明模型存在；
# 返回 "not supported when using Codex with a ChatGPT account" 才是不可用。
# 结论：Plus 只能用 gpt-6* 与 gpt-5.6-* 系列；
# gpt-5.5 / gpt-5.4 / gpt-5.2 / gpt-5.6 / gpt-5.1 / gpt-5.1-codex* / o3 都被拒。
CODEX_ACCOUNT_MODELS = (
    ('gpt-6-sol', 'GPT-6 Sol'),
    ('gpt-6-luna', 'GPT-6 Luna'),
    ('gpt-6-astra', 'GPT-6 Astra'),
    ('gpt-6.1-sol', 'GPT-6.1 Sol'),
    ('gpt-5.6-sol', 'GPT-5.6 Sol'),
    ('gpt-5.6-terra', 'GPT-5.6 Terra'),
    ('gpt-5.6-luna', 'GPT-5.6 Luna'),
)
CLAUDE_ACCOUNT_MODEL_IDS = {item[0] for item in CLAUDE_ACCOUNT_MODELS}
CODEX_ACCOUNT_MODEL_IDS = {item[0] for item in CODEX_ACCOUNT_MODELS}


def codex_jwt_claims(token):
    """解出 JWT 的载荷，只为了拿 email 与套餐；解不出来返回空字典。

    不验签：本机读自己 CLI 写的令牌，用途只是显示身份。
    任何异常都吞掉 —— 一个格式坏掉的令牌不该让「查看进度」整页报错。
    """
    if not isinstance(token, str) or token.count('.') != 2:
        return {}
    payload = token.split('.')[1]
    payload += '=' * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError, binascii.Error):
        return {}
    return data if isinstance(data, dict) else {}


def codex_identity(auth):
    """从 ~/.codex/auth.json 的内容里取身份与套餐。"""
    if not isinstance(auth, dict) or auth.get('auth_mode') != 'chatgpt':
        return None
    tokens = auth.get('tokens')
    if not isinstance(tokens, dict):
        return None
    claims = codex_jwt_claims(tokens.get('id_token'))
    auth_info = claims.get('https://api.openai.com/auth')
    plan = ''
    if isinstance(auth_info, dict):
        plan = str(auth_info.get('chatgpt_plan_type') or '')
    return {'email': str(claims.get('email') or ''),
            'plan': plan,
            'plan_label': CODEX_PLAN_LABELS.get(plan, plan or '未知套餐')}


def claude_identity(text):
    """从 `claude auth status --text` 的输出里取邮箱与套餐。

    官方输出在不同版本里措辞会变，所以按「哪一行像邮箱」兜底，
    拿不到就返回空值，由调用方显示成「已登录（身份未显示）」。
    """
    clean = LOGIN_ANSI.sub('', text or '')
    lowered = clean.lower()
    if 'not logged in' in lowered:
        return None
    email = ''
    found = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', clean)
    if found:
        email = found.group(0)
    plan = ''
    for word, label in (('max', 'Max'), ('pro', 'Pro'), ('team', 'Team'),
                        ('enterprise', 'Enterprise'), ('free', '免费版')):
        if re.search(r'\b' + word + r'\b', lowered):
            plan = label
            break
    return {'email': email, 'plan': plan.lower(), 'plan_label': plan or '订阅账号'}


def codebuddy_identity(folder=None):
    """从 CodeBuddy 的 *.info 里取展示身份。

    只读 account 段里的展示字段（displayName / enterpriseUserName /
    enterpriseName），**不读 auth 段**，令牌与 refresh 原文都不离开本函数。
    实测真实文件里 account 段有 displayName=李静、enterpriseName=某企业版，
    这些足够让机主认得出是谁在登录，不必编造邮箱（它确实没有邮箱字段）。

    拿不到任何展示字段时返回 None，由调用方显示「已登录（身份未显示）」，
    绝不把状态词当身份标签用 —— 那样会渲染出「已登录 · 已登录」这种废话。
    """
    root = folder or CODEBUDDY_AUTH_DIR
    try:
        candidates = sorted(Path(root).glob('*.info'))
    except OSError:
        return None
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        account = data.get('account')
        if not isinstance(account, dict) or not account:
            continue
        name = str(account.get('displayName') or account.get('enterpriseUserName')
                   or account.get('nickname') or '').strip()
        org = str(account.get('enterpriseName') or '').strip()
        # nickname 是机器生成的随机串（实测形如 mex6tjwvq4cp），
        # 拿来当身份只会让人更糊涂，所以只在没有别的名字时才用，
        # 而且不把它当成真名。
        if name and name == str(account.get('nickname') or '').strip():
            name = ''
        if not name and not org:
            continue
        return {'email': '', 'name': name, 'plan': '', 'plan_label': org or '登录'}
    return None


def account_identity_label(identity, fallback='已登录'):
    """身份的可读标签：优先「名字 · 组织」，其次「邮箱 · 套餐」。

    名字（name）是 CodeBuddy 这类没有邮箱字段的执行器用的。
    plan_label 兜底值可能是状态词（如「已登录」），必须挡掉，
    否则会渲染成「已登录 · 已登录」。
    """
    if not identity:
        return fallback
    name = str(identity.get('name') or '').strip()
    email = str(identity.get('email') or '').strip()
    plan = str(identity.get('plan_label') or '').strip()
    if plan in ('已登录', '登录', fallback):
        plan = ''
    who = name or email
    parts = [part for part in (who, plan) if part]
    return ' · '.join(parts) if parts else fallback


def account_models(provider):
    """账号模式下该执行器可选的真实模型；没有清单就返回空元组。"""
    if provider == 'claude':
        return CLAUDE_ACCOUNT_MODELS
    if provider == 'codex':
        return CODEX_ACCOUNT_MODELS
    # CodeBuddy 账号模式的模型以它自己的目录为准（models.json），
    # 这里不硬编码，交给调用方走 catalog()，避免写死一份会过期的清单。
    return ()
