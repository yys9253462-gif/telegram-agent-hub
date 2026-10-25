#!/usr/bin/env python3
"""从 GitHub 公开仓库的固定提交下载源码，安全解包后执行安装器。

公开仓库不需要任何访问令牌；脚本不使用、不请求、不保存 GitHub 凭据。
固定提交（完整 40 字符 SHA）由发布者给出，避免默认分支被改动后静默升级。
"""
import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

# 发布者应填入实际仓库和经过审阅的完整提交 SHA；不使用浮动默认分支。
DEFAULT_REPO = 'yys9253462-gif/telegram-agent-hub'
DEFAULT_REF = ''
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXTRACTED = 128 * 1024 * 1024


class BootstrapError(Exception):
    pass


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != 'https' or target.hostname != 'codeload.github.com' or target.port not in (None, 443) or target.username or target.password:
            raise BootstrapError('拒绝非 GitHub codeload HTTPS 重定向')
        # 新请求从零创建，不携带任何身份信息。
        return urllib.request.Request(newurl, headers={'User-Agent': 'telegram-agent-hub-bootstrap'})


def fetch_archive(repo, commit):
    """公开仓库匿名下载归档；不发送 Authorization。按顺序尝试多个源。"""
    last = None
    for url in (
        'https://api.github.com/repos/' + repo + '/tarball/' + commit,
        'https://ghproxy.net/https://api.github.com/repos/' + repo + '/tarball/' + commit,
        'https://gh-proxy.com/https://api.github.com/repos/' + repo + '/tarball/' + commit,
    ):
        try:
            request = urllib.request.Request(url, headers={
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
                'User-Agent': 'telegram-agent-hub-bootstrap',
            })
            with urllib.request.build_opener(SafeRedirect()).open(request, timeout=90) as response:
                body = response.read(MAX_ARCHIVE + 1)
        except urllib.error.HTTPError as exc:
            last = 'HTTP ' + str(exc.code)
            if exc.code == 404:
                raise BootstrapError('仓库或提交不存在；若为私有仓库，请克隆后执行 bash install.sh') from None
            if exc.code == 403:
                last = 'GitHub 限流'
            continue
        except urllib.error.URLError:
            last = '网络不可达'
            continue
        if len(body) > MAX_ARCHIVE:
            raise BootstrapError('仓库归档超过大小限制')
        return body
    raise BootstrapError('所有下载源均失败（' + str(last) + '），请检查网络或改用镜像安装命令')


def extract_archive(data, destination):
    destination = Path(destination).resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        members = archive.getmembers()
        if not members or len(members) > 10000:
            raise BootstrapError('归档为空或文件数超出限制')
        total = 0
        root = None
        seen = set()
        checked = []
        for member in members:
            name = member.name
            path = PurePosixPath(name)
            if '\\' in name or '\x00' in name or path.is_absolute() or '..' in path.parts or not path.parts or ':' in name:
                raise BootstrapError('归档路径不安全')
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise BootstrapError('归档不得包含符号链接、硬链接或特殊文件')
            if root is None:
                root = path.parts[0]
            if path.parts[0] != root:
                raise BootstrapError('归档须只有一个顶级目录')
            relative = Path(*path.parts)
            target = destination / relative
            if not target.resolve().is_relative_to(destination):
                raise BootstrapError('归档路径越界')
            key = str(relative)
            if key in seen:
                raise BootstrapError('归档包含重复路径')
            seen.add(key)
            total += member.size
            if member.size < 0 or total > MAX_EXTRACTED:
                raise BootstrapError('归档解包大小超出限制')
            checked.append((member, target))
        # 先完整校验，绝不使用 extractall，也不保留归档所有者或执行权限。
        for member, target in checked:
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise BootstrapError('无法读取归档文件')
                fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with source, os.fdopen(fd, 'wb') as output:
                    shutil.copyfileobj(source, output)
    return destination / root


def main(argv=None):
    parser = argparse.ArgumentParser(description='从公开仓库固定提交安装电报双智能体工作台')
    parser.add_argument('--repo', default=DEFAULT_REPO, help='owner/repository，默认官方仓库')
    parser.add_argument('--ref', default=DEFAULT_REF, help='完整的 40 字符 commit SHA，默认使用内置固定版本')
    parser.add_argument('--download-only', action='store_true', help='仅解包，不执行安装器')
    parser.add_argument('--output', type=Path, help='仅下载时使用，目标目录必须不存在')
    args, installer_args = parser.parse_known_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        raise BootstrapError('仓库名必须是 owner/repository 形式')
    if not re.fullmatch(r'[0-9a-fA-F]{40}', args.ref):
        raise BootstrapError('缺少固定提交；请使用发布页给出的完整 40 字符 commit SHA')
    if args.output and not args.download_only:
        raise BootstrapError('--output 仅用于 --download-only')
    data = fetch_archive(args.repo, args.ref)
    if args.download_only:
        destination = args.output or Path(tempfile.mkdtemp(prefix='telegram-agent-hub-download-')) / 'source'
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
        source = extract_archive(data, destination)
        print('源码已安全解包到：' + str(source))
        return 0
    with tempfile.TemporaryDirectory(prefix='telegram-agent-hub-') as folder:
        os.chmod(folder, 0o700)
        source = extract_archive(data, folder)
        installer = source / 'install.py'
        if not installer.is_file():
            raise BootstrapError('仓库中不存在 install.py')
        command = [sys.executable, str(installer)] + installer_args
        if hasattr(os, 'geteuid') and os.geteuid() != 0:
            command = ['sudo'] + command
        return subprocess.run(command).returncode


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (BootstrapError, OSError, ValueError, tarfile.TarError) as exc:
        print('引导失败：' + (str(exc) if isinstance(exc, BootstrapError) else type(exc).__name__) + '。', file=sys.stderr)
        sys.exit(1)
