import os, sys, base64, json, sqlite3, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sub"))
import subserver as s  # noqa: E402

VLESS = "vless://11111111-1111-1111-1111-111111111111@1.2.3.4:443?encryption=none&security=tls&type=ws&host=h&path=%2Fp#Old%20Name"


def make_vmess(ps):
    j = {"v": "2", "ps": ps, "add": "1.2.3.4", "port": "443", "id": "x", "aid": "0",
         "net": "ws", "type": "none", "host": "h", "path": "/p", "tls": "tls"}
    return "vmess://" + base64.b64encode(json.dumps(j).encode()).decode()


class TestRelabel(unittest.TestCase):
    def test_vless_name_replaced(self):
        out = s.relabel(VLESS, "📦 NEW")
        self.assertEqual(s.parse_label(out)[0], "📦 NEW")
        self.assertTrue(out.startswith("vless://11111111-"))  # body untouched
        self.assertIn("host=h", out)

    def test_vmess_ps_replaced(self):
        out = s.relabel(make_vmess("Old"), "🔄 UP")
        self.assertEqual(s.parse_label(out)[0], "🔄 UP")
        raw = out[len("vmess://"):]
        j = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
        self.assertEqual(j["add"], "1.2.3.4")  # body untouched


class TestDecorate(unittest.TestCase):
    def _info(self, **kw):
        base = {"used_bytes": 0, "limit_bytes": 0, "expiry_ts": 0, "disabled_ts": 0}
        base.update(kw); return base

    def test_active_prepends_two_working_clones(self):
        links = [VLESS, make_vmess("Real2")]
        out = s.decorate(links, self._info(limit_bytes=100, used_bytes=40))
        self.assertEqual(len(out), 4)
        self.assertTrue(out[0].startswith("vless://11111111-"))  # a real working clone
        self.assertIn("باقیمانده ۶۰ بایت", s.parse_label(out[0])[0])  # RTL-safe: Persian unit + digits, no "GB"
        self.assertIn("آپدیت", s.parse_label(out[1])[0])
        self.assertEqual(out[2:], links)  # real configs preserved, in order

    def test_disabled_keeps_only_info_configs(self):
        links = [VLESS, make_vmess("Real2")]
        out = s.decorate(links, self._info(disabled_ts=123))
        self.assertEqual(len(out), 2)
        self.assertIn("تمام شد", s.parse_label(out[0])[0])

    def test_empty_or_none_unchanged(self):
        self.assertEqual(s.decorate([], self._info()), [])
        self.assertEqual(s.decorate([VLESS], None), [VLESS])


class TestBuildResponse(unittest.TestCase):
    def _b64(self, links):
        return base64.b64encode("\n".join(links).encode()).decode()

    def _info(self, **kw):
        base = {"used_bytes": 40, "limit_bytes": 100, "expiry_ts": 0, "created_ts": 0, "label": "x", "disabled_ts": 0}
        base.update(kw); return base

    def test_raw_active_has_info_plus_real(self):
        b64 = self._b64([VLESS, make_vmess("Real2")])
        code, ctype, body, extra = s.build_response("sub-u-aa", b64, self._info(), "v2rayNG", False)
        self.assertEqual(code, 200)
        lines = base64.b64decode(body).decode().splitlines()
        self.assertEqual(len(lines), 4)  # 2 info + 2 real
        self.assertIn("Subscription-Userinfo", extra)

    def test_raw_disabled_only_info(self):
        b64 = self._b64([VLESS, make_vmess("Real2")])
        code, ctype, body, extra = s.build_response("sub-u-aa", b64, self._info(disabled_ts=99), "v2rayNG", False)
        lines = base64.b64decode(body).decode().splitlines()
        self.assertEqual(len(lines), 2)

    def test_html_for_browser(self):
        b64 = self._b64([VLESS])
        code, ctype, body, extra = s.build_response("sub-u-aa", b64, self._info(), "Mozilla/5.0", False)
        self.assertIn("text/html", ctype)
        self.assertIn("باقیمانده", body.decode("utf-8"))  # info-config status name in the page

    def test_terabytes_show_three_decimal_places_on_site(self):
        self.assertEqual(s.fmt_bytes(1177.6 * 1024 ** 3), "1.150 TB")


class TestUserInfo(unittest.TestCase):
    def test_subscriber_sees_usage_since_reset_not_lifetime(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
        old = s.DB_PATH; s.DB_PATH = tmp.name
        try:
            c = sqlite3.connect(tmp.name)
            c.execute("CREATE TABLE users(token TEXT,used_bytes INTEGER,usage_reset_bytes INTEGER,"
                      "limit_bytes INTEGER,expiry_ts INTEGER,created_ts INTEGER,label TEXT,disabled_ts INTEGER)")
            c.execute("INSERT INTO users VALUES('aa',175,150,1000,0,0,'A',0)")
            c.commit(); c.close()
            info = s.user_info("sub-u-aa")
            self.assertEqual(info["used_bytes"], 25)
            self.assertEqual(info["lifetime_used_bytes"], 175)
        finally:
            s.DB_PATH = old; os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
