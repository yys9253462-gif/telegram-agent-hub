#!/usr/bin/env python3
"""固定仓库代码更新：验证归档、停机替换、健康检查和失败回滚。"""
import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import bootstrap

REPO = 'yys9253462-gif/telegram-agent-hub'
MAX_ARCHIVE = 20 * 1024 * 1024
REQUIRED_FILES = frozenset({
    'bot.py', 'credentials.py', 'cloudflare_auth.py', 'github_auth.py',
    'transcribe.py', 'image_tool.py',
    'bootstrap.py', 'install.py', 'updater.py',
})
# 拆分出来的模块。先作为可选文件登记：旧版本没有它们也能通过校验；
# 新版本带上它们时升级器才会一起替换。必须先发布一版认得这些文件名的
# 升级器，再发布真正依赖它们的代码，否则旧升级器会漏拷新模块。
OPTIONAL_FILES = frozenset({'usage.py', 'textfmt.py'})
CODE_FILES = REQUIRED_FILES | OPTIONAL_FILES
COMMIT_RE = re.compile(r'[0-9a-f]{40}')
HASH_RE = re.compile(r'[0-9a-f]{64}')
SERVICE_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\.service')


class UpdateError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _trusted(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise UpdateError('更新安装元数据或目录权限不安全')


def _app_path(app_dir):
    app = Path(app_dir)
    if not app.is_absolute() or app != app.resolve():
        raise UpdateError('安装目录必须是无符号链接的绝对路径')
    for parent in (app, *app.parents):
        _trusted(parent, directory=True)
    return app


def _json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        raise UpdateError('无法读取更新元数据或任务状态') from None


def _atomic(path, data, mode=0o644):
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _write_json(path, value):
    _atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def load_install(app_dir):
    app = _app_path(app_dir)
    path = app / 'update-install.json'
    _trusted(path)
    config = _json(path)
    if not isinstance(config, dict) or config.get('app_dir') != str(app):
        raise UpdateError('更新安装目录与可信配置不匹配')
    if not isinstance(config.get('service'), str) or not SERVICE_RE.fullmatch(config['service']):
        raise UpdateError('更新服务名称无效')
    for key in ('work_dir', 'python'):
        value = config.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise UpdateError('更新安装路径配置无效')
    return config


def create_manifest(app_dir, commit, service):
    app = Path(app_dir)
    if not isinstance(commit, str) or (commit and not COMMIT_RE.fullmatch(commit)) or not SERVICE_RE.fullmatch(service):
        raise UpdateError('安装版本或服务名称无效')
    files = {}
    for name in sorted(CODE_FILES):
        path = app / name
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise UpdateError('受控代码路径不安全')
            files[name] = digest(path.read_bytes())
    if not REQUIRED_FILES.issubset(files):
        raise UpdateError('安装代码不完整')
    value = {'commit': commit, 'files': files, 'service': service}
    _write_json(app / 'installed-version.json', value)
    return value


def _manifest(app, config):
    path = app / 'installed-version.json'
    _trusted(path)
    value = _json(path)
    if (not isinstance(value, dict) or not isinstance(value.get('commit'), str)
            or (value['commit'] and not COMMIT_RE.fullmatch(value['commit']))
            or value.get('service') != config['service']):
        raise UpdateError('已安装版本元数据无效，请先由安装器建立基线')
    files = value.get('files')
    if (not isinstance(files, dict) or not REQUIRED_FILES.issubset(files)
            or not set(files).issubset(CODE_FILES)
            or any(not isinstance(h, str) or not HASH_RE.fullmatch(h) for h in files.values())):
        raise UpdateError('代码校验基线无效')
    return value


def local_modified(app, manifest):
    modified = []
    for name in sorted(CODE_FILES):
        path = app / name
        expected = manifest['files'].get(name)
        if path.is_symlink() or (path.exists() and (not path.is_file() or expected is None)):
            modified.append(name)
        elif expected is not None and (not path.is_file() or digest(path.read_bytes()) != expected):
            modified.append(name)
    return modified


def _fetch(url, limit, accept):
    request = urllib.request.Request(url, headers={
        'Accept': accept, 'User-Agent': 'telegram-agent-hub-updater',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        with urllib.request.build_opener(bootstrap.SafeRedirect()).open(request, timeout=60) as response:
            body = response.read(limit + 1)
    except Exception:
        raise UpdateError('无法从 GitHub 获取更新，请稍后重试') from None
    if len(body) > limit:
        raise UpdateError('更新下载超过大小限制')
    return body


def latest_version():
    body = _fetch('https://api.github.com/repos/' + REPO + '/contents/version.txt',
                  16384, 'application/vnd.github.raw+json')
    try:
        text = body.decode('utf-8').strip()
        if text.startswith('{'):
            value = json.loads(text)
            if value.get('encoding') != 'base64':
                raise ValueError()
            text = base64.b64decode(''.join(value['content'].split()), validate=True).decode('utf-8').strip()
        match = re.fullmatch(r'([0-9a-f]{40})\s+sha256:([0-9a-f]{64})', text)
        if not match:
            raise ValueError()
        return {'commit': match.group(1), 'sha256': match.group(2)}
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise UpdateError('远程版本格式无效，需要完整提交号和 SHA256') from None


def check(app_dir):
    config = load_install(app_dir)
    app = Path(config['app_dir'])
    installed = _manifest(app, config)
    latest = latest_version()
    return {'current': installed['commit'], 'latest': latest['commit'],
            'update_available': installed['commit'] != latest['commit'],
            'local_modified': local_modified(app, installed)}


def _ensure_idle(config):
    path = Path(config['work_dir']) / 'bridge-state.json'
    if not path.exists():
        return
    state = _json(path)
    if not isinstance(state, dict) or not isinstance(state.get('tasks'), list):
        raise UpdateError('任务状态无效，无法安全更新')
    for task in state['tasks']:
        if not isinstance(task, dict):
            raise UpdateError('任务状态无效，无法安全更新')
        if task.get('status') in ('queued', 'running'):
            raise UpdateError('仍有排队或执行中的任务，请完成任务后更新')


def _service(action, service):
    try:
        result = subprocess.run(['/usr/bin/systemctl', action, service],
                                capture_output=True, timeout=45, check=False)
    except (OSError, subprocess.SubprocessError):
        raise UpdateError('系统服务操作失败') from None
    if result.returncode:
        raise UpdateError('系统服务操作失败：' + action)
    return result.stdout.decode('utf-8', errors='replace').strip()


def _healthy(service):
    _service('is-active', service)
    before = _service('show', service)
    fields = dict(line.split('=', 1) for line in before.splitlines() if '=' in line)
    pid = fields.get('MainPID', '0')
    if pid == '0':
        raise UpdateError('新服务未建立运行进程')
    time.sleep(3)
    _service('is-active', service)
    after = dict(line.split('=', 1) for line in _service('show', service).splitlines() if '=' in line)
    if after.get('MainPID') != pid or after.get('NRestarts') != fields.get('NRestarts'):
        raise UpdateError('新服务启动后发生异常重启')


@contextmanager
def _lock(app):
    import fcntl
    fd = os.open(str(app / '.update.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError('已有更新正在执行') from None
        yield
    finally:
        os.close(fd)


def _prepare(latest, folder):
    data = _fetch('https://api.github.com/repos/' + REPO + '/tarball/' + latest['commit'],
                  MAX_ARCHIVE, 'application/vnd.github+json')
    if digest(data) != latest['sha256']:
        raise UpdateError('更新归档 SHA256 校验失败')
    try:
        source = bootstrap.extract_archive(data, folder)
    except Exception:
        raise UpdateError('更新归档路径或内容不安全') from None
    files = {name: (source / name).read_bytes() for name in CODE_FILES if (source / name).is_file()}
    if not REQUIRED_FILES.issubset(files):
        raise UpdateError('更新归档缺少运行必需代码')
    try:
        for name, content in files.items():
            compile(content, name, 'exec')
    except (SyntaxError, ValueError):
        raise UpdateError('新版本代码编译检查失败') from None
    return files


def _apply_locked(app, config):
    installed = _manifest(app, config)
    if local_modified(app, installed):
        raise UpdateError('本地代码已修改，拒绝覆盖，请先处理本地修改')
    _ensure_idle(config)
    latest = latest_version()
    if latest['commit'] == installed['commit']:
        return {'success': True, 'message': '当前已是最新版本', 'commit': installed['commit']}
    with tempfile.TemporaryDirectory(prefix='.update-stage-', dir=str(app)) as folder:
        files = _prepare(latest, folder)
        if local_modified(app, installed):
            raise UpdateError('准备更新期间本地代码发生变化')
        _ensure_idle(config)
        backups = app / 'updates-backup'
        if backups.exists():
            _trusted(backups, directory=True)
        else:
            backups.mkdir(mode=0o700)
        backup = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'), dir=str(backups)))
        old_files = {}
        for name in installed['files']:
            old_files[name] = (app / name).read_bytes()
            _atomic(backup / name, old_files[name])
        old_metadata = (app / 'installed-version.json').read_bytes()
        _atomic(backup / 'installed-version.json', old_metadata)
        stopped = False
        replaced = False
        try:
            # stop 返回异常时也要尝试恢复服务，因为 systemctl 可能已停止进程。
            stopped = True
            _service('stop', config['service'])
            _ensure_idle(config)
            if local_modified(app, installed):
                raise UpdateError('停机期间本地代码发生变化')
            replaced = True
            for name, content in files.items():
                _atomic(app / name, content)
            for name in set(old_files) - set(files):
                (app / name).unlink()
            create_manifest(app, latest['commit'], config['service'])
            _service('start', config['service'])
            _healthy(config['service'])
        except Exception:
            recovery_ok = True
            if replaced:
                try:
                    _service('stop', config['service'])
                except UpdateError:
                    recovery_ok = False
                for name in set(files) - set(old_files):
                    try:
                        (app / name).unlink(missing_ok=True)
                    except OSError:
                        recovery_ok = False
                for name, content in old_files.items():
                    try:
                        _atomic(app / name, content)
                    except OSError:
                        recovery_ok = False
                try:
                    _atomic(app / 'installed-version.json', old_metadata)
                except OSError:
                    recovery_ok = False
            if stopped and recovery_ok:
                try:
                    _service('start', config['service'])
                    _service('is-active', config['service'])
                except UpdateError:
                    recovery_ok = False
            message = '更新失败，已恢复旧版本并启动服务' if recovery_ok else '更新失败，恢复未完成，服务保持停止；请使用备份目录人工恢复：' + str(backup)
            failure = UpdateError(message)
            failure.backup = str(backup)
            raise failure from None
    return {'success': True, 'message': '更新成功，服务运行正常', 'commit': latest['commit'], 'backup': str(backup)}


def apply(app_dir):
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise UpdateError('应用更新必须通过 root 独立更新服务执行')
    config = load_install(app_dir)
    app = Path(config['app_dir'])
    with _lock(app):
        try:
            result = _apply_locked(app, config)
        except Exception as exc:
            result = {'success': False, 'message': str(exc) if isinstance(exc, UpdateError) else '更新失败，请检查安装状态',
                      'backup': getattr(exc, 'backup', '')}
        result['status'] = 'success' if result['success'] else 'failed'
        result.setdefault('backup', '')
        result.setdefault('commit', '')
        _write_json(app / 'update-result.json', result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='独立服务代码更新器')
    parser.add_argument('--apply', action='store_true', required=True)
    parser.add_argument('--app-dir', required=True)
    args = parser.parse_args(argv)
    try:
        result = apply(args.app_dir)
    except (UpdateError, OSError):
        print('更新失败：安装配置、权限或更新锁无效。', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
