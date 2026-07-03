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


class TestRouteGet(TestStats):  # reuse the seeded DB from TestStats.setUp
    def _session_cookie(self):
        bot._sessions.clear()
        sid, csrf = bot.new_session(now=1000)
        return "mj_sess=%s" % sid, csrf

    def test_no_session_returns_expired(self):
        st, hdr, body = bot.route_admin("GET", "/a/", {}, "", b"", now=1000)
        self.assertEqual(st, 200)
        self.assertIn("منقضی", body.decode("utf-8"))

    def test_login_sets_cookie_and_redirects(self):
        tok = bot.mint_login(now=1000)
        st, hdr, body = bot.route_admin("GET", "/a/login/" + tok, {}, "", b"", now=1001)
        self.assertEqual(st, 302)
        self.assertEqual(hdr["Location"], "/a/")
        self.assertIn("mj_sess=", hdr["Set-Cookie"])
        self.assertIn("HttpOnly", hdr["Set-Cookie"])

    def test_dashboard_lists_user(self):
        cookie, csrf = self._session_cookie()
        st, hdr, body = bot.route_admin("GET", "/a/", {}, cookie, b"", now=1001)
        self.assertEqual(st, 200)
        self.assertIn("One", body.decode("utf-8"))        # the seeded label
        self.assertIn("<svg", body.decode("utf-8"))       # panel chart

    def test_user_detail(self):
        cookie, csrf = self._session_cookie()
        st, hdr, body = bot.route_admin("GET", "/a/user", {"token": ["t1"]}, cookie, b"", now=1001)
        self.assertEqual(st, 200)
        self.assertIn("One", body.decode("utf-8"))
        self.assertIn(csrf, body.decode("utf-8"))         # forms carry the csrf token

    def test_dashboard_has_logout(self):
        cookie, csrf = self._session_cookie()
        st, hdr, body = bot.route_admin("GET", "/a/", {}, cookie, b"", now=1001)
        page = body.decode("utf-8")
        self.assertIn("/a/logout", page)   # logout form present
        self.assertIn(csrf, page)          # and it carries the csrf token


class TestRoutePost(TestStats):
    def setUp(self):
        super().setUp()
        bot.xr_add_user = lambda *a, **k: True       # stub xray
        bot.xr_remove_user = lambda *a, **k: None
        bot.SUB_DIR = tempfile.mkdtemp()             # write_sub target
        bot._sessions.clear()
        self.sid, self.csrf = bot.new_session(now=1000)
        self.cookie = "mj_sess=%s" % self.sid

    def _post(self, path, fields):
        body = urllib.parse.urlencode(fields).encode()
        return bot.route_admin("POST", path, {}, self.cookie, body, now=1001)

    def test_addvol_increases_limit(self):
        st, hdr, _ = self._post("/a/addvol", {"token": "t1", "gb": "5", "csrf": self.csrf})
        self.assertEqual(st, 302)
        c = bot.db(); lim = c.execute("SELECT limit_bytes FROM users WHERE token='t1'").fetchone()["limit_bytes"]; c.close()
        self.assertEqual(lim, 1000 + 5 * bot.GB)

    def test_csrf_mismatch_rejected(self):
        st, hdr, _ = self._post("/a/addvol", {"token": "t1", "gb": "5", "csrf": "WRONG"})
        self.assertEqual(st, 403)

    def test_delete_requires_confirm_and_removes(self):
        st, _, _ = self._post("/a/delete", {"token": "t1", "confirm": "yes", "csrf": self.csrf})
        self.assertEqual(st, 302)
        c = bot.db(); n = c.execute("SELECT COUNT(*) c FROM users WHERE token='t1'").fetchone()["c"]; c.close()
        self.assertEqual(n, 0)

    def test_new_creates_link(self):
        st, hdr, _ = self._post("/a/new", {"gb": "10", "days": "30", "name": "Fresh", "csrf": self.csrf})
        self.assertEqual(st, 302)
        c = bot.db(); n = c.execute("SELECT COUNT(*) c FROM users WHERE label='Fresh'").fetchone()["c"]; c.close()
        self.assertEqual(n, 1)

    def test_logout_invalidates_session(self):
        st, hdr, body = self._post("/a/logout", {"csrf": self.csrf})
        self.assertEqual(st, 200)
        self.assertNotIn(self.sid, bot._sessions)          # server-side invalidation
        self.assertIn("Max-Age=0", hdr["Set-Cookie"])      # cookie cleared
        self.assertIn("خارج", body.decode("utf-8"))

    def test_logout_csrf_mismatch_keeps_session(self):
        st, hdr, _ = self._post("/a/logout", {"csrf": "WRONG"})
        self.assertEqual(st, 403)
        self.assertIn(self.sid, bot._sessions)

    def test_after_logout_dashboard_denied(self):
        self._post("/a/logout", {"csrf": self.csrf})
        st, hdr, body = bot.route_admin("GET", "/a/", {}, self.cookie, b"", now=1002)
        self.assertEqual(st, 200)
        self.assertIn("منقضی", body.decode("utf-8"))


class TestConfigPage(TestStats):
    def setUp(self):
        super().setUp()
        bot.xr_add_user = lambda *a, **k: True
        bot.xr_remove_user = lambda *a, **k: None
        bot.SUB_DIR = tempfile.mkdtemp()
        self._eps, self._dom = bot.ENDPOINTS, bot.DOMAIN
        bot.ENDPOINTS = [
            {"proto": "vless", "net": "ws", "tag": "vless-ws", "port": 10000, "path": "/p1",
             "label": "VLESS-WS", "tls_ports": [443, 2053], "notls_ports": [80, 8080]},
            {"proto": "trojan", "net": "ws", "tag": "trojan-ws", "port": 10002, "path": "/p3",
             "label": "TROJAN-WS", "tls_ports": [2087], "notls_ports": [8880]}]
        bot.DOMAIN = "cdn.example.ir"
        bot._sessions.clear()
        self.sid, self.csrf = bot.new_session(now=1000)
        self.cookie = "mj_sess=%s" % self.sid

    def tearDown(self):
        bot.ENDPOINTS, bot.DOMAIN = self._eps, self._dom
        super().tearDown()

    def _post(self, fields):
        body = urllib.parse.urlencode(fields).encode()
        return bot.route_admin("POST", "/a/config", {}, self.cookie, body, now=1001)

    def test_config_get_shows_endpoints_and_ips(self):
        st, hdr, body = bot.route_admin("GET", "/a/config", {}, self.cookie, b"", now=1001)
        page = body.decode("utf-8")
        self.assertEqual(st, 200)
        self.assertIn("VLESS-WS", page)
        self.assertIn("trojan-ws", page)
        self.assertIn("name=ips", page)               # clean-IPs textarea present

    def test_config_post_saves_recipe_and_ips(self):
        st, hdr, _ = self._post({
            "en_vless-ws": "on", "cnt_vless-ws": "2",
            "cnt_trojan-ws": "1",                      # trojan checkbox omitted -> disabled
            "ips": "9.9.9.9, 8.8.8.8", "csrf": self.csrf})
        self.assertEqual(st, 302)
        rec = bot.get_recipe()
        self.assertEqual(rec["vless-ws"], {"enabled": True, "count": 2})
        self.assertFalse(rec["trojan-ws"]["enabled"])
        self.assertEqual(bot.get_ips(), ["9.9.9.9", "8.8.8.8"])

    def test_config_post_bad_ips_keeps_old(self):
        bot.set_ips(["5.5.5.5"])
        st, hdr, _ = self._post({"en_vless-ws": "on", "cnt_vless-ws": "1",
                                 "ips": "not-an-ip", "csrf": self.csrf})
        self.assertEqual(st, 302)
        self.assertEqual(bot.get_ips(), ["5.5.5.5"])   # invalid input ignored, old IPs kept

    def test_dashboard_links_to_config(self):
        st, hdr, body = bot.route_admin("GET", "/a/", {}, self.cookie, b"", now=1001)
        self.assertIn("/a/config", body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
