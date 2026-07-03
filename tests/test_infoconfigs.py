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


if __name__ == "__main__":
    unittest.main()
