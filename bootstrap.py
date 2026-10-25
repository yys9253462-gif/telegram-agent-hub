#!/usr/bin/env python3
"""从 GitHub 私有仓库固定提交下载源码，安全解包后执行安装器。"""
import argparse
import getpass
import io
import json
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
DEFAULT_REPO = ''
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
        # 新请求从零创建，不复制 Authorization、Cookie 或其他身份信息。
        return urllib.request.Request(newurl, headers={'User-Agent': 'telegram-agent-hub-bootstrap'})


def github_token():
    supplied = os.environ.pop('AGENT_GITHUB_TOKEN', '')
    if supplied:
        return supplied
    if shutil.which('gh'):
        result = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com'], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    token = getpass.getpass('GitHub 私有仓库只读 PAT（只在内存使用，不保存）: ').strip()
    if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise BootstrapError('GitHub token 无效')
    return token


def fetch_archive(repo, commit, token):
    url = 'https://api.github.com/repos/' + repo + '/tarball/' + commit
    request = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'telegram-agent-hub-bootstrap',
    })
    with urllib.request.build_opener(SafeRedirect()).open(request, timeout=60) as response:
        body = response.read(MAX_ARCHIVE + 1)
    if len(body) > MAX_ARCHIVE:
        raise BootstrapError('仓库归档超过大小限制')
    return body


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
            if str(relative) in seen:
                raise BootstrapError('归档包含重复路径')
            seen.add(str(relative))
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=DEFAULT_REPO, help='owner/repository')
    parser.add_argument('--ref', default=DEFAULT_REF, help='完整的 40 字符 commit SHA')
    parser.add_argument('--download-only', action='store_true', help='仅解包，不执行安装器')
    parser.add_argument('--output', type=Path, help='仅下载时使用，目标目录必须不存在')
    args, installer_args = parser.parse_known_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        raise BootstrapError('请通过 --repo 指定真实仓库 owner/repository')
    if not re.fullmatch(r'[0-9a-fA-F]{40}', args.ref):
        raise BootstrapError('--ref 必须是审阅过的完整 40 字符 commit SHA')
    if args.output and not args.download_only:
        raise BootstrapError('--output 仅用于 --download-only')
    token = github_token()
    data = fetch_archive(args.repo, args.ref, token)
    token = None
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
        print('引导失败：' + (str(exc) if isinstance(exc, BootstrapError) else type(exc).__name__) + '。认证信息不会写入文件或日志。', file=sys.stderr)
        sys.exit(1)
