import os, sys, base64, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402

EPS = [
    {"proto": "vless", "net": "ws", "tag": "vless-ws", "port": 10000, "path": "/p1",
     "label": "VLESS-WS", "tls_ports": [443, 2053], "notls_ports": [80, 8080]},   # 4 slots
    {"proto": "vmess", "net": "ws", "tag": "vmess-ws", "port": 10003, "path": "/p2",
     "label": "VMESS-WS", "tls_ports": [8443], "notls_ports": [80]},              # 2 slots
    {"proto": "trojan", "net": "ws", "tag": "trojan-ws", "port": 10002, "path": "/p3",
     "label": "TROJAN-WS", "tls_ports": [2087], "notls_ports": [8880]},           # 2 slots
]


class RecipeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close()
        self.subdir = tempfile.mkdtemp()
        self._db, self._sub, self._eps, self._ips, self._dom, self._adu = (
            bot.DB_PATH, bot.SUB_DIR, bot.ENDPOINTS, bot.DEFAULT_IPS, bot.DOMAIN, bot._adu)
        bot.DB_PATH = self.tmp.name; bot.SUB_DIR = self.subdir
        bot.ENDPOINTS = EPS; bot.DEFAULT_IPS = ["1.1.1.1", "2.2.2.2"]; bot.DOMAIN = "cdn.example.ir"
        bot._adu = lambda *args: True
        bot.init_db()

    def tearDown(self):
        bot.DB_PATH, bot.SUB_DIR, bot.ENDPOINTS, bot.DEFAULT_IPS, bot.DOMAIN = (
            self._db, self._sub, self._eps, self._ips, self._dom)
        bot._adu = self._adu
        os.unlink(self.tmp.name)

    def publish(self, token, label):
        secret = "11111111-1111-1111-1111-111111111111"
        self.assertTrue(bot.xr_add_user(token, secret))
        self.assertTrue(bot.write_sub(token, secret, label))

    def _links(self, token):
        raw = open(bot.sub_path(token)).read().strip()
        return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode().splitlines()


class TestRecipeModel(RecipeBase):
    def test_default_recipe_matches_endpoints(self):
        r = bot.get_recipe()
        self.assertEqual(r["vless-ws"], {"enabled": True, "count": 4})
        self.assertEqual(r["vmess-ws"], {"enabled": True, "count": 2})
        self.assertEqual(r["trojan-ws"], {"enabled": True, "count": 2})

    def test_set_get_roundtrip(self):
        bot.set_recipe({"vless-ws": {"enabled": True, "count": 3}})
        self.assertEqual(bot.get_recipe()["vless-ws"], {"enabled": True, "count": 3})
        # tags absent from stored recipe still fall back to endpoint default
        self.assertEqual(bot.get_recipe()["vmess-ws"], {"enabled": True, "count": 2})

    def test_capacity_limit_blocks_excessive_client_creation(self):
        with self.assertRaises(ValueError):
            bot.set_recipe({"vless-ws": {"enabled": True, "count": 17}})
        with self.assertRaises(ValueError):
            bot.emitted_configs("a", {"vless-ws": {"enabled": True, "count": 17}})
        bot.ENDPOINTS = EPS + [{**EPS[0], "tag": "extra-a"}, {**EPS[0], "tag": "extra-b"}]
        recipe = {ep["tag"]: {"enabled": True, "count": 16} for ep in bot.ENDPOINTS}
        with self.assertRaises(ValueError): bot.set_recipe(recipe)  # 80 > 64 per link


class TestWriteSubHonorsRecipe(RecipeBase):
    def test_default_emits_one_per_slot(self):
        self.publish("aa", "A")
        links = self._links("aa")
        self.assertEqual(len(links), 8)                                  # 4+2+2
        self.assertEqual(sum(l.startswith("vless://") for l in links), 4)
        self.assertEqual(sum(l.startswith("vmess://") for l in links), 2)
        self.assertEqual(sum(l.startswith("trojan://") for l in links), 2)

    def test_recipe_limits_and_disables(self):
        bot.set_recipe({"vless-ws": {"enabled": True, "count": 2},
                        "vmess-ws": {"enabled": False, "count": 0},
                        "trojan-ws": {"enabled": True, "count": 1}})
        self.publish("bb", "B")
        links = self._links("bb")
        self.assertEqual(len(links), 3)
        self.assertEqual(sum(l.startswith("vless://") for l in links), 2)
        self.assertEqual(sum(l.startswith("vmess://") for l in links), 0)
        self.assertEqual(sum(l.startswith("trojan://") for l in links), 1)

    def test_high_count_has_no_cap(self):
        # user requirement: must be able to make e.g. 10 configs of one type
        bot.set_recipe({"vless-ws": {"enabled": False, "count": 0},
                        "vmess-ws": {"enabled": False, "count": 0},
                        "trojan-ws": {"enabled": True, "count": 10}})
        self.publish("dd", "D")
        links = self._links("dd")
        self.assertEqual(len(links), 10)
        self.assertTrue(all(l.startswith("trojan://") for l in links))

    def test_count_cycles_ports_and_ips(self):
        # only vless-ws, count 6 > its 4 slots -> ports cycle, IPs round-robin
        bot.set_recipe({"vless-ws": {"enabled": True, "count": 6},
                        "vmess-ws": {"enabled": False, "count": 0},
                        "trojan-ws": {"enabled": False, "count": 0}})
        self.publish("cc", "C")
        links = self._links("cc")
        self.assertEqual(len(links), 6)
        hosts = [l.split("@", 1)[1].split("?", 1)[0] for l in links]     # ip:port
        self.assertEqual(hosts, ["1.1.1.1:443", "2.2.2.2:2053", "1.1.1.1:80",
                                 "2.2.2.2:8080", "1.1.1.1:443", "2.2.2.2:2053"])


if __name__ == "__main__":
    unittest.main()
