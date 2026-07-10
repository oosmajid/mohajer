import os, sys, time, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402


class FreezeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close()
        self._db, self._eps = bot.DB_PATH, bot.ENDPOINTS
        bot.DB_PATH = self.tmp.name
        bot.ENDPOINTS = [{"tag": "vless-ws", "port": 10000, "proto": "vless", "net": "ws"}]
        bot.init_db()
        # stub every xray/network side-effect so we test pure state transitions
        self.added, self.removed, self.wrote, self.cut = [], [], [], []
        self._save = {}
        for name, fn in [
            ("xr_add_user",    lambda t, s: self.added.append(t)),
            ("xr_remove_user", lambda t: self.removed.append(t)),
            ("write_sub",      lambda t, s, l: self.wrote.append(t)),
            ("online_tags_of", lambda t: set()),
            ("force_disconnect", lambda tags: self.cut.append(tags)),
        ]:
            self._save[name] = getattr(bot, name); setattr(bot, name, fn)

    def tearDown(self):
        for k, v in self._save.items(): setattr(bot, k, v)
        bot.DB_PATH, bot.ENDPOINTS = self._db, self._eps
        os.unlink(self.tmp.name)

    def _mk(self, token="t1", limit=0, expiry=0, used=0, disabled=0, frozen=0):
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,used_bytes,disabled_ts,frozen)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (token, "uuid-" + token, "u_" + token, "L", limit, expiry, int(time.time()), used, disabled, frozen))
        c.commit(); c.close()

    def _frozen(self, token="t1"):
        c = bot.db(); r = c.execute("SELECT frozen FROM users WHERE token=?", (token,)).fetchone(); c.close()
        return r["frozen"]


class FreezeTests(FreezeBase):
    def test_migration_added_frozen_column(self):
        cols = [r[1] for r in bot.db().execute("PRAGMA table_info(users)").fetchall()]
        self.assertIn("frozen", cols)

    def test_freeze_sets_flag_rmus_and_cuts_session(self):
        self._mk()
        bot.freeze_user("t1")
        self.assertEqual(self._frozen(), 1)
        self.assertIn("t1", self.removed)      # removed from xray
        self.assertEqual(len(self.cut), 1)     # live session dropped

    def test_unfreeze_clears_flag_and_readds_when_valid(self):
        self._mk(frozen=1)
        bot.unfreeze_user("t1")
        self.assertEqual(self._frozen(), 0)
        self.assertIn("t1", self.added)        # brought back live
        self.assertIn("t1", self.wrote)        # sub regenerated

    def test_unfreeze_does_not_readd_if_exhausted(self):
        self._mk(frozen=1, limit=100, used=200)   # over quota
        bot.unfreeze_user("t1")
        self.assertEqual(self._frozen(), 0)
        self.assertNotIn("t1", self.added)     # exhausted -> enforcer will disable, don't re-add

    def test_unfreeze_does_not_readd_if_also_disabled(self):
        self._mk(frozen=1, disabled=123456)
        bot.unfreeze_user("t1")
        self.assertNotIn("t1", self.added)

    def test_resync_all_skips_frozen(self):
        self._mk(token="a", frozen=1)
        self._mk(token="b", frozen=0)
        bot.resync_all()
        self.assertNotIn("a", self.added)      # frozen stays out of xray across restarts
        self.assertIn("b", self.added)


if __name__ == "__main__":
    unittest.main()
