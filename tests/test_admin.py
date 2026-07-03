import os, sys, tempfile, unittest, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402


class TestAuth(unittest.TestCase):
    def setUp(self):
        bot._login_tokens.clear(); bot._sessions.clear()

    def test_login_token_single_use(self):
        tok = bot.mint_login(now=1000)
        self.assertTrue(bot.consume_login(tok, now=1001))
        self.assertFalse(bot.consume_login(tok, now=1002))  # already used

    def test_login_token_expires(self):
        tok = bot.mint_login(now=1000)
        self.assertFalse(bot.consume_login(tok, now=1000 + bot.LOGIN_TTL + 1))

    def test_session_and_csrf(self):
        sid, csrf = bot.new_session(now=1000)
        self.assertEqual(bot.session_csrf(sid, now=1001), csrf)
        self.assertIsNone(bot.session_csrf(sid, now=1000 + bot.SESS_TTL + 1))
        self.assertIsNone(bot.session_csrf("bogus", now=1001))

    def test_cookie_sid(self):
        self.assertEqual(bot.cookie_sid("mj_sess=abc; other=1"), "abc")
        self.assertIsNone(bot.cookie_sid(""))
        self.assertIsNone(bot.cookie_sid("other=1"))


class TestStats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close()
        self._orig = bot.DB_PATH; bot.DB_PATH = self.tmp.name; bot.init_db()
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,base_bytes,last_raw,used_bytes,disabled_ts) "
                  "VALUES('t1','u','u_t1','One',1000,0,0,0,0,300,0)")
        day = bot.day_key()
        c.execute("INSERT INTO usage_daily(token,day,start_used,end_used) VALUES('t1',?,100,300)", (day,))
        c.commit(); c.close()

    def tearDown(self):
        bot.DB_PATH = self._orig; os.unlink(self.tmp.name)

    def test_daily_series_length_and_today(self):
        ser = bot.daily_series(days=7)
        self.assertEqual(len(ser), 7)
        self.assertEqual(ser[-1][0], bot.day_key())   # last entry is today
        self.assertEqual(ser[-1][1], 200)             # 300-100
        self.assertEqual(ser[0][1], 0)                # older day filled 0

    def test_users_overview(self):
        ov = bot.users_overview()
        self.assertEqual(len(ov), 1)
        self.assertEqual(ov[0]["today"], 200)
        self.assertEqual(ov[0]["used_bytes"], 300)


if __name__ == "__main__":
    unittest.main()
