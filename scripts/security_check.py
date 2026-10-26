#!/usr/bin/env python3
"""提交前扫描源码；只报告文件和规则，不回显命中内容。"""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
RULES = {
    '真实模型密钥': r'\bsk-[A-Za-z0-9_-]{24,}',
    '代码托管令牌': r'\bgh[pousr]_[A-Za-z0-9]{20,}',
    '细粒度托管令牌': r'github_pat_[A-Za-z0-9_]{30,}',
    '机器人令牌': r'\b\d{8,12}:AA[A-Za-z0-9_-]{25,}',
    '云访问凭据': r'\bAKIA[A-Z0-9]{16}\b',
    '私钥正文': r'-----BEGIN (?:OPENSSH|RSA|EC|DSA|ENCRYPTED) PRIVATE KEY-----',
}

# 明确是假值的占位符。测试夹具必须用这类值，否则测试本身就成了泄漏源。
# 只在这份白名单里放「官方文档示例」和「明显构造」两类，不能放真实前缀的真令牌。
FAKE_VALUES = (
    'AKIAIOSFODNN7EXAMPLE',            # AWS 官方文档里的示例值
    'ghp_abcdefghijklmnopqrstuvwxyz01',  # 顺序字母，明显构造
)


def is_fake(hit):
    """命中的片段是否整段属于已知假值。"""
    return any(hit == fake or fake.startswith(hit) or hit.startswith(fake) for fake in FAKE_VALUES)


def has_private_key_body(text, match):
    """私钥是否真有正文。

    真实私钥的标题行后面紧跟一行 base64（长度通常 64，且字符集是 base64）。
    测试夹具常写成 '-----BEGIN RSA PRIVATE KEY-----\\nx' —— 只有标题没有正文，
    这种不该算泄漏，否则扫描器会被测试自己逼着放弃这条规则。
    """
    rest = text[match.end():]
    for line in rest.splitlines()[:3]:
        line = line.strip()
        if not line:
            continue
        if len(line) >= 32 and re.fullmatch(r'[A-Za-z0-9+/=]+', line):
            return True
        break
    return False


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
            for match in re.finditer(regex, text):
                if is_fake(match.group(0)):
                    continue
                if label == '私钥正文' and not has_private_key_body(text, match):
                    continue
                failures.append((str(path.relative_to(root)), label))
                break
    return failures

if __name__ == '__main__':
    failures = scan(ROOT)
    for filename, label in failures:
        print(filename + '：' + label)
    print('敏感扫描：' + ('失败' if failures else '通过'))
    sys.exit(bool(failures))
