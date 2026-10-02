import os
import re
import sys
import tempfile
import unittest
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'bot'))
os.environ.setdefault('DPBOT_ENV', '/nonexistent-dpbot-env')
import bot


class BulkExtensionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = bot.DB_PATH
        bot.DB_PATH = os.path.join(self.tmp.name, 'db.sqlite')
        bot.init_db()
        c = bot.db()
        for token, limit, expiry, frozen, disabled, deleting in (
                ('active', bot.GB, 2000, 0, 0, 0), ('expired', bot.GB*2, 500, 0, 500, 0),
                ('frozen', bot.GB*3, 2000, 1, 0, 0), ('unlimited', 0, 0, 0, 0, 0),
                ('deleting', bot.GB, 2000, 0, 0, 1)):
            c.execute('INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,used_bytes,'
                      'usage_reset_bytes,frozen,disabled_ts,pending_delete) VALUES(?,?,?,?,?,?,0,50,10,?,?,?)',
                      (token, 'secret-'+token, 'u_'+token, token, limit, expiry, frozen, disabled, deleting))
        c.commit(); c.close()
        bot._sessions.clear()
        self.sid, self.csrf = bot.new_session(now=1000)

    def tearDown(self):
        bot.DB_PATH = self.original
        bot._sessions.clear()
        self.tmp.cleanup()

    def users(self):
        c = bot.db(); rows = c.execute('SELECT * FROM users').fetchall(); c.close()
        return {row['token']: dict(row) for row in rows}

    def post(self, path, fields):
        return bot.route_admin('POST', path, {}, 'mj_sess='+self.sid,
                               urllib.parse.urlencode(dict(fields, csrf=self.csrf)).encode(), now=1001)

    def test_volume_preserves_unlimited_identity_usage_and_manual_freeze(self):
        before = self.users()
        operation, rec = bot.prepare_bulk_extension('volume', '0.5', now=1000)
        self.assertEqual(set(rec['tokens']), {'active', 'expired', 'frozen'})
        self.assertEqual(self.users(), before)  # review does not apply
        self.assertEqual(bot.apply_bulk_extension(operation, now=1001), 3)
        after = self.users()
        for token, row in before.items():
            expected = dict(row)
            if token in rec['tokens']: expected['limit_bytes'] += bot.GB//2
            self.assertEqual(after[token], expected)

    def test_time_uses_current_expiry_or_confirmation_time_for_expired_links(self):
        operation, _ = bot.prepare_bulk_extension('time', '0,25', now=1000)
        self.assertEqual(bot.apply_bulk_extension(operation, now=1100), 3)
        rows = self.users()
        self.assertEqual(rows['active']['expiry_ts'], 2000 + 21600)
        self.assertEqual(rows['expired']['expiry_ts'], 1100 + 21600)
        self.assertEqual(rows['unlimited']['expiry_ts'], 0)
        self.assertEqual(rows['frozen']['frozen'], 1)
        self.assertEqual(rows['expired']['disabled_ts'], 500)  # enforcer owns reactivation

    def test_double_or_concurrent_submission_applies_once(self):
        operation, _ = bot.prepare_bulk_extension('volume', 1, now=1000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: bot.apply_bulk_extension(operation, now=1001), range(2)))
        self.assertEqual(results, [3, 3])
        self.assertEqual(self.users()['active']['limit_bytes'], bot.GB*2)

    def test_reviewed_set_excludes_new_deleted_or_now_unlimited_links(self):
        operation, _ = bot.prepare_bulk_extension('volume', 1, now=1000)
        c = bot.db()
        c.execute("DELETE FROM users WHERE token='expired'")
        c.execute("UPDATE users SET limit_bytes=0 WHERE token='frozen'")
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts) "
                  "VALUES('new','secret','u_new','new',?,2000,1001)", (bot.GB,))
        c.commit(); c.close()
        self.assertEqual(bot.apply_bulk_extension(operation, now=1001), 1)
        self.assertEqual(self.users()['new']['limit_bytes'], bot.GB)

    def test_invalid_values_and_expired_review_cannot_modify_users(self):
        before = self.users()
        for kind, amount in [('volume', 'nan'), ('time', 'inf'), ('time', -1), ('volume', 0),
                             ('bad', 1), ('volume', '1e100')]:
            with self.subTest(kind=kind, amount=amount), self.assertRaises(ValueError):
                bot.prepare_bulk_extension(kind, amount, now=1000)
        operation, _ = bot.prepare_bulk_extension('volume', 1, now=1000)
        with self.assertRaises(ValueError): bot.apply_bulk_extension(operation, now=1600)
        self.assertEqual(self.users(), before)

    def test_overflow_rolls_back_the_whole_group(self):
        c = bot.db(); c.execute("UPDATE users SET limit_bytes=? WHERE token='frozen'", (2**63-1,)); c.commit(); c.close()
        before = self.users()
        operation, _ = bot.prepare_bulk_extension('volume', 1, now=1000)
        with self.assertRaises(ValueError): bot.apply_bulk_extension(operation, now=1001)
        self.assertEqual(self.users(), before)

    def test_authenticated_preview_and_confirmation_routes(self):
        before = self.users()
        status, _, page = self.post('/a/bulk-preview', {'kind': 'volume', 'amount': '0.5'})
        self.assertEqual(status, 200)
        self.assertIn('تأیید افزایش برای 3 لینک'.encode(), page)
        operation = re.search(rb'name=operation value=\x27([a-f0-9]+)', page)[1].decode()
        self.assertEqual(self.users(), before)
        self.post('/a/bulk-apply', {'operation': operation})
        self.assertEqual(self.users(), before)
        status, headers, _ = self.post('/a/bulk-apply', {'operation': operation, 'confirm': 'yes'})
        self.assertEqual(status, 302)
        self.assertIn('/a/?msg=', headers['Location'])
        self.assertEqual(self.users()['active']['limit_bytes'], int(1.5*bot.GB))

    def test_csrf_and_login_are_required(self):
        before = self.users()
        status, _, _ = bot.route_admin('POST', '/a/bulk-preview', {}, 'mj_sess='+self.sid,
                                      b'csrf=wrong&kind=volume&amount=1', now=1001)
        self.assertEqual(status, 403)
        status, _, page = bot.route_admin('POST', '/a/bulk-preview', {}, '',
                                         b'kind=volume&amount=1', now=1001)
        self.assertNotIn(b'action=\x27/a/bulk-apply', page)
        self.assertEqual(self.users(), before)


if __name__ == '__main__':
    unittest.main()
