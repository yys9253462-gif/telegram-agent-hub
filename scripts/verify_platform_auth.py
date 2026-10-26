"""反向验证：把每处保护故意改坏，确认 test_platform_auth.py 真的报红。

永远绿的测试等于没有测试。这个脚本对每个变体单独还原，
避免变体之间互相掩盖。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / 'app'
TARGET = ROOT / 'credentials.py'
BOT = ROOT / 'bot.py'
TEXT = ROOT / 'textfmt.py'
CF = ROOT / 'cloudflare_auth.py'

# (说明, 文件, 原文, 替换为)
VARIANTS = [
    ('凭据文件最终权限不再收紧到 0600', TARGET,
     'os.chmod(self.path, 0o600)', 'os.chmod(self.path, 0o644)'),
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
     "            DEVICE_FLOW.update({'provider': '', 'device_code': '', 'user_code': ''})\n        else:",
     "            pass\n        else:"),
    ('等待令牌时不再校验形态', BOT,
     "    candidate = looks_like('cloudflare', text)\n    if not candidate:",
     "    candidate = True\n    if not candidate:"),
    ('脱敏去掉 cfut_ 规则', TEXT,
     "    value = re.sub(r'\\bcfut_[A-Za-z0-9_-]{20,}', '[已隐藏令牌]', value)\n", ''),
    ('设备码重复发起不再复用', BOT,
     "    if running is not None and running.is_alive():", "    if False:"),
    # ── Cloudflare 设备码相关 ──
    # 这几处都属于「改坏了不报错、只是功能静默失效」的类型，必须有反向验证兜着。
    ('dash 请求丢掉 wrangler 的 UA', CF,
     "        'Accept': ACCEPT, 'Content-Type': 'application/x-www-form-urlencoded',\n        'User-Agent': CLIENT_UA})",
     "        'Accept': ACCEPT, 'Content-Type': 'application/x-www-form-urlencoded'})"),
    # ★ 最致命的一处：Cloudflare 用 HTTP 400 返回 authorization_pending。
    # 把 4xx 一律当异常，轮询每一轮都是「失败」，设备码流程永远走不完。
    ('400 响应体不再解析（轮询必然失败）', CF,
     "        if isinstance(body, dict) and body.get('error'):\n            return body\n",
     ""),
    ('令牌自省略去 offline_access', CF,
     "DEFAULT_SCOPE = 'offline_access account:read user:read zone:read workers_tail:read'",
     "DEFAULT_SCOPE = 'account:read user:read zone:read workers_tail:read'"),
    ('slow_down 仍按原间隔猛轮', CF,
     "        if error == 'slow_down':\n            # 规范要求：收到 slow_down 在原间隔上加 5 秒。\n            wait += 5\n            continue",
     "        if error == 'slow_down':\n            continue"),
    ('用令牌自检接口校验 OAuth 令牌', CF,
     "    request = urllib.request.Request(API_BASE + '/user', headers={",
     "    request = urllib.request.Request(VERIFY_URL, headers={"),
    ('403 提示不再给出粘贴退路', CF,
     "'要么该 client_id 不被接受。请稍后重试，或改用「粘贴API令牌」。'",
     "'要么该 client_id 不被接受。请稍后重试。'"),
    ('改回按 GitHub 的十五分钟提示', BOT,
     "            '这个码只有 ' + str(minutes) + ' 分钟有效（比 GitHub 短得多），'",
     "            '这个码 ' + str(minutes) + ' 分钟内有效，'"),
    ('设备码消息丢掉自带码的主链接', BOT,
     "    complete = flow.get('verification_uri_complete') or flow.get('verification_uri') or ''",
     "    complete = flow.get('verification_uri') or ''"),
    ('撤销 Cloudflare 时不清设备码', BOT,
     "            CLOUDFLARE_FLOW.update({'thread': None, 'device_code': '', 'user_code': ''})\n        send(chat,",
     "            pass\n        send(chat,"),
    ('Cloudflare 设备码复用两个平台共用状态', BOT,
     "    running = CLOUDFLARE_FLOW.get('thread')\n    if running is not None and running.is_alive():\n        remain",
     "    running = DEVICE_FLOW.get('thread')\n    if running is not None and running.is_alive():\n        remain"),
    ('凭据形态不再接受 OAuth 令牌', TARGET,
     "|[A-Za-z0-9_-]{40}|[A-Za-z0-9._~+/-]{20,})$'",
     "|[A-Za-z0-9_-]{40})$'"),
]


def run_tests():
    """跑测试并返回 (是否通过, 摘要)。

    unittest 的 verbosity 输出走标准错误，摘要要从 stderr 取；
    只看 stdout 会永远拿到「无输出」，把判断依据悄悄丢掉。
    同时清缓存，避免读到旧的 .pyc（这个坑已经踩过一次）。
    """
    shutil.rmtree(ROOT / '__pycache__', ignore_errors=True)
    result = subprocess.run([sys.executable, str(REPO / 'tests' / 'test_platform_auth.py')],
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
