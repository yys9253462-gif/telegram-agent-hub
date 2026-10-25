#!/usr/bin/env python3
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent

def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj

class InstallerTests(unittest.TestCase):
    def test_no_shell_interpretation(self):
        m = module('install')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'config.json'
            path.write_text('{"note": "$(touch /tmp/SHOULD_NOT_EXIST)"}')
            self.assertEqual(m.read_json(path)['note'], '$(touch /tmp/SHOULD_NOT_EXIST)')
            with self.assertRaises(m.InstallError):
                m.validate_url('http://example.com')
            with self.assertRaises(m.InstallError):
                m.env_quote('a\nb')

    def test_secret_environment(self):
        m = module('install')
        self.assertEqual(m.env_quote('x"y\\z'), '"x\\"y\\\\z"')
        self.assertTrue(m.INSTALLER_SHA256.get('claude'))
        self.assertTrue(m.INSTALLER_SHA256.get('codebuddy'))

    def test_archive_rejects_escape_and_links(self):
        m = module('bootstrap')
        for name, link in [('../escape', False), ('root/link', True)]:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w:gz') as t:
                item = tarfile.TarInfo(name)
                if link:
                    item.type = tarfile.SYMTYPE
                    item.linkname = '/etc/passwd'
                t.addfile(item)
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(m.BootstrapError):
                    m.extract_archive(buf.getvalue(), d)

    def test_redirect_drops_credentials(self):
        m = module('bootstrap')
        import urllib.request
        req = urllib.request.Request('https://api.github.com/repos/a/b/tarball/abc', headers={'Authorization':'Bearer FAKE_TEST_ONLY'})
        redir = m.SafeRedirect().redirect_request(req, None, 302, '', {}, 'https://codeload.github.com/a/b/legacy.tar.gz/abc')
        self.assertIsNone(redir.get_header('Authorization'))
        with self.assertRaises(m.BootstrapError):
            m.SafeRedirect().redirect_request(req, None, 302, '', {}, 'https://example.com/file')

if __name__ == '__main__':
    unittest.main()
