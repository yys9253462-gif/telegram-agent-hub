import _root  # noqa: F401  仓库根目录进 sys.path
import base64
import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import updater

OLD = '1' * 40
NEW = '2' * 40


def archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as output:
        for name, content in files.items():
            data = content.encode('utf-8')
            member = tarfile.TarInfo(name)
            member.size = len(data)
            output.addfile(member, io.BytesIO(data))
    return stream.getvalue()


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Path(self.temp.name).resolve()
        self.work = self.app / 'work'
        self.work.mkdir()
        self.config = {'app_dir': str(self.app), 'work_dir': str(self.work),
                       'python': str(self.app / 'python3'), 'service': 'telegram-agent-hub.service'}
        (self.app / 'update-install.json').write_text(json.dumps(self.config), encoding='utf-8')
        for name in updater.REQUIRED_FILES:
            (self.app / name).write_text("VERSION = 'old'\n", encoding='utf-8')
        updater.create_manifest(self.app, OLD, self.config['service'])
        self.new_files = {name: "VERSION = 'new'\n" for name in updater.REQUIRED_FILES}
        self.new_files['image_tool.py'] = "VERSION = 'new'\n"
        self.data = archive({'repo/' + name: body for name, body in self.new_files.items()})
        self.latest = {'commit': NEW, 'sha256': updater.digest(self.data)}
        self.calls = []
        for target, value in (
            ('_app_path', lambda app: Path(app)), ('_trusted', lambda *a, **k: None),
            ('latest_version', lambda: self.latest), ('_fetch', lambda *a: self.data),
            ('_lock', lambda *a: nullcontext()), ('_service', self.service),
        ):
            active = patch.object(updater, target, side_effect=value)
            active.start()
            self.addCleanup(active.stop)
        active = patch.object(updater.os, 'geteuid', return_value=0, create=True)
        active.start()
        self.addCleanup(active.stop)
        active = patch.object(updater.time, 'sleep')
        active.start()
        self.addCleanup(active.stop)

    def service(self, action, service):
        self.calls.append(action)
        return 'MainPID=123\nNRestarts=0' if action == 'show' else 'active'

    def busy(self):
        (self.work / 'bridge-state.json').write_text(json.dumps({'tasks': [{'status': 'running'}]}), encoding='utf-8')

    def test_check_and_local_modification(self):
        result = updater.check(self.app)
        self.assertTrue(result['update_available'])
        self.assertEqual(result['current'], OLD)
        self.assertEqual(result['local_modified'], [])
        (self.app / 'bot.py').write_text('modified = True', encoding='utf-8')
        self.assertEqual(updater.check(self.app)['local_modified'], ['bot.py'])
        self.assertFalse(updater.apply(self.app)['success'])
        self.assertNotIn('stop', self.calls)

    def test_success_keeps_configuration_and_backup(self):
        config = self.app / 'bot.env'
        config.write_bytes(b'SECRET=keep\n')
        original = (self.app / 'bot.py').read_bytes()
        result = updater.apply(self.app)
        self.assertTrue(result['success'])
        self.assertEqual(config.read_bytes(), b'SECRET=keep\n')
        self.assertEqual(json.loads((self.app / 'installed-version.json').read_text())['commit'], NEW)
        backups = list((self.app / 'updates-backup').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'bot.py').read_bytes(), original)
        self.assertFalse((backups[0] / 'bot.env').exists())
        self.assertEqual(set(p.name for p in backups[0].iterdir()), updater.REQUIRED_FILES | {'installed-version.json'})
        self.assertIn('is-active', self.calls)

    def test_app_directory_layout_installs_flat(self):
        """程序文件挪到仓库 app/ 后，归档里是 repo/app/bot.py；装到机器上仍是平铺的。"""
        self.new_files['usage.py'] = "VERSION = 'new'\n"
        self.data = archive({'repo/app/' + name: body for name, body in self.new_files.items()})
        self.latest = {'commit': NEW, 'sha256': updater.digest(self.data)}
        result = updater.apply(self.app)
        self.assertTrue(result['success'], result)
        self.assertEqual((self.app / 'bot.py').read_text(), "VERSION = 'new'\n")
        self.assertEqual((self.app / 'usage.py').read_text(), "VERSION = 'new'\n")
        self.assertFalse((self.app / 'app').exists())

    def test_failed_health_restores_all_code(self):
        original = (self.app / 'bot.py').read_bytes()
        metadata = (self.app / 'installed-version.json').read_bytes()
        with patch.object(updater, '_healthy', side_effect=updater.UpdateError('启动失败')):
            result = updater.apply(self.app)
        self.assertFalse(result['success'])
        self.assertIn('已恢复', result['message'])
        self.assertEqual((self.app / 'bot.py').read_bytes(), original)
        self.assertEqual((self.app / 'installed-version.json').read_bytes(), metadata)
        self.assertEqual((self.app / 'image_tool.py').read_text(encoding='utf-8'), "VERSION = 'old'\n")
        self.assertEqual(self.calls.count('start'), 2)

    def test_incomplete_rollback_never_starts_mixed_version(self):
        original_atomic = updater._atomic
        original_metadata = (self.app / 'installed-version.json').read_bytes()
        attempts = []
        rollback = False

        def fail_health(service):
            nonlocal rollback
            rollback = True
            raise updater.UpdateError('启动失败')

        def atomic(path, content, mode=0o644):
            if rollback and path.parent == self.app:
                attempts.append(path.name)
                if path.name == 'bot.py':
                    raise OSError('恢复写入失败')
            return original_atomic(path, content, mode)

        with patch.object(updater, '_healthy', side_effect=fail_health), patch.object(updater, '_atomic', side_effect=atomic):
            result = updater.apply(self.app)
        self.assertFalse(result['success'])
        self.assertEqual(self.calls.count('start'), 1)
        self.assertEqual(self.calls[-1], 'stop')
        self.assertIn('人工恢复', result['message'])
        self.assertIn(result['backup'], result['message'])
        self.assertTrue(updater.REQUIRED_FILES.issubset(attempts))
        self.assertIn('installed-version.json', attempts)
        self.assertEqual((self.app / 'installed-version.json').read_bytes(), original_metadata)
        self.assertEqual((self.app / 'image_tool.py').read_text(encoding='utf-8'), "VERSION = 'old'\n")

    def test_hash_mismatch_never_stops(self):
        self.latest['sha256'] = '0' * 64
        result = updater.apply(self.app)
        self.assertFalse(result['success'])
        self.assertIn('SHA256', result['message'])
        self.assertNotIn('stop', self.calls)

    def test_unsafe_archive_never_stops(self):
        self.data = archive({'repo/../../outside.py': 'evil = True'})
        self.latest['sha256'] = updater.digest(self.data)
        result = updater.apply(self.app)
        self.assertFalse(result['success'])
        self.assertIn('不安全', result['message'])
        self.assertNotIn('stop', self.calls)
        self.assertFalse((self.app.parent / 'outside.py').exists())

    def test_missing_runtime_module_refused_before_stop(self):
        self.data = archive({'repo/' + name: body for name, body in self.new_files.items() if name != 'credentials.py'})
        self.latest['sha256'] = updater.digest(self.data)
        result = updater.apply(self.app)
        self.assertFalse(result['success'])
        self.assertIn('缺少运行必需代码', result['message'])
        self.assertNotIn('stop', self.calls)

    def test_busy_before_stop(self):
        self.busy()
        self.assertFalse(updater.apply(self.app)['success'])
        self.assertNotIn('stop', self.calls)

    def test_busy_after_stop_starts_old_service(self):
        def service(action, name):
            if action == 'stop':
                self.busy()
            return self.service(action, name)
        with patch.object(updater, '_service', side_effect=service):
            result = updater.apply(self.app)
        self.assertFalse(result['success'])
        self.assertEqual(self.calls, ['stop', 'start', 'is-active'])
        self.assertEqual(json.loads((self.app / 'installed-version.json').read_text())['commit'], OLD)

    def test_remote_archive_configuration_ignored(self):
        contents = {'repo/' + name: body for name, body in self.new_files.items()}
        contents.update({'repo/bot.env': 'SECRET=changed', 'repo/work/credentials.json': '{}',
                         'repo/test_fake.py': 'raise RuntimeError()', 'repo/security_check.py': 'raise RuntimeError()'})
        self.data = archive(contents)
        self.latest['sha256'] = updater.digest(self.data)
        self.assertTrue(updater.apply(self.app)['success'])
        for name in ('bot.env', 'test_fake.py', 'security_check.py'):
            self.assertFalse((self.app / name).exists())
        self.assertFalse((self.work / 'credentials.json').exists())

    def test_missing_manifest_refused(self):
        (self.app / 'installed-version.json').unlink()
        self.assertFalse(updater.apply(self.app)['success'])
        self.assertNotIn('stop', self.calls)

    def test_unknown_installed_commit_can_upgrade(self):
        updater.create_manifest(self.app, '', self.config['service'])
        self.assertEqual(updater.check(self.app)['current'], '')
        result = updater.apply(self.app)
        self.assertTrue(result['success'])
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['commit'], NEW)
        self.assertTrue(Path(result['backup']).is_dir())
        self.assertEqual(updater.load_install(self.app), self.config)

    def test_invalid_service_refused(self):
        self.config['service'] = '--bad.service'
        (self.app / 'update-install.json').write_text(json.dumps(self.config), encoding='utf-8')
        with self.assertRaises(updater.UpdateError):
            updater.apply(self.app)


class BoundaryTests(unittest.TestCase):
    def test_version_raw_and_base64_and_strict_parsing(self):
        text = OLD + '\nsha256:' + 'a' * 64 + '\n'
        encoded = json.dumps({'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}).encode()
        for body in (text.encode(), encoded):
            with patch.object(updater, '_fetch', return_value=body):
                self.assertEqual(updater.latest_version(), {'commit': OLD, 'sha256': 'a' * 64})
        for body in (b'main', (text + 'other').encode(), b'{}', b'{"encoding":"base64","content":"!"}'):
            with patch.object(updater, '_fetch', return_value=body):
                with self.assertRaises(updater.UpdateError):
                    updater.latest_version()

    def test_metadata_permissions_and_owner(self):
        # 保留真实校验函数，以便在 Windows 上也验证 Linux mode/uid 规则。
        for mode, uid in ((stat.S_IFREG | 0o666, 0), (stat.S_IFREG | 0o644, 1000), (stat.S_IFLNK | 0o644, 0)):
            path = unittest.mock.Mock()
            path.lstat.return_value = SimpleNamespace(st_mode=mode, st_uid=uid)
            with self.assertRaises(updater.UpdateError):
                updater._trusted(path)
        path = unittest.mock.Mock()
        path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0)
        updater._trusted(path)

    def test_fetch_limits_and_anonymous_fixed_endpoint(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'x' * 12
        opener = unittest.mock.Mock()
        opener.open.return_value = response
        with patch.object(updater.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(updater.UpdateError):
                updater._fetch('https://api.github.com/repos/' + updater.REPO + '/contents/version.txt', 10, 'application/vnd.github.raw+json')
        request = opener.open.call_args.args[0]
        self.assertFalse(request.has_header('Authorization'))
        self.assertEqual(response.__enter__.return_value.read.call_args.args, (11,))

    def test_health_rejects_restarting_process(self):
        with patch.object(updater, '_service', side_effect=['active', 'MainPID=123\nNRestarts=0', 'active', 'MainPID=456\nNRestarts=1']), patch.object(updater.time, 'sleep'):
            with self.assertRaises(updater.UpdateError):
                updater._healthy('telegram-agent-hub.service')


if __name__ == '__main__':
    unittest.main()
