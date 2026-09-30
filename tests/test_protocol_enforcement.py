import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402


EPS = [
    {"proto": "vless", "net": "ws", "tag": "vless-ws", "port": 10000,
     "path": "/v", "label": "VLESS", "tls_ports": [443, 2053], "notls_ports": []},
    {"proto": "vmess", "net": "ws", "tag": "vmess-ws", "port": 10001,
     "path": "/m", "label": "VMess", "tls_ports": [443], "notls_ports": []},
    {"proto": "trojan", "net": "ws", "tag": "trojan-ws", "port": 10002,
     "path": "/t", "label": "Trojan", "tls_ports": [], "notls_ports": [80]},
]


class ProtocolEnforcement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db, self.old_sub, self.old_eps = bot.DB_PATH, bot.SUB_DIR, bot.ENDPOINTS
        bot.DB_PATH, bot.SUB_DIR, bot.ENDPOINTS = os.path.join(self.tmp.name, "db.sqlite"), self.tmp.name, EPS
        bot.init_db()
        self.saved = {}
        self.added, self.removed, self.kicked = [], [], []
        self.patch("_adu", lambda ep, secret, email: self.added.append((ep["tag"], secret, email)) or True)
        self.patch("_rmu_email", lambda tag, email: self.removed.append((tag, email)) or True)
        self.patch("force_disconnect", lambda tags: self.kicked.append(set(tags)))
        self.patch("xr_online_map", lambda: {})
        self.patch("xr_usage", lambda token: 0)
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({}, {}))
        self.recipe(2)

    def tearDown(self):
        for name, original in self.saved.items(): setattr(bot, name, original)
        bot.DB_PATH, bot.SUB_DIR, bot.ENDPOINTS = self.old_db, self.old_sub, self.old_eps
        self.tmp.cleanup()

    def patch(self, name, replacement):
        self.saved.setdefault(name, getattr(bot, name))
        setattr(bot, name, replacement)

    def recipe(self, vless_count, vmess_count=0, trojan_count=0):
        bot.set_recipe({"vless-ws": {"enabled": bool(vless_count), "count": vless_count},
                        "vmess-ws": {"enabled": bool(vmess_count), "count": vmess_count},
                        "trojan-ws": {"enabled": bool(trojan_count), "count": trojan_count}})

    def user(self, token, mode="legacy", frozen=0, disabled=0, limit=0, used=0):
        slots = bot._encode_slots(bot.active_slot_keys(token))
        emissions = bot._encode_emissions(bot.emitted_configs(token))
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,"
                  "used_bytes,disabled_ts,frozen,credential_mode,active_slots,active_emissions) "
                  "VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?)",
                  (token, "00000000-0000-4000-8000-000000000001", "u_" + token, token,
                   limit, 0, used, disabled, frozen, mode, slots, emissions))
        c.commit(); c.close()

    def test_new_link_uses_distinct_credentials_per_external_port(self):
        self.assertTrue(bot.xr_add_user("new", "00000000-0000-4000-8000-000000000001"))
        self.assertEqual({email.split(".")[2] for _, _, email in self.added}, {"c0", "c1"})
        self.assertEqual(len({secret for _, secret, _ in self.added}), 2)
        bot.write_sub("new", "00000000-0000-4000-8000-000000000001", "new")
        raw = base64.b64decode(open(bot.sub_path("new")).read()).decode()
        self.assertIn(":443?", raw)
        self.assertIn(":2053?", raw)
        self.assertNotIn("00000000-0000-4000-8000-000000000001", raw)

    def test_count_reduction_revokes_duplicate_on_surviving_port(self):
        self.recipe(3)  # two ports; index 2 repeats the first port
        self.user("a", mode="emissions")
        self.assertTrue(bot.xr_add_user("a", "unused"))
        original = bot._emission_credential_map("a")
        self.assertEqual(len(original), 3)
        self.assertEqual(len({r["secret"] for r in original.values()}), 3)
        self.assertTrue(bot.write_sub("a", "unused", "a"))
        with open(bot.sub_path("a")) as f: old_sub = f.read()
        self.recipe(2)
        # Regeneration while Xray still holds the old membership must not
        # publish new settings or silently mint an unregistered identity.
        bot.regenerate_all_subs()
        with open(bot.sub_path("a")) as f: self.assertEqual(f.read(), old_sub)
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "unused"))
        current = bot._emission_credential_map("a")
        self.assertEqual(set(current), {("vless-ws", 0), ("vless-ws", 1)})
        self.assertIn(("vless-ws", original[("vless-ws", 2)]["email"]), self.removed)
        self.assertEqual(current[("vless-ws", 0)]["secret"], original[("vless-ws", 0)]["secret"])
        with open(bot.sub_path("a")) as f: raw = base64.b64decode(f.read()).decode()
        self.assertEqual(len(raw.splitlines()), 2)
        self.assertNotIn(original[("vless-ws", 2)]["secret"], raw)

    def test_legacy_count_reduction_migrates_only_affected_tag(self):
        bot.set_ips(["1.1.1.1"])
        self.recipe(3, vmess_count=1)
        self.user("a")
        self.recipe(2, vmess_count=1)
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.assertIn(("vless-ws", "u_a.vless-ws"), self.removed)
        self.assertNotIn(("vmess-ws", "u_a.vmess-ws"), self.removed)
        self.assertEqual(len(bot._emission_credential_map("a")), 2)
        self.assertEqual(bot.emission_mode_tags("a"), {"vless-ws"})

    def test_clean_ip_change_rotates_existing_uri_identity(self):
        bot.set_ips(["1.1.1.1"])
        self.user("a", mode="emissions")
        self.assertTrue(bot.xr_add_user("a", "unused"))
        original = bot._emission_credential_map("a")
        bot.set_ips(["2.2.2.2"])
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "unused"))
        current = bot._emission_credential_map("a")
        for key, old in original.items():
            self.assertIn((key[0], old["email"]), self.removed)
            self.assertNotEqual(current[key]["secret"], old["secret"])
        with open(bot.sub_path("a")) as f: raw = base64.b64decode(f.read()).decode()
        self.assertIn("2.2.2.2", raw)
        self.assertNotIn("1.1.1.1", raw)

    def test_reality_duplicate_emissions_have_distinct_revocable_ids(self):
        bot.ENDPOINTS = EPS + [{"proto": "vless", "net": "tcp", "tag": "vless-reality", "port": 10443,
            "label": "REALITY", "reality": {"addr": "1.2.3.4", "port": 443, "pbk": "public",
            "sni": "example.com", "fp": "chrome", "sid": "abc", "priv": "private"}}]
        bot.set_recipe({"vless-ws": {"enabled": False, "count": 0},
                        "vmess-ws": {"enabled": False, "count": 0},
                        "trojan-ws": {"enabled": False, "count": 0},
                        "vless-reality": {"enabled": True, "count": 3}})
        self.user("a", mode="emissions")
        self.assertTrue(bot.xr_add_user("a", "unused"))
        original = bot._emission_credential_map("a")
        self.assertEqual(len({r["secret"] for r in original.values()}), 3)
        self.assertTrue(bot.write_sub("a", "unused", "a"))
        bot.set_recipe({"vless-ws": {"enabled": False, "count": 0},
                        "vmess-ws": {"enabled": False, "count": 0},
                        "trojan-ws": {"enabled": False, "count": 0},
                        "vless-reality": {"enabled": True, "count": 2}})
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.assertIn(("vless-reality", original[("vless-reality", 2)]["email"]), self.removed)
        with open(bot.sub_path("a")) as f: raw = base64.b64decode(f.read()).decode()
        self.assertEqual(len(raw.splitlines()), 2)
        self.assertNotIn(original[("vless-reality", 2)]["secret"], raw)

    def test_failed_rmu_keeps_old_subscription_then_resync_finishes(self):
        self.recipe(3)
        self.user("a", mode="emissions")
        self.assertTrue(bot.xr_add_user("a", "unused"))
        self.assertTrue(bot.write_sub("a", "unused", "a"))
        with open(bot.sub_path("a")) as f: old_sub = f.read()
        removed = bot._emission_credential_map("a")[("vless-ws", 2)]
        self.recipe(2)
        self.patch("_rmu_email", lambda tag, email: False)
        c = bot.db(); u = c.execute("SELECT * FROM users WHERE token='a'").fetchone(); c.close()
        ok, _, _ = bot._reconcile_user_membership(u)
        self.assertFalse(ok)
        with open(bot.sub_path("a")) as f: self.assertEqual(f.read(), old_sub)
        self.assertIn(("vless-ws", 2), bot._emission_credential_map("a"))
        self.patch("_rmu_email", lambda tag, email: self.removed.append((tag, email)) or True)
        self.assertTrue(bot.resync_all())
        self.assertIn(("vless-ws", removed["email"]), self.removed)
        with open(bot.sub_path("a")) as f: raw = base64.b64decode(f.read()).decode()
        self.assertEqual(len(raw.splitlines()), 2)

    def test_failed_legacy_migration_can_roll_back_without_extra_ids(self):
        self.recipe(3)
        self.user("a")
        self.assertTrue(bot.write_sub("a", "00000000-0000-4000-8000-000000000001", "a"))
        self.recipe(2)
        self.patch("_rmu_email", lambda tag, email: email != "u_a.vless-ws")
        c = bot.db(); u = c.execute("SELECT * FROM users WHERE token='a'").fetchone(); c.close()
        self.assertFalse(bot._reconcile_user_membership(u)[0])
        self.assertEqual(len(bot._emission_credential_map("a")), 2)
        self.recipe(3)
        self.patch("_rmu_email", lambda tag, email: self.removed.append((tag, email)) or True)
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.assertEqual(bot._emission_credential_map("a"), {})
        self.assertIn(("vless-ws", "00000000-0000-4000-8000-000000000001", "u_a.vless-ws"), self.added)
        with open(bot.sub_path("a")) as f: raw = base64.b64decode(f.read()).decode()
        self.assertIn("00000000-0000-4000-8000-000000000001", raw)

    def test_publish_failure_remains_pending_and_retries(self):
        self.recipe(3)
        self.user("a", mode="emissions")
        self.assertTrue(bot.xr_add_user("a", "unused"))
        self.assertTrue(bot.write_sub("a", "unused", "a"))
        self.recipe(2)
        self.patch("write_sub", lambda *args: False)
        self.patch("_emergency_clear_dynamic_users", lambda: False)
        self.assertFalse(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.assertEqual(bot.meta_get("membership_sync_pending"), "1")
        self.patch("write_sub", self.saved["write_sub"])
        self.assertTrue(bot.resync_all())
        self.assertEqual(bot.meta_get("membership_sync_pending"), "")

    def test_disappearing_emission_counter_does_not_lose_survivor_usage(self):
        self.recipe(3)
        self.user("a", mode="emissions")
        self.assertTrue(bot.xr_add_user("a", "unused"))
        creds = bot._emission_credential_map("a")
        gone = creds[("vless-ws", 2)]["email"]
        live = [creds[("vless-ws", i)]["email"] for i in (0, 1)]
        c = bot.db(); c.execute("UPDATE users SET usage_anchor=0 WHERE token='a'"); c.commit(); c.close()
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 300},
                   {gone: 100, live[0]: 100, live[1]: 100}))
        bot.refresh_usage("a")
        self.recipe(2)
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 220},
                   {live[0]: 110, live[1]: 110}))
        bot.refresh_usage("a")
        c = bot.db(); used = c.execute("SELECT used_bytes FROM users WHERE token='a'").fetchone()[0]; c.close()
        self.assertEqual(used, 320)
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 360},
                   {live[0]: 250, live[1]: 110}))
        bot.refresh_usage("a")
        c = bot.db(); used = c.execute("SELECT used_bytes FROM users WHERE token='a'").fetchone()[0]; c.close()
        self.assertEqual(used, 460)

    def test_new_user_create_and_resync_register_each_emission_once(self):
        self.recipe(3)
        token = bot.create_user(1, 1, "new")
        self.assertIsNotNone(token)
        self.assertEqual(bot.credential_mode(token), "emissions")
        self.assertEqual(len(bot._emission_credential_map(token)), 3)
        self.added.clear()
        self.assertTrue(bot.resync_all())
        self.assertEqual(len(self.added), 3)

    def test_upgrade_migrates_all_old_identities_and_marks_pending_once(self):
        self.recipe(3)
        self.user("a")
        self.assertTrue(bot.write_sub("a", "00000000-0000-4000-8000-000000000001", "a"))
        bot.init_db()  # startup on an existing pre-emission database
        c = bot.db(); u = c.execute("SELECT emission_migration_pending FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual(u[0], 1)
        self.assertEqual(bot.meta_get("membership_sync_pending"), "1")
        self.assertTrue(bot.resync_all())
        c = bot.db(); u = c.execute("SELECT credential_mode,emission_migration_pending FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual(tuple(u), ("emissions", 0))
        self.assertEqual(bot.meta_get("membership_sync_pending"), "")
        self.assertEqual(set(self.removed), {(ep["tag"], "u_a." + ep["tag"]) for ep in EPS})
        self.assertEqual(len(bot._emission_credential_map("a")), 3)
        with open(bot.sub_path("a")) as f: raw = base64.b64decode(f.read()).decode()
        self.assertNotIn("00000000-0000-4000-8000-000000000001", raw)

    def test_upgrade_retry_preserves_old_subscription_until_revocation(self):
        self.recipe(3)
        self.user("a")
        self.assertTrue(bot.write_sub("a", "00000000-0000-4000-8000-000000000001", "a"))
        with open(bot.sub_path("a")) as f: old_sub = f.read()
        bot.init_db()
        self.patch("_rmu_email", lambda tag, email: False if email == "u_a.vless-ws" else True)
        self.assertFalse(bot.resync_all())
        self.assertEqual(bot.meta_get("membership_sync_pending"), "1")
        with open(bot.sub_path("a")) as f: self.assertEqual(f.read(), old_sub)
        c = bot.db(); u = c.execute("SELECT credential_mode,emission_migration_pending FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual(tuple(u), ("legacy", 1))
        self.patch("_rmu_email", lambda tag, email: self.removed.append((tag, email)) or True)
        self.assertTrue(bot.resync_all())
        c = bot.db(); u = c.execute("SELECT credential_mode,emission_migration_pending FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual(tuple(u), ("emissions", 0))
        with open(bot.sub_path("a")) as f: self.assertNotEqual(f.read(), old_sub)

    def test_upgrade_revokes_frozen_legacy_without_registering_new_ids(self):
        self.user("a", frozen=1)
        bot.init_db()
        self.assertTrue(bot.resync_all())
        self.assertEqual(self.added, [])
        self.assertIn(("vless-ws", "u_a.vless-ws"), self.removed)
        self.assertEqual(bot.credential_mode("a"), "emissions")

    def test_revocation_banks_fresh_email_bytes_before_counter_disappears(self):
        self.recipe(3)
        self.user("a", mode="emissions", used=300)
        self.assertTrue(bot.xr_add_user("a", "unused"))
        creds = bot._emission_credential_map("a")
        gone = creds[("vless-ws", 2)]["email"]
        live = [creds[("vless-ws", i)]["email"] for i in (0, 1)]
        c = bot.db(); c.execute("UPDATE users SET usage_anchor=0 WHERE token='a'")
        c.executemany("INSERT INTO usage_ledger(email,token,last_raw,total_bytes) VALUES(?,'a',100,100)",
                      [(gone,), (live[0],), (live[1],)])
        c.commit(); c.close()
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 350},
                   {gone: 150, live[0]: 100, live[1]: 100}))
        self.recipe(2)
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 200},
                   {live[0]: 100, live[1]: 100}))
        bot.refresh_usage("a")
        c = bot.db(); used = c.execute("SELECT used_bytes FROM users WHERE token='a'").fetchone()[0]; c.close()
        self.assertEqual(used, 350)

    def test_failed_stats_read_defers_revocation(self):
        self.recipe(3)
        self.user("a", mode="emissions")
        self.assertTrue(bot.xr_add_user("a", "unused"))
        self.recipe(2)
        self.patch("xr_usage_all", lambda: None)
        self.patch("_emergency_clear_dynamic_users", lambda: self.fail("must preserve unbanked counters"))
        self.assertFalse(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.assertEqual(self.removed, [])
        self.assertEqual(bot.meta_get("membership_sync_pending"), "1")

    def test_failed_legacy_anchor_read_defers_restart_and_revocation(self):
        self.user("a", used=100)
        c = bot.db(); c.execute("UPDATE users SET last_raw=100 WHERE token='a'"); c.commit(); c.close()
        self.recipe(1)
        self.patch("xr_usage_all", lambda: None)
        self.patch("_emergency_clear_dynamic_users", lambda: self.fail("must keep old counters"))
        self.assertFalse(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.assertEqual(self.removed, [])
        self.assertEqual(bot.meta_get("membership_sync_pending"), "1")

    def test_unstable_stats_epoch_defers_revocation(self):
        self.recipe(3)
        self.user("a", mode="emissions")
        self.assertTrue(bot.xr_add_user("a", "unused"))
        self.recipe(2)
        self.patch("_stats_epoch_still_running", lambda pid: False)
        self.patch("_emergency_clear_dynamic_users", lambda: self.fail("must wait for stable stats"))
        self.assertFalse(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.assertEqual(self.removed, [])

    def test_missing_revoked_email_with_live_survivor_restarts_once(self):
        self.recipe(3)
        self.user("a", mode="emissions", used=300)
        self.assertTrue(bot.xr_add_user("a", "unused"))
        creds = bot._emission_credential_map("a")
        gone = creds[("vless-ws", 2)]["email"]
        live = creds[("vless-ws", 0)]["email"]
        c = bot.db(); c.execute("UPDATE users SET usage_anchor=0 WHERE token='a'")
        c.executemany("INSERT INTO usage_ledger(email,token,last_raw,total_bytes) VALUES(?,'a',100,100)",
                      [(r["email"],) for r in creds.values()])
        c.commit(); c.close()
        pid = {"value": "old-pid"}; restarts = []
        self.patch("xray_pid", lambda: pid["value"])
        bot.meta_set("xray_pid", pid["value"])
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot(
            {"a": 110} if pid["value"] == "old-pid" else {},
            {live: 110} if pid["value"] == "old-pid" else {}, pid["value"]))
        self.recipe(2)
        def emergency():
            restarts.append(1)
            pid["value"] = "new-pid"
            bot.resync_all(allow_missing_stats_restart=False)
            return True
        self.patch("_emergency_clear_dynamic_users", emergency)
        self.assertTrue(bot.resync_all())
        self.assertEqual(restarts, [1])
        self.assertIn(("vless-ws", gone), self.removed)
        self.assertEqual(bot.meta_get("membership_sync_pending"), "")

    def test_legacy_link_keeps_imported_identity_for_non_port_edit(self):
        self.user("a")
        bot.set_endpoint_settings({"vless-ws": {"label": "Renamed"}})
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "00000000-0000-4000-8000-000000000001"))
        self.assertEqual(bot.credential_mode("a"), "legacy")
        self.assertEqual(len(self.added), 0)

    def test_removing_one_port_migrates_legacy_and_cuts_old_uri(self):
        self.user("a")
        self.patch("xr_online_map", lambda: {"a": {"vless-ws"}})
        bot.set_endpoint_settings({"vless-ws": {"tls_ports": [2053]}})
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "00000000-0000-4000-8000-000000000001"))
        self.assertEqual(bot.credential_mode("a"), "legacy")
        self.assertIn("vless-ws", bot.emission_mode_tags("a"))
        self.assertEqual({key[1] for key in bot._emission_credential_map("a")}, {0, 1})
        self.assertIn(("vless-ws", "u_a.vless-ws"), self.removed)
        self.assertTrue(any("vless-ws" in tags for tags in self.kicked))
        raw = base64.b64decode(open(bot.sub_path("a")).read()).decode()
        self.assertIn(":2053?", raw)
        self.assertNotIn(":443?", raw)

    def test_disabling_whole_protocol_keeps_other_legacy_uris(self):
        bot.set_ips(["1.1.1.1"])
        self.recipe(2, vmess_count=1)
        self.user("a")
        self.recipe(0, vmess_count=1)
        self.patch("xr_online_map", lambda: {"a": {"vless-ws"}})
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.assertEqual(bot.credential_mode("a"), "legacy")
        self.assertIn(("vless-ws", "u_a.vless-ws"), self.removed)
        self.assertNotIn(("vmess-ws", "u_a.vmess-ws"), self.removed)
        c = bot.db(); u = c.execute("SELECT active_slots FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual(bot._decode_slots(u["active_slots"]), {("vmess-ws", "tls", 443)})
        self.assertIn("vless-ws", bot.emission_mode_tags("a"))
        bot.set_recipe({"vless-ws": {"enabled": True, "count": 2},
                        "vmess-ws": {"enabled": True, "count": 1},
                        "trojan-ws": {"enabled": False, "count": 0}})
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "unused"))
        vless_ids = [secret for tag, secret, _ in self.added if tag == "vless-ws"]
        self.assertTrue(vless_ids)
        self.assertNotIn("00000000-0000-4000-8000-000000000001", vless_ids)

    def test_removed_slot_credential_never_revives_on_reenable(self):
        self.user("a", mode="slots")
        self.assertTrue(bot.xr_add_user("a", "unused"))
        old = bot._slot_credential_map("a")[("vless-ws", "tls", 443)]
        bot.set_endpoint_settings({"vless-ws": {"tls_ports": [2053]}})
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.assertIn(("vless-ws", old["email"]), self.removed)
        self.assertNotIn(("vless-ws", "tls", 443), bot._slot_credential_map("a"))
        converted = bot._emission_credential_map("a")[("vless-ws", 0)]
        bot.set_endpoint_settings({"vless-ws": {"tls_ports": [443, 2053]}})
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "unused"))
        new = bot._emission_credential_map("a")[("vless-ws", 0)]
        self.assertNotEqual(old["secret"], new["secret"])
        self.assertNotEqual(old["email"], new["email"])
        self.assertIn(("vless-ws", converted["email"]), self.removed)

    def test_per_email_ledger_keeps_survivor_growth_when_slot_disappears(self):
        self.user("a", mode="slots", used=200)
        gone, live = "u_a.vless-ws.tls443.old", "u_a.vless-ws.tls2053.live"
        c = bot.db()
        c.execute("UPDATE users SET usage_anchor=0 WHERE token='a'")
        c.executemany("INSERT INTO usage_ledger(email,token,last_raw,total_bytes) VALUES(?,'a',100,100)",
                      [(gone,), (live,)])
        c.commit(); c.close()
        # Xray might retain a revoked email's counter briefly. Only the live
        # credential's extra 10 bytes should be counted.
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 210}, {gone: 100, live: 110}))
        bot.refresh_all_usage()
        c = bot.db(); u = c.execute("SELECT used_bytes FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual(u["used_bytes"], 210)
        # The removed counter then vanishes, and the survivor has transferred
        # well beyond the old aggregate. Per-email deltas still count exactly.
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 250}, {live: 250}))
        bot.refresh_all_usage()
        c = bot.db(); u = c.execute("SELECT used_bytes FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual(u["used_bytes"], 350)

    def test_single_user_refresh_uses_ledger_after_counter_disappears(self):
        self.user("a", mode="slots", used=200)
        gone, live = "u_a.vless-ws.tls443.old", "u_a.vless-ws.tls2053.live"
        c = bot.db()
        c.execute("UPDATE users SET usage_anchor=0 WHERE token='a'")
        c.executemany("INSERT INTO usage_ledger(email,token,last_raw,total_bytes) VALUES(?,'a',100,100)",
                      [(gone,), (live,)])
        c.commit(); c.close()
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 110}, {live: 110}))
        bot.refresh_usage("a")
        c = bot.db(); u = c.execute("SELECT used_bytes FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual(u["used_bytes"], 210)

    def test_failed_delete_is_retained_and_retried(self):
        self.user("a", mode="slots")
        self.assertTrue(bot.xr_add_user("a", "unused"))
        self.patch("_rmu_email", lambda tag, email: False)
        self.patch("_emergency_clear_dynamic_users", lambda: False)
        self.assertFalse(bot.delete_user("a"))
        c = bot.db(); u = c.execute("SELECT * FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual((u["pending_delete"], u["auth_pending"]), (1, 1))
        self.assertFalse(bot._eligible_for_membership(u))
        self.patch("_rmu_email", lambda tag, email: True)
        self.assertTrue(bot.delete_user("a"))
        c = bot.db(); u = c.execute("SELECT token FROM users WHERE token='a'").fetchone(); c.close()
        self.assertIsNone(u)

    def test_failed_freeze_removal_stays_frozen_for_retry(self):
        self.user("a", mode="slots")
        self.assertTrue(bot.xr_add_user("a", "unused"))
        self.patch("_rmu_email", lambda tag, email: False)
        self.patch("_emergency_clear_dynamic_users", lambda: False)
        self.assertFalse(bot.freeze_user("a"))
        c = bot.db(); u = c.execute("SELECT frozen,auth_pending FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual((u["frozen"], u["auth_pending"]), (1, 1))

    def test_restart_fallback_finalizes_delete_after_api_failure(self):
        self.user("a", mode="slots")
        self.patch("_rmu_email", lambda tag, email: False)
        self.patch("_emergency_clear_dynamic_users", lambda: True)
        self.assertTrue(bot.delete_user("a"))
        c = bot.db(); u = c.execute("SELECT token FROM users WHERE token='a'").fetchone(); c.close()
        self.assertIsNone(u)

    def test_restart_fallback_restores_only_eligible_links(self):
        self.user("a", mode="slots")
        self.user("b", mode="slots")
        self.assertTrue(bot.xr_add_user("a", "unused"))
        self.assertTrue(bot.xr_add_user("b", "unused"))
        self.added.clear()
        self.patch("_rmu_email", lambda tag, email: False)
        self.patch("xray_pid", lambda: "new-pid")
        commands = []
        self.patch("refresh_all_usage", lambda: commands.append("usage"))
        self.patch("subprocess", type("RunStub", (), {"run": staticmethod(
            lambda cmd, **kwargs: commands.append("restart") or subprocess.CompletedProcess(cmd, 0, "", ""))}))
        self.assertTrue(bot.freeze_user("a"))
        self.assertEqual(commands, ["usage", "restart"])
        self.assertEqual({email.split(".")[0] for _, _, email in self.added}, {"u_b"})

    def test_missing_legacy_stats_defers_rotation_and_resync_retries(self):
        self.user("a")
        self.patch("_emergency_clear_dynamic_users", lambda: False)
        c = bot.db(); c.execute("UPDATE users SET last_raw=100,used_bytes=100 WHERE token='a'"); c.commit(); c.close()
        self.patch("xray_pid", lambda: "same-pid")
        bot.meta_set("xray_pid", "same-pid")
        bot.set_endpoint_settings({"vless-ws": {"tls_ports": [2053]}})
        self.assertFalse(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.assertEqual(bot.meta_get("membership_sync_pending"), "1")
        self.assertNotIn(("vless-ws", "u_a.vless-ws"), self.removed)
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot(
            {"a": 100}, {"u_a.vless-ws": 100}))
        self.assertTrue(bot.resync_all())
        self.assertEqual(bot.meta_get("membership_sync_pending"), "")
        self.assertIn(("vless-ws", "u_a.vless-ws"), self.removed)

    def test_resync_restarts_once_for_persistently_missing_legacy_stats(self):
        self.user("a", used=100)
        c = bot.db(); c.execute("UPDATE users SET last_raw=100 WHERE token='a'"); c.commit(); c.close()
        bot.meta_set("xray_pid", "old-pid")
        bot.set_endpoint_settings({"vless-ws": {"tls_ports": [2053]}})
        pid = {"value": "old-pid"}
        self.patch("xray_pid", lambda: pid["value"])
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({}, {}, pid["value"]))
        restarts = []
        def run(cmd, **kwargs):
            restarts.append(cmd)
            pid["value"] = "new-pid"
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.patch("subprocess", type("RunStub", (), {"run": staticmethod(run)}))
        self.assertTrue(bot.resync_all())
        self.assertEqual(restarts, [["systemctl", "restart", bot.XRAY_SERVICE]])
        c = bot.db(); u = c.execute("SELECT used_bytes,usage_anchor,active_slots FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual((u["used_bytes"], u["usage_anchor"]), (100, 100))
        self.assertEqual(bot._decode_slots(u["active_slots"]), {("vless-ws", "tls", 2053)})
        self.assertEqual(bot.meta_get("membership_sync_pending"), "")

    def test_resync_waits_if_missing_legacy_stat_reappears(self):
        self.user("a", used=100)
        c = bot.db(); c.execute("UPDATE users SET last_raw=100 WHERE token='a'"); c.commit(); c.close()
        bot.meta_set("xray_pid", "same-pid")
        bot.set_endpoint_settings({"vless-ws": {"tls_ports": [2053]}})
        self.patch("xray_pid", lambda: "same-pid")
        polls = iter([{}, {}, {"a": 100}])
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot(next(polls), {}, "same-pid"))
        self.patch("_emergency_clear_dynamic_users", lambda: self.fail("restart should wait"))
        self.assertFalse(bot.resync_all())
        self.assertEqual(bot.meta_get("membership_sync_pending"), "1")

    def test_failed_settings_rmu_restarts_and_revokes_old_identity(self):
        self.user("a")
        bot.set_endpoint_settings({"vless-ws": {"tls_ports": [2053]}})
        prior_remove = bot._rmu_email
        first = {"pending": True}
        self.patch("_rmu_email", lambda tag, email: False if first["pending"] else prior_remove(tag, email))
        self.patch("xray_pid", lambda: "new-pid")
        self.patch("subprocess", type("RunStub", (), {"run": staticmethod(
            lambda cmd, **kwargs: first.update(pending=False) or subprocess.CompletedProcess(cmd, 0, "", ""))}))
        self.assertTrue(bot.xr_reconcile_user_endpoints("a", "unused"))
        self.assertIn("vless-ws", bot.emission_mode_tags("a"))
        self.assertEqual(bot.meta_get("membership_sync_pending"), "")
        c = bot.db(); u = c.execute("SELECT active_slots FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual(bot._decode_slots(u["active_slots"]), {("vless-ws", "tls", 2053)})

    def test_new_xray_pid_banks_ledger_counters_before_new_epoch(self):
        self.user("a", mode="slots", used=100)
        email = "u_a.vless-ws.tls443.old"
        c = bot.db()
        c.execute("UPDATE users SET usage_anchor=0 WHERE token='a'")
        c.execute("INSERT INTO usage_ledger(email,token,last_raw,total_bytes) VALUES(?,'a',100,100)", (email,))
        c.commit(); c.close()
        bot.meta_set("xray_pid", "old-pid")
        self.patch("xray_pid", lambda: "new-pid")
        self.assertTrue(bot.begin_xray_counter_epoch())
        self.assertFalse(bot.begin_xray_counter_epoch())
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 150}, {email: 150}))
        bot.refresh_usage("a")
        c = bot.db(); u = c.execute("SELECT used_bytes FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual(u["used_bytes"], 250)

    def test_bulk_refresh_observes_pid_epoch_before_new_raw(self):
        self.user("a", used=100)
        c = bot.db(); c.execute("UPDATE users SET last_raw=100 WHERE token='a'"); c.commit(); c.close()
        bot.meta_set("xray_pid", "old-pid")
        self.patch("xray_pid", lambda: "new-pid")
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 150}, {"u_a.vless-ws": 150}, "new-pid"))
        bot.refresh_all_usage()
        bot.refresh_all_usage()
        c = bot.db(); u = c.execute("SELECT used_bytes,base_bytes,last_raw FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual((u["used_bytes"], u["base_bytes"], u["last_raw"]), (250, 100, 150))

    def test_ledger_anchor_observes_new_pid_before_snapshot(self):
        self.user("a", used=100)
        c = bot.db(); c.execute("UPDATE users SET last_raw=100 WHERE token='a'"); c.commit(); c.close()
        bot.meta_set("xray_pid", "old-pid")
        self.patch("xray_pid", lambda: "new-pid")
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 150}, {"u_a.vless-ws": 150}, "new-pid"))
        self.assertTrue(bot._enable_usage_ledger("a"))
        c = bot.db()
        u = c.execute("SELECT used_bytes,usage_anchor FROM users WHERE token='a'").fetchone()
        ledger = c.execute("SELECT last_raw,total_bytes FROM usage_ledger WHERE email='u_a.vless-ws'").fetchone()
        c.close()
        self.assertEqual((u["used_bytes"], u["usage_anchor"]), (250, 250))
        self.assertEqual((ledger["last_raw"], ledger["total_bytes"]), (150, 0))

    def test_statsquery_discards_result_when_pid_changes_during_read(self):
        self.user("a", used=100)
        c = bot.db(); c.execute("UPDATE users SET last_raw=100 WHERE token='a'"); c.commit(); c.close()
        bot.meta_set("xray_pid", "old-pid")
        pids = iter(["old-pid", "new-pid", "new-pid", "new-pid"])
        self.patch("xray_pid", lambda: next(pids))
        values = iter([100, 150])
        def run(cmd, **kwargs):
            value = next(values)
            data = {"stat": [{"name": "user>>>u_a.vless-ws>>>traffic>>>downlink", "value": value}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")
        self.patch("subprocess", type("RunStub", (), {"run": staticmethod(run)}))
        self.patch("xr_usage_all", self.saved["xr_usage_all"])
        snapshot = bot.xr_usage_all()
        self.assertEqual(dict(snapshot), {"a": 150})
        self.assertEqual(snapshot.epoch_pid, "new-pid")
        c = bot.db(); u = c.execute("SELECT base_bytes,last_raw FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual((u["base_bytes"], u["last_raw"]), (100, 0))

    def test_single_refresh_reloads_user_after_midread_pid_change(self):
        self.user("a", used=100)
        c = bot.db(); c.execute("UPDATE users SET last_raw=100 WHERE token='a'"); c.commit(); c.close()
        bot.meta_set("xray_pid", "old-pid")
        pid = {"value": "old-pid"}
        self.patch("xray_pid", lambda: pid["value"])
        def read_new_raw(token):
            pid["value"] = "new-pid"
            bot.begin_xray_counter_epoch()
            return bot.UsageValue(150, "new-pid")
        self.patch("xr_usage", read_new_raw)
        bot.refresh_usage("a")
        c = bot.db(); u = c.execute("SELECT used_bytes,base_bytes FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual((u["used_bytes"], u["base_bytes"]), (250, 100))

    def test_legacy_freeze_renewal_uses_new_stats_email_with_same_secret(self):
        self.user("a", used=100)
        c = bot.db(); c.execute("UPDATE users SET last_raw=100 WHERE token='a'"); c.commit(); c.close()
        old = "u_a.vless-ws"
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 100}, {old: 100}))
        self.assertTrue(bot.freeze_user("a"))
        new = bot.legacy_email("a", "vless-ws")
        self.assertNotEqual(new, old)
        self.assertTrue(bot.unfreeze_user("a"))
        self.assertIn(("vless-ws", "00000000-0000-4000-8000-000000000001", new), self.added)
        self.patch("xr_usage_all", lambda: bot.UsageSnapshot({"a": 250}, {old: 100, new: 150}))
        bot.refresh_usage("a")
        c = bot.db(); u = c.execute("SELECT used_bytes FROM users WHERE token='a'").fetchone(); c.close()
        self.assertEqual(u["used_bytes"], 250)

    def test_empty_online_map_still_cuts_subscribed_inbound(self):
        self.user("a")
        self.assertEqual(bot.tags_to_cut_for_user("a"), {"vless-ws"})

    def test_resync_skips_ineligible_users(self):
        self.user("active", mode="slots")
        self.user("frozen", mode="slots", frozen=1)
        self.user("exhausted", mode="slots", limit=10, used=10)
        bot.resync_all()
        self.assertEqual({email.split(".")[0] for _, _, email in self.added}, {"u_active"})


if __name__ == "__main__":
    unittest.main()
