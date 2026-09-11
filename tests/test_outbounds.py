import os, sys, json, base64, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402


class ParseLinkTests(unittest.TestCase):
    def test_vless_ws_tls(self):
        ob = bot.parse_outbound_link(
            "vless://11111111-2222-3333-4444-555555555555@1.2.3.4:443"
            "?type=ws&security=tls&host=a.example.com&sni=a.example.com&path=%2Fxyz&encryption=none#name", "clean")
        self.assertEqual(ob["tag"], "clean")
        self.assertEqual(ob["protocol"], "vless")
        v = ob["settings"]["vnext"][0]
        self.assertEqual((v["address"], v["port"]), ("1.2.3.4", 443))
        self.assertEqual(v["users"][0]["id"], "11111111-2222-3333-4444-555555555555")
        st = ob["streamSettings"]
        self.assertEqual(st["network"], "ws")
        self.assertEqual(st["security"], "tls")
        self.assertEqual(st["tlsSettings"]["serverName"], "a.example.com")
        self.assertEqual(st["wsSettings"]["path"], "/xyz")
        self.assertEqual(st["wsSettings"]["headers"]["Host"], "a.example.com")

    def test_trojan(self):
        ob = bot.parse_outbound_link("trojan://secretpw@5.6.7.8:8443?security=tls&sni=b.example.com#x", "t1")
        self.assertEqual(ob["protocol"], "trojan")
        s = ob["settings"]["servers"][0]
        self.assertEqual((s["address"], s["port"], s["password"]), ("5.6.7.8", 8443, "secretpw"))
        self.assertEqual(ob["streamSettings"]["tlsSettings"]["serverName"], "b.example.com")

    def test_shadowsocks_userinfo_form(self):
        userinfo = base64.urlsafe_b64encode(b"aes-128-gcm:pw123").decode().rstrip("=")
        ob = bot.parse_outbound_link("ss://%s@9.9.9.9:8388#n" % userinfo, "ss1")
        s = ob["settings"]["servers"][0]
        self.assertEqual(ob["protocol"], "shadowsocks")
        self.assertEqual((s["address"], s["port"], s["method"], s["password"]),
                         ("9.9.9.9", 8388, "aes-128-gcm", "pw123"))

    def test_shadowsocks_fully_encoded_form(self):
        raw = base64.urlsafe_b64encode(b"aes-256-gcm:pw@9.9.9.9:8388").decode().rstrip("=")
        ob = bot.parse_outbound_link("ss://%s#n" % raw, "ss2")
        s = ob["settings"]["servers"][0]
        self.assertEqual((s["address"], s["port"], s["method"], s["password"]),
                         ("9.9.9.9", 8388, "aes-256-gcm", "pw"))

    def test_socks_with_auth(self):
        ob = bot.parse_outbound_link("socks://user:p%40ss@10.0.0.1:1080", "sk")
        self.assertEqual(ob["protocol"], "socks")
        s = ob["settings"]["servers"][0]
        self.assertEqual((s["address"], s["port"]), ("10.0.0.1", 1080))
        self.assertEqual(s["users"][0], {"user": "user", "pass": "p@ss"})

    def test_http_proxy(self):
        ob = bot.parse_outbound_link("http://10.0.0.2:3128", "hp")
        self.assertEqual(ob["protocol"], "http")
        self.assertNotIn("users", ob["settings"]["servers"][0])

    def test_bad_scheme_and_missing_parts_raise(self):
        for bad in ("ftp://1.2.3.4:21", "vless://@1.2.3.4:443", "vless://uuid@:443", "notalink"):
            with self.assertRaises(ValueError):
                bot.parse_outbound_link(bad, "x")


class DomainParseTests(unittest.TestCase):
    def test_splits_and_keeps_prefixes(self):
        self.assertEqual(
            bot.parse_domains("geosite:google\n gemini.google.com , claude.ai\n\n#comment"),
            ["geosite:google", "gemini.google.com", "claude.ai"])

    def test_empty(self):
        self.assertEqual(bot.parse_domains("  \n "), [])


class BuildSectionsTests(unittest.TestCase):
    OB = [{"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": ["geosite:google", "claude.ai"]}]

    def test_direct_is_default_first_outbound(self):
        outs, rules, tests = bot.build_xray_sections(self.OB)
        self.assertEqual(outs[0]["tag"], "direct")          # first = default route
        self.assertEqual(outs[0]["protocol"], "freedom")
        self.assertEqual(outs[-1]["tag"], "block")
        self.assertIn("mj-clean", [o["tag"] for o in outs])

    def test_test_inbound_is_loopback_only_and_routed_to_its_outbound(self):
        outs, rules, tests = bot.build_xray_sections(self.OB)
        self.assertEqual(len(tests), 1)
        self.assertEqual(tests[0]["listen"], "127.0.0.1")   # never exposed publicly
        self.assertEqual(tests[0]["port"], bot.ob_test_port(0))
        self.assertEqual(rules[0], {"type": "field", "inboundTag": ["mjtest-clean"], "outboundTag": "mj-clean"})

    def test_domain_rule_present(self):
        outs, rules, tests = bot.build_xray_sections(self.OB)
        dom = [r for r in rules if "domain" in r]
        self.assertEqual(dom[0]["domain"], ["geosite:google", "claude.ai"])
        self.assertEqual(dom[0]["outboundTag"], "mj-clean")

    def test_no_outbounds_means_no_rules(self):
        outs, rules, tests = bot.build_xray_sections([])
        self.assertEqual(rules, [])
        self.assertEqual(tests, [])
        self.assertEqual([o["tag"] for o in outs], ["direct", "block"])


class CatchAllTests(unittest.TestCase):
    """No domains listed = send EVERYTHING through that outbound."""
    ALL = {"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": []}
    SOME = {"tag": "ai", "link": "socks://5.6.7.8:1080", "domains": ["claude.ai"]}

    def test_empty_domains_makes_it_the_default_outbound(self):
        outs, rules, tests = bot.build_xray_sections([self.ALL])
        self.assertEqual(outs[0]["tag"], "mj-clean")        # first outbound = xray's default route
        self.assertIn("direct", [o["tag"] for o in outs])
        self.assertTrue(any(r.get("ip") == ["geoip:private"] and r["outboundTag"] == "direct" for r in rules))

    def test_domain_rules_still_beat_the_catch_all(self):
        outs, rules, _ = bot.build_xray_sections([self.ALL, self.SOME])
        self.assertEqual(outs[0]["tag"], "mj-clean")
        dom = [r for r in rules if "domain" in r]
        self.assertEqual((dom[0]["domain"], dom[0]["outboundTag"]), (["claude.ai"], "mj-ai"))
        # a routing rule is evaluated before the default, so claude.ai leaves via "ai"
        self.assertLess(rules.index(dom[0]), len(rules))

    def test_first_empty_one_wins(self):
        second = {"tag": "other", "link": "socks://9.9.9.9:1080", "domains": []}
        outs, _, _ = bot.build_xray_sections([self.ALL, second])
        self.assertEqual(outs[0]["tag"], "mj-clean")
        self.assertEqual(bot.ob_catchall_index([self.ALL, second]), 0)

    def test_all_domains_listed_keeps_direct_default(self):
        outs, _, _ = bot.build_xray_sections([self.SOME])
        self.assertEqual(outs[0]["tag"], "direct")
        self.assertIsNone(bot.ob_catchall_index([self.SOME]))

    def test_test_port_still_follows_list_order(self):
        _, _, tests = bot.build_xray_sections([self.ALL, self.SOME])
        self.assertEqual([t["port"] for t in tests], [bot.ob_test_port(0), bot.ob_test_port(1)])


class ApplyBase(unittest.TestCase):
    """Writes BASE to a temp file and points bot.XRAY_CONF at it; xray calls are stubbed."""
    BASE = {
        "log": {"loglevel": "warning"},
        "api": {"tag": "api", "services": ["StatsService"]},
        "stats": {}, "policy": {"levels": {"0": {"statsUserOnline": True}}},
        "inbounds": [{"tag": "vless-ws", "port": 10000, "protocol": "vless"},
                     {"tag": "api", "port": 10085, "protocol": "dokodemo-door"}],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
    }

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(self.BASE, self.tmp); self.tmp.close()
        self._conf, self._run = bot.XRAY_CONF, bot.subprocess.run
        self._wait, bot.OB_BIND_WAIT = bot.OB_BIND_WAIT, 0   # nothing to wait for; xray is stubbed
        bot.XRAY_CONF = self.tmp.name

    def tearDown(self):
        bot.subprocess.run = self._run
        bot.XRAY_CONF, bot.OB_BIND_WAIT = self._conf, self._wait
        for p in (self.tmp.name, self.tmp.name + ".mjnew.json"):
            if os.path.exists(p): os.unlink(p)
        d, base = os.path.dirname(self.tmp.name), os.path.basename(self.tmp.name)
        for f in os.listdir(d):
            if f.startswith(base + ".bak."): os.unlink(os.path.join(d, f))

    def _stub_run(self, test_rc=0):
        self.cmds = []
        def run(cmd, *a, **k):
            self.cmds.append(list(cmd))
            class R: pass
            r = R(); r.returncode = (test_rc if "-test" in cmd else 0); r.stdout = ""; r.stderr = "bad config"
            return r
        bot.subprocess.run = run


class ApplyConfigTests(ApplyBase):
    """apply_xray_outbounds must preserve real inbounds/api/policy and never write a broken config."""

    def test_apply_preserves_real_inbounds_and_api(self):
        self._stub_run()
        ok, msg = bot.apply_xray_outbounds([{"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": ["claude.ai"]}])
        self.assertTrue(ok, msg)
        cfg = json.load(open(self.tmp.name))
        tags = [i["tag"] for i in cfg["inbounds"]]
        self.assertIn("vless-ws", tags)          # endpoint inbound untouched
        self.assertIn("api", tags)               # api inbound untouched
        self.assertIn("mjtest-clean", tags)      # our loopback test inbound added
        self.assertEqual(cfg["policy"]["levels"]["0"]["statsUserOnline"], True)  # policy preserved
        self.assertEqual(cfg["routing"]["rules"][-1]["domain"], ["claude.ai"])

    def test_validated_file_keeps_a_json_extension(self):
        # xray picks the config format from the file EXTENSION; a temp named ".mjnew"
        # made `xray -test` fail with "failed to get format of ...", so every apply died.
        self._stub_run()
        bot.apply_xray_outbounds([{"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": []}])
        tested = [c for c in self.cmds if "-test" in c]
        self.assertTrue(tested, "xray -test was never run")
        self.assertTrue(tested[0][-1].endswith(".json"), tested[0])

    def test_domain_strategy_does_not_force_dns_on_every_connection(self):
        # IPIfNonMatch resolves the destination for every connection that matches no rule,
        # which taxes ALL traffic (latency) for zero benefit to our domain-only rules.
        self._stub_run()
        bot.apply_xray_outbounds([{"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": ["claude.ai"]}])
        self.assertEqual(json.load(open(self.tmp.name))["routing"]["domainStrategy"], "AsIs")

    def test_invalid_config_is_never_written(self):
        self._stub_run(test_rc=1)               # xray -test fails
        before = open(self.tmp.name).read()
        ok, msg = bot.apply_xray_outbounds([{"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": []}])
        self.assertFalse(ok)
        self.assertIn("نامعتبر", msg)
        self.assertEqual(open(self.tmp.name).read(), before)   # untouched
        self.assertFalse(os.path.exists(self.tmp.name + ".mjnew.json"))

    def test_bad_link_rejected_before_touching_config(self):
        self._stub_run()
        before = open(self.tmp.name).read()
        ok, msg = bot.apply_xray_outbounds([{"tag": "x", "link": "ftp://1.2.3.4:21", "domains": []}])
        self.assertFalse(ok)
        self.assertEqual(open(self.tmp.name).read(), before)

    def test_removing_outbounds_drops_routing_and_test_inbounds(self):
        self._stub_run()
        bot.apply_xray_outbounds([{"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": ["claude.ai"]}])
        ok, _ = bot.apply_xray_outbounds([])
        self.assertTrue(ok)
        cfg = json.load(open(self.tmp.name))
        self.assertNotIn("routing", cfg)
        self.assertEqual([i["tag"] for i in cfg["inbounds"]], ["vless-ws", "api"])  # test inbound cleaned up


class PreserveExistingRoutingTests(ApplyBase):
    """The shipped xray config routes the api inbound to the api service. Losing that rule
       kills the gRPC API, and with it the bot's ability to add/remove users."""
    BASE = dict(ApplyBase.BASE,
                routing={"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                                   {"type": "field", "domain": ["ads.example"], "outboundTag": "blocked"}]},
                outbounds=[{"tag": "direct", "protocol": "freedom"},
                           {"tag": "blocked", "protocol": "blackhole"}])

    def _apply(self, obs):
        self._stub_run()
        ok, msg = bot.apply_xray_outbounds(obs)
        self.assertTrue(ok, msg)
        return json.load(open(self.tmp.name))

    def test_api_rule_and_foreign_outbound_survive(self):
        cfg = self._apply([{"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": ["claude.ai"]}])
        self.assertEqual(cfg["routing"]["rules"][0], {"type": "field", "inboundTag": ["api"], "outboundTag": "api"})
        self.assertIn("blocked", [o["tag"] for o in cfg["outbounds"]])
        self.assertTrue(any(r.get("domain") == ["ads.example"] for r in cfg["routing"]["rules"]))
        self.assertEqual(cfg["outbounds"][-1]["tag"], "block")

    def test_api_rule_survives_removing_every_outbound(self):
        self._apply([{"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": ["claude.ai"]}])
        cfg = self._apply([])
        self.assertTrue(any(r.get("outboundTag") == "api" for r in cfg["routing"]["rules"]))

    def test_stale_rules_for_deleted_outbounds_are_dropped(self):
        self._apply([{"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": ["claude.ai"]}])
        cfg = self._apply([])          # "clean" is gone; its domain rule must go with it
        self.assertFalse(any(r.get("outboundTag") == "mj-clean" for r in cfg["routing"]["rules"]))
        self.assertNotIn("mj-clean", [o["tag"] for o in cfg["outbounds"]])

    def test_our_rules_are_not_duplicated_on_reapply(self):
        obs = [{"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": ["claude.ai"]}]
        self._apply(obs)
        cfg = self._apply(obs)
        self.assertEqual(len([r for r in cfg["routing"]["rules"] if r.get("domain") == ["claude.ai"]]), 1)
        self.assertEqual(len([r for r in cfg["routing"]["rules"] if r.get("inboundTag") == ["mjtest-clean"]]), 1)
        self.assertEqual(len([o for o in cfg["outbounds"] if o["tag"] == "mj-clean"]), 1)

    def test_catch_all_outbound_is_first_in_written_config(self):
        cfg = self._apply([{"tag": "clean", "link": "socks://1.2.3.4:1080", "domains": []}])
        self.assertEqual(cfg["outbounds"][0]["tag"], "mj-clean")


if __name__ == "__main__":
    unittest.main()
