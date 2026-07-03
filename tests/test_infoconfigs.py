import os, sys, base64, json, unittest
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
        self.assertIn("باقی‌مانده", s.parse_label(out[0])[0])
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


if __name__ == "__main__":
    unittest.main()
