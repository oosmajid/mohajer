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


if __name__ == "__main__":
    unittest.main()
