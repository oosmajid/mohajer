import os, sys, json, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402

EPS = [
    {"proto": "vless",  "net": "ws",    "tag": "vless-ws",  "port": 10000},
    {"proto": "vmess",  "net": "ws",    "tag": "vmess-ws",  "port": 10003},
    {"proto": "trojan", "net": "ws",    "tag": "trojan-ws", "port": 10002},
    {"proto": "vless",  "net": "xhttp", "tag": "vless-xh",  "port": 10001},
]


class PortsToKick(unittest.TestCase):
    def setUp(self):
        self._eps = bot.ENDPOINTS; bot.ENDPOINTS = EPS

    def tearDown(self):
        bot.ENDPOINTS = self._eps

    def test_none_hits_every_port(self):
        # None = "can't tell" -> reset all endpoint ports (safe fallback)
        self.assertEqual(bot.ports_to_kick(None), [10000, 10001, 10002, 10003])

    def test_empty_is_noop(self):
        # definitely offline -> nothing to cut, no one disturbed
        self.assertEqual(bot.ports_to_kick(set()), [])

    def test_single_tag_scopes_to_its_port(self):
        # target only on vless-ws -> only 10000 reset; vmess/trojan/xhttp users untouched
        self.assertEqual(bot.ports_to_kick({"vless-ws"}), [10000])

    def test_multi_tag(self):
        self.assertEqual(bot.ports_to_kick({"vless-ws", "trojan-ws"}), [10000, 10002])

    def test_unknown_tag_ignored(self):
        self.assertEqual(bot.ports_to_kick({"ghost-tag"}), [])


class OnlineMapParse(unittest.TestCase):
    def _fake_run(self, payload):
        class R: pass
        def run(*a, **k):
            r = R(); r.stdout = json.dumps(payload); r.stderr = ""; return r
        return run

    def test_parse_groups_tags_by_token(self):
        payload = {"users": [
            "user>>>u_18c6ea9357e8c2f8.vless-ws>>>online",
            "user>>>u_3f132da311a4471a.trojan-ws>>>online",
            "user>>>u_3f132da311a4471a.vless-ws>>>online",
        ]}
        orig = bot.subprocess.run; bot.subprocess.run = self._fake_run(payload)
        try:
            m = bot.xr_online_map()
        finally:
            bot.subprocess.run = orig
        self.assertEqual(m["18c6ea9357e8c2f8"], {"vless-ws"})
        self.assertEqual(m["3f132da311a4471a"], {"trojan-ws", "vless-ws"})

    def test_empty_users_is_empty_map_not_none(self):
        # statsUserOnline on but nobody online -> {} (distinct from API failure = None)
        orig = bot.subprocess.run; bot.subprocess.run = self._fake_run({"users": []})
        try:
            self.assertEqual(bot.xr_online_map(), {})
        finally:
            bot.subprocess.run = orig

    def test_api_failure_returns_none(self):
        def boom(*a, **k): raise OSError("xray down")
        orig = bot.subprocess.run; bot.subprocess.run = boom
        try:
            self.assertIsNone(bot.xr_online_map())
        finally:
            bot.subprocess.run = orig


if __name__ == "__main__":
    unittest.main()
