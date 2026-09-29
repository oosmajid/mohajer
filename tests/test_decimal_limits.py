import os, sys, tempfile, time, unittest, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402


class DecimalLimitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._db, self._add, self._sub = bot.DB_PATH, bot.xr_add_user, bot.write_sub
        bot.DB_PATH = self.tmp.name
        bot.init_db()
        bot.xr_add_user = lambda *args, **kwargs: True
        bot.write_sub = lambda *args, **kwargs: None
        bot._sessions.clear()
        self.sid, self.csrf = bot.new_session(now=1000)
        self.cookie = "mj_sess=" + self.sid

    def tearDown(self):
        bot.DB_PATH, bot.xr_add_user, bot.write_sub = self._db, self._add, self._sub
        bot._sessions.clear()
        os.unlink(self.tmp.name)

    def test_create_and_extend_keep_fractional_days_and_gigabytes(self):
        start = time.time()
        token = bot.create_user(0.5, 0.25, label="Fraction")
        c = bot.db(); user = c.execute("SELECT limit_bytes,expiry_ts FROM users WHERE token=?", (token,)).fetchone(); c.close()
        self.assertEqual(user["limit_bytes"], bot.GB // 2)
        self.assertAlmostEqual(user["expiry_ts"] - start, 6 * 3600, delta=2)
        bot.extend_volume(token, 0.25)
        bot.extend_time(token, 0.5)
        c = bot.db(); user = c.execute("SELECT limit_bytes,expiry_ts FROM users WHERE token=?", (token,)).fetchone(); c.close()
        self.assertEqual(user["limit_bytes"], int(0.75 * bot.GB))
        self.assertAlmostEqual(user["expiry_ts"] - start, 18 * 3600, delta=2)

    def test_web_forms_accept_decimal_values(self):
        self.assertIn("name=days step=any", bot.render_new(self.csrf))
        self.assertIn("name=gb step=any", bot.render_new(self.csrf))
        body = urllib.parse.urlencode({"csrf": self.csrf, "gb": "1.25", "days": "0.5",
                                       "name": "Web decimal"}).encode()
        st, _, _ = bot.route_admin("POST", "/a/new", {}, self.cookie, body, now=1001)
        self.assertEqual(st, 302)
        c = bot.db(); user = c.execute("SELECT token,limit_bytes,expiry_ts FROM users WHERE label='Web decimal'").fetchone(); c.close()
        self.assertEqual(user["limit_bytes"], int(1.25 * bot.GB))
        self.assertAlmostEqual(user["expiry_ts"] - time.time(), 12 * 3600, delta=2)


if __name__ == "__main__":
    unittest.main()
