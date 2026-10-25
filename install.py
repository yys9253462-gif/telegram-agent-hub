#!/usr/bin/env python3
"""Telegram Agent Hub 的标准库安装器；配置只作为 JSON 读取。"""
import argparse
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path('/opt/telegram-agent-hub')
CONFIG = Path('/etc/telegram-agent-hub')
OFFICIAL_INSTALLERS = {
    'claude': 'https://claude.ai/install.sh',
    'codebuddy': 'https://www.codebuddy.cn/cli/install.sh',
}
# Codex 官方只发 npm 包，没有可校验哈希的安装脚本，所以走单独的 npm 安装路径。
# 版本钉死：npm 的 latest 标签随时可能变，回归时排查不到是代码还是上游变的问题。
CODEX_PACKAGE = '@openai/codex'
CODEX_VERSION = '0.164.0'
APT_DEPENDENCIES = ['python3', 'python3-venv', 'python3-pip', 'ca-certificates', 'curl', 'git',
                    'openssh-client', 'sudo', 'ffmpeg', 'util-linux']
# 执行器顺序即机器人菜单里的顺序，也是 ENABLED_AGENTS 的排列依据；首个启用项成为默认执行器。
ENGINES_ORDER = ('claude', 'codebuddy', 'codex')
# 官方脚本变动时拒绝执行，由维护者重新审阅并更新哈希。
INSTALLER_SHA256 = {
    'claude': '3a68d3406cf674e17bed1733a4dcf37805e2e47d87417700007d7e1aa766a944',
    'codebuddy': 'e0cb8fcbd2a874724fbf0b16bf5161c66c31bd4a57412efeea90e4d7bb2a956f',
}


class InstallError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InstallError('接口发生重定向，已阻止认证信息跨地址发送')


def read_json(path):
    result = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(result, dict):
        raise InstallError('配置必须是 JSON 对象')
    return result


def env_quote(value):
    value = str(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InstallError('环境变量含不支持的控制字符')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def environment_text(values):
    if any(not re.fullmatch(r'[A-Z_][A-Z0-9_]*', k) for k in values):
        raise InstallError('环境变量名称无效')
    return ''.join(k + '=' + env_quote(v) + '\n' for k, v in values.items())


def validate_url(value):
    p = urllib.parse.urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise InstallError('API 地址必须是无用户名、密码、查询参数的 HTTPS URL')
    return value.rstrip('/')


def ask(label, default='', secret=False):
    prompt = label + (' [已设置，回车保留]' if secret and default else (' [' + str(default) + ']' if default != '' else '')) + ': '
    return (getpass.getpass(prompt) if secret else input(prompt)).strip() or default


def welcome():
    """首次安装给出一句话说明与获取凭据的路径，避免用户卡在第一步。"""
    print('''
============================================================
 电报双智能体工作台 · 安装向导
============================================================
安装过程会询问少量必填配置，密钥输入时不回显。
开始前请准备好：
  1. 电报机器人令牌：在对 Telegram 的 @BotFather 发送 /newbot 获取
  2. 你自己的数字用户编号：对 @userinfobot 发送任意消息即可看到
  3. 模型网关地址与密钥：任何兼容 Anthropic 或 OpenAI 协议的网关
  4. （可选）生图接口地址、密钥与模型名
按 Ctrl+C 可随时中止，不会留下半成品服务。
============================================================
''')


def boolean(label, default=False):
    while True:
        answer = input(label + (' [Y/n，是/否]: ' if default else ' [y/N，是/否]: ')).strip().lower()
        if not answer:
            return default
        if answer in ('y', 'yes', '是'):
            return True
        if answer in ('n', 'no', '否'):
            return False
        print('请输入 y、n、是或否。')


def interactive_available():
    """判断能否进行交互问答：需要可读的标准输入且能控制回显。"""
    try:
        if not sys.stdin.isatty():
            return False
        import termios
        termios.tcgetattr(sys.stdin.fileno())
        return True
    except Exception:
        return False


def collect_config(old, yes=False):
    c = dict(old)
    if not yes:
        if not interactive_available():
            raise InstallError(
                '当前没有可交互的终端，无法完成问答安装。\n'
                '请在真正的终端里执行安装命令（不要用 nohup、重定向或 CI 管道），\n'
                '或者准备好 JSON 配置后加 --yes --config /路径/config.json 无人值守安装。\n'
                '配置模板见仓库中的 install.example.json。')
        if not old:
            welcome()
        for key, label, secret in [
            ('telegram_bot_token', 'Telegram Bot Token', True),
            ('telegram_owner_id', 'Telegram owner 数字 ID', False),
            ('chat_base_url', '聊天网关 base URL（不带 /v1）', False),
            ('chat_api_key', '聊天网关 API key', True),
            ('default_model', '默认模型 ID', False),
        ]:
            c[key] = ask(label, c.get(key, ''), secret)
        c['enable_claude'] = boolean('启用 Claude Code', c.get('enable_claude', True))
        c['enable_codebuddy'] = boolean('启用 CodeBuddy Code', c.get('enable_codebuddy', True))
        c['enable_codex'] = boolean('启用 Codex CLI（走 npm，需要能访问 npm registry）', c.get('enable_codex', False))
        c['voice_enabled'] = boolean('启用语音转写（下载 faster-whisper tiny 模型）', c.get('voice_enabled', True))
        c['image_enabled'] = boolean('配置图片生成', c.get('image_enabled', False))
        if c['image_enabled']:
            for key, label, secret in [('image_base_url', '图片 API base URL', False), ('image_api_key', '图片 API key', True), ('image_model', '图片模型 ID', False)]:
                c[key] = ask(label, c.get(key, ''), secret)
        c['local_label'] = ask('本机显示名称', c.get('local_label', '本机'))
        c['remote_enabled'] = boolean('配置远程 SSH 目标', c.get('remote_enabled', False))
        if c['remote_enabled']:
            for key, label, default in [('remote_host', '远程主机', ''), ('remote_user', '远程用户', 'root'), ('remote_alias', 'SSH 别名', 'agent-remote'), ('remote_port', 'SSH 端口', 22), ('remote_label', '远程显示名称', '远程服务器')]:
                c[key] = ask(label, c.get(key, default))
            c['ssh_import_path'] = ask('可选：导入专门给机器人的现有私钥路径（空则生成专用密钥）', c.get('ssh_import_path', ''))
        c['sudo_nopasswd'] = boolean('明确授权：允许运行用户 sudo NOPASSWD: ALL（等同本机管理员权限）', c.get('sudo_nopasswd', True))
        c['sudo_authorized'] = True
        # 平台授权是可选能力：不填 client_id 时，机器人的「授权GitHub」会提示未配置，
        # 其余功能不受影响。所以这里允许留空，不强制交互。
        c['github_client_id'] = ask('可选：GitHub OAuth App 的 client_id（用于设备码授权，留空则关闭该功能）',
                                    c.get('github_client_id', ''))
        c['memory_bundle'] = ask('可选私有记忆技能包路径（不迁移则留空）', c.get('memory_bundle', ''))
    for key in ('enable_claude', 'enable_codebuddy', 'voice_enabled'):
        c.setdefault(key, True)
    for key in ('image_enabled', 'remote_enabled', 'sudo_nopasswd', 'enable_codex'):
        # 前三个是「高风险默认关」，enable_codex 是「新增组件默认关」：Codex 走 npm 装系统级包，
        # 老用户升级时不该因为多了一个字段就被动多装一个执行器。两种理由不同，结论一致。
        c.setdefault(key, False)
    c.setdefault('default_model', 'auto')
    c.setdefault('local_label', '本机')
    c.setdefault('github_client_id', '')
    validate_config(c)
    return c


def validate_config(c):
    for key in ('telegram_bot_token', 'telegram_owner_id', 'chat_base_url', 'chat_api_key', 'default_model'):
        if not str(c.get(key, '')).strip():
            raise InstallError('缺少配置字段：' + key)
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', str(c['telegram_bot_token'])):
        raise InstallError('Telegram bot token 格式无效')
    if not re.fullmatch(r'[1-9][0-9]*', str(c['telegram_owner_id'])):
        raise InstallError('owner ID 必须是正整数')
    for key in ('enable_claude', 'enable_codebuddy', 'enable_codex', 'voice_enabled', 'image_enabled', 'remote_enabled', 'sudo_nopasswd'):
        if not isinstance(c.get(key), bool):
            raise InstallError('配置字段必须是布尔值：' + key)
    engines = [name for name in ENGINES_ORDER if c.get('enable_' + name)]
    if not engines:
        raise InstallError('至少启用一个智能体')
    base = validate_url(c['chat_base_url'])
    c['chat_base_url'] = base[:-3] if base.endswith('/v1') else base
    if c['image_enabled']:
        validate_url(c.get('image_base_url', ''))
        if not c.get('image_api_key') or not c.get('image_model'):
            raise InstallError('图片 API key 和模型不能为空')
    if c['sudo_nopasswd'] and c.get('sudo_authorized') is not True:
        raise InstallError('无人值守启用 sudo 须在 JSON 明确设置 sudo_authorized: true')
    if c['remote_enabled']:
        for key in ('remote_host', 'remote_user', 'remote_alias'):
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', str(c.get(key, ''))):
                raise InstallError('SSH 配置值无效：' + key)
        if not 1 <= int(c.get('remote_port', 22)) <= 65535:
            raise InstallError('SSH 端口超出范围')
    for value in c.values():
        if isinstance(value, str):
            env_quote(value)


def run(command, *, user=None, home=None, capture=False, quiet=False, stdin_text=None):
    env = os.environ.copy()
    if home:
        env.update(HOME=str(home), PATH=str(home / '.local/bin') + ':' + os.environ.get('PATH', '/usr/bin:/bin'))
    if user:
        command = ['runuser', '-u', user, '--', 'env', 'HOME=' + str(home), 'PATH=' + env['PATH']] + list(command)
    result = subprocess.run(command, env=env, text=True, stdin=subprocess.PIPE if stdin_text is not None else None,
                            input=stdin_text,
                            stdout=subprocess.PIPE if capture or quiet else None,
                            stderr=subprocess.PIPE if capture or quiet else None)
    if result.returncode:
        raise InstallError('命令失败：' + Path(command[0]).name + '（输出已隐藏，避免泄露认证信息）')
    return result.stdout or ''


def atomic_write(path, text, mode=0o600, uid=0, gid=0):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        if hasattr(os, 'fchown'):
            os.fchown(fd, uid, gid)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def json_text(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + '\n'


def runtime_environment(c, home):
    engines = [name for name in ENGINES_ORDER if c['enable_' + name]]
    values = {
        'HOME': str(home), 'AGENT_HOME': str(home), 'APP_DIR': str(APP), 'CONFIG_DIR': str(CONFIG),
        'PATH': str(home / '.local/bin') + ':' + str(APP / '.venv/bin') + ':/usr/local/bin:/usr/bin:/bin',
        'TELEGRAM_BOT_TOKEN': c['telegram_bot_token'], 'TELEGRAM_OWNER_ID': c['telegram_owner_id'],
        'ANTHROPIC_BASE_URL': c['chat_base_url'].rstrip('/'), 'ANTHROPIC_API_KEY': c['chat_api_key'],
        'ANTHROPIC_MODEL': c['default_model'],
        'OPENAI_API_KEY': c['chat_api_key'], 'OPENAI_BASE_URL': c['chat_base_url'].rstrip('/') + '/v1',
        'CLAUDE_BOT_WORKDIR': str(home / 'tasks'), 'LOCAL_LABEL': c.get('local_label', '本机'),
        'REMOTE_ALIAS': c.get('remote_alias', '') if c['remote_enabled'] else '',
        'REMOTE_LABEL': c.get('remote_label', '远程服务器'), 'ENABLED_AGENTS': ','.join(engines),
        'VOICE_ENABLED': '1' if c['voice_enabled'] else '0', 'WHISPER_MODEL': 'tiny',
        'IMAGE_CONFIG': str(CONFIG / 'image.json'), 'IMAGE_CONFIG_FILE': str(CONFIG / 'image.json'),
        'AGENT_TOOL_PYTHON': '/usr/bin/python3',
        'IMAGE_MODEL': c.get('image_model', '') if c['image_enabled'] else '',
        'IMAGE_MODELS': c.get('image_model', '') if c['image_enabled'] else '',
        # GitHub 设备码授权只需 client_id（公开值，无 secret）。
        # 留空则该功能在机器人里提示「未配置」，不会报错也不会误发请求。
        'GITHUB_CLIENT_ID': c.get('github_client_id', ''),
        'DISABLE_AUTOUPDATER': '1', 'HF_HOME': str(home / '.cache/huggingface'),
    }
    if c.get('_test_api_base'):
        values['TELEGRAM_API_BASE'] = c['_test_api_base']
    return values


def service_text(user, home):
    return '''[Unit]
Description=Telegram Agent Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=%s
Group=%s
WorkingDirectory=%s/tasks
EnvironmentFile=%s/bot.env
ExecStart=%s/.venv/bin/python %s/bot.py
Restart=on-failure
RestartSec=5
UMask=0077
KillMode=control-group
TimeoutStopSec=30
MemoryHigh=650M
MemoryMax=900M
TasksMax=48
CPUQuota=100%%
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
''' % (user, user, home, CONFIG, APP, APP)


def gateway_models(c):
    base = c['chat_base_url'].rstrip('/')
    url = base + ('/models' if base.endswith('/v1') else '/v1/models')
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + c['chat_api_key']})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
        result = json.load(response)
    models = result.get('data', [])
    if not models or any(not isinstance(m, dict) or not isinstance(m.get('id'), str) for m in models):
        raise InstallError('网关 /models 返回格式错误或列表为空')
    endpoint = base if base.endswith('/v1') else base + '/v1'
    return {'models': [{'id': m['id'], 'name': m['id'], 'vendor': 'Custom',
                        'apiKey': c['chat_api_key'], 'url': endpoint,
                        'maxInputTokens': 128000, 'maxOutputTokens': 8192,
                        'supportsToolCall': m.get('supports_tool_call', True),
                        'supportsImages': m.get('supports_images', False)} for m in models],
            'availableModels': [m['id'] for m in models]}


def install_cli(name, user, home):
    if (home / '.local/bin' / name).exists():
        run([str(home / '.local/bin' / name), '--version'], user=user, home=home, quiet=True)
        return
    url = OFFICIAL_INSTALLERS[name]
    validate_url(url)
    # 官方页面会拒绝部分默认客户端；下载落地后仍以固定哈希为信任依据。
    with tempfile.TemporaryDirectory(prefix='agent-cli-download-') as directory:
        downloaded = Path(directory) / 'installer.sh'
        run(['curl', '--proto', '=https', '--proto-redir', '=https', '--tlsv1.2', '-fLsS', '--max-time', '60', url, '-o', str(downloaded)], quiet=True)
        script = downloaded.read_bytes()
    if len(script) > 2 * 1024 * 1024 or b'\x00' in script or not script.startswith(b'#!'):
        raise InstallError('官方安装脚本格式无效')
    expected = INSTALLER_SHA256.get(name)
    if not expected or hashlib.sha256(script).hexdigest() != expected:
        raise InstallError('官方安装脚本 SHA256 不匹配，请维护者重新审阅官方脚本并更新安装器')
    print(name + '：官方脚本 SHA256 已核验')
    target = home / ('.install-' + name + '.sh')
    import pwd
    account = pwd.getpwnam(user)
    atomic_write(target, script.decode('utf-8'), 0o600, account.pw_uid, account.pw_gid)
    try:
        run(['bash', '-n', str(target)], quiet=True)
        run(['bash', str(target)], user=user, home=home, quiet=True)
        run([str(home / '.local/bin' / name), '--version'], user=user, home=home, quiet=True)
    finally:
        target.unlink(missing_ok=True)


def ensure_node_runtime():
    """Codex CLI 需要 node/npm。

    apt 的 nodejs 包在 Debian 12 上是 18.x、Ubuntu 22.04 上是 12.x 起，都低于 Codex 要求的
    最低版本，所以 apt 那条路只当兜底；真正可靠的是 NodeSource 的 22.x 源。
    这里显式检查大版本，不合格就直接换源，不靠「装上了就算成功」。
    """
    if shutil.which('npm') and shutil.which('node'):
        version = subprocess.run(['node', '--version'], text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL).stdout.strip()
        match = re.fullmatch(r'v(\d+)\.', version + '.')
        if match and int(match.group(1)) >= 20:
            return
    # NodeSource 安装脚本同样先下载再执行；它只负责配源，包本身仍走 apt 的签名校验。
    run(['apt-get', 'install', '-y', 'ca-certificates', 'curl', 'gnupg'], quiet=True)
    run(['bash', '-c', 'curl -fsSL https://deb.nodesource.com/setup_22.x | bash -'], quiet=True)
    run(['apt-get', 'install', '-y', 'nodejs'], quiet=True)
    if not (shutil.which('npm') and shutil.which('node')):
        raise InstallError('Node.js 安装失败，无法继续安装 Codex CLI')


def install_codex_cli():
    """用 npm 安装 Codex CLI。

    这条路和 Claude/CodeBuddy 的「下载官方脚本 + 校验 SHA256」不是一回事：Codex 没有安装脚本，
    只有 npm 包。所以这里不假装有哈希校验，改成三件事守住边界：
    版本钉死、包名固定、装完立刻用 --version 自检。
    """
    if not shutil.which('node') or not shutil.which('npm'):
        raise InstallError('未找到 node/npm，无法安装 Codex CLI（启用 Codex 时安装器会自动配置 Node.js）')
    # 以 root 装到系统级 node_modules，机器人用户直接可用，不依赖它自己的 npm 前缀。
    run(['npm', 'install', '-g', '--no-fund', '--no-audit', CODEX_PACKAGE + '@' + CODEX_VERSION], quiet=True)
    if not shutil.which('codex'):
        raise InstallError('npm 安装完成但未找到 codex 命令，请检查 npm 全局 bin 目录是否在 PATH 中')
    # 版本自检：确认装上的确实是钉死的那个版本，而不是镜像源上的其它构建。
    actual = run(['codex', '--version'], capture=True).strip()
    if CODEX_VERSION not in actual:
        raise InstallError('Codex 版本不符，期望 ' + CODEX_VERSION + '，实际 ' + (actual or '无输出'))


def codex_config_text(base_url, model):
    """生成 Codex 的 provider 配置。

    base_url 必须写到 /v1（不能写 /v1/responses），wire_api 用 responses；
    requires_openai_auth 关掉，否则 Codex 会坚持走 ChatGPT 账号登录而不是 API key。
    凭据不落在这个文件里，运行时由 bot.env 的 OPENAI_API_KEY 提供。
    """
    base = base_url.rstrip('/')
    if not base.endswith('/v1'):
        base += '/v1'
    return (
        '# Codex 指向安装时填写的模型网关。\n'
        '# 凭据由 systemd EnvironmentFile 注入 OPENAI_API_KEY，不写在这里。\n'
        'model = ' + json.dumps(str(model)) + '\n'
        'model_provider = "gateway"\n'
        '\n'
        '[model_providers.gateway]\n'
        'name = "Model Gateway"\n'
        'base_url = ' + json.dumps(base) + '\n'
        'wire_api = "responses"\n'
        'env_key = "OPENAI_API_KEY"\n'
        'requires_openai_auth = false\n'
    )


def configure_ssh(c, user, home, uid, gid, yes):
    if not c['remote_enabled']:
        return
    folder = home / '.ssh'
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    os.chown(folder, uid, gid)
    key = folder / 'telegram-agent-ed25519'
    imported = c.get('ssh_import_path')
    if imported:
        source = Path(imported).expanduser()
        if not source.is_file() or source.is_symlink():
            raise InstallError('导入私钥必须是普通文件')
        if source.resolve() != key.resolve():
            atomic_write(key, source.read_text(), 0o600, uid, gid)
    elif not key.exists():
        run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'telegram-agent-hub', '-f', str(key)], user=user, home=home, quiet=True)
    public = run(['ssh-keygen', '-y', '-f', str(key)], user=user, home=home, capture=True).strip()
    print('请将以下专用公钥放入远端 authorized_keys：\n' + public)
    host, port = c['remote_host'], str(c.get('remote_port', 22))
    scanned = run(['ssh-keyscan', '-T', '10', '-p', port, host], capture=True)
    if not scanned.strip():
        raise InstallError('未取得远端主机公钥')
    pending = folder / 'known_hosts.pending'
    atomic_write(pending, scanned, 0o600, uid, gid)
    try:
        fingerprints = run(['ssh-keygen', '-lf', str(pending)], capture=True)
        print('请通过可信渠道独立核对以下 SSH 主机指纹：\n' + fingerprints)
        expected = c.get('remote_fingerprint', '')
        if yes:
            if not expected or expected not in fingerprints.split():
                raise InstallError('--yes 配置 SSH 须提供已独立核对的 remote_fingerprint（SHA256:...）')
        else:
            expected = ask('输入已独立核对的 SHA256 指纹')
            if not expected.startswith('SHA256:') or expected not in fingerprints.split():
                raise InstallError('主机指纹不匹配')
            c['remote_fingerprint'] = expected
        # 只信任已核对的那一把主机公钥。
        trusted = []
        for line in scanned.splitlines():
            atomic_write(pending, line + '\n', 0o600, uid, gid)
            if expected in run(['ssh-keygen', '-lf', str(pending)], capture=True).split():
                trusted.append(line)
        atomic_write(folder / 'known_hosts', '\n'.join(trusted) + '\n', 0o600, uid, gid)
    finally:
        pending.unlink(missing_ok=True)
    config = 'Host ' + c['remote_alias'] + '\n  HostName ' + host + '\n  User ' + c['remote_user'] + '\n  Port ' + port + '\n  IdentityFile ' + str(key) + '\n  IdentitiesOnly yes\n  BatchMode yes\n  StrictHostKeyChecking yes\n  UserKnownHostsFile ' + str(folder / 'known_hosts') + '\n'
    atomic_write(folder / 'config', config, 0o600, uid, gid)
    if not yes:
        input('远端公钥授权完成后按回车测试连接：')
    run(['ssh', '-o', 'ConnectTimeout=15', c['remote_alias'], 'true'], user=user, home=home, quiet=True)


def shared_memory(source, home, uid, gid):
    memory = home / 'memory'
    memory.mkdir(mode=0o700, exist_ok=True)
    os.chown(memory, uid, gid)
    supplied = source / 'memory'
    if supplied.is_dir():
        for item in supplied.rglob('*'):
            if item.is_symlink():
                raise InstallError('记忆素材不能包含符号链接')
            dest = memory / item.relative_to(supplied)
            if item.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                os.chown(dest, uid, gid)
            elif item.is_file() and not dest.exists():
                atomic_write(dest, item.read_text(encoding='utf-8'), 0o600, uid, gid)
    # 私有记忆包只导入素材，不携带配置、登录态或原生会话。
    bundle = os.environ.get('AGENT_MEMORY_BUNDLE', '')
    if bundle:
        import tarfile
        with tarfile.open(bundle, 'r:gz') as archive:
            for item in archive.getmembers():
                relative = Path(item.name)
                if relative.is_absolute() or '..' in relative.parts or item.issym() or item.islnk():
                    raise InstallError('记忆包路径无效')
                if not item.isfile() or item.size > 2 * 1024 * 1024:
                    continue
                if not relative.parts or relative.parts[0] not in ('skills', 'memory-import', 'server-memory'):
                    continue
                dest = memory / relative
                content = archive.extractfile(item).read().decode('utf-8')
                atomic_write(dest, content, 0o600, uid, gid)
                parent = dest.parent
                while parent != memory:
                    os.chown(parent, uid, gid)
                    parent = parent.parent
    skills = memory / 'skills'
    skills.mkdir(exist_ok=True)
    os.chown(skills, uid, gid)
    for agent in ('.claude', '.codebuddy', '.codex'):
        folder = home / agent
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chown(folder, uid, gid)
        link = folder / 'skills'
        if not link.exists() and not link.is_symlink():
            link.symlink_to(skills, target_is_directory=True)
    instructions = memory / 'AGENTS.md'
    if not instructions.exists():
        atomic_write(instructions, '所有任务在授权范围内执行；共享记忆位于 ' + str(memory) + '。\n', 0o600, uid, gid)
    # Codex 读的是 AGENTS.md，Claude/CodeBuddy 读各自命名的文件，三个都指向同一份共享记忆。
    for directory in (home, home / 'tasks'):
        for name in ('CLAUDE.md', 'CODEBUDDY.md', 'AGENTS.md'):
            link = directory / name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(instructions)


def platform_check():
    if sys.platform != 'linux' or platform.machine() not in ('x86_64', 'aarch64', 'arm64'):
        raise InstallError('仅支持 Linux x86_64/arm64')
    release = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            release[key] = value.strip('"')
    if (release.get('ID'), release.get('VERSION_ID')) not in {('debian', '12'), ('ubuntu', '22.04'), ('ubuntu', '24.04')}:
        raise InstallError('仅支持 Debian 12 / Ubuntu 22.04、24.04')
    if not Path('/run/systemd/system').is_dir():
        raise InstallError('需要 systemd 系统')


def verify_telegram(c):
    api_base = c.get('_test_api_base', 'https://api.telegram.org')
    request = urllib.request.Request(api_base + '/bot' + c['telegram_bot_token'] + '/getMe')
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            result = json.load(response)
        if result.get('ok') is not True or not result.get('result', {}).get('is_bot'):
            raise InstallError('Telegram getMe 验证失败')
    except InstallError:
        raise
    except Exception:
        raise InstallError('Telegram getMe 验证失败，请检查 token 或网络；认证信息已隐藏') from None


def install(args, c):
    import pwd
    platform_check()
    if os.geteuid() != 0:
        raise InstallError('请通过 sudo python3 install.py 执行')
    user = args.user
    home = Path('/home') / user
    verify_telegram(c)
    source = Path(__file__).resolve().parent
    unit = Path('/etc/systemd/system') / (args.service_name + '.service')
    if not args.no_deps:
        dependencies = list(APT_DEPENDENCIES)
        run(['apt-get', 'update'], quiet=True)
        run(['apt-get', 'install', '-y'] + dependencies, quiet=True)
        if c['enable_codex']:
            ensure_node_runtime()
    try:
        account = pwd.getpwnam(user)
        if account.pw_uid == 0 or Path(account.pw_dir) != home:
            raise InstallError('运行用户不能是 root，且 home 必须是 ' + str(home))
    except KeyError:
        run(['useradd', '--create-home', '--home-dir', str(home), '--shell', '/bin/bash', user], quiet=True)
        account = pwd.getpwnam(user)
    uid, gid = account.pw_uid, account.pw_gid
    for folder in (home, home / 'tasks'):
        folder.mkdir(parents=True, exist_ok=True)
        os.chown(folder, uid, gid)
        os.chmod(folder, 0o700)
    CONFIG.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chmod(CONFIG, 0o750)
    os.chown(CONFIG, 0, gid)
    backup = CONFIG / ('backup-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(os.getpid()))
    backup.mkdir(mode=0o700)
    tracked = [unit, CONFIG / 'bot.env', CONFIG / 'config.json', CONFIG / 'image.json', home / '.codebuddy/models.json', home / '.claude/settings.json', home / '.codex/config.toml', home / '.ssh/config', home / '.ssh/known_hosts', home / '.ssh/telegram-agent-ed25519', Path('/etc/sudoers.d') / args.service_name]
    tracked += [APP / p.name for p in source.glob('*.py')]
    saved = []
    for index, path in enumerate(tracked):
        if path.is_symlink():
            raise InstallError('拒绝覆盖符号链接：' + str(path))
        data = path.read_bytes() if path.exists() else None
        meta = path.stat() if data is not None else None
        saved.append((path, data, meta))
        if data is not None:
            atomic_write(backup / str(index), data.decode('utf-8'), 0o600)
    was_active = subprocess.run(['systemctl', 'is-active', '--quiet', args.service_name]).returncode == 0
    was_enabled = subprocess.run(['systemctl', 'is-enabled', '--quiet', args.service_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    try:
        models = gateway_models(c) if c['enable_codebuddy'] else None
        for name in ENGINES_ORDER:
            if not c['enable_' + name]:
                continue
            # Codex 没有官方安装脚本，走 npm；另两个走带哈希校验的官方脚本。
            if name == 'codex':
                install_codex_cli()
            else:
                install_cli(name, user, home)
        if was_active:
            run(['systemctl', 'stop', args.service_name], quiet=True)
        APP.mkdir(parents=True, exist_ok=True)
        os.chmod(APP, 0o755)
        for item in source.glob('*.py'):
            atomic_write(APP / item.name, item.read_text(encoding='utf-8'), 0o644)
        venv = APP / '.venv'
        if not (venv / 'bin/python').exists():
            run(['python3', '-m', 'venv', str(venv)], quiet=True)
        if c['voice_enabled']:
            speech = home / 'speech'
            speech.mkdir(mode=0o700, exist_ok=True)
            os.chown(speech, uid, gid)
            speech_venv = speech / 'venv'
            if not (speech_venv / 'bin/python').exists():
                run(['python3', '-m', 'venv', str(speech_venv)], user=user, home=home, quiet=True)
            run([str(speech_venv / 'bin/pip'), 'install', '--disable-pip-version-check', 'faster-whisper'], user=user, home=home, quiet=True)
            run([str(speech_venv / 'bin/python'), '-c', "from pathlib import Path; from faster_whisper import WhisperModel; from faster_whisper.utils import download_model; p=str(Path.home() / 'speech/model'); download_model('tiny', output_dir=p); WhisperModel(p, device='cpu', compute_type='int8')"], user=user, home=home, quiet=True)
        if c.get('memory_bundle'):
            os.environ['AGENT_MEMORY_BUNDLE'] = str(Path(c['memory_bundle']).expanduser().resolve(strict=True))
        else:
            os.environ.pop('AGENT_MEMORY_BUNDLE', None)
        shared_memory(source, home, uid, gid)
        configure_ssh(c, user, home, uid, gid, args.yes)
        if models:
            atomic_write(home / '.codebuddy/models.json', json_text(models), 0o600, uid, gid)
        atomic_write(home / '.claude/settings.json', json_text({'env': {'ANTHROPIC_BASE_URL': c['chat_base_url'], 'ANTHROPIC_API_KEY': c['chat_api_key'], 'ANTHROPIC_MODEL': c['default_model'], 'DISABLE_AUTOUPDATER': '1'}}), 0o600, uid, gid)
        if c['enable_codex']:
            atomic_write(home / '.codex/config.toml', codex_config_text(c['chat_base_url'], c['default_model']), 0o600, uid, gid)
        sudo_path = Path('/etc/sudoers.d') / args.service_name
        if c['sudo_nopasswd']:
            atomic_write(sudo_path, user + ' ALL=(ALL) NOPASSWD: ALL\n', 0o440)
            run(['visudo', '-cf', str(sudo_path)], quiet=True)
        elif sudo_path.exists():
            sudo_path.unlink()
        image = {'base_url': c.get('image_base_url', ''), 'api_key': c.get('image_api_key', ''), 'model': c.get('image_model', '')}
        if c['image_enabled']:
            atomic_write(CONFIG / 'image.json', json_text(image), 0o640, 0, gid)
        else:
            (CONFIG / 'image.json').unlink(missing_ok=True)
        # 生图工具与桥接程序同目录，缺一即视为通道未启用。
        tool_path = APP / 'image_tool.py'
        if c['image_enabled'] and not tool_path.exists():
            raise InstallError('生图工具缺失：' + str(tool_path))
        atomic_write(CONFIG / 'bot.env', environment_text(runtime_environment(c, home)), 0o640, 0, gid)
        # 导入路径只是安装输入，避免保存调用者的本机路径。
        stored = {k: v for k, v in c.items() if k != 'ssh_import_path' and not k.startswith('_')}
        atomic_write(CONFIG / 'config.json', json_text(stored), 0o600)
        atomic_write(unit, service_text(user, home), 0o644)
        run(['systemd-analyze', 'verify', str(unit)], quiet=True)
        run(['systemctl', 'daemon-reload'], quiet=True)
        run(['systemctl', 'enable', args.service_name], quiet=True)
        run(['systemctl', 'restart', args.service_name], quiet=True)
        time.sleep(2)
        run(['systemctl', 'is-active', '--quiet', args.service_name], quiet=True)
        restarts = run(['systemctl', 'show', args.service_name, '--property=NRestarts', '--value'], capture=True).strip()
        if restarts != '0':
            raise InstallError('服务启动后发生自动重启，已触发安装回滚')
        if c['image_enabled']:
            # 用桥接程序的环境跑一次协议握手，确认生图工具真的可用。
            probe = run([str(APP / '.venv/bin/python'), str(tool_path)], user=user, home=home, capture=True,
                        stdin_text=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}) + '\n')
            if 'generate_image' not in probe:
                raise InstallError('生图工具协议自检失败，已触发安装回滚')
    except BaseException:
        subprocess.run(['systemctl', 'stop', args.service_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not was_enabled:
            subprocess.run(['systemctl', 'disable', args.service_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for path, data, meta in reversed(saved):
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data.decode('utf-8'), meta.st_mode & 0o777, meta.st_uid, meta.st_gid)
        subprocess.run(['systemctl', 'daemon-reload'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['systemctl', 'restart' if was_active else 'stop', args.service_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print('安装失败，代码、配置、SSH 配置和服务文件已恢复；新装依赖、模型缓存和专用密钥保留。', file=sys.stderr)
        raise
    print('安装完成。服务：' + args.service_name + '；运行用户：' + user + '；备份：' + str(backup))


def main(argv=None):
    global APP, CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='无人值守 JSON 配置，不能包含 shell 代码')
    parser.add_argument('--yes', action='store_true', help='继承配置且不交互；授权字段必须显式提供')
    parser.add_argument('--dry-run', action='store_true', help='只验证配置并显示不含秘密的计划')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--uninstall', action='store_true', help='停用服务并移除本安装器 sudo 规则，保留全部数据')
    parser.add_argument('--no-deps', action='store_true')
    parser.add_argument('--test-api-base', help='仅隔离测试：loopback Telegram 假 API 根地址，禁止用于正式服务')
    parser.add_argument('--service-name', default='telegram-agent-hub')
    parser.add_argument('--user', default='agentbot')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', args.user) or args.user == 'root':
        parser.error('运行用户名无效')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,60}', args.service_name):
        parser.error('服务名称无效')
    if args.service_name != 'telegram-agent-hub':
        APP = Path('/opt') / args.service_name
        CONFIG = Path('/etc') / args.service_name
        if args.user == 'agentbot':
            parser.error('自定义测试服务必须同时指定独立 --user，避免修改正式用户配置')
    if args.status:
        return subprocess.run(['systemctl', 'is-active', args.service_name]).returncode
    if args.uninstall:
        if not args.yes and not boolean('停用服务并撤销其 sudo 规则，保留配置、工作目录、模型和专用密钥'):
            return 0
        if not hasattr(os, 'geteuid') or os.geteuid() != 0:
            raise InstallError('卸载须使用 sudo')
        run(['systemctl', 'disable', '--now', args.service_name], quiet=True)
        unit = Path('/etc/systemd/system') / (args.service_name + '.service')
        unit.unlink(missing_ok=True)
        (Path('/etc/sudoers.d') / args.service_name).unlink(missing_ok=True)
        run(['systemctl', 'daemon-reload'], quiet=True)
        print('服务已卸载，配置与数据已保留。')
        return 0
    existing = CONFIG / 'config.json'
    c = read_json(existing) if existing.exists() else {}
    if args.config:
        c.update(read_json(args.config))
    c.pop('_test_api_base', None)
    c = collect_config(c, args.yes)
    if args.test_api_base:
        p = urllib.parse.urlsplit(args.test_api_base)
        if args.service_name == 'telegram-agent-hub' or args.user == 'agentbot':
            raise InstallError('--test-api-base 必须使用独立 --service-name 和 --user')
        if p.scheme not in ('http', 'https') or p.hostname not in ('127.0.0.1', '::1') or p.username or p.password or p.path not in ('', '/') or p.query or p.fragment:
            raise InstallError('--test-api-base 仅允许 127.0.0.1 或 [::1] 的 HTTP(S) 根地址')
        if p.port is None or not 1 <= p.port <= 65535:
            raise InstallError('--test-api-base 必须显式指定有效端口')
        c['_test_api_base'] = args.test_api_base.rstrip('/')
        print('隔离测试模式：Telegram 请求发送至本机假 API，禁止用于正式部署。')
    if args.dry_run:
        print(json_text({'app_dir': str(APP), 'config_dir': str(CONFIG), 'user': args.user, 'service': args.service_name,
                         'agents': [n for n in ENGINES_ORDER if c['enable_' + n]],
                         'voice_enabled': c['voice_enabled'], 'remote_enabled': c['remote_enabled'],
                         'sudo_nopasswd': c['sudo_nopasswd'], 'apt_dependencies': not args.no_deps}))
        return 0
    install(args, c)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (InstallError, OSError, ValueError, KeyError) as exc:
        # 网络异常信息可能包含带认证路径，禁止直接输出原始异常。
        print('安装未完成：' + (str(exc) if isinstance(exc, InstallError) else type(exc).__name__) + '。配置和密钥不会输出到日志。', file=sys.stderr)
        sys.exit(1)
