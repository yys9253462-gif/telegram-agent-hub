"""反向验证：把每处保护故意改坏，确认 test_platform_auth.py 真的报红。

永远绿的测试等于没有测试。这个脚本对每个变体单独还原，
避免变体之间互相掩盖。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / 'credentials.py'
BOT = ROOT / 'bot.py'

# (说明, 文件, 原文, 替换为)
VARIANTS = [
    ('凭据文件去掉 0600 建文件参数', TARGET,
     'os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600', 'os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644'),
    ('摘要里改为回显令牌原文', TARGET,
     "'fingerprint': record.get('fingerprint') or fingerprint(record['value']),",
     "'fingerprint': record['value'],"),
    ('损坏文件改为直接抛异常', TARGET,
     "        except (OSError, ValueError):\n            # 文件缺失或损坏都当成「什么都没授权」。损坏时不能抛，\n            # 否则每次查询授权状态都会让机器人操作失败。\n            return {}",
     "        except (OSError, ValueError):\n            raise"),
    ('未授权时不再剔除继承变量', BOT,
     "        if spec['env'] not in overrides:\n            environment.pop(spec['env'], None)",
     "        if False:\n            environment.pop(spec['env'], None)"),
    ('撤销时不清设备码等待态', BOT,
     "                DEVICE_FLOW.update({'provider': '', 'device_code': '', 'user_code': ''})\n            send(chat, (PROVIDERS[provider]['label'] + ' 授权已撤销",
     "                pass\n            send(chat, (PROVIDERS[provider]['label'] + ' 授权已撤销"),
    ('等待令牌时不再校验形态', BOT,
     "    candidate = looks_like('cloudflare', text)\n    if not candidate:",
     "    candidate = True\n    if not candidate:"),
    ('脱敏去掉 cfut_ 规则', BOT,
     "    value = re.sub(r'\\bcfut_[A-Za-z0-9_-]{20,}', '[已隐藏令牌]', value)\n", ''),
    ('设备码重复发起不再复用', BOT,
     "    if running is not None and running.is_alive():", "    if False:"),
]


def run_tests():
    """跑测试并返回 (是否通过, 摘要)。

    unittest 的 verbosity 输出走标准错误，摘要要从 stderr 取；
    只看 stdout 会永远拿到「无输出」，把判断依据悄悄丢掉。
    同时清缓存，避免读到旧的 .pyc（这个坑已经踩过一次）。
    """
    shutil.rmtree(ROOT / '__pycache__', ignore_errors=True)
    result = subprocess.run([sys.executable, str(ROOT / 'test_platform_auth.py')],
                            capture_output=True, text=True,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    combined = ((result.stdout or '') + (result.stderr or '')).strip().splitlines()
    tail = next((line for line in reversed(combined) if line.startswith(('OK', 'FAILED'))), '无结论行')
    return result.returncode == 0, tail


def read_source(path):
    """按字节读取。

    不能用 read_text/write_text：Windows 上 write_text 会把 \\n 转成 \\r\\n，
    于是这个脚本每跑一次就把 bot.py 改成 CRLF，安全扫描下一轮就报「非统一换行」。
    这个坑真实发生过一次，所以这里坚持二进制读写，保证行尾字节原样往返。
    """
    return path.read_bytes()


def write_source(path, data):
    path.write_bytes(data)


def main():
    backups = {}
    failures = []
    try:
        for _, path, _, _ in VARIANTS:
            if path not in backups:
                backups[path] = read_source(path)
        for description, path, old, new in VARIANTS:
            original = backups[path]
            old_bytes = old.encode('utf-8')
            new_bytes = new.encode('utf-8')
            if old_bytes not in original:
                print('[跳过] ' + description + '：锚点未找到，测试脚本需同步更新')
                failures.append(description + '（锚点失效）')
                continue
            write_source(path, original.replace(old_bytes, new_bytes, 1))
            try:
                passed, summary = run_tests()
            finally:
                write_source(path, original)
            status = '[无效] 仍然全绿' if passed else '[有效] 已报红'
            print(status + ' → ' + description + '  ' + summary)
            if passed:
                failures.append(description)
    finally:
        for path, content in backups.items():
            write_source(path, content)
        shutil.rmtree(ROOT / '__pycache__', ignore_errors=True)
    print()
    if failures:
        print('有 ' + str(len(failures)) + ' 个变体没被抓到：' + '；'.join(failures))
        return 1
    print('全部 ' + str(len(VARIANTS)) + ' 个变体均正确报红，测试有效。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
