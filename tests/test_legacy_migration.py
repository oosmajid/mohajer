import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402


class LegacyMigrationTest(unittest.TestCase):
    def test_first_boot_revokes_protocols_and_ports_hidden_by_old_recipe(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        previous = (bot.DB_PATH, bot.ENDPOINTS)
        bot.DB_PATH = tmp.name
        bot.ENDPOINTS = [
            {"tag": "vless-ws", "proto": "vless", "net": "ws", "path": "/v",
             "tls_ports": [443, 2053], "notls_ports": [80]},
            {"tag": "trojan-ws", "proto": "trojan", "net": "ws", "path": "/t",
             "tls_ports": [443], "notls_ports": []},
        ]
        try:
            conn = sqlite3.connect(tmp.name)
            conn.execute("CREATE TABLE users(token TEXT PRIMARY KEY,uuid TEXT,email TEXT UNIQUE,label TEXT,"
                         "limit_bytes INTEGER,expiry_ts INTEGER,created_ts INTEGER,"
                         "base_bytes INTEGER DEFAULT 0,last_raw INTEGER DEFAULT 0,used_bytes INTEGER DEFAULT 0)")
            conn.execute("CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT)")
            conn.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts) "
                         "VALUES('old','secret','u_old','Old',0,0,1)")
            conn.execute("INSERT INTO meta(k,v) VALUES('config_recipe',?)", (json.dumps({
                "vless-ws": {"enabled": True, "count": 1},
                "trojan-ws": {"enabled": False, "count": 0},
            }),))
            conn.commit(); conn.close()
            bot.init_db()
            c = bot.db(); row = c.execute("SELECT active_slots FROM users WHERE token='old'").fetchone(); c.close()
            self.assertEqual(bot._decode_slots(row["active_slots"]), {
                ("vless-ws", "tls", 443), ("vless-ws", "tls", 2053),
                ("vless-ws", "none", 80), ("trojan-ws", "tls", 443),
            })
            self.assertEqual(bot.active_slot_keys("old"), {("vless-ws", "tls", 443)})
            self.assertEqual(bot.meta_get("membership_sync_pending"), "1")
        finally:
            bot.DB_PATH, bot.ENDPOINTS = previous
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
