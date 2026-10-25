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
import os
import queue
import re
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit

LOGIN_PROVIDERS = ('codex', 'codebuddy')
LOGIN_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
LOGIN_URL = re.compile(r'https://[^\s<>"\']+')
LOGIN_DEVICE_CODE = re.compile(r'(?<![A-Za-z0-9_-])([A-Z0-9]{4,6}-[A-Z0-9]{4,6})(?![A-Za-z0-9_-])')
LOGIN_CODEX_HOSTS = ('auth.openai.com', 'chatgpt.com')
LOGIN_CODEBUDDY_HOST = 'copilot.tencent.com'
LOGIN_SECRET_QUERY = re.compile(r'(^|[?&])(state|code|token|access_token)=([^&\s]*)', re.IGNORECASE)
LOGIN_GATEWAY_PREFIX = ('OPENAI_', 'ANTHROPIC_', 'CODEBUDDY_')
LOGIN_TRUST_PROMPT = 'Do you trust the files in this folder?'
LOGIN_METHOD_PROMPT = 'Select login method:'
LOGIN_SUCCESS_HINTS = ('logged in', 'login success', 'successfully', 'success',
                       '登录成功', '授权成功', '欢迎回来')


def login_clean_env(env):
    """保留 HOME、PATH 等运行环境，去掉会覆盖账号授权的网关变量。"""
    return {key: value for key, value in env.items()
            if not key.upper().startswith(LOGIN_GATEWAY_PREFIX)}


def login_sanitize_url(url):
    """只保留官方主机与路径，登录状态参数一律遮盖。"""
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.netloc:
        return None
    query = LOGIN_SECRET_QUERY.sub(
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
    """从官方界面取浏览器登录链接。"""
    clean = LOGIN_ANSI.sub('', text)
    for match in LOGIN_URL.finditer(clean):
        url = match.group(0).rstrip('.,);')
        parsed = urlsplit(url)
        if parsed.netloc == LOGIN_CODEBUDDY_HOST and parsed.path == '/login':
            return {'url': login_sanitize_url(url)}
    return None


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
                    chunk = chunk.decode('utf-8', errors='replace')
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
            while True:
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
                        session.buffer = (session.buffer + chunk)[-self.buffer_limit:]
                        text = LOGIN_ANSI.sub('', session.buffer)
                        if session.provider == 'codebuddy':
                            step = self._advance_codebuddy(session, text, step)
                            prompt = codebuddy_login_prompt(session.buffer)
                            if prompt and not ready:
                                ready = True
                                self._emit(session, 'ready', **prompt)
                            elif ready and any(hint in text.lower() for hint in LOGIN_SUCCESS_HINTS):
                                final_state = 'succeeded'
                                session.stopped.set()
                            elif ready and time.monotonic() >= next_probe:
                                # 官方界面不一定退出，用官方命令的真实行为核实登录是否生效。
                                next_probe = time.monotonic() + 20
                                if self._codebuddy_authenticated(binary, env):
                                    final_state = 'succeeded'
                                    session.stopped.set()
                        else:
                            prompt = codex_login_prompt(session.buffer)
                            if prompt and not ready:
                                ready = True
                                self._emit(session, 'ready', **prompt)
                except queue.Empty:
                    pass
                returncode = session.process.poll()
                if returncode is not None and (eof or not reader.is_alive()) and output.empty():
                    final_state = 'succeeded' if returncode == 0 else 'failed'
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
