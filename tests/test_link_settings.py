import os, sys, tempfile, unittest, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402


EP = {"proto": "vless", "net": "ws", "tag": "vless-ws", "port": 10000,
      "path": "/ready", "label": "VLESS-WS", "tls_ports": [443, 2053], "notls_ports": [80]}


class LinkSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.orig = {k: getattr(bot, k) for k in (
            "DB_PATH", "ENDPOINTS", "DEFAULT_IPS", "DOMAIN", "write_sub",
            "apply_xray_outbounds", "resync_all", "xr_reconcile_user_endpoints", "xr_reconcile_all_users",
            "regenerate_all_subs", "xr_online_map", "refresh_all_usage", "refresh_usage",
            "xr_remove_user", "force_disconnect", "tags_to_cut_for_user")}
        bot.DB_PATH = self.tmp.name
        bot.ENDPOINTS = [dict(EP)]
        bot.DEFAULT_IPS = ["1.1.1.1"]
        bot.DOMAIN = "cdn.example.ir"
        bot.init_db()
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts) "
                  "VALUES('t1','secret','u_t1','Ali',0,0,1)")
        c.commit(); c.close()
        bot.write_sub = lambda *args: None
        self.apply_calls = []
        bot.apply_xray_outbounds = lambda *args, **kwargs: (self.apply_calls.append(1) or (True, "ok"))
        bot.resync_all = lambda: None
        bot.xr_reconcile_user_endpoints = lambda *args, **kwargs: True
        bot.xr_reconcile_all_users = lambda *args, **kwargs: True
        bot.regenerate_all_subs = lambda: None
        bot.refresh_all_usage = lambda: None
        bot.xr_online_map = lambda: {}
        bot._sessions.clear()
        self.sid, self.csrf = bot.new_session(now=1000)
        self.cookie = "mj_sess=" + self.sid

    def tearDown(self):
        for k, v in self.orig.items(): setattr(bot, k, v)
        bot._sessions.clear()
        os.unlink(self.tmp.name)

    def post(self, fields):
        fields = {"token": "t1", "csrf": self.csrf, **fields}
        return bot.route_admin("POST", "/a/user-config", {}, self.cookie,
                               urllib.parse.urlencode(fields).encode(), now=1001)

    def test_mode_is_full_snapshot_and_badge_disappears_on_reset(self):
        st, _, _ = self.post({"action": "customize"})
        self.assertEqual(st, 302)
        self.assertIsNotNone(bot.get_link_override("t1"))
        bot.set_ips(["9.9.9.9"])
        bot.set_recipe({"vless-ws": {"enabled": False, "count": 0}})
        self.assertEqual(bot.effective_ips("t1"), ["1.1.1.1"])
        self.assertEqual(bot.effective_recipe("t1")["vless-ws"]["count"], 3)
        page = bot.render_dashboard(self.csrf)
        self.assertIn("اختصاصی", page)
        self.post({"action": "default"})
        self.assertIsNone(bot.get_link_override("t1"))
        self.assertEqual(bot.effective_ips("t1"), ["9.9.9.9"])
        self.assertNotIn("<span class=linkbadge>", bot.render_dashboard(self.csrf))

    def test_custom_save_changes_only_this_link_and_keeps_prepared_path(self):
        self.post({"action": "customize"})
        fields = {
            "action": "save", "endpoint_fields": "1", "outbound_fields": "1",
            "en_vless-ws": "on", "cnt_vless-ws": "2", "label_vless-ws": "Ali WS",
            "hostidx_vless-ws": "0", "tls_vless-ws_443": "on", "fm_vless-ws": "{}",
            "ips": "8.8.8.8", "new_ob_tag": "clean", "new_ob_link": "socks://1.2.3.4:1080",
            "new_ob_dom": "claude.ai",
        }
        st, _, _ = self.post(fields)
        self.assertEqual(st, 302)
        custom = bot.get_link_override("t1")
        self.assertEqual(custom["recipe"]["vless-ws"]["count"], 2)
        self.assertEqual(custom["endpoint_settings"]["vless-ws"]["tls_ports"], [443])
        self.assertEqual(custom["endpoint_settings"]["vless-ws"]["notls_ports"], [])
        self.assertEqual(custom["endpoint_settings"]["vless-ws"]["path"], "/ready")
        self.assertEqual(custom["outbounds"][0]["domains"], ["claude.ai"])
        self.assertEqual(bot.get_ips(), ["1.1.1.1"])
        self.assertEqual(bot.get_recipe()["vless-ws"]["count"], 3)

    def test_invalid_fragment_does_not_change_link(self):
        self.post({"action": "customize"})
        old = bot.get_link_override("t1")
        st, hdr, _ = self.post({"action": "save", "endpoint_fields": "1", "en_vless-ws": "on",
                                "cnt_vless-ws": "1", "label_vless-ws": "VLESS",
                                "hostidx_vless-ws": "0", "fm_vless-ws": "{bad}", "ips": "8.8.8.8"})
        self.assertEqual(st, 302)
        self.assertIn("msg=", hdr["Location"])
        self.assertEqual(bot.get_link_override("t1"), old)

    def test_switching_mode_without_outbounds_does_not_restart_xray(self):
        self.post({"action": "customize"})
        self.post({"action": "default"})
        self.assertEqual(self.apply_calls, [])

    def test_copied_global_outbounds_do_not_restart_until_they_diverge(self):
        bot.set_outbounds([{"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": []}])
        self.post({"action": "customize"})
        self.assertEqual(self.apply_calls, [])
        self.assertEqual(bot._custom_outbound_sets(), {})
        bot.set_outbounds([])
        self.assertIn("t1", bot._custom_outbound_sets())

    def test_custom_page_renders_copied_outbound(self):
        bot.set_outbounds([{"tag": "clean", "link": "socks://1.2.3.4:1080",
                            "domains": ["example.com"]}])
        self.assertEqual(self.post({"action": "customize"})[0], 302)
        status, _, page = bot.route_admin(
            "GET", "/a/user-config", {"token": ["t1"]}, self.cookie, b"", now=1001)
        self.assertEqual(status, 200)
        self.assertIn(b"ob_tag_0", page)
        self.assertIn(b"ob_link_0", page)
        self.assertIn(b"ob_dom_0", page)

    def test_reality_label_is_saved(self):
        bot.ENDPOINTS = [{"tag": "reality", "proto": "vless", "label": "REALITY",
                          "reality": {"port": 443, "addr": "example.com", "pbk": "key",
                                      "sni": "example.com", "fp": "chrome", "sid": "00"}}]
        old = bot.global_settings_snapshot()
        parsed = bot._parse_config_fields({"endpoint_fields": "1", "en_reality": "on",
                                           "cnt_reality": "1", "label_reality": "Ali Reality",
                                           "ips": "1.1.1.1"}, old)
        self.assertEqual(parsed["endpoint_settings"]["reality"]["label"], "Ali Reality")

    def test_credentials_reconcile_before_outbound_restart(self):
        self.post({"action": "customize"})
        order = []
        bot.xr_reconcile_user_endpoints = lambda *a, **k: (order.append("reconcile") or True)
        bot.refresh_all_usage = lambda: order.append("refresh")
        bot.apply_xray_outbounds = lambda *a, **k: (order.append("restart") or (True, "ok"))
        self.post({"action": "save", "endpoint_fields": "1", "outbound_fields": "1",
                   "en_vless-ws": "on", "cnt_vless-ws": "1", "label_vless-ws": "Ali WS",
                   "hostidx_vless-ws": "0", "tls_vless-ws_443": "on", "ips": "1.1.1.1",
                   "new_ob_tag": "clean", "new_ob_link": "socks://1.2.3.4:1080"})
        self.assertEqual(order, ["reconcile", "refresh", "restart"])

    def test_deleting_custom_link_banks_other_usage_before_outbound_restart(self):
        self.post({"action": "customize"})
        order = []
        bot.refresh_usage = lambda token: None
        bot.xr_remove_user = lambda token: True
        bot.force_disconnect = lambda tags: None
        bot.tags_to_cut_for_user = lambda token: set()
        bot.refresh_all_usage = lambda: order.append("refresh")
        bot.apply_xray_outbounds = lambda *a, **k: (order.append("restart") or (True, "ok"))
        self.assertTrue(bot.delete_user("t1"))
        self.assertEqual(order, ["refresh", "restart"])

    def test_outbound_apply_failure_keeps_revoked_port_settings(self):
        self.post({"action": "customize"})
        bot.apply_xray_outbounds = lambda *a, **k: (False, "xray failed")
        _, headers, _ = self.post({"action": "save", "endpoint_fields": "1", "outbound_fields": "1",
                                   "en_vless-ws": "on", "cnt_vless-ws": "1",
                                   "label_vless-ws": "Ali WS", "hostidx_vless-ws": "0",
                                   "tls_vless-ws_443": "on", "ips": "1.1.1.1",
                                   "new_ob_tag": "clean", "new_ob_link": "socks://1.2.3.4:1080"})
        self.assertIn("msg=", headers["Location"])
        self.assertEqual(bot.get_link_override("t1")["endpoint_settings"]["vless-ws"]["tls_ports"], [443])
        self.assertEqual(bot.meta_get("outbound_sync_pending"), "1")

    def test_global_save_rolls_back_if_revocation_fails(self):
        calls = []
        bot.xr_reconcile_all_users = lambda *a, **k: (calls.append(1) and len(calls) > 1)
        form = {"csrf": self.csrf, "endpoint_fields": "1", "en_vless-ws": "on",
                "cnt_vless-ws": "1", "label_vless-ws": "Updated",
                "hostidx_vless-ws": "0", "tls_vless-ws_443": "on", "ips": "8.8.8.8"}
        status, headers, _ = bot.route_admin("POST", "/a/config", {}, self.cookie,
                                              urllib.parse.urlencode(form).encode(), now=1001)
        self.assertEqual(status, 302)
        self.assertIn("msg=", headers["Location"])
        self.assertEqual(bot.get_recipe()["vless-ws"]["count"], 3)
        self.assertEqual(bot.get_ips(), ["1.1.1.1"])
        self.assertEqual(len(calls), 2)

    def test_settings_commits_carry_durable_retry_markers(self):
        updated = bot.global_settings_snapshot()
        updated["recipe"]["vless-ws"]["count"] = 1
        bot.store_global_config(updated)
        self.assertEqual(bot.meta_get("membership_sync_pending"), "1")
        bot.meta_set("membership_sync_pending", "")
        self.assertTrue(bot.set_link_override("t1", updated, queue_sync=True, queue_outbound=True))
        self.assertEqual(bot.meta_get("membership_sync_pending"), "1")
        self.assertEqual(bot.meta_get("outbound_sync_pending"), "1")
        self.assertEqual(bot.get_link_override("t1")["recipe"]["vless-ws"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
