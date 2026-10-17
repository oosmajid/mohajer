import os, sys, sqlite3, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402
import shop  # noqa: E402

ADMIN, ALICE, BOB, SUPPORT = 1, 100, 200, 300


def msg(uid, text, chat_type="private", **extra):
    m = {"from": {"id": uid, "first_name": "U%d" % uid}, "chat": {"id": uid, "type": chat_type}, "text": text}
    m.update(extra)
    return {"message": m}


def cb(uid, data, mid=5):
    return {"callback_query": {"id": "cb", "from": {"id": uid, "first_name": "U%d" % uid}, "data": data,
                               "message": {"message_id": mid, "chat": {"id": uid, "type": "private"}}}}


class ShopFoundationTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self._db, self._tg, self._admins = bot.DB_PATH, bot.tg, set(bot.ADMIN_IDS)
        bot.DB_PATH = self.path; bot.init_db()
        bot.ADMIN_IDS.clear(); bot.ADMIN_IDS.add(ADMIN)
        self.calls = []
        bot.tg = lambda method, **p: self.calls.append((method, p)) or {"ok": True}
        bot.pending.clear(); shop._hits.clear()

    def tearDown(self):
        bot.tg = self._tg; bot.DB_PATH = self._db
        bot.ADMIN_IDS.clear(); bot.ADMIN_IDS.update(self._admins)
        bot.pending.clear(); shop._hits.clear()
        for suffix in ("", "-wal", "-shm"):
            try: os.unlink(self.path + suffix)
            except OSError: pass  # Windows may still hold the handle

    def texts(self, chat=None):
        return [p.get("text", "") for m, p in self.calls
                if m in ("sendMessage", "editMessageText") and (chat is None or p.get("chat_id") == chat)]

    def last(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method][-1]

    def row(self, sql, args=()):
        c = bot.db(); r = c.execute(sql, args).fetchone(); c.close(); return r

    def test_init_db_is_idempotent(self):
        bot.init_db(); bot.init_db()
        for table in ("shop_customers", "shop_staff", "shop_admin_log"):
            self.assertIsNotNone(self.row("SELECT name FROM sqlite_master WHERE name=?", (table,)))

    def test_customer_start_registers_once_and_shows_reply_menu(self):
        bot.handle_update(msg(ALICE, "/start"))
        bot.handle_update(msg(ALICE, "/start"))
        self.assertEqual(self.row("SELECT COUNT(*) n FROM shop_customers")["n"], 1)
        markup = self.last()["reply_markup"]
        buttons = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn(shop.B_BUY, buttons); self.assertIn(shop.B_ACCOUNT, buttons)
        self.assertNotIn("خصوصی", " ".join(self.texts()))

    def test_referral_only_for_new_customer_and_real_inviter(self):
        bot.handle_update(msg(ALICE, "/start"))
        bot.handle_update(msg(BOB, "/start ref_%d" % ALICE))
        self.assertEqual(self.row("SELECT referrer_id r FROM shop_customers WHERE tg_id=?", (BOB,))["r"], ALICE)
        bot.handle_update(msg(BOB, "/start ref_999"))      # existing customer: inviter never changes
        self.assertEqual(self.row("SELECT referrer_id r FROM shop_customers WHERE tg_id=?", (BOB,))["r"], ALICE)
        bot.handle_update(msg(400, "/start ref_400"))      # self-invite
        bot.handle_update(msg(500, "/start ref_777"))      # unknown inviter
        self.assertIsNone(self.row("SELECT referrer_id r FROM shop_customers WHERE tg_id=400")["r"])
        self.assertIsNone(self.row("SELECT referrer_id r FROM shop_customers WHERE tg_id=500")["r"])

    def test_group_chats_are_ignored(self):
        bot.handle_update(msg(ALICE, "/start", chat_type="group"))
        self.assertEqual(self.calls, [])

    def test_customer_state_survives_in_db(self):
        bot.handle_update(msg(ALICE, "/start"))
        shop.set_state(ALICE, {"stage": "x", "n": 1})
        self.assertEqual(shop.get_state(ALICE), {"stage": "x", "n": 1})
        bot.handle_update(msg(ALICE, "/start"))
        self.assertIsNone(shop.get_state(ALICE))

    def test_rate_limit_drops_flood(self):
        for _ in range(shop.RATE_N + 5):
            bot.handle_update(msg(ALICE, "سلام"))
        self.assertEqual(len(self.texts(ALICE)), shop.RATE_N)
        self.assertFalse(shop.rate_limited(ALICE, now=10**10))   # window passed

    def test_maintenance_blocks_customers_not_admin(self):
        bot.handle_update(cb(ADMIN, "sa:maint"))
        self.assertTrue(shop.flag("maintenance"))
        bot.handle_update(msg(ALICE, "/start"))
        self.assertEqual(self.last()["text"], shop.MAINTENANCE_TEXT)
        bot.handle_update(msg(ADMIN, "/start"))
        self.assertEqual(self.last()["text"], bot.WELCOME)
        bot.handle_update(cb(ADMIN, "sa:maint"))
        self.assertFalse(shop.flag("maintenance"))
        actions = [r["action"] for r in bot.db().execute("SELECT action FROM shop_admin_log ORDER BY id")]
        self.assertEqual(actions, ["maintenance_on", "maintenance_off"])

    def test_blocked_customer_gets_blocked_message(self):
        bot.handle_update(msg(ALICE, "/start"))
        c = bot.db(); c.execute("UPDATE shop_customers SET blocked=1 WHERE tg_id=?", (ALICE,)); c.commit(); c.close()
        bot.handle_update(msg(ALICE, shop.B_BUY))
        self.assertEqual(self.last()["text"], shop.BLOCKED_TEXT)

    def test_admin_adds_support_by_id_and_by_forward_then_removes(self):
        bot.handle_update(cb(ADMIN, "sa:staff_add"))
        bot.handle_update(msg(ADMIN, str(SUPPORT)))
        self.assertEqual(shop.staff_role(SUPPORT), shop.ROLE_SUPPORT)
        bot.handle_update(cb(ADMIN, "sa:staff_add"))
        bot.handle_update(msg(ADMIN, "fwd", forward_origin={"type": "user", "sender_user": {"id": 301}}))
        self.assertEqual(shop.staff_role(301), shop.ROLE_SUPPORT)
        bot.handle_update(msg(SUPPORT, "/start"))
        buttons = [b["text"] for row in self.last()["reply_markup"]["keyboard"] for b in row]
        self.assertEqual(buttons, [shop.B_RECEIPTS, shop.B_TICKETS])
        self.assertIsNone(shop.get_customer(SUPPORT))       # staff are not counted as customers
        bot.handle_update(cb(ADMIN, "sa:staff_rm:%d" % SUPPORT))
        self.assertIsNone(shop.staff_role(SUPPORT))
        actions = [r["action"] for r in bot.db().execute("SELECT action FROM shop_admin_log ORDER BY id")]
        self.assertEqual(actions, ["staff_add", "staff_add", "staff_remove"])

    def test_admin_cannot_add_self_as_support(self):
        bot.handle_update(cb(ADMIN, "sa:staff_add"))
        bot.handle_update(msg(ADMIN, str(ADMIN)))
        self.assertIsNone(shop.staff_role(ADMIN))

    def test_admin_reply_buttons_open_menus_and_cancel_pending(self):
        bot.pending[ADMIN] = {"stage": "rename", "token": "t"}
        bot.handle_update(msg(ADMIN, shop.B_LINKS))
        self.assertNotIn(ADMIN, bot.pending)
        self.assertEqual(self.last()["text"], bot.WELCOME)
        bot.handle_update(msg(ADMIN, shop.B_SHOP))
        self.assertIn("مدیریت فروشگاه", self.last()["text"])

    def test_admin_start_sends_reply_keyboard_then_inline_panel(self):
        bot.handle_update(msg(ADMIN, "/start"))
        sends = [p for m, p in self.calls if m == "sendMessage"]
        self.assertIn("keyboard", sends[-2]["reply_markup"])
        self.assertIn("inline_keyboard", sends[-1]["reply_markup"])

    def test_non_admin_cannot_use_admin_callbacks(self):
        bot.handle_update(cb(ALICE, "sa:maint"))
        bot.handle_update(cb(ALICE, "del:t1"))
        self.assertFalse(shop.flag("maintenance"))
        self.assertEqual([m for m, _ in self.calls], ["answerCallbackQuery", "answerCallbackQuery"])

    def test_backup_is_a_readable_copy(self):
        bot.handle_update(msg(ALICE, "/start"))
        data = shop.make_backup()
        fd, out = tempfile.mkstemp(suffix=".db"); os.close(fd)
        try:
            with open(out, "wb") as f: f.write(data)
            c = sqlite3.connect(out)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM shop_customers").fetchone()[0], 1)
            c.close()
        finally:
            os.unlink(out)

    def test_backup_due_once_per_day_after_hour(self):
        shop.set_setting("backup_auto", "1")
        night = 1792195200 - bot.IRAN_OFFSET + 2 * 3600          # 02:00 Iran time
        self.assertFalse(shop.backup_due(night))
        morning = night + 3 * 3600                                # 05:00
        self.assertTrue(shop.backup_due(morning))
        shop.set_setting("backup_day", bot.day_key(morning))
        self.assertFalse(shop.backup_due(morning + 3600))


if __name__ == "__main__":
    unittest.main()
