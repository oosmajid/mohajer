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


class ShopCase(unittest.TestCase):
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



class ShopFoundationTests(ShopCase):
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


class ShopFreeTestTests(ShopCase):
    def setUp(self):
        super().setUp()
        self.minted = []
        self._create = bot.create_user
        bot.create_user = self.fake_create
        bot.handle_update(msg(ALICE, "/start"))

    def tearDown(self):
        bot.create_user = self._create
        super().tearDown()

    def fake_create(self, vol_gb, dur_days, label=None):
        token = "tok%d" % len(self.minted)
        self.minted.append((vol_gb, dur_days, label))
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,used_bytes) "
                  "VALUES(?,?,?,?,?,?,?,0)", (token, "u", "u_" + token, label, int(vol_gb * bot.GB),
                                              int(1e9 + dur_days * 86400), int(1e9)))
        c.commit(); c.close()
        return token

    def test_free_test_is_given_once(self):
        bot.handle_update(msg(ALICE, shop.B_TEST))
        self.assertEqual(self.last()["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "c:test")
        bot.handle_update(cb(ALICE, "c:test"))
        bot.handle_update(cb(ALICE, "c:test"))   # double tap
        self.assertEqual(len(self.minted), 1)
        vol_gb, days, label = self.minted[0]
        self.assertAlmostEqual(vol_gb * 1024, 500); self.assertEqual(days, 1); self.assertIn("U100", label)
        self.assertEqual(self.row("SELECT tg_id FROM shop_services WHERE token='tok0'")["tg_id"], ALICE)
        edits = [p for m, p in self.calls if m == "editMessageText"]
        self.assertIn(bot.sub_url("tok0"), edits[0]["text"])
        self.assertEqual(edits[0]["reply_markup"]["inline_keyboard"][0][0]["url"], bot.sub_url("tok0"))
        bot.handle_update(msg(ALICE, shop.B_TEST))
        self.assertIn("قبلاً", self.last()["text"])

    def test_failed_mint_gives_the_test_back(self):
        bot.create_user = lambda *a, **k: None
        bot.handle_update(cb(ALICE, "c:test"))
        self.assertEqual(self.row("SELECT test_used_ts t FROM shop_customers WHERE tg_id=?", (ALICE,))["t"], 0)
        self.assertIsNone(self.row("SELECT * FROM shop_services"))

    def test_disabled_test_and_admin_settings(self):
        bot.handle_update(cb(ADMIN, "sa:test_toggle"))
        bot.handle_update(cb(ALICE, "c:test"))
        self.assertEqual(self.minted, [])
        bot.handle_update(cb(ADMIN, "sa:test_toggle"))
        bot.handle_update(cb(ADMIN, "sa:test_mb:1024"))
        bot.handle_update(cb(ADMIN, "sa:test_hours:48"))
        bot.handle_update(cb(ADMIN, "sa:test_mb:999"))        # not an offered choice
        self.assertEqual((shop.test_mb(), shop.test_hours()), (1024, 48))
        bot.handle_update(cb(ALICE, "c:test"))
        self.assertAlmostEqual(self.minted[0][0], 1.0); self.assertEqual(self.minted[0][1], 2)
        actions = [r["action"] for r in bot.db().execute("SELECT action FROM shop_admin_log ORDER BY id")]
        self.assertEqual(actions, ["test_off", "test_on", "test_mb", "test_hours"])

    def test_followup_once_near_end_and_feedback(self):
        bot.handle_update(cb(ALICE, "c:test"))
        expiry = self.row("SELECT expiry_ts e FROM users WHERE token='tok0'")["e"]
        self.calls.clear()
        shop.send_test_followups(now=expiry - 10 * 3600)       # plenty of time left
        self.assertEqual(self.calls, [])
        shop.send_test_followups(now=expiry - 3600)
        shop.send_test_followups(now=expiry - 60)
        self.assertEqual(len(self.texts(ALICE)), 1)
        bot.handle_update(cb(ALICE, "c:fb:bad:tok0"))
        bot.handle_update(cb(ALICE, "c:fb:bad:tok0"))
        self.assertEqual(self.row("SELECT feedback f FROM shop_services")["f"], "bad")
        self.assertEqual(len(self.texts(ADMIN)), 1)            # owner told once
        bot.handle_update(cb(BOB, "c:fb:good:tok0"))           # someone else's link
        self.assertEqual(self.row("SELECT feedback f FROM shop_services")["f"], "bad")

    def test_followup_on_data_use_and_skips_deleted_links(self):
        bot.handle_update(cb(ALICE, "c:test"))
        c = bot.db(); c.execute("UPDATE users SET used_bytes=limit_bytes*0.9 WHERE token='tok0'"); c.commit(); c.close()
        self.calls.clear(); shop.send_test_followups(now=int(1e9))
        self.assertEqual(len(self.texts(ALICE)), 1)
        c = bot.db(); c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts) VALUES('gone',?,'test',0)", (BOB,))
        c.commit(); c.close()
        self.calls.clear(); shop.send_test_followups(now=int(1e9))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.row("SELECT followup_sent f FROM shop_services WHERE token='gone'")["f"], 1)

    def test_account_page(self):
        bot.handle_update(msg(ALICE, shop.B_ACCOUNT))
        text = self.last()["text"]
        self.assertIn(str(ALICE), text); self.assertIn("هنوز نگرفتی", text)

    def test_guide_flow(self):
        bot.handle_update(msg(ALICE, shop.B_GUIDE))
        keys = [b["callback_data"] for row in self.last()["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(keys, ["c:guide:android", "c:guide:ios", "c:guide:windows", "c:guide:mac"])
        bot.handle_update(cb(ALICE, "c:guide:ios"))
        edit = self.last("editMessageText")
        self.assertIn("apps.apple.com", edit["reply_markup"]["inline_keyboard"][0][0]["url"])
        bot.handle_update(cb(ALICE, "c:guides"))
        self.assertEqual(self.last("editMessageText")["text"], shop.GUIDE_TEXT)

    def test_jalali_dates(self):
        self.assertEqual(shop.jalali(1792195200), (1405, 7, 25))   # 2026-10-17
        self.assertEqual(shop.jalali(1773952200), (1404, 12, 29))  # 2026-03-20 Iran time
        self.assertEqual(shop.fa_date(1792195200), "۱۴۰۵/۰۷/۲۵")


if __name__ == "__main__":
    unittest.main()
