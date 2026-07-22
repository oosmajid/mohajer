import os, sys, json, time, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402


def fake_stat(mapping):
    # a SUCCESSFUL statsquery result (returncode 0) reporting {token: bytes}
    stats = [{"name": "user>>>u_%s.vless-ws>>>traffic>>>downlink" % t, "value": str(v)}
             for t, v in mapping.items()]
    class R: pass
    r = R(); r.returncode = 0; r.stdout = json.dumps({"stat": stats}); r.stderr = ""
    return lambda *a, **k: r


def fail_exc(*a, **k):
    raise OSError("xray api unreachable")


def fail_rc(*a, **k):
    class R: pass
    r = R(); r.returncode = 1; r.stdout = ""; r.stderr = "connection refused"
    return r


class UsageBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close()
        self._db, self._eps, self._run = bot.DB_PATH, bot.ENDPOINTS, bot.subprocess.run
        bot.DB_PATH = self.tmp.name
        bot.ENDPOINTS = [{"tag": "vless-ws", "port": 10000, "proto": "vless", "net": "ws"}]
        bot.init_db()

    def tearDown(self):
        bot.subprocess.run = self._run
        bot.DB_PATH, bot.ENDPOINTS = self._db, self._eps
        os.unlink(self.tmp.name)

    def _mk(self, token, base, last, used):
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,base_bytes,last_raw,used_bytes)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (token, "u-" + token, "u_" + token, "L", 0, 0, int(time.time()), base, last, used))
        c.commit(); c.close()

    def _row(self, token):
        c = bot.db(); r = c.execute("SELECT base_bytes,last_raw,used_bytes FROM users WHERE token=?", (token,)).fetchone(); c.close()
        return r["used_bytes"], r["base_bytes"], r["last_raw"]


class UsageResetTests(UsageBase):
    def test_failed_read_leaves_usage_untouched(self):
        # steady state: real counter 5000, base 1000, used 6000
        self._mk("a", base=1000, last=5000, used=6000)
        bot.subprocess.run = fail_exc
        bot.refresh_all_usage()
        self.assertEqual(self._row("a"), (6000, 1000, 5000))   # exception -> skip poll
        bot.subprocess.run = fail_rc
        bot.refresh_all_usage()
        self.assertEqual(self._row("a"), (6000, 1000, 5000))   # nonzero returncode -> skip poll

    def test_no_double_count_after_transient_failure(self):
        # THE BUG: a transient failed read used to be read as raw=0 -> "reset" -> base+=last,
        # then the real counter returns and gets double-counted (used jumps by ~last_raw).
        self._mk("a", base=1000, last=5000, used=6000)
        bot.subprocess.run = fail_exc          # transient failure
        bot.refresh_all_usage()
        bot.subprocess.run = fake_stat({"a": 5000})   # real counter unchanged (xray never reset)
        bot.refresh_all_usage()
        used, base, last = self._row("a")
        self.assertEqual(used, 6000)           # fixed: stays 6000 (was 11000 with the bug)
        self.assertEqual((base, last), (1000, 5000))

    def test_missing_token_in_success_is_not_a_reset(self):
        self._mk("a", base=1000, last=5000, used=6000)
        self._mk("b", base=0, last=200, used=200)
        bot.subprocess.run = fake_stat({"b": 250})   # a absent this poll
        bot.refresh_all_usage()
        self.assertEqual(self._row("a")[0], 6000)    # a untouched, NOT reset to base
        self.assertEqual(self._row("b")[0], 250)     # b updated normally

    def test_genuine_reset_still_carries_over(self):
        # real xray restart: a REAL reading (300) below last_raw (5000) is a true reset
        self._mk("a", base=1000, last=5000, used=6000)
        bot.subprocess.run = fake_stat({"a": 300})
        bot.refresh_all_usage()
        used, base, last = self._row("a")
        self.assertEqual((base, last, used), (6000, 300, 6300))

    def test_normal_growth(self):
        self._mk("a", base=1000, last=5000, used=6000)
        bot.subprocess.run = fake_stat({"a": 5500})
        bot.refresh_all_usage()
        self.assertEqual(self._row("a")[0], 6500)

    def test_single_user_refresh_failed_read_untouched(self):
        self._mk("a", base=1000, last=5000, used=6000)
        bot.subprocess.run = fail_exc
        bot.refresh_usage("a")
        self.assertEqual(self._row("a"), (6000, 1000, 5000))


if __name__ == "__main__":
    unittest.main()
