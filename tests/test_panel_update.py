import os, sys, json, base64, tempfile, unittest, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sub"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot             # noqa: E402
import subserver as s  # noqa: E402

REALITY_ENV = json.dumps({"port": 8388, "addr": "203.0.113.7", "ext_port": 49536, "pbk": "PUB",
                          "priv": "PRIV", "sid": "ab12", "sni": "www.speedtest.net"})
WS_EP = {"proto": "vless", "net": "ws", "tag": "vless-ws", "port": 10000, "path": "/p1",
         "label": "VLESS-WS", "tls_ports": [443], "notls_ports": []}


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close()
        self.subdir = tempfile.mkdtemp()
        self._saved = (bot.DB_PATH, bot.SUB_DIR, bot.ENDPOINTS, bot.DEFAULT_IPS, bot.DOMAIN)
        bot.DB_PATH = self.tmp.name; bot.SUB_DIR = self.subdir; bot.DEFAULT_IPS = ["1.1.1.1"]
        bot.DOMAIN = "cdn.example.ir"
        bot.init_db()

    def tearDown(self):
        bot.DB_PATH, bot.SUB_DIR, bot.ENDPOINTS, bot.DEFAULT_IPS, bot.DOMAIN = self._saved
        os.unlink(self.tmp.name)

    def links(self, token):
        raw = open(bot.sub_path(token)).read().strip()
        return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode().splitlines()


class TestRealityEndpoint(DbCase):
    def setUp(self):
        super().setUp()
        self.ep = bot._reality_endpoint(REALITY_ENV)
        bot.ENDPOINTS = [WS_EP, self.ep]

    def test_env_parsing_maps_nat_ports(self):
        self.assertEqual(self.ep["port"], 8388)                  # xray listens here
        self.assertEqual(self.ep["reality"]["port"], 49536)      # clients dial the NAT port
        self.assertEqual(self.ep["reality"]["flow"], "xtls-rprx-vision")
        self.assertIsNone(bot._reality_endpoint(""))
        self.assertIsNone(bot._reality_endpoint('{"port": 1}'))  # incomplete -> ignored

    def test_reality_defaults_to_zero_links(self):
        self.assertEqual(bot.get_recipe()["vless-reality"]["count"], 0)
        bot.write_sub("t1", "uuid-1", "x")
        self.assertFalse(any(l.startswith("vless://") and "security=reality" in l for l in self.links("t1")))

    def test_reality_count_emits_direct_links(self):
        bot.set_recipe({"vless-ws": {"enabled": True, "count": 1}, "vless-reality": {"enabled": True, "count": 2}})
        bot.write_sub("t1", "uuid-1", "x")
        rl = [l for l in self.links("t1") if "security=reality" in l]
        self.assertEqual(len(rl), 2)
        u = urllib.parse.urlsplit(rl[0]); q = urllib.parse.parse_qs(u.query)
        self.assertEqual((u.hostname, u.port), ("203.0.113.7", 49536))
        self.assertEqual(q["pbk"], ["PUB"]); self.assertEqual(q["sni"], ["www.speedtest.net"])
        self.assertNotEqual(rl[0].split("#")[1], rl[1].split("#")[1])   # distinct names

    def test_config_page_lists_reality(self):
        page = bot.render_config("csrf")
        self.assertIn("cnt_vless-reality", page)
        self.assertIn("پورت مستقیمِ آماده‌شده", page)


class TestBotMessages(DbCase):
    def setUp(self):
        super().setUp()
        bot.ENDPOINTS = [WS_EP]
        self.edits = []
        self._fns = (bot.send, bot.edit, bot.answer, bot.xr_add_user)
        bot.send = lambda *a, **k: None
        bot.edit = lambda *a, **k: self.edits.append(a)
        bot.answer = lambda *a, **k: None
        bot.xr_add_user = lambda token, secret: True
        bot.pending.clear()

    def tearDown(self):
        bot.send, bot.edit, bot.answer, bot.xr_add_user = self._fns
        bot.pending.clear()
        super().tearDown()

    def test_new_link_menu_has_test_button(self):
        cbs = [b["callback_data"] for row in bot.vol_kb() for b in row]
        self.assertEqual(cbs[0], "testlink")

    def test_test_button_creates_500mb_one_day_link(self):
        bot.route_cb(10, 20, "testlink", "cb")
        c = bot.db(); u = c.execute("SELECT * FROM users").fetchone(); c.close()
        self.assertEqual(u["label"], "Test")
        self.assertEqual(u["limit_bytes"], 500 * 1024 ** 2)
        self.assertAlmostEqual(u["expiry_ts"] - u["created_ts"], 86400, delta=5)
        self.assertIn(bot.sub_url(u["token"]), self.edits[-1][2])

    def test_result_text_is_short(self):
        token = bot.create_user(10, 30, "Ali")
        t = bot.result_text(token)
        self.assertIn("Ali", t); self.assertIn(bot.sub_url(token), t)
        for gone in ("VLESS", "Trojan", "VMess", "مرورگر"):
            self.assertNotIn(gone, t)


class TestSubPage(unittest.TestCase):
    def page(self):
        b64 = base64.b64encode(b"vless://a@1.1.1.1:443?security=tls#x").decode()
        info = {"used_bytes": 1, "limit_bytes": 0, "expiry_ts": 0, "created_ts": 0}
        return s.build_response("sub-u-x", b64, info, "Mozilla/5.0", False)[2].decode()

    def test_happ_downloads_and_deeplink(self):
        p = self.page()
        for _, url in s.HAPP_APPS:
            self.assertIn(url, p)
        self.assertIn("releases/latest/download/Happ.apk", p)
        self.assertIn("happ://add/", p)

    def test_qr_code(self):
        p = self.page()
        self.assertIn('src="/sub-qr.js"', p)
        self.assertIn("createSvgTag", p)
        self.assertTrue(os.path.isfile(s.QR_JS_PATH))
        self.assertTrue(s.SAFE.match(s.QR_JS_NAME))   # routed by the /sub- tunnel rule

    def test_clients_still_get_raw_base64(self):
        b64 = base64.b64encode(b"vless://a@1.1.1.1:443#x").decode()
        st, ctype, body, _ = s.build_response("sub-u-x", b64, None, "v2rayNG/1.8", False)
        self.assertTrue(ctype.startswith("text/plain")); self.assertNotIn(b"happ://", body)


if __name__ == "__main__":
    unittest.main()
