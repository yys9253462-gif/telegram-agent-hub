import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _root  # noqa: F401
import updater


class UpdaterTokenTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        updater.GITHUB_TOKEN['value'] = None
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for name in ('GITHUB_TOKEN', 'GH_TOKEN'):
            os.environ.pop(name, None)

    def tearDown(self):
        self.env.stop()
        updater.GITHUB_TOKEN['value'] = None

    def write(self, record):
        (self.work / 'credentials.json').write_text(json.dumps({'github': record}))

    def test_reads_bot_credentials(self):
        self.write({'value': 'gho_test', 'expires_at': time.time() + 3600})
        self.assertEqual(updater.github_token(str(self.work)), 'gho_test')

    def test_expired_or_missing(self):
        self.write({'value': 'gho_old', 'expires_at': time.time() - 10})
        self.assertEqual(updater.github_token(str(self.work)), '')
        self.assertEqual(updater.github_token(str(self.work / 'nope')), '')

    def test_env_wins(self):
        self.write({'value': 'gho_file'})
        os.environ['GH_TOKEN'] = 'gho_env'
        self.assertEqual(updater.github_token(str(self.work)), 'gho_env')

    def test_header_only_for_api_and_falls_back(self):
        updater.GITHUB_TOKEN['value'] = 'gho_x'
        seen = []

        def fake_open(request, limit):
            seen.append(request.get_header('Authorization'))
            if request.get_header('Authorization'):
                raise updater.UpdateError('401')
            return b'ok'
        with mock.patch.object(updater, '_open', fake_open):
            self.assertEqual(updater._fetch('https://api.github.com/x', 10, 'a'), b'ok')
            self.assertEqual(seen, ['Bearer gho_x', None])
            seen.clear()
            updater._fetch('https://codeload.github.com/x', 10, 'a')
            self.assertEqual(seen, [None])


if __name__ == '__main__':
    unittest.main()
