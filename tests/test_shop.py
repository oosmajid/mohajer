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
        bot.pending.clear(); shop._hits.clear(); shop.review_pending.clear(); shop.ticket_pending.clear()

    def fake_tg(self, method, **p):
        self.calls.append((method, p))
        return {"ok": True, "result": {"message_id": len(self.calls)}}

    def tearDown(self):
        bot.tg = self._tg; bot.DB_PATH = self._db
        bot.ADMIN_IDS.clear(); bot.ADMIN_IDS.update(self._admins)
        bot.pending.clear(); shop._hits.clear(); shop.review_pending.clear(); shop.ticket_pending.clear()
        for suffix in ("", "-wal", "-shm"):
            try: os.unlink(self.path + suffix)
            except OSError: pass  # Windows may still hold the handle

    def fake_create(self, vol_gb, dur_days, label=None):
        """Same row create_user() writes (0 = unlimited, expiry from NOW), minus the xray/sub-file side."""
        token = "tok%d" % len(self.minted)
        self.minted.append((vol_gb, dur_days, label))
        now = int(time.time())
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,used_bytes,usage_anchor) "
                  "VALUES(?,?,?,?,?,?,?,0,0)", (token, "u", "u_" + token, label, int(vol_gb * bot.GB),
                                                int(now + dur_days * 86400) if dur_days else 0, now))
        c.commit(); c.close()
        return token

    def texts(self, chat=None):
        return [p.get("text", "") for m, p in self.calls
                if m in ("sendMessage", "editMessageText") and (chat is None or p.get("chat_id") == chat)]

    def last(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method][-1]

    def row(self, sql, args=()):
        c = bot.db(); r = c.execute(sql, args).fetchone(); c.close(); return r

    def q(self, sql, args=()):
        c = bot.db(); rows = c.execute(sql, args).fetchall(); c.close(); return rows



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
        actions = [r["action"] for r in self.q("SELECT action FROM shop_admin_log ORDER BY id")]
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
        actions = [r["action"] for r in self.q("SELECT action FROM shop_admin_log ORDER BY id")]
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
        actions = [r["action"] for r in self.q("SELECT action FROM shop_admin_log ORDER BY id")]
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
        actions = [r["action"] for r in self.q("SELECT action FROM shop_admin_log WHERE action LIKE 'order%'")]
        self.assertEqual(actions, ["order_approve", "order_deliver"])

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
        bot.handle_update(cb(ALICE, "c:rcpt:%d" % o["id"]))          # the rejected invoice is still a candidate: pick
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

    def test_failed_delivery_is_retried_not_requeued(self):
        o = self.buy(); bot.handle_update(photo(ALICE))
        bot.create_user = lambda *a, **k: None                       # xray down: nothing can be minted
        bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertEqual(self.row("SELECT status,decided_by,delivered_ts FROM shop_orders")[:], ("approved", ADMIN, None))
        self.assertTrue(any("ساخت سرویس الان انجام نشد" in t for t in self.texts(ADMIN)))
        bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))             # a second click cannot re-approve
        self.assertEqual(self.minted, [])
        shop.retry_deliveries(now=int(time.time()) + 10)              # too early: nothing yet
        self.assertEqual(self.minted, [])
        bot.create_user = self.fake_create
        shop.retry_deliveries(now=int(time.time()) + shop.RETRY_AFTER + 1)
        self.assertEqual(len(self.minted), 1)
        self.assertIsNotNone(self.row("SELECT delivered_ts FROM shop_orders")["delivered_ts"])
        self.assertTrue(any(bot.sub_url("tok0") in t for t in self.texts(ALICE)))
        shop.retry_deliveries(now=int(time.time()) + shop.RETRY_AFTER + 1)   # idempotent
        self.assertEqual(len(self.minted), 1)

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
        return [tuple(r) for r in self.q("SELECT kind, delta, balance FROM shop_wallet_tx WHERE tg_id=? ORDER BY id", (uid,))]

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
        bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"]))  # right away: a double tap, not a new order
        self.assertIn("چند لحظه پیش", self.last("editMessageText")["text"])
        shop.DUP_WINDOW, dw = 0, shop.DUP_WINDOW
        try: bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"]))  # later: a real attempt, not enough left
        finally: shop.DUP_WINDOW = dw
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
        now = int(time.time())
        c = bot.db(); c.execute("UPDATE users SET expiry_ts=? WHERE token='tok0'", (now + 600,)); c.commit(); c.close()
        self.calls.clear()
        shop.run_auto_renewals(now=now); shop.run_auto_renewals(now=now + 3600)
        self.assertEqual(len(self.texts(ALICE)), 1)
        self.assertIn("کافی نیست", self.texts(ALICE)[0])
        shop.run_auto_renewals(now=now + shop.AUTO_NOTE_EVERY + 1)
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
        actions = [r["action"] for r in self.q("SELECT action FROM shop_admin_log WHERE action IN ('wallet_adjust','block')")]
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
        actions = [r["action"] for r in self.q("SELECT action FROM shop_admin_log WHERE action LIKE 'outage%'")]
        self.assertEqual(actions, ["outage_start", "outage_end"])


class ShopReferralTests(SalesCase):
    def setUp(self):
        super().setUp()
        shop.set_setting("bot_username", "MohajerShopBot")
        bot.handle_update(msg(BOB, "/start ref_%d" % ALICE))       # Bob is Alice's friend
        self.calls.clear()

    def gift(self, uid, amount):
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % uid)); bot.handle_update(msg(ADMIN, "%d هدیه" % amount))

    def alice_buys(self):
        """Rewards only ever go to a paying customer, so most tests make Alice one first."""
        self.gift(ALICE, 150000); bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"])); self.calls.clear()

    def ledger(self, uid):
        return [tuple(r) for r in self.q("SELECT kind, delta FROM shop_wallet_tx WHERE tg_id=? AND kind='referral' ORDER BY id", (uid,))]

    def test_invite_page_link_share_and_stats(self):
        bot.handle_update(msg(ALICE, shop.B_INVITE))
        page = self.last()
        self.assertIn("https://t.me/MohajerShopBot?start=ref_%d" % ALICE, page["text"])
        self.assertIn("دعوت‌شده‌ها: ۱ · خریدارها: ۰", page["text"])
        share = page["reply_markup"]["inline_keyboard"][0][0]["url"]
        self.assertTrue(share.startswith("https://t.me/share/url?url=https%3A%2F%2Ft.me%2FMohajerShopBot"))
        bot.handle_update(cb(ALICE, "c:ref_msg"))
        self.assertTrue(self.last()["text"].endswith("start=ref_%d" % ALICE))

    def test_inviter_hears_about_new_friend_once(self):
        bot.handle_update(msg(300 + 1, "/start ref_%d" % ALICE))
        bot.handle_update(msg(300 + 1, "/start ref_%d" % ALICE))
        self.assertEqual(len([t for t in self.texts(ALICE) if "عضو شد" in t]), 1)

    def test_friend_gets_first_purchase_discount_only(self):
        bot.handle_update(msg(BOB, shop.B_BUY))
        self.assertIn("۱۰٪ تخفیف", self.last()["text"])
        self.assertIn("۱۳۵٬۰۰۰ تومان", self.last()["reply_markup"]["inline_keyboard"][0][0]["text"])
        bot.handle_update(cb(BOB, "c:plan:%d" % self.plan["id"]))
        self.assertIn("<s>۱۵۰٬۰۰۰</s>", self.last("editMessageText")["text"])
        o = self.buy(BOB)
        self.assertEqual((o["price"], o["discount"]), (135000, 15000))
        self.assertTrue(135001 <= o["amount"] <= 135999)
        bot.handle_update(photo(BOB)); bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        o2 = self.buy(BOB)
        self.assertEqual((o2["price"], o2["discount"]), (150000, 0))
        bot.handle_update(msg(ALICE, shop.B_BUY))                   # not invited: full price
        self.assertNotIn("تخفیف", self.last()["text"])

    def test_inviter_who_never_bought_gets_no_reward(self):
        o = self.buy(BOB); bot.handle_update(photo(BOB)); bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertEqual(self.ledger(ALICE), [])                   # Alice has not bought anything herself
        self.assertEqual((o["price"], o["discount"]), (135000, 15000))   # Bob's discount still applies

    def test_inviter_rewarded_once_after_first_real_purchase(self):
        self.alice_buys()
        bot.handle_update(cb(BOB, "c:test"))                        # a free test is not a purchase
        self.assertEqual(self.ledger(ALICE), [])
        o = self.buy(BOB); bot.handle_update(photo(BOB)); bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertEqual(self.ledger(ALICE), [("referral", 13500)])    # 10% of what Bob paid (135,000)
        self.assertIn("پاداش", self.texts(ALICE)[-1])
        self.gift(BOB, 150000); bot.handle_update(cb(BOB, "c:wpay:%d" % self.plan["id"]))
        self.assertEqual(self.ledger(ALICE), [("referral", 13500)])
        bot.handle_update(msg(ALICE, shop.B_INVITE))
        self.assertIn("خریدارها: ۱", self.last()["text"]); self.assertIn("۱۳٬۵۰۰ تومان", self.last()["text"])

    def test_wallet_first_purchase_also_rewards_and_discounts(self):
        self.alice_buys(); self.gift(BOB, 135000)
        bot.handle_update(cb(BOB, "c:plan:%d" % self.plan["id"]))
        self.assertEqual(self.last("editMessageText")["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         "c:wpay:%d" % self.plan["id"])
        bot.handle_update(cb(BOB, "c:wpay:%d" % self.plan["id"]))
        self.assertEqual(shop.balance(BOB), 0)
        self.assertEqual(self.ledger(ALICE), [("referral", 13500)])

    def test_failed_wallet_purchase_neither_counts_nor_rewards(self):
        self.gift(BOB, 135000)
        bot.create_user = lambda *a, **k: None
        bot.handle_update(cb(BOB, "c:wpay:%d" % self.plan["id"]))
        self.assertEqual(self.ledger(ALICE), [])
        self.assertEqual(shop.first_buy_discount(BOB), 10)

    def test_admin_changes_percentages(self):
        bot.handle_update(cb(ADMIN, "sa:ref"))
        self.assertIn("دعوت‌شده‌ها: ۱", self.last("editMessageText")["text"])
        bot.handle_update(cb(ADMIN, "sa:ref_reward:20")); bot.handle_update(cb(ADMIN, "sa:ref_discount:0"))
        bot.handle_update(cb(ADMIN, "sa:ref_reward:7"))           # not a choice
        self.assertEqual((shop.ref_reward_pct(), shop.ref_discount_pct()), (20, 0))
        self.alice_buys(); o = self.buy(BOB)
        self.assertEqual(o["price"], 150000)
        bot.handle_update(photo(BOB)); bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertEqual(self.ledger(ALICE), [("referral", 30000)])
        actions = [r["action"] for r in self.q("SELECT action FROM shop_admin_log WHERE action LIKE 'ref_%'")]
        self.assertEqual(actions, ["ref_reward", "ref_discount"])


class ShopTicketTests(SalesCase):
    def setUp(self):
        super().setUp()
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % ALICE)); bot.handle_update(msg(ADMIN, "150000 هدیه"))
        bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"]))
        self.tok = self.row("SELECT token FROM shop_services WHERE tg_id=?", (ALICE,))["token"]
        self.calls.clear()

    def report(self, uid=ALICE, service=None, cat="down", carrier="mci", text="وصل نمی‌شه"):
        bot.handle_update(msg(uid, shop.B_REPORT))
        bot.handle_update(cb(uid, "c:rep_go"))
        bot.handle_update(cb(uid, "c:rs:%s" % (service or self.tok)))
        bot.handle_update(cb(uid, "c:rc:%s" % cat))
        if carrier: bot.handle_update(cb(uid, "c:rk:%s" % carrier))
        if text is not None: bot.handle_update(msg(uid, text))
        return self.row("SELECT * FROM shop_tickets WHERE tg_id=? ORDER BY id DESC", (uid,))

    def staff_cards(self, chat):
        return [p for m, p in self.calls if m == "sendMessage" and p["chat_id"] == chat and "تیکت #" in p["text"]]

    def test_quick_fix_comes_first_and_can_end_it(self):
        bot.handle_update(msg(ALICE, shop.B_REPORT))
        self.assertIn("آپدیت", self.last()["text"])
        bot.handle_update(cb(ALICE, "c:rep_ok"))
        self.assertIsNone(self.row("SELECT * FROM shop_tickets"))

    def test_full_report_reaches_staff_with_service_details(self):
        t = self.report()
        self.assertEqual((t["category"], t["carrier"], t["token"], t["status"]), ("down", "mci", self.tok, "open"))
        self.assertIsNone(shop.get_state(ALICE))
        self.assertIn("ثبت شد", self.texts(ALICE)[-1])
        for staff in (ADMIN, SUPPORT):
            card = self.staff_cards(staff)[-1]
            self.assertIn("همراه اول", card["text"]); self.assertIn(self.tok, card["text"]); self.assertIn("وصل نمی‌شه", card["text"])
            self.assertEqual(card["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "t:r:%d" % t["id"])

    def test_payment_question_skips_carrier_and_accepts_screenshot(self):
        t = self.report(service="-", cat="pay", carrier=None, text=None)
        self.assertIsNone(t)
        bot.handle_update(photo(ALICE, "shot"))                    # a screenshot, not a receipt
        t = self.row("SELECT * FROM shop_tickets")
        self.assertEqual((t["category"], t["carrier"], t["token"]), ("pay", None, None))
        self.assertEqual(self.row("SELECT file FROM shop_ticket_msgs")["file"], "photo:big-shot")
        self.assertIsNone(self.row("SELECT * FROM shop_orders WHERE receipt_uid='shot'"))
        self.assertTrue(any(m == "sendPhoto" and p["chat_id"] == SUPPORT for m, p in self.calls))

    def test_disabled_service_suggests_renewal_first(self):
        c = bot.db(); c.execute("UPDATE users SET disabled_ts=1 WHERE token=?", (self.tok,)); c.commit(); c.close()
        bot.handle_update(msg(ALICE, shop.B_REPORT)); bot.handle_update(cb(ALICE, "c:rep_go"))
        bot.handle_update(cb(ALICE, "c:rs:%s" % self.tok))
        self.assertEqual(self.last("editMessageText")["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "c:renew:%s" % self.tok)
        bot.handle_update(cb(ALICE, "c:rcats")); bot.handle_update(cb(ALICE, "c:rc:other")); bot.handle_update(msg(ALICE, "سؤال"))
        self.assertEqual(self.row("SELECT category FROM shop_tickets")["category"], "other")

    def test_menu_tap_cancels_a_half_written_report(self):
        bot.handle_update(msg(ALICE, shop.B_REPORT)); bot.handle_update(cb(ALICE, "c:rep_go"))
        bot.handle_update(cb(ALICE, "c:rs:-")); bot.handle_update(cb(ALICE, "c:rc:other"))
        bot.handle_update(msg(ALICE, shop.B_WALLET))
        self.assertIsNone(shop.get_state(ALICE)); self.assertIsNone(self.row("SELECT * FROM shop_tickets"))

    def test_others_cannot_report_on_my_service(self):
        bot.handle_update(msg(BOB, "/start")); bot.handle_update(cb(BOB, "c:rs:%s" % self.tok))
        self.assertIn("پیدا نشد", self.last("editMessageText")["text"])

    def test_staff_reply_customer_follow_up_close_and_rating(self):
        t = self.report()
        bot.handle_update(cb(SUPPORT, "t:r:%d" % t["id"])); bot.handle_update(msg(SUPPORT, "آپدیت کن لطفاً"))
        self.assertEqual(self.row("SELECT status FROM shop_tickets")["status"], "answered")
        self.assertIn("آپدیت کن لطفاً", self.texts(ALICE)[-1])
        bot.handle_update(cb(ALICE, "c:tk:%d" % t["id"])); bot.handle_update(msg(ALICE, "هنوز نه"))
        self.assertEqual(self.row("SELECT status FROM shop_tickets")["status"], "open")
        self.assertIn("هنوز نه", self.staff_cards(ADMIN)[-1]["text"])
        bot.handle_update(cb(ADMIN, "t:c:%d:1" % t["id"]))           # canned "fixed"
        self.assertIn("برطرف شد", self.texts(ALICE)[-1])
        bot.handle_update(cb(ADMIN, "t:x:%d" % t["id"]))
        self.assertEqual(self.row("SELECT status FROM shop_tickets")["status"], "closed")
        bot.handle_update(cb(ALICE, "c:tkr:%d:0" % t["id"]))         # not happy -> reopened
        t2 = self.row("SELECT * FROM shop_tickets")
        self.assertEqual((t2["status"], t2["rating"]), ("open", 0))
        self.assertEqual(shop.get_state(ALICE), {"stage": "tk_reply", "id": t["id"]})
        bot.handle_update(cb(ALICE, "c:tkr:%d:1" % t["id"]))         # rating is given once
        self.assertEqual(self.row("SELECT rating FROM shop_tickets")["rating"], 0)
        msgs = [tuple(r) for r in self.q("SELECT role, text FROM shop_ticket_msgs ORDER BY id")]
        self.assertEqual([r for r, _ in msgs], ["customer", "staff", "customer", "staff"])

    def test_customer_closes_when_solved(self):
        t = self.report()
        bot.handle_update(cb(ALICE, "c:tkok:%d" % t["id"]))
        self.assertEqual(self.row("SELECT status,rating FROM shop_tickets")[:], ("closed", 1))
        bot.handle_update(cb(ALICE, "c:tk:%d" % t["id"]))
        self.assertNotEqual(shop.get_state(ALICE), {"stage": "tk_reply", "id": t["id"]})

    def test_open_ticket_limit(self):
        for i in range(shop.MAX_OPEN_TICKETS): self.report(text="مشکل %d" % i)
        self.report(text="یکی دیگه")
        self.assertEqual(self.row("SELECT COUNT(*) n FROM shop_tickets")["n"], shop.MAX_OPEN_TICKETS)

    def test_ticket_lists_for_support_and_admin(self):
        t = self.report()
        bot.handle_update(msg(SUPPORT, shop.B_TICKETS))
        self.assertEqual(self.last()["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "t:v:%d" % t["id"])
        bot.handle_update(msg(ADMIN, shop.B_SHOP))
        self.assertIn("تیکت‌ها (۱)", self.last()["reply_markup"]["inline_keyboard"][1][1]["text"])
        bot.handle_update(cb(ADMIN, "sa:tickets"))
        self.assertIn("#%d" % t["id"], self.last("editMessageText")["reply_markup"]["inline_keyboard"][0][0]["text"])

    def test_customer_cannot_use_staff_ticket_buttons(self):
        t = self.report()
        bot.handle_update(cb(ALICE, "t:x:%d" % t["id"]))
        self.assertEqual(self.row("SELECT status FROM shop_tickets")["status"], "open")

    def test_carrier_alert_after_several_customers(self):
        others = [401, 402]
        for uid in others:
            bot.handle_update(msg(uid, "/start"))
            self.report(uid=uid, service="-", cat="slow", carrier="irancell")
        self.assertFalse([t for t in self.texts(ADMIN) if "گزارش از ایرانسل" in t])
        self.report(cat="slow", carrier="irancell")
        alerts = [p for m, p in self.calls if m == "sendMessage" and p["chat_id"] == ADMIN and "گزارش از ایرانسل" in p["text"]]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "sa:outage")
        bot.handle_update(msg(403, "/start")); self.report(uid=403, service="-", cat="down", carrier="irancell")
        alerts = [t for t in self.texts(ADMIN) if "گزارش از ایرانسل" in t]
        self.assertEqual(len(alerts), 1)                              # at most once an hour


if __name__ == "__main__":
    unittest.main()


class ShopHardeningTests(SalesCase):
    """Review findings of PR #1: money can only move once, staff stay in their lane, state never leaks."""

    def set_link(self, token, **cols):
        c = bot.db()
        c.execute("UPDATE users SET %s WHERE token=?" % ",".join("%s=?" % k for k in cols), (*cols.values(), token))
        c.commit(); c.close()

    def own(self, uid=ALICE):
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % uid)); bot.handle_update(msg(ADMIN, "150000 هدیه"))
        bot.handle_update(cb(uid, "c:wpay:%d" % self.plan["id"]))
        return self.row("SELECT token FROM shop_services WHERE tg_id=? ORDER BY created_ts DESC", (uid,))["token"]

    def test_parse_amount_rejects_decimals_and_junk(self):
        for ok, n in (("150000", 150000), ("150,000", 150000), ("۱۵۰٬۰۰۰", 150000), ("150.000 تومان", 150000), ("1,500,000", 1500000)):
            self.assertEqual(shop.parse_amount(ok), n, ok)
        for bad in ("1.5", "150000.5", "100k", "1,5", "-5000", "0", "1 میلیون", "", "10000000000"):
            self.assertIsNone(shop.parse_amount(bad), bad)
        self.assertIsNone(shop._int("²")); self.assertIsNone(shop._int("1" * 20)); self.assertEqual(shop._int("۱۲"), 12)
        bot.handle_update(msg(BOB, "/start ref_²"))                  # must not crash the update
        self.assertIsNotNone(shop.get_customer(BOB))

    def test_support_cannot_change_or_approve_a_corrected_amount(self):
        o = self.buy(); bot.handle_update(photo(ALICE))
        bot.handle_update(cb(SUPPORT, "r:amt:%d" % o["id"])); bot.handle_update(msg(SUPPORT, "999999"))
        self.assertIsNone(self.row("SELECT paid_amount FROM shop_orders")["paid_amount"])
        bot.handle_update(cb(ADMIN, "r:amt:%d" % o["id"])); bot.handle_update(msg(ADMIN, "9999999"))   # > 2x invoice
        self.assertIsNone(self.row("SELECT paid_amount FROM shop_orders")["paid_amount"])
        self.assertIn("برابر فاکتور", self.texts(ADMIN)[-1])
        bot.handle_update(msg(ADMIN, "200000"))
        self.assertEqual(self.row("SELECT paid_amount FROM shop_orders")["paid_amount"], 200000)
        bot.handle_update(cb(SUPPORT, "r:ok:%d" % o["id"]))          # corrected -> owner only
        self.assertEqual(self.row("SELECT status FROM shop_orders")["status"], "review")
        bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertEqual(self.row("SELECT status FROM shop_orders")["status"], "approved")
        self.assertEqual(shop.balance(ALICE), 50000)                  # the overpay, bounded by the cap

    def test_support_can_still_approve_an_untouched_invoice(self):
        o = self.buy(); bot.handle_update(photo(ALICE))
        bot.handle_update(cb(SUPPORT, "r:ok:%d" % o["id"]))
        self.assertEqual(self.row("SELECT status FROM shop_orders")["status"], "approved")

    def test_double_tap_wallet_purchase_buys_once(self):
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % ALICE)); bot.handle_update(msg(ADMIN, "400000 هدیه"))
        bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"]))
        bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"]))
        self.assertEqual(len(self.minted), 1); self.assertEqual(shop.balance(ALICE), 250000)
        self.assertIn("چند لحظه پیش", self.last("editMessageText")["text"])

    def test_renewal_is_delivered_once_even_if_a_later_step_fails(self):
        tok = self.own(); before = self.row("SELECT * FROM users WHERE token=?", (tok,))
        o = self.buy(token=tok); bot.handle_update(photo(ALICE))
        real = shop.reset_warnings
        shop.reset_warnings = lambda token: (_ for _ in ()).throw(RuntimeError("db locked"))
        try: bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        finally: shop.reset_warnings = real
        o = self.row("SELECT * FROM shop_orders WHERE id=?", (o["id"],))
        self.assertEqual(o["status"], "approved"); self.assertIsNotNone(o["delivered_ts"])   # the extension is committed
        after = self.row("SELECT * FROM users WHERE token=?", (tok,))
        self.assertEqual(after["limit_bytes"] - before["limit_bytes"], 30 * bot.GB)
        shop.retry_deliveries(now=int(time.time()) + shop.RETRY_AFTER + 1); bot.handle_update(cb(ADMIN, "r:ok:%d" % o["id"]))
        self.assertEqual(self.row("SELECT limit_bytes FROM users WHERE token=?", (tok,))["limit_bytes"], after["limit_bytes"])
        self.assertEqual(len(self.minted), 1)

    def test_auto_renew_never_charges_twice_for_a_link_that_stays_disabled(self):
        tok = self.own(); bot.handle_update(cb(ALICE, "c:auto:%s" % tok))
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % ALICE)); bot.handle_update(msg(ADMIN, "450000 هدیه"))
        now = int(time.time())
        self.set_link(tok, expiry_ts=now - 10, disabled_ts=now - 5)   # expired and disabled; xray never re-adds it in tests
        shop.run_auto_renewals(now=now)
        self.assertEqual(shop.balance(ALICE), 300000)
        for dt in (60, 120, 3600, 7200):
            shop.run_auto_renewals(now=now + dt)
        self.assertEqual(shop.balance(ALICE), 300000)                 # one renewal, however often the ticker runs
        self.assertEqual(self.row("SELECT COUNT(*) n FROM shop_orders WHERE kind='renew'")["n"], 1)
        u = self.row("SELECT * FROM users WHERE token=?", (tok,))
        self.assertGreater(u["expiry_ts"], now + 29 * 86400)

    def test_blocked_customers_are_not_auto_renewed(self):
        tok = self.own(); bot.handle_update(cb(ALICE, "c:auto:%s" % tok))
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % ALICE)); bot.handle_update(msg(ADMIN, "150000 هدیه"))
        bot.handle_update(cb(ADMIN, "sa:ublock:%d" % ALICE))
        now = int(time.time()); self.set_link(tok, expiry_ts=now + 60)
        shop.run_auto_renewals(now=now)
        self.assertEqual(shop.balance(ALICE), 150000)

    def test_renewal_keeps_unlimited_and_covers_overshoot(self):
        tok = self.own()
        self.set_link(tok, limit_bytes=0)                             # admin made it unlimited
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % ALICE)); bot.handle_update(msg(ADMIN, "300000 هدیه"))
        bot.handle_update(cb(ALICE, "c:wpay:%d:%s" % (self.plan["id"], tok)))
        self.assertEqual(self.row("SELECT limit_bytes FROM users WHERE token=?", (tok,))["limit_bytes"], 0)
        self.set_link(tok, limit_bytes=10 * bot.GB, used_bytes=12 * bot.GB)   # overshot by 2GB
        bot.handle_update(cb(ALICE, "c:renew:%s" % tok)); time.sleep(0)
        shop.DUP_WINDOW, dw = 0, shop.DUP_WINDOW
        try: bot.handle_update(cb(ALICE, "c:wpay:%d:%s" % (self.plan["id"], tok)))
        finally: shop.DUP_WINDOW = dw
        self.assertEqual(self.row("SELECT limit_bytes FROM users WHERE token=?", (tok,))["limit_bytes"], 42 * bot.GB)

    def test_link_being_deleted_cannot_be_renewed(self):
        tok = self.own(); self.set_link(tok, pending_delete=1)
        bot.handle_update(cb(ALICE, "c:renew:%s" % tok))
        self.assertIn("دیگه وجود نداره", self.last()["text"])
        bot.handle_update(msg(ALICE, shop.B_SERVICES))
        self.assertIn("هنوز سرویسی نداری", self.last()["text"])

    def test_receipt_photo_wins_over_a_pending_ticket_prompt(self):
        bot.handle_update(cb(ALICE, "c:report")); bot.handle_update(cb(ALICE, "c:rep_go"))
        bot.handle_update(cb(ALICE, "c:rs:-")); bot.handle_update(cb(ALICE, "c:rc:pay"))
        self.assertEqual(shop.get_state(ALICE)["stage"], "tk_text")
        o = self.buy(); bot.handle_update(photo(ALICE))
        self.assertEqual(self.row("SELECT status FROM shop_orders WHERE id=?", (o["id"],))["status"], "review")
        self.assertEqual(self.row("SELECT COUNT(*) n FROM shop_tickets")["n"], 0)
        self.assertIsNone(shop.get_state(ALICE))

    def test_receipt_for_several_old_invoices_asks_which(self):
        o1 = self.buy(); shop.expire_orders(now=int(time.time()) + shop.ORDER_TTL + 1)
        o2 = self.buy(); shop.expire_orders(now=int(time.time()) + shop.ORDER_TTL + 1)
        bot.handle_update(photo(ALICE))
        kb = self.last()["reply_markup"]["inline_keyboard"]
        self.assertEqual([r[0]["callback_data"] for r in kb], ["c:rcpt:%d" % o2["id"], "c:rcpt:%d" % o1["id"]])
        bot.handle_update(cb(ALICE, "c:rcpt:%d" % o1["id"]))
        self.assertEqual(self.row("SELECT status FROM shop_orders WHERE id=?", (o1["id"],))["status"], "review")
        self.assertEqual(self.row("SELECT status FROM shop_orders WHERE id=?", (o2["id"],))["status"], "expired")

    def test_rejected_invoice_accepts_a_clearer_receipt(self):
        o = self.buy(); bot.handle_update(photo(ALICE)); bot.handle_update(cb(ADMIN, "r:nor:%d:1" % o["id"]))
        bot.handle_update(photo(ALICE, "clear"))
        self.assertEqual(self.row("SELECT status,note FROM shop_orders WHERE id=?", (o["id"],))[:], ("review", None))

    def test_stale_staff_prompt_never_eats_a_command_or_expires(self):
        bot.handle_update(cb(ALICE, "c:report")); bot.handle_update(cb(ALICE, "c:rep_go"))
        bot.handle_update(cb(ALICE, "c:rs:-")); bot.handle_update(cb(ALICE, "c:rc:other")); bot.handle_update(msg(ALICE, "سلام"))
        tid = self.row("SELECT id FROM shop_tickets")["id"]
        bot.handle_update(cb(ADMIN, "t:r:%d" % tid)); bot.handle_update(msg(ADMIN, "/start"))
        self.assertEqual(self.row("SELECT COUNT(*) n FROM shop_ticket_msgs WHERE role='staff'")["n"], 0)
        self.assertEqual(shop.ticket_pending, {})
        bot.handle_update(cb(ADMIN, "t:r:%d" % tid)); bot.handle_update(cb(ADMIN, "list"))   # legacy menu tap
        self.assertEqual(shop.ticket_pending, {})
        bot.handle_update(cb(ADMIN, "t:r:%d" % tid)); shop.ticket_pending[ADMIN]["ts"] -= shop.PENDING_TTL + 1
        bot.handle_update(msg(ADMIN, "دیر شد"))
        self.assertEqual(self.row("SELECT COUNT(*) n FROM shop_ticket_msgs WHERE role='staff'")["n"], 0)
        o = self.buy(); bot.handle_update(photo(ALICE))
        bot.handle_update(cb(ADMIN, "r:nox:%d" % o["id"])); bot.handle_update(msg(ADMIN, "/admin"))
        self.assertEqual(self.row("SELECT status FROM shop_orders WHERE id=?", (o["id"],))["status"], "review")
        self.assertIn("/a/login/", self.last()["text"])              # and /admin itself still works

    def test_admin_keyboard_has_web_login_and_queue_buttons(self):
        bot.handle_update(msg(ADMIN, "/start"))
        buttons = [b["text"] for row in [p for m, p in self.calls if m == "sendMessage"][-2]["reply_markup"]["keyboard"] for b in row]
        self.assertEqual(buttons, [shop.B_LINKS, shop.B_SHOP, shop.B_RECEIPTS, shop.B_TICKETS, shop.B_WEB])
        bot.handle_update(msg(ADMIN, shop.B_WEB)); self.assertIn("/a/login/", self.last()["text"])
        bot.handle_update(msg(ADMIN, shop.B_RECEIPTS)); self.assertIn("رسیدی در انتظار", self.last()["text"])
        bot.handle_update(cb(ADMIN, "sa:users")); bot.handle_update(msg(ADMIN, "/admin"))
        self.assertIn("/a/login/", self.last()["text"])              # a shop prompt never swallows a command

    def test_backup_contains_no_admin_sessions(self):
        bot.meta_set("sess_abc", '{"csrf":"x"}'); bot.meta_set("clean_ips", "1.1.1.1")
        data = shop.make_backup()
        fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        try:
            with open(path, "wb") as f: f.write(data)
            c = sqlite3.connect(path)
            keys = [r[0] for r in c.execute("SELECT k FROM meta")]
            self.assertNotIn("sess_abc", keys); self.assertIn("clean_ips", keys)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM shop_plans").fetchone()[0], 1)
            c.close()
        finally:
            os.unlink(path)
        self.assertNotIn(b"sess_abc", data)                           # VACUUM really dropped the page

    def test_ticket_card_stays_under_telegram_limit_and_caps_messages(self):
        bot.handle_update(cb(ALICE, "c:report")); bot.handle_update(cb(ALICE, "c:rep_go"))
        bot.handle_update(cb(ALICE, "c:rs:-")); bot.handle_update(cb(ALICE, "c:rc:other")); bot.handle_update(msg(ALICE, "x" * 3000))
        tid = self.row("SELECT id FROM shop_tickets")["id"]
        for i in range(shop.MAX_TICKET_MSGS + 5):
            shop._hits.clear()   # the per-user flood limit is a separate guard; this test is about the DB caps
            shop.set_state(ALICE, {"stage": "tk_reply", "id": tid}); bot.handle_update(msg(ALICE, "y" * 3000))
        self.assertLessEqual(self.row("SELECT COUNT(*) n FROM shop_ticket_msgs")["n"], shop.MAX_DAILY_MSGS)
        self.assertIn("زیادی فرستادی", self.texts(ALICE)[-1])
        card = shop.ticket_card(shop.get_ticket(tid), history=10)
        self.assertLessEqual(len(card), 4096)
        self.assertEqual(self.row("SELECT MAX(LENGTH(text)) n FROM shop_ticket_msgs")["n"], shop.MSG_CHARS)

    def test_open_ticket_cap_holds_on_a_stale_keyboard(self):
        for _ in range(shop.MAX_OPEN_TICKETS):
            bot.handle_update(cb(ALICE, "c:report")); bot.handle_update(cb(ALICE, "c:rep_go"))
            bot.handle_update(cb(ALICE, "c:rs:-")); bot.handle_update(cb(ALICE, "c:rc:other")); bot.handle_update(msg(ALICE, "مشکل"))
        bot.handle_update(cb(ALICE, "c:rs:-")); bot.handle_update(cb(ALICE, "c:rc:other")); bot.handle_update(msg(ALICE, "چهارمی"))
        self.assertEqual(self.row("SELECT COUNT(*) n FROM shop_tickets")["n"], shop.MAX_OPEN_TICKETS)

    def test_closed_ticket_is_not_reopened_by_a_stale_customer_state(self):
        bot.handle_update(cb(ALICE, "c:report")); bot.handle_update(cb(ALICE, "c:rep_go"))
        bot.handle_update(cb(ALICE, "c:rs:-")); bot.handle_update(cb(ALICE, "c:rc:other")); bot.handle_update(msg(ALICE, "مشکل"))
        tid = self.row("SELECT id FROM shop_tickets")["id"]
        shop.set_state(ALICE, {"stage": "tk_reply", "id": tid})
        bot.handle_update(cb(ADMIN, "t:x:%d" % tid))
        bot.handle_update(msg(ALICE, "هنوز هستم"))
        self.assertEqual(self.row("SELECT status FROM shop_tickets")["status"], "closed")
        bot.handle_update(cb(ALICE, "c:tkr:%d:0" % tid))             # "not solved" on a closed ticket reopens it
        self.assertEqual(self.row("SELECT status FROM shop_tickets")["status"], "open")

    def test_staff_cannot_be_blocked_before_removal(self):
        bot.handle_update(msg(SUPPORT, "/start"))
        c = bot.db(); c.execute("INSERT OR IGNORE INTO shop_customers(tg_id,first_name,joined_ts) VALUES(?,?,0)", (SUPPORT, "s")); c.commit(); c.close()
        bot.handle_update(cb(ADMIN, "sa:ublock:%d" % SUPPORT))
        self.assertEqual(self.row("SELECT blocked FROM shop_customers WHERE tg_id=?", (SUPPORT,))["blocked"], 0)

    def test_plan_add_rejects_nan_and_duplicate_cards(self):
        bot.handle_update(cb(ADMIN, "sa:plan_add")); bot.handle_update(msg(ADMIN, "nan 30 150000"))
        self.assertEqual(self.row("SELECT COUNT(*) n FROM shop_plans")["n"], 1)
        bot.handle_update(cb(ADMIN, "sa:card_add")); bot.handle_update(msg(ADMIN, "6037991234567890 کسی دیگر"))
        self.assertEqual(self.row("SELECT COUNT(*) n FROM shop_cards")["n"], 1)
        self.assertIn("قبلاً اضافه شده", self.last()["text"])

    def test_services_tap_does_not_query_xray(self):
        tok = self.own()
        bot.refresh_usage = lambda token: (_ for _ in ()).throw(AssertionError("xray must not be called"))
        bot.handle_update(cb(ALICE, "c:svc:%s" % tok))
        self.assertIn(bot.sub_url(tok), self.last("editMessageText")["text"])

    def test_housekeeping_turns_off_auto_renew_of_deleted_links(self):
        tok = self.own(); bot.handle_update(cb(ALICE, "c:auto:%s" % tok))
        c = bot.db(); c.execute("DELETE FROM users WHERE token=?", (tok,)); c.commit(); c.close()
        self.calls.clear(); shop.housekeeping()
        self.assertEqual(self.row("SELECT auto_renew FROM shop_services WHERE token=?", (tok,))["auto_renew"], 0)
        self.assertIn("حذف شد", self.texts(ALICE)[-1])
        shop.housekeeping(); self.assertEqual(len(self.texts(ALICE)), 1)   # once a day

    def test_broadcast_reports_failures_and_waits_on_flood(self):
        self.own(ALICE); bot.handle_update(msg(BOB, "/start")); self.own(BOB)
        slept, real_sleep = [], time.sleep
        def flaky(method, **p):
            self.calls.append((method, p))
            if method == "sendMessage" and p.get("chat_id") == BOB and not any(c == "retried" for c in slept):
                slept.append("retried"); return {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
            if method == "sendMessage" and p.get("chat_id") == ALICE: return {"ok": False, "error_code": 403}
            return {"ok": True, "result": {"message_id": 1}}
        bot.tg = flaky; time.sleep = lambda s: slept.append(s)
        try: sent, failed = shop.broadcast([ALICE, BOB], "hi", pause=0)
        finally: time.sleep = real_sleep
        self.assertEqual((sent, failed), (1, 1)); self.assertIn(1, slept)


class ShopSecondReviewTests(SalesCase):
    """Follow-ups from the independent review of the hardening commit."""

    def own(self, uid=ALICE):
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % uid)); bot.handle_update(msg(ADMIN, "150000 هدیه"))
        bot.handle_update(cb(uid, "c:wpay:%d" % self.plan["id"]))
        return self.row("SELECT token FROM shop_services WHERE tg_id=? ORDER BY created_ts DESC", (uid,))["token"]

    def test_failure_after_the_delivery_commit_never_refunds(self):
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % ALICE)); bot.handle_update(msg(ADMIN, "150000 هدیه"))
        real = shop.deliver_message
        shop.deliver_message = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telegram down"))
        try: bot.handle_update(cb(ALICE, "c:wpay:%d" % self.plan["id"]))
        finally: shop.deliver_message = real
        o = self.row("SELECT * FROM shop_orders")
        self.assertEqual((o["status"], o["token"]), ("approved", "tok0")); self.assertIsNotNone(o["delivered_ts"])
        self.assertEqual(shop.balance(ALICE), 0)                      # paid once, delivered once, no refund
        self.assertIn("از کیف پولت", self.last("editMessageText")["text"])

    def test_refund_is_refused_once_delivered(self):
        tok = self.own(); o = self.row("SELECT * FROM shop_orders")
        self.assertFalse(shop.refund_undelivered(o["id"], ALICE, o["price"]))
        self.assertEqual(shop.balance(ALICE), 0)

    def test_undeliverable_wallet_order_is_refunded_after_a_day(self):
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % ALICE)); bot.handle_update(msg(ADMIN, "150000 هدیه"))
        bot.create_user = lambda *a, **k: None
        c = bot.db(); c.execute("UPDATE shop_customers SET wallet=0 WHERE tg_id=?", (ALICE,)); c.commit(); c.close()
        # simulate a crash right after the debit: approved, undelivered, wallet already charged
        now = int(time.time())
        c = bot.db()
        c.execute("INSERT INTO shop_orders(tg_id,kind,plan_id,gb,days,price,amount,status,created_ts,decided_ts,pay_method) "
                  "VALUES(?,?,?,?,?,?,?,'approved',?,?,'wallet')", (ALICE, "buy", self.plan["id"], 30, 30, 150000, 150000, now - 90000, now - 90000))
        c.commit(); c.close()
        shop.retry_deliveries(now=now - 80000)                        # still inside the retry window: nothing
        self.assertEqual(self.row("SELECT status FROM shop_orders ORDER BY id DESC")["status"], "approved")
        shop.retry_deliveries(now=now)
        self.assertEqual(self.row("SELECT status FROM shop_orders ORDER BY id DESC")["status"], "failed")
        self.assertEqual(shop.balance(ALICE), 150000)
        self.assertIn("برگشت", self.texts(ALICE)[-1])

    def test_admin_prompt_expires(self):
        bot.handle_update(cb(ADMIN, "sa:uwal:%d:add" % ALICE))
        bot.pending[ADMIN]["ts"] -= shop.PENDING_TTL + 1
        bot.handle_update(msg(ADMIN, "50000 بازگشت وجه"))
        self.assertEqual(shop.balance(ALICE), 0); self.assertNotIn(ADMIN, bot.pending)

    def test_receipt_asks_when_a_previous_invoice_was_auto_cancelled(self):
        o1 = self.buy(); o2 = self.buy()                                 # buying again cancels o1, but it may have been paid
        self.assertEqual(self.row("SELECT status FROM shop_orders WHERE id=?", (o1["id"],))["status"], "cancelled")
        bot.handle_update(photo(ALICE))
        kb = self.last()["reply_markup"]["inline_keyboard"]
        self.assertEqual([r[0]["callback_data"] for r in kb], ["c:rcpt:%d" % o2["id"], "c:rcpt:%d" % o1["id"]])
        bot.handle_update(cb(ALICE, "c:rcpt:%d" % o1["id"]))
        self.assertEqual(self.row("SELECT status FROM shop_orders WHERE id=?", (o1["id"],))["status"], "review")

    def test_daily_backup_is_built_once_and_sent_to_every_owner(self):
        bot.ADMIN_IDS.update({ADMIN, 2}); shop.set_setting("backup_auto", "1")
        builds, sent = [], []
        real_make, real_upload = shop.make_backup, shop.upload
        shop.make_backup = lambda: builds.append(1) or b"db"
        shop.upload = lambda method, chat, *a, **k: sent.append(chat) or {"ok": chat != ADMIN}   # first owner fails
        try:
            shop._tick_backup(); shop._tick_backup()
        finally:
            shop.make_backup, shop.upload = real_make, real_upload
        self.assertEqual(len(builds), 1); self.assertEqual(sent, [ADMIN, 2])
        self.assertEqual(shop.setting("backup_day"), bot.day_key())

    def test_outage_end_waits_for_the_running_broadcast(self):
        self.own(ALICE)
        bot.handle_update(cb(ADMIN, "sa:outage")); bot.handle_update(msg(ADMIN, "کند است"))
        real_bg = shop.run_bg; shop.run_bg = lambda fn: None             # the sending thread never runs: lock stays held
        try:
            bot.handle_update(cb(ADMIN, "sa:outage_send"))
            bot.handle_update(cb(ADMIN, "sa:outage")); bot.handle_update(cb(ADMIN, "sa:outage_end"))
            self.assertTrue(any("هنوز تمام نشده" in t for t in self.texts(ADMIN))); self.assertTrue(shop.setting("outage"))
        finally:
            shop.run_bg = real_bg; shop._bc_lock.release()
