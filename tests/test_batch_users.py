import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'bot'))
os.environ.setdefault('DPBOT_ENV', '/nonexistent-dpbot-env')
import bot


class BatchUsersTest(unittest.TestCase):
    def test_one_command_keeps_protocol_credentials_and_cleans_tempfile(self):
        eps = [{'tag': proto, 'port': 10000+i, 'proto': proto, 'net': 'ws', 'path': '/'+proto}
               for i, proto in enumerate(('vless', 'vmess', 'trojan'))]
        inputs = [bot._user_inbound(ep, 'secret-'+ep['tag'], 'u_probe.'+ep['tag']) for ep in eps]
        filenames = []
        def run(cmd, **kwargs):
            self.assertEqual(cmd[1:3], ['api', 'adu'])
            filenames.append(cmd[-1])
            with open(cmd[-1]) as f: cfg = json.load(f)
            self.assertEqual(cfg['inbounds'], inputs)
            output = ''.join('add user: u_probe.%s\nresult: ok\n' % ep['tag'] for ep in eps)
            return subprocess.CompletedProcess(cmd, 0, output, '')
        with mock.patch.object(bot.subprocess, 'run', side_effect=run) as command:
            self.assertTrue(bot._adu_inbounds(inputs))
        self.assertEqual(command.call_count, 1)
        self.assertFalse(os.path.exists(filenames[0]))

    def test_failed_or_missing_result_is_failure_even_with_zero_exit(self):
        ep = {'tag':'vless', 'port':10000, 'proto':'vless', 'net':'ws', 'path':'/v'}
        ib = bot._user_inbound(ep, 'secret', 'u_probe.vless')
        for output in ('add user: u_probe.vless\nrpc error: unavailable\n',
                       'add user: u_probe.vless\ninbound does not exists\n',
                       'Added 0 user(s) in total.\n'):
            with self.subTest(output=output), mock.patch.object(bot.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, output, '')):
                self.assertFalse(bot._adu_inbounds([ib]))

    def test_existing_user_is_idempotent(self):
        ep = {'tag':'vless', 'port':10000, 'proto':'vless', 'net':'ws', 'path':'/v'}
        ib = bot._user_inbound(ep, 'secret', 'u_probe.vless')
        with mock.patch.object(bot.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, 'add user: u_probe.vless\nrpc error: user already exists\n', '')):
            self.assertTrue(bot._adu_inbounds([ib]))

    def test_empty_batch_does_not_launch_xray(self):
        with mock.patch.object(bot.subprocess, 'run') as command:
            self.assertTrue(bot._adu_inbounds([]))
        command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
