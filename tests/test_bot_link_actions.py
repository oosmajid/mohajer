import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402


class BotLinkActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close()
        self._db = bot.DB_PATH; bot.DB_PATH = self.tmp.name; bot.init_db()
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,used_bytes) "
                  "VALUES('t1','u','u_t1','Old',1000,0,0,300)")
        c.commit(); c.close()
        self.edits = []
        self._send, self._edit, self._answer, self._admin = bot.send, bot.edit, bot.answer, bot.is_admin
        bot.send = lambda *a, **k: None
        bot.edit = lambda *a, **k: self.edits.append((a, k))
        bot.answer = lambda *a, **k: None
        bot.is_admin = lambda uid: True
        bot.pending.clear()

    def tearDown(self):
        bot.send, bot.edit, bot.answer, bot.is_admin = self._send, self._edit, self._answer, self._admin
        bot.pending.clear()
        bot.DB_PATH = self._db; os.unlink(self.tmp.name)

    def test_detail_keyboard_has_rename_and_confirmed_reset_actions(self):
        callbacks = [b["callback_data"] for row in bot.detail_kb("t1") for b in row]
        self.assertIn("rn:t1", callbacks)
        self.assertIn("rstq:t1", callbacks)

    def test_bot_rename_flow_updates_label(self):
        bot.route_cb(10, 20, "rn:t1", "cb")
        self.assertEqual(bot.pending[10], {"stage": "rename", "token": "t1"})
        bot.handle_update({"message": {"from": {"id": 1}, "chat": {"id": 10}, "text": "  New  "}})
        c = bot.db(); label = c.execute("SELECT label FROM users WHERE token='t1'").fetchone()["label"]; c.close()
        self.assertEqual(label, "New")

    def test_back_from_rename_cancels_pending_input(self):
        bot.route_cb(10, 20, "rn:t1", "cb")
        bot.route_cb(10, 20, "u:t1", "cb")
        self.assertNotIn(10, bot.pending)

    def test_bot_reset_requires_confirmation_then_zeroes_current_usage(self):
        bot.route_cb(10, 20, "rstq:t1", "cb")
        callbacks = [b["callback_data"] for row in self.edits[-1][0][3] for b in row]
        self.assertIn("rst:t1", callbacks)
        bot.route_cb(10, 20, "rst:t1", "cb")
        c = bot.db(); u = c.execute("SELECT * FROM users WHERE token='t1'").fetchone(); c.close()
        self.assertEqual(bot.current_usage(u), 0)


if __name__ == "__main__":
    unittest.main()
