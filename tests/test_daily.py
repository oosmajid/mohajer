import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402


class TestDayKey(unittest.TestCase):
    def test_epoch_is_first_day(self):
        self.assertEqual(bot.day_key(0), "1970-01-01")

    def test_before_tehran_midnight(self):
        # UTC 20:00 -> +3:30 -> 23:30 -> same day
        self.assertEqual(bot.day_key(20 * 3600), "1970-01-01")

    def test_at_tehran_midnight(self):
        # UTC 20:30 -> +3:30 -> 00:00 -> next day
        self.assertEqual(bot.day_key(20 * 3600 + 1800), "1970-01-02")


class TestDailyRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._orig = bot.DB_PATH
        bot.DB_PATH = self.tmp.name
        bot.init_db()
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,base_bytes,last_raw,used_bytes) "
                  "VALUES('aa','u','u_aa','A',0,0,0,0,0,0)")
        c.commit(); c.close()

    def tearDown(self):
        bot.DB_PATH = self._orig
        os.unlink(self.tmp.name)

    def test_record_and_today_delta(self):
        day = bot.day_key()
        c = bot.db()
        bot.record_daily(c, "aa", 100, day)     # first poll of the day
        bot.record_daily(c, "aa", 500, day)     # later poll
        c.execute("UPDATE users SET used_bytes=500 WHERE token='aa'")
        c.commit(); c.close()
        total, today = bot.panel_usage_summary()
        self.assertEqual(total, 500)
        self.assertEqual(today, 400)            # 500 - 100

    def test_end_used_never_decreases(self):
        day = bot.day_key()
        c = bot.db()
        bot.record_daily(c, "aa", 500, day)
        bot.record_daily(c, "aa", 300, day)     # counter reset attempt -> ignored
        row = c.execute("SELECT start_used,end_used FROM usage_daily WHERE token='aa' AND day=?", (day,)).fetchone()
        c.close()
        self.assertEqual(row["start_used"], 500)
        self.assertEqual(row["end_used"], 500)

    def test_prune_removes_old_days(self):
        c = bot.db()
        c.execute("INSERT INTO usage_daily(token,day,start_used,end_used) VALUES('aa','2000-01-01',0,10)")
        c.execute("INSERT INTO usage_daily(token,day,start_used,end_used) VALUES('aa',?,0,10)", (bot.day_key(),))
        c.commit()
        bot.prune_daily(c, keep_days=30)
        c.commit()
        rows = [r["day"] for r in c.execute("SELECT day FROM usage_daily").fetchall()]
        c.close()
        self.assertIn(bot.day_key(), rows)
        self.assertNotIn("2000-01-01", rows)


class TestEnforcerHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close()
        self._orig = bot.DB_PATH; bot.DB_PATH = self.tmp.name; bot.init_db()
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,base_bytes,last_raw,used_bytes) "
                  "VALUES('bb','u','u_bb','B',0,0,0,0,0,0)")
        c.commit(); c.close()

    def tearDown(self):
        bot.DB_PATH = self._orig; os.unlink(self.tmp.name)

    def test_refresh_all_usage_writes_daily(self):
        day = bot.day_key()
        c = bot.db(); bot.record_daily(c, "bb", 100, day); c.commit(); c.close()  # simulate earlier poll
        bot.xr_usage_all = lambda: {"bb": 250}
        bot.refresh_all_usage()
        total, today = bot.panel_usage_summary()
        self.assertEqual(total, 250)
        self.assertEqual(today, 150)   # 250 - 100


if __name__ == "__main__":
    unittest.main()
