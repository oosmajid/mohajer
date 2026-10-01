import os, sys, json, base64, unittest, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402

EP = {"proto": "vless", "net": "ws", "tag": "vless-ws", "port": 10000, "path": "/p1", "label": "VLESS-WS"}


class TestFragmentParam(unittest.TestCase):
    def setUp(self):
        self._fm, self._dom = bot.FRAGMENT_FM, bot.DOMAIN
        bot.DOMAIN = "cdn.example.ir"

    def tearDown(self):
        bot.FRAGMENT_FM, bot.DOMAIN = self._fm, self._dom

    def _qs(self, link):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)

    def test_default_is_packets_1_3_fragment(self):
        fm = json.loads(bot.FRAGMENT_FM)
        self.assertEqual(fm["tcp"][0]["type"], "fragment")
        self.assertEqual(fm["tcp"][0]["settings"]["packets"], "1-3")

    def test_tls_ws_vless_and_trojan_carry_fm(self):
        for proto in ("vless", "trojan"):
            q = self._qs(bot._ws_link(dict(EP, proto=proto), "sec", "1.1.1.1", 443, "tls"))
            self.assertEqual(json.loads(q["fm"][0]), json.loads(bot.FRAGMENT_FM), proto)
            self.assertEqual(q["fp"], ["chrome"], proto)
            self.assertEqual(q["alpn"], ["http/1.1"], proto)
            self.assertEqual(q["sni"], ["cdn.example.ir"], proto)

    def test_tls_xhttp_allows_h2(self):
        q = self._qs(bot._ws_link(dict(EP, net="xhttp", label="VLESS-XHTTP"), "sec", "1.1.1.1", 443, "tls"))
        self.assertIn("fm", q)
        self.assertEqual(q["alpn"], ["h2,http/1.1"])
        self.assertEqual(q["mode"], ["auto"])

    def test_notls_untouched(self):
        q = self._qs(bot._ws_link(EP, "sec", "1.1.1.1", 80, "none"))
        for k in ("fm", "fp", "alpn"):
            self.assertNotIn(k, q)

    def test_empty_disables(self):
        bot.FRAGMENT_FM = ""
        q = self._qs(bot._ws_link(EP, "sec", "1.1.1.1", 443, "tls"))
        self.assertNotIn("fm", q)
        self.assertEqual(q["fp"], ["chrome"])
        self.assertEqual(q["alpn"], ["http/1.1"])

    def test_vmess_unchanged(self):
        link = bot._ws_link(dict(EP, proto="vmess"), "sec", "1.1.1.1", 443, "tls")
        j = json.loads(base64.b64decode(link[len("vmess://"):]))
        self.assertNotIn("fm", j)


if __name__ == "__main__":
    unittest.main()
