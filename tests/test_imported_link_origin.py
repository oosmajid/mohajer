import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot


class ImportedLinkOriginTest(unittest.TestCase):
    def test_imported_url_survives_default_change_and_db_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            old = bot.DB_PATH, bot.SUB_BASE
            try:
                bot.DB_PATH = os.path.join(directory, "users.db")
                bot.init_db()
                bot.SUB_BASE = "https://cdn2.example.org"
                bot.meta_set("sub_base_imported", "https://cdn3.example.org")
                self.assertEqual(bot.sub_url("imported"), "https://cdn3.example.org/sub-u-imported")
                self.assertEqual(bot.sub_url("new"), "https://cdn2.example.org/sub-u-new")
                bot.SUB_BASE = "https://replacement.example.org"
                self.assertEqual(bot.sub_url("imported"), "https://cdn3.example.org/sub-u-imported")
            finally:
                bot.DB_PATH, bot.SUB_BASE = old

    def test_imported_endpoint_keeps_provisioned_host_as_its_default(self):
        endpoint = {"tag": "imported-ws", "host": "cdn3.example.org", "sni": "cdn3.example.org"}
        settings = bot._endpoint_defaults(endpoint)
        self.assertEqual(settings["host"], "cdn3.example.org")
        self.assertEqual(settings["sni"], "cdn3.example.org")


if __name__ == "__main__":
    unittest.main()
