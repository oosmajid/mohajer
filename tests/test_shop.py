import os, sys, sqlite3, tempfile, time, unittest
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
        self.calls, self.minted = [], []
        bot.tg = self.fake_tg
        bot.pending.clear(); shop._hits.clear(); shop.review_pending.clear()

    def fake_tg(self, method, **p):
        self.calls.append((method, p))
        return {"ok": True, "result": {"message_id": len(self.calls)}}

    def tearDown(self):
        bot.tg = self._tg; bot.DB_PATH = self._db
        bot.ADMIN_IDS.clear(); bot.ADMIN_IDS.update(self._admins)
        bot.pending.clear(); shop._hits.clear(); shop.review_pending.clear()
        for suffix in ("", "-wal", "-shm"):
            try: os.unlink(self.path + suffix)
            except OSError: pass  # Windows may still hold the handle

    def fake_create(self, vol_gb, dur_days, label=None):
        token = "tok%d" % len(self.minted)
        self.minted.append((vol_gb, dur_days, label))
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,used_bytes) "
                  "VALUES(?,?,?,?,?,?,?,0)", (token, "u", "u_" + token, label, int(vol_gb * bot.GB),
                                              int(1e9 + dur_days * 86400), int(1e9)))
        c.commit(); c.close()
        return token

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
        self._create = bot.create_user
        bot.create_user = self.fake_create
        bot.handle_update(msg(ALICE, "/start"))

    def tearDown(self):
        bot.create_user = self._create
        super().tearDown()

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


def photo(uid, uniq="r1"):
    return {"message": {"from": {"id": uid, "first_name": "U%d" % uid}, "chat": {"id": uid, "type": "private"},
                        "photo": [{"file_id": "small", "file_unique_id": uniq + "s"},
                                  {"file_id": "big-" + uniq, "file_unique_id": uniq}]}}


class SalesCase(ShopCase):
    """One plan, one card, a support member and customer Alice; link minting is faked."""

    def setUp(self):
        super().setUp()
        self._fns = {k: getattr(bot, k) for k in ("create_user", "refresh_usage", "maybe_reenable")}
        bot.create_user = self.fake_create
        bot.refresh_usage = lambda token: None
        bot.maybe_reenable = lambda token, refresh=False: None
        c = bot.db()
        c.execute("INSERT INTO shop_staff(tg_id,role,added_ts) VALUES(?, 'support', 0)", (SUPPORT,))
        c.commit(); c.close()
        bot.handle_update(cb(ADMIN, "sa:plan_add")); bot.handle_update(msg(ADMIN, "۳۰ 30 150,000"))
        bot.handle_update(cb(ADMIN, "sa:card_add")); bot.handle_update(msg(ADMIN, "6037-9912-3456-7890 فاطمه میرخلیلی"))
        self.plan = self.row("SELECT * FROM shop_plans")
        bot.handle_update(msg(ALICE, "/start"))
        self.calls.clear()

    def tearDown(self):
        for k, v in self._fns.items(): setattr(bot, k, v)
        super().tearDown()

    def buy(self, uid=ALICE, token=None):
        suffix = (":" + token) if token else ""
        bot.handle_update(cb(uid, "c:plan:%d%s" % (self.plan["id"], suffix)))
        bot.handle_update(cb(uid, "c:pay:%d%s" % (self.plan["id"], suffix)))
        return self.row("SELECT * FROM shop_orders WHERE tg_id=? ORDER BY id DESC", (uid,))

    def review_copies(self):
        return [p for m, p in self.calls if m == "sendPhoto"]



class ShopSalesTests(SalesCase):
    """Plans, card-to-card invoices, receipts and their review."""

    def test_admin_inputs_parse_persian_digits_and_separators(self):
        self.assertEqual((self.plan["gb"], self.plan["days"], self.plan["price"]), (30, 30, 150000))
        card = self.row("SELECT * FROM shop_cards")
        self.assertEqual((card["number"], card["holder"]), ("6037991234567890", "فاطمه میرخلیلی"))
        bot.handle_update(cb(ADMIN, "sa:plan_add")); bot.handle_update(msg(ADMIN, "30 30"))
        self.assertEqual(self.row("SELECT COUNT(*) n FROM shop_plans")["n"], 1)
        self.assertEqual(shop.parse_amount("۱۵۰.۰۰۰ تومان"), 150000)
        self.assertEqual(shop.toman(150000), "۱۵۰٬۰۰۰ تومان")

    def test_buy_flow_creates_unique_amount_invoice(self):
        bot.handle_update(msg(ALICE, shop.B_BUY))
        self.assertEqual(self.last()["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "c:plan:%d" % self.plan["id"])
        o = self.buy()
        self.assertEqual(o["status"], "awaiting")
        self.assertTrue(150001 <= o["amount"] <= 150999)
        self.assertIn("6037991234567890", self.last("editMessageText")["text"])
        o2 = self.buy()      # a new invoice cancels the unpaid one
        self.assertEqual(self.row("SELECT status FROM shop_orders WHERE id=?", (o["id"],))["status"], "cancelled")
        self.assertEqual(o2["status"], "awaiting")

    def test_hidden_plan_is_not_sold(self):
        bot.handle_update(cb(ADMIN, "sa:plan_act:%d" % self.plan["id"]))
        bot.handle_update(msg(ALICE, shop.B_BUY))
        self.assertIn("پلنی", self.last()["text"])
        bot.handle_update(cb(ALICE, "c:pay:%d" % self.plan["id"]))
        self.assertIsNone(self.row("SELECT * FROM shop_orders"))

    def test_no_active_card_tells_customer_and_owner(self):
        bot.handle_update(cb(ADMIN, "sa:card_act:1"))
        self.calls.clear()
        self.assertIsNone(self.buy())
        self.assertIn("موقتاً", self.last("editMessageText")["text"])
        self.assertTrue(any("کارت فعالی" in t for t in self.texts(ADMIN)))

    def test_receipt_goes_to_admin_and_support_then_support_approves(self):
        o = self.buy()
        bot.handle_update(photo(ALICE))
        self.assertEqual(self.row("SELECT status FROM shop_orders")["status"], "review")
        copies = self.review_copies()
        self.assertEqual(sorted(p["chat_id"] for p in copies), [ADMIN, SUPPORT])
        self.assertEqual(copies[0]["photo"], "big-r1")
        bot.handle_update(cb(SUPPORT, "r:ok:%d" % o["id"]))
        self.assertEqual(len(self.minted), 1)
        self.assertEqual(self.minted[0][:2], (30, 30))
        o = self.row("SELECT * FROM shop_orders")
        self.assertEqual((o["status"], o["decided_by"], o["token"]), ("approved", SUPPORT, "tok0"))
        self.assertEqual(self.row("SELECT tg_id,kind FROM shop_services WHERE token='tok0'")[:], (ALICE, "paid"))
        self.assertTrue(any(bot.sub_url("tok0") in t for t in self.texts(ALICE)))
        self.assertEqual(len([m for m, _ in self.calls if m == "editMessageCaption"]), 2)   # both copies updated
        bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))                                   # late second click
        self.assertEqual(len(self.minted), 1)
        actions = [r["action"] for r in bot.db().execute("SELECT action FROM shop_admin_log WHERE action LIKE 'order%'")]
        self.assertEqual(actions, ["order_approve"])

    def test_duplicate_receipt_and_receipt_without_order(self):
        bot.handle_update(photo(ALICE))
        self.assertIn("سفارش پرداخت‌نشده", self.texts(ALICE)[-1])
        self.buy(); bot.handle_update(photo(ALICE, "same"))
        bot.handle_update(msg(BOB, "/start")); self.buy(BOB); bot.handle_update(photo(BOB, "same"))
        self.assertIn("قبلاً ارسال شده", self.texts(BOB)[-1])
        self.assertEqual(self.row("SELECT status FROM shop_orders WHERE tg_id=?", (BOB,))["status"], "awaiting")

    def test_reject_with_preset_and_custom_reason(self):
        o = self.buy(); bot.handle_update(photo(ALICE))
        bot.handle_update(cb(ADMIN, "r:no:%d" % o["id"]))
        kb = self.last("editMessageReplyMarkup")["reply_markup"]["inline_keyboard"]
        self.assertEqual(kb[0][0]["callback_data"], "r:nor:%d:0" % o["id"])
        bot.handle_update(cb(ADMIN, "r:nor:%d:2" % o["id"]))
        self.assertEqual(self.row("SELECT status,note FROM shop_orders")[:], ("rejected", shop.REJECT_REASONS[2]))
        self.assertIn(shop.REJECT_REASONS[2], self.texts(ALICE)[-1])
        o = self.buy(); bot.handle_update(photo(ALICE, "r2"))
        bot.handle_update(cb(SUPPORT, "r:nox:%d" % o["id"]))
        bot.handle_update(msg(SUPPORT, "اسم صاحب حساب فرق دارد"))
        self.assertEqual(self.row("SELECT note FROM shop_orders WHERE id=?", (o["id"],))["note"], "اسم صاحب حساب فرق دارد")
        self.assertEqual(self.minted, [])

    def test_fixed_paid_amount_blocks_short_payment(self):
        o = self.buy(); bot.handle_update(photo(ALICE))
        bot.handle_update(cb(ADMIN, "r:amt:%d" % o["id"])); bot.handle_update(msg(ADMIN, "۱۰۰٬۰۰۰"))
        bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertEqual(self.row("SELECT status FROM shop_orders")["status"], "review")
        self.assertEqual(self.minted, [])
        bot.handle_update(cb(ADMIN, "r:amt:%d" % o["id"])); bot.handle_update(msg(ADMIN, "200000"))
        bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertEqual(self.row("SELECT status,paid_amount FROM shop_orders")[:], ("approved", 200000))

    def test_failed_delivery_returns_order_to_queue(self):
        o = self.buy(); bot.handle_update(photo(ALICE))
        bot.create_user = lambda *a, **k: None
        bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertEqual(self.row("SELECT status,decided_by FROM shop_orders")[:], ("review", None))
        bot.handle_update(msg(ADMIN, shop.B_SHOP))
        self.assertIn("(۱)", self.last()["reply_markup"]["inline_keyboard"][1][0]["text"])

    def test_expiry_and_late_receipt(self):
        o = self.buy()
        shop.expire_orders(now=o["created_ts"] + 60)
        self.assertEqual(self.row("SELECT status FROM shop_orders")["status"], "awaiting")
        shop.expire_orders(now=o["created_ts"] + shop.ORDER_TTL + 1)
        self.assertEqual(self.row("SELECT status FROM shop_orders")["status"], "expired")
        self.assertIn("مهلت پرداخت", self.texts(ALICE)[-1])
        shop.receive_receipt(ALICE, ALICE, "photo:x", "late", now=o["created_ts"] + 5 * 3600)
        self.assertEqual(self.row("SELECT status FROM shop_orders")["status"], "review")
        self.assertIn("دیرتر از مهلت", self.review_copies()[-1]["caption"])
        o = self.buy()
        shop.expire_orders(now=o["created_ts"] + shop.ORDER_TTL + 1)
        shop.receive_receipt(ALICE, ALICE, "photo:y", "too-late", now=o["created_ts"] + shop.LATE_RECEIPT + 60)
        self.assertEqual(self.row("SELECT status FROM shop_orders WHERE id=?", (o["id"],))["status"], "expired")

    def test_cancel_order(self):
        o = self.buy()
        bot.handle_update(cb(BOB, "c:cancel:%d" % o["id"]))
        self.assertEqual(self.row("SELECT status FROM shop_orders")["status"], "awaiting")
        bot.handle_update(cb(ALICE, "c:cancel:%d" % o["id"]))
        self.assertEqual(self.row("SELECT status FROM shop_orders")["status"], "cancelled")

    def test_renewal_extends_same_link(self):
        o = self.buy(); bot.handle_update(photo(ALICE)); bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        before = self.row("SELECT limit_bytes,expiry_ts FROM users WHERE token='tok0'")
        bot.handle_update(cb(ALICE, "c:renew:tok0"))
        self.assertIn("تمدید", self.last()["text"])
        o = self.buy(token="tok0")
        self.assertEqual((o["kind"], o["token"]), ("renew", "tok0"))
        bot.handle_update(photo(ALICE, "r2")); bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        after = self.row("SELECT limit_bytes,expiry_ts FROM users WHERE token='tok0'")
        self.assertEqual(len(self.minted), 1)                        # no new link
        self.assertEqual(after["limit_bytes"] - before["limit_bytes"], 30 * bot.GB)
        self.assertGreater(after["expiry_ts"], before["expiry_ts"])

    def test_renewal_of_deleted_link_mints_new_one_and_others_cannot_renew(self):
        o = self.buy(); bot.handle_update(photo(ALICE)); bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertIsNone(self.buy(BOB, token="tok0"))               # not Bob's link
        o = self.buy(token="tok0")
        c = bot.db(); c.execute("DELETE FROM users WHERE token='tok0'"); c.commit(); c.close()
        bot.handle_update(photo(ALICE, "r3")); bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertEqual(self.row("SELECT token FROM shop_orders WHERE id=?", (o["id"],))["token"], "tok1")

    def test_support_queue_lists_and_resends(self):
        o = self.buy(); bot.handle_update(photo(ALICE)); self.calls.clear()
        bot.handle_update(msg(SUPPORT, shop.B_RECEIPTS))
        self.assertEqual(self.last()["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "r:q:%d" % o["id"])
        bot.handle_update(cb(SUPPORT, "r:q:%d" % o["id"]))
        self.assertEqual(self.review_copies()[-1]["chat_id"], SUPPORT)

    def test_customer_cannot_use_review_buttons(self):
        o = self.buy(); bot.handle_update(photo(ALICE))
        bot.handle_update(cb(ALICE, "r:ok:%d" % o["id"]))
        self.assertEqual(self.row("SELECT status FROM shop_orders")["status"], "review")


class ShopWalletTests(SalesCase):
    def topup(self, amount, uid=ALICE, uniq="t1"):
        bot.handle_update(cb(uid, "c:topup_amt:%d" % amount))
        o = self.row("SELECT * FROM shop_orders WHERE tg_id=? ORDER BY id DESC", (uid,))
        bot.handle_update(photo(uid, uniq)); bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        return self.row("SELECT * FROM shop_orders WHERE id=?", (o["id"],))

    def ledger(self, uid=ALICE):
        return [tuple(r) for r in bot.db().execute("SELECT kind, delta, balance FROM shop_wallet_tx WHERE tg_id=? ORDER BY id", (uid,))]

    def test_topup_by_card_credits_what_was_paid(self):
        bot.handle_update(msg(ALICE, shop.B_WALLET))
        self.assertIn("۰ تومان", self.last()["text"])
        o = self.topup(200000)
        self.assertEqual(o["kind"], "topup"); self.assertEqual(o["status"], "approved")
        self.assertEqual(shop.balance(ALICE), o["amount"])
        self.assertIn("شارژ شد", self.texts(ALICE)[-1])
        self.assertNotIn("📦", self.review_copies()[-1]["caption"])
        bot.handle_update(cb(ALICE, "c:topup_amt:100000"))       # short top-up is still credited, as paid
        o = self.row("SELECT * FROM shop_orders ORDER BY id DESC")
        bot.handle_update(photo(ALICE, "t2"))
        bot.handle_update(cb(ADMIN, "r:amt:%d" % o["id"])); bot.handle_update(msg(ADMIN, "90000"))
        bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertEqual([k for k, _, _ in self.ledger()], ["topup", "topup"])
        self.assertEqual(self.ledger()[-1][1], 90000)

    def test_custom_topup_amount_is_stored_step_and_validated(self):
        bot.handle_update(cb(ALICE, "c:topup_custom"))
        self.assertEqual(shop.get_state(ALICE), {"stage": "topup_amount"})
        bot.handle_update(msg(ALICE, "سلام"))
        self.assertIn("به تومان", self.last()["text"])
        bot.handle_update(msg(ALICE, "۵۰۰"))                      # below minimum
        self.assertIsNone(self.row("SELECT * FROM shop_orders"))
        bot.handle_update(cb(ALICE, "c:topup_custom")); bot.handle_update(msg(ALICE, "۳۰۰٬۰۰۰"))
        o = self.row("SELECT * FROM shop_orders")
        self.assertEqual((o["kind"], o["price"]), ("topup", 300000))

    def test_wallet_purchase_is_instant_and_atomic(self):
        self.topup(200000)
        bot.handle_update(cb(ALICE, "c:plan:%d" % self.plan["id"]))
        kb = self.last("editMessageText")["reply_markup"]["inline_keyboard"]
        self.assertEqual(kb[0][0]["callback_data"], "c:wpay:%d" % self.plan["id"])
        before = shop.balance(ALICE)
        bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"]))
        self.assertEqual(shop.balance(ALICE), before - 150000)
        o = self.row("SELECT * FROM shop_orders ORDER BY id DESC")
        self.assertEqual((o["pay_method"], o["status"], o["token"]), ("wallet", "approved", "tok0"))
        self.assertTrue(any(bot.sub_url("tok0") in t for t in self.texts(ALICE)))
        bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"]))  # not enough left
        self.assertIn("کافی نیست", self.last("editMessageText")["text"])
        self.assertEqual(len(self.minted), 1)

    def test_short_balance_offers_exact_topup(self):
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % ALICE)); bot.handle_update(msg(ADMIN, "100000 هدیه"))
        bot.handle_update(cb(ALICE, "c:plan:%d" % self.plan["id"]))
        kb = self.last("editMessageText")["reply_markup"]["inline_keyboard"]
        self.assertEqual(kb[0][0]["callback_data"], "c:topup_amt:50000")

    def test_failed_wallet_delivery_is_refunded(self):
        self.topup(200000); start = shop.balance(ALICE)
        bot.create_user = lambda *a, **k: None
        bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"]))
        self.assertEqual(shop.balance(ALICE), start)
        self.assertEqual([k for k, _, _ in self.ledger()][-2:], ["purchase", "refund"])
        self.assertEqual(self.row("SELECT status FROM shop_orders ORDER BY id DESC")["status"], "failed")

    def test_card_overpayment_goes_to_wallet(self):
        o = self.buy(); bot.handle_update(photo(ALICE))
        bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertEqual(shop.balance(ALICE), o["amount"] - 150000)  # the unique-amount tomans come back
        self.assertEqual(self.ledger()[0][0], "overpay")
        bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))            # second click credits nothing
        self.assertEqual(len(self.ledger()), 1)

    def test_ledger_never_goes_negative_and_refs_are_unique(self):
        self.assertIsNone(shop.wallet_change(ALICE, -1, "admin"))
        self.assertEqual(shop.wallet_change(ALICE, 500, "topup", ref="order:1"), 500)
        self.assertIsNone(shop.wallet_change(ALICE, 500, "topup", ref="order:1"))
        self.assertEqual(shop.balance(ALICE), 500)

    def test_auto_renew_from_wallet(self):
        self.topup(400000)
        bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"]))
        bot.handle_update(cb(ALICE, "c:auto:tok0"))
        self.assertEqual(self.row("SELECT auto_renew FROM shop_services WHERE token='tok0'")["auto_renew"], 1)
        u = self.row("SELECT * FROM users WHERE token='tok0'")
        shop.run_auto_renewals(now=u["expiry_ts"] - 5 * 3600)         # not due yet
        self.assertEqual(len(self.ledger()), 2)
        shop.run_auto_renewals(now=u["expiry_ts"] - 600)
        after = self.row("SELECT * FROM users WHERE token='tok0'")
        self.assertEqual(after["limit_bytes"] - u["limit_bytes"], 30 * bot.GB)
        self.assertEqual(self.ledger()[-1][0], "auto")
        self.assertEqual(len(self.minted), 1)
        self.assertIn("تمدید خودکار انجام شد", self.texts(ALICE)[-1])

    def test_auto_renew_low_balance_reminds_once_a_day(self):
        self.topup(200000)
        bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"]))
        bot.handle_update(cb(ALICE, "c:auto:tok0"))
        c = bot.db(); c.execute("UPDATE users SET disabled_ts=1 WHERE token='tok0'"); c.commit(); c.close()
        self.calls.clear()
        shop.run_auto_renewals(now=10**9); shop.run_auto_renewals(now=10**9 + 3600)
        self.assertEqual(len(self.texts(ALICE)), 1)
        self.assertIn("کافی نیست", self.texts(ALICE)[0])
        shop.run_auto_renewals(now=10**9 + shop.AUTO_NOTE_EVERY + 1)
        self.assertEqual(len(self.texts(ALICE)), 2)

    def test_others_cannot_toggle_auto_renew(self):
        self.topup(200000); bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"]))
        bot.handle_update(msg(BOB, "/start")); bot.handle_update(cb(BOB, "c:auto:tok0"))
        self.assertEqual(self.row("SELECT auto_renew FROM shop_services WHERE token='tok0'")["auto_renew"], 0)

    def test_admin_user_page_adjusts_wallet_and_blocks(self):
        bot.handle_update(cb(ADMIN, "sa:users"))
        bot.handle_update(msg(ADMIN, "@nobody"))
        self.assertIn("پیدا نشد", self.last()["text"])
        bot.handle_update(msg(ADMIN, "۱۰۰"))
        self.assertIn("<code>100</code>", self.last()["text"])
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:sub" % ALICE)); bot.handle_update(msg(ADMIN, "1000 اشتباه"))
        self.assertIn("کمتر", self.last()["text"])
        bot.handle_update(msg(ADMIN, "50000"))                         # reason missing
        self.assertIn("دلیل", self.last()["text"])
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % ALICE)); bot.handle_update(msg(ADMIN, "50000 بازگشت وجه"))
        self.assertEqual(shop.balance(ALICE), 50000)
        self.assertIn("بازگشت وجه", self.texts(ALICE)[-1])
        self.assertEqual(self.row("SELECT actor,note FROM shop_wallet_tx")[:], (ADMIN, "بازگشت وجه"))
        bot.handle_update(cb(ADMIN, "sa:ublock:%d" % ALICE))
        bot.handle_update(msg(ALICE, shop.B_WALLET))
        self.assertEqual(self.last()["text"], shop.BLOCKED_TEXT)
        actions = [r["action"] for r in bot.db().execute("SELECT action FROM shop_admin_log WHERE action IN ('wallet_adjust','block')")]
        self.assertEqual(actions, ["wallet_adjust", "block"])


class ShopServicesTests(SalesCase):
    def setUp(self):
        super().setUp()
        self._bg = shop.run_bg
        shop.run_bg = lambda fn: fn()

    def tearDown(self):
        shop.run_bg = self._bg
        super().tearDown()

    def own(self, uid=ALICE):
        """Give uid a paid link (via wallet) and return its token."""
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % uid)); bot.handle_update(msg(ADMIN, "150000 هدیه"))
        bot.handle_update(cb(uid, "c:wpay:%d" % self.plan["id"]))
        return self.row("SELECT token FROM shop_services WHERE tg_id=? ORDER BY created_ts DESC", (uid,))["token"]

    def set_link(self, token, **cols):
        c = bot.db()
        c.execute("UPDATE users SET %s WHERE token=?" % ",".join("%s=?" % k for k in cols), (*cols.values(), token))
        c.execute("UPDATE shop_services SET warn_flags=0 WHERE token=?", (token,))
        c.commit(); c.close()

    def test_empty_services_offers_buy(self):
        bot.handle_update(msg(ALICE, shop.B_SERVICES))
        self.assertEqual(self.last()["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "c:buy")

    def test_service_list_and_detail(self):
        tok = self.own()
        self.set_link(tok, used_bytes=12 * bot.GB, expiry_ts=int(time.time()) + 10 * 86400 + 3600)
        c = bot.db(); c.execute("INSERT INTO usage_daily(token,day,start_used,end_used) VALUES(?,?,?,?)",
                                (tok, bot.day_key(), 10 * bot.GB, 12 * bot.GB)); c.commit(); c.close()
        bot.handle_update(msg(ALICE, shop.B_SERVICES))
        button = self.last()["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["callback_data"], "c:svc:%s" % tok)
        self.assertIn("۱۲ گیگ/۳۰ گیگ", button["text"])
        bot.handle_update(cb(ALICE, "c:svc:%s" % tok))
        view = self.last("editMessageText")
        self.assertIn("باقی‌مانده: <b>۱۸ گیگ</b>", view["text"])
        self.assertIn("▰▰▰▰▱▱▱▱▱▱ ۴۰٪", view["text"])
        self.assertIn("مصرف امروز: ۲ گیگ", view["text"])
        self.assertIn("۱۰ روز", view["text"])
        callbacks = [b.get("callback_data") for row in view["reply_markup"]["inline_keyboard"] for b in row]
        self.assertIn("c:renew:%s" % tok, callbacks); self.assertIn("c:auto:%s" % tok, callbacks)
        bot.handle_update(cb(ALICE, "c:auto:%s" % tok))            # toggle keeps the detail keyboard
        kb = self.last("editMessageReplyMarkup")["reply_markup"]["inline_keyboard"]
        self.assertIn("c:svcs", [b.get("callback_data") for row in kb for b in row])

    def test_disabled_detail_explains_grace_and_others_see_nothing(self):
        tok = self.own(); self.set_link(tok, disabled_ts=int(time.time()))
        bot.handle_update(cb(ALICE, "c:svc:%s" % tok))
        self.assertIn("غیرفعال شده", self.last("editMessageText")["text"])
        bot.handle_update(msg(BOB, "/start")); bot.handle_update(cb(BOB, "c:svc:%s" % tok))
        self.assertIn("وجود نداره", self.last("editMessageText")["text"])

    def test_no_warning_right_after_purchase(self):
        tok = self.own(); self.calls.clear()
        shop.send_service_warnings()
        self.assertEqual(self.texts(ALICE), [])

    def test_warnings_once_each_most_urgent_first(self):
        tok = self.own(); now = int(time.time())
        self.set_link(tok, expiry_ts=now + int(2.5 * 86400), limit_bytes=100 * bot.GB, used_bytes=0)
        self.calls.clear()
        shop.send_service_warnings(now); shop.send_service_warnings(now)
        self.assertEqual(len(self.texts(ALICE)), 1); self.assertIn("۳ روز", self.texts(ALICE)[0])
        shop.send_service_warnings(now + 2 * 86400)
        self.assertIn("۱ روز", self.texts(ALICE)[-1])
        c = bot.db(); c.execute("UPDATE users SET used_bytes=? WHERE token=?", (85 * bot.GB, tok)); c.commit(); c.close()
        shop.send_service_warnings(now + 2 * 86400)
        self.assertIn("۸۰٪", self.texts(ALICE)[-1])
        c = bot.db(); c.execute("UPDATE users SET disabled_ts=? WHERE token=?", (now, tok)); c.commit(); c.close()
        shop.send_service_warnings(now + 2 * 86400); shop.send_service_warnings(now + 2 * 86400)
        self.assertEqual(len(self.texts(ALICE)), 4); self.assertIn("تموم شد", self.texts(ALICE)[-1])
        self.assertEqual(self.last()["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "c:renew:%s" % tok)

    def test_quiet_customers_and_auto_renew_still_get_ended(self):
        tok = self.own(); now = int(time.time())
        bot.handle_update(cb(ALICE, "c:notify"))
        self.assertEqual(shop.get_customer(ALICE)["notify"], 0)
        self.set_link(tok, expiry_ts=now + 3600)
        self.calls.clear(); shop.send_service_warnings(now)
        self.assertEqual(self.texts(ALICE), [])
        self.set_link(tok, disabled_ts=now); shop.send_service_warnings(now)
        self.assertEqual(len(self.texts(ALICE)), 1)

    def test_test_links_are_left_to_the_follow_up(self):
        bot.handle_update(cb(ALICE, "c:test"))
        tok = self.row("SELECT token FROM shop_services WHERE kind='test'")["token"]
        self.set_link(tok, disabled_ts=int(time.time())); self.calls.clear()
        shop.send_service_warnings()
        self.assertEqual(self.texts(ALICE), [])

    def test_renewal_starts_a_new_warning_period(self):
        tok = self.own(); now = int(time.time())
        self.set_link(tok, expiry_ts=now + 3600); shop.send_service_warnings(now)
        self.assertNotEqual(self.row("SELECT warn_flags f FROM shop_services")["f"], 0)
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % ALICE)); bot.handle_update(msg(ADMIN, "150000 هدیه"))
        bot.handle_update(cb(ALICE, "c:wpay:%d:%s" % (self.plan["id"], tok)))
        self.assertEqual(self.row("SELECT warn_flags f FROM shop_services")["f"], 0)   # 31 days left now

    def test_outage_broadcast_and_resolved_go_to_same_people(self):
        self.own(ALICE)
        bot.handle_update(msg(BOB, "/start"))                     # no service: not told
        bot.handle_update(cb(ADMIN, "sa:outage"))
        bot.handle_update(msg(ADMIN, "همراه اول کند است"))
        self.assertIn("پیش‌نمایش", self.last()["text"])
        self.calls.clear()
        bot.handle_update(cb(ADMIN, "sa:outage_send"))
        self.assertIn("همراه اول کند است", self.texts(ALICE)[-1])
        self.assertEqual(self.texts(BOB), [])
        self.assertIn("۱ نفر", self.texts(ADMIN)[0])
        bot.handle_update(cb(ADMIN, "sa:outage_send"))           # double tap does not resend
        self.assertEqual(len(self.texts(ALICE)), 1)
        bot.handle_update(cb(ADMIN, "sa:outage")); bot.handle_update(cb(ADMIN, "sa:outage_end"))
        self.assertIn("برطرف شد", self.texts(ALICE)[-1])
        self.assertFalse(shop.setting("outage"))
        actions = [r["action"] for r in bot.db().execute("SELECT action FROM shop_admin_log WHERE action LIKE 'outage%'")]
        self.assertEqual(actions, ["outage_start", "outage_end"])


if __name__ == "__main__":
    unittest.main()
