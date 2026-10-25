#!/usr/bin/env python3
"""提交前扫描源码；只报告文件和规则，不回显命中内容。"""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
RULES = {
    '真实模型密钥': r'\bsk-[A-Za-z0-9_-]{24,}',
    '代码托管令牌': r'\bgh[pousr]_[A-Za-z0-9]{20,}',
    '细粒度托管令牌': r'github_pat_[A-Za-z0-9_]{30,}',
    '机器人令牌': r'\b\d{8,12}:AA[A-Za-z0-9_-]{25,}',
    '云访问凭据': r'\bAKIA[A-Z0-9]{16}\b',
    '私钥正文': r'-----BEGIN (?:OPENSSH|RSA|EC|DSA|ENCRYPTED) PRIVATE KEY-----',
}

def scan(root):
    failures = []
    for path in root.rglob('*'):
        if not path.is_file() or any(x in {'.git', '__pycache__', '.venv'} for x in path.parts):
            continue
        if path.suffix in {'.gz', '.zip', '.png', '.jpg'}:
            failures.append((str(path.relative_to(root)), '不允许归档或图片产物'))
            continue
        raw = path.read_bytes()
        if b'\r\n' in raw:
            failures.append((str(path.relative_to(root)), '非统一换行'))
        text = raw.decode('utf-8', errors='ignore')
        for label, regex in RULES.items():
            if re.search(regex, text):
                failures.append((str(path.relative_to(root)), label))
    return failures

if __name__ == '__main__':
    failures = scan(ROOT)
    for filename, label in failures:
        print(filename + '：' + label)
    print('敏感扫描：' + ('失败' if failures else '通过'))
    sys.exit(bool(failures))
