import base64
import copy
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot


class RelayIngressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = {k: getattr(bot, k) for k in ("DB_PATH", "SUB_DIR", "ENDPOINTS", "DEFAULT_IPS", "_adu")}
        bot.DB_PATH = self.tmp.name + "/db"
        bot.SUB_DIR = self.tmp.name
        bot.DEFAULT_IPS = ["1.1.1.1"]
        bot.ENDPOINTS = [{"tag": "backend-a", "port": 10443, "proto": "vless", "label": "ورودی دوم",
                          "reality": {"addr": "192.0.2.1", "port": 8443, "pbk": "key", "priv": "private",
                                      "sid": "ab", "sni": "example.com", "fp": "chrome", "flow": "xtls-rprx-vision"}}]
        bot.init_db()
        c = bot.db()
        for token in ("one", "two"):
            c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,credential_mode,active_slots) "
                      "VALUES(?,?,?, ?,0,0,1,'slots','[]')", (token, "original", "u_" + token, token))
        c.commit(); c.close()
        self.route = {"label": "مسیر دلخواه", "address": "relay.example.com", "port": 443,
                      "backend": "backend-a", "enabled": True, "count": 2}

    def tearDown(self):
        for key, value in self.original.items(): setattr(bot, key, value)
        self.tmp.cleanup()

    def save(self, routes, direct=False):
        settings = bot.global_settings_snapshot()
        settings["recipe"]["backend-a"] = {"enabled": direct, "count": 1 if direct else 0}
        settings["ingresses"] = routes
        bot.store_global_config(settings)

    def links(self, token="one"):
        bot.write_sub(token, "original", token)
        return base64.b64decode(Path(bot.sub_path(token)).read_bytes()).decode().splitlines()

    def test_relay_only_auth_uses_backend_identity_and_configurable_address(self):
        self.save([self.route])
        calls = []
        bot._adu = lambda ep, secret, email: calls.append((ep["tag"], secret, email)) or True
        self.assertTrue(bot.xr_add_user("one", "original"))
        links = [urllib.parse.urlsplit(link) for link in self.links()]
        self.assertEqual(len(links), 2)
        self.assertEqual({(link.hostname, link.port) for link in links}, {("relay.example.com", 443)})
        self.assertEqual({link.username for link in links}, {calls[0][1]})
        self.assertEqual(bot.active_slot_keys("one"), {("backend-a", "reality", 8443)})
        self.assertTrue(all("واسط" in urllib.parse.unquote(link.fragment) for link in links))

    def test_changing_relay_address_port_keeps_secret_and_direct_configuration(self):
        self.save([self.route], direct=True)
        original = self.links()
        original_identity = urllib.parse.urlsplit(original[1]).username
        changed = {**self.route, "address": "198.51.100.1", "port": 2053}
        self.save([changed], direct=True)
        updated = self.links()
        self.assertEqual(updated[0], original[0])
        self.assertEqual(urllib.parse.urlsplit(updated[1]).username, original_identity)
        self.assertEqual(bot.active_slot_keys("one"), {("backend-a", "reality", 8443)})
        self.save([], direct=True)
        self.assertEqual(self.links(), original[:1])
        self.assertEqual(bot.active_slot_keys("one"), {("backend-a", "reality", 8443)})

    def test_custom_snapshot_is_independent_and_old_snapshots_do_not_inherit_relay(self):
        self.save([])
        old = bot.global_settings_snapshot()
        old.pop("ingresses")
        bot.set_link_override("two", old)
        self.save([self.route])
        self.assertEqual(len(self.links("one")), 2)
        self.assertEqual(self.links("two"), [])
        custom = copy.deepcopy(bot.global_settings_snapshot())
        custom["ingresses"][0]["count"] = 3
        bot.set_link_override("two", custom)
        self.assertEqual(len(self.links("two")), 3)
        self.assertEqual(len(self.links("one")), 2)

    def test_off_and_zero_emit_no_relay_and_require_no_authorization(self):
        for change in ({"enabled": False}, {"count": 0}):
            self.save([{**self.route, **change}])
            self.assertEqual(self.links(), [])
            self.assertEqual(bot.active_slot_keys("one"), set())

    def test_multiple_relays_share_backend_auth_and_counts_are_uncapped(self):
        self.save([self.route, {**self.route, "address": "198.51.100.2", "count": 10}])
        self.assertEqual(len(self.links()), 12)
        c = bot.db()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM slot_credentials WHERE token='one'").fetchone()[0], 1)
        c.close()

    def test_invalid_address_or_unprepared_backend_is_rejected(self):
        for change in ({"address": "https://relay.example.com"}, {"address": "1.2.3.999"},
                       {"backend": "unknown"}, {"port": 65536}, {"count": -1}):
            with self.assertRaises(ValueError): bot._normal_ingresses([{**self.route, **change}])

    def test_form_add_delete_and_old_form_preservation(self):
        form = {"ingress_fields": "1", **{"new_ingress_" + key: str(value) for key, value in self.route.items()}}
        form["new_ingress_enabled"] = "on"
        self.assertEqual(bot._parse_ingresses(form, {}), [self.route])
        self.assertEqual(bot._parse_ingresses({}, {"ingresses": [self.route]}), [self.route])
        self.assertEqual(bot._parse_ingresses({"ingress_fields": "1", "ingress_0_label": "x", "ingress_0_delete": "on"}, {}), [])

    def test_compatibility_endpoints_hidden_when_unused_and_internal_tag_is_not_caption(self):
        bot.ENDPOINTS[0]["compatibility"] = True
        self.save([{**self.route, "enabled": False, "count": 0}])
        page = bot._render_config_fields(bot.global_settings_snapshot())
        self.assertNotIn("name='cnt_backend-a'", page)
        self.assertIn("name='preserve_backend-a'", page)
        self.assertNotIn("backend-a ·", page)
        self.assertIn("new_ingress_address", page)
        self.assertIn("config-nav", page)
        parsed = bot._parse_config_fields({"preserve_backend-a": "1", "endpoint_fields": "1", "ips": "1.1.1.1"}, bot.global_settings_snapshot())
        self.assertEqual(parsed["recipe"]["backend-a"], {"enabled": False, "count": 0})


if __name__ == "__main__": unittest.main()
