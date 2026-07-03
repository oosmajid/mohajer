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


if __name__ == "__main__":
    unittest.main()
