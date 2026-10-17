# Mohajer shop - the customer storefront of the Telegram bot (stdlib only).
# bot.py keeps answering its admins exactly as before and hands every other update
# here: customers (buy / test / wallet / services / referrals / tickets) and support
# staff. This module reuses bot.py's core (db, tg, create_user, enforcer state ...)
# through `core`, which bot.py binds once at import time (see bind()).
# Roadmap and feature list: docs/SHOP.md.
import os, json, time, html, sqlite3, secrets, tempfile, threading, urllib.request

core = None   # the bot module (bot.py); set by bind()

def bind(module):
    global core
    core = module

# ---------------- schema ----------------
def _add_cols(c, table, cols):
    have = {r[1] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()}
    for name, decl in cols:
        if name not in have:
            c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))

def init_db():
    """Create/migrate the shop tables. Idempotent; called at the end of core.init_db()."""
    c = core.db()
    c.execute("""CREATE TABLE IF NOT EXISTS shop_customers(
        tg_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT,
        joined_ts INTEGER NOT NULL, last_seen INTEGER NOT NULL DEFAULT 0,
        referrer_id INTEGER, blocked INTEGER NOT NULL DEFAULT 0,
        state TEXT)""")                       # state = JSON of the customer's current input step
    c.execute("""CREATE TABLE IF NOT EXISTS shop_staff(
        tg_id INTEGER PRIMARY KEY, role TEXT NOT NULL, added_ts INTEGER NOT NULL, added_by INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS shop_admin_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, actor INTEGER,
        action TEXT NOT NULL, target TEXT, detail TEXT)""")
    c.commit(); c.close()

# ---------------- settings (meta, prefixed) ----------------
def setting(key, default=None):
    return core.meta_get("shop_" + key, default)

def set_setting(key, value):
    core.meta_set("shop_" + key, value)

def flag(key):
    return setting(key, "0") == "1"

# ---------------- roles ----------------
ROLE_SUPPORT = "support"   # sees receipts + tickets; no settings, plans or staff

def owner_ids():
    if core.ADMIN_IDS: return sorted(core.ADMIN_IDS)
    a = core.meta_get("admin_id")
    return [int(a)] if a else []

def staff_role(uid):
    c = core.db(); r = c.execute("SELECT role FROM shop_staff WHERE tg_id=?", (uid,)).fetchone(); c.close()
    return r["role"] if r else None

def log(actor, action, target="", detail=""):
    """Audit trail of everything an admin or support member changes."""
    c = core.db()
    c.execute("INSERT INTO shop_admin_log(ts,actor,action,target,detail) VALUES(?,?,?,?,?)",
              (int(time.time()), actor, action, str(target), str(detail)))
    c.commit(); c.close()

# ---------------- telegram helpers ----------------
def reply_kb(rows):
    return {"keyboard": [[{"text": t} for t in row] for row in rows],
            "resize_keyboard": True, "is_persistent": True}

def say(chat, text, markup=None):
    """sendMessage with any reply_markup (core.send only takes inline keyboards)."""
    p = dict(chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview="true")
    if markup is not None: p["reply_markup"] = markup
    return core.tg("sendMessage", **p)

def upload(method, chat, field, filename, data, caption=""):
    """Multipart upload (sendDocument/sendPhoto); core.tg only urlencodes."""
    b = secrets.token_hex(16)
    parts = []
    for k, v in (("chat_id", str(chat)), ("caption", caption), ("parse_mode", "HTML")):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode())
    parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n' % (b, field, filename)).encode() + data + b"\r\n")
    parts.append(("--%s--\r\n" % b).encode())
    req = urllib.request.Request(core.API_URL + method, data=b"".join(parts),
                                 headers={"Content-Type": "multipart/form-data; boundary=" + b})
    try:
        with urllib.request.urlopen(req, timeout=120, context=core.SSLCTX) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print("tg upload err", method, e, flush=True); return {}

# ---------------- anti-spam ----------------
RATE_N, RATE_WINDOW = 20, 10      # at most 20 updates per 10s per user
_hits = {}
_hits_lock = threading.Lock()

def rate_limited(uid, now=None):
    now = time.time() if now is None else now
    with _hits_lock:
        recent = [t for t in _hits.get(uid, ()) if now - t < RATE_WINDOW]
        limited = len(recent) >= RATE_N
        if not limited: recent.append(now)
        _hits[uid] = recent
        if len(_hits) > 5000:     # forget idle users so the dict stays tiny
            for k in [k for k, v in _hits.items() if not v or now - v[-1] >= RATE_WINDOW]:
                _hits.pop(k, None)
        return limited

# ---------------- customers ----------------
def get_customer(uid):
    c = core.db(); r = c.execute("SELECT * FROM shop_customers WHERE tg_id=?", (uid,)).fetchone(); c.close()
    return r

def touch_customer(user, referrer=None):
    """Register on first contact (remembering who invited them), refresh name/last_seen after."""
    uid = user["id"]; now = int(time.time())
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM shop_customers WHERE tg_id=?", (uid,)).fetchone():
            c.execute("UPDATE shop_customers SET first_name=?,username=?,last_seen=? WHERE tg_id=?",
                      (user.get("first_name", ""), user.get("username"), now, uid))
        else:
            if referrer == uid or not (referrer and c.execute(
                    "SELECT 1 FROM shop_customers WHERE tg_id=?", (referrer,)).fetchone()):
                referrer = None   # only a real, different, existing customer can be the inviter
            c.execute("INSERT INTO shop_customers(tg_id,first_name,username,joined_ts,last_seen,referrer_id) "
                      "VALUES(?,?,?,?,?,?)", (uid, user.get("first_name", ""), user.get("username"), now, now, referrer))
        c.commit()
    finally:
        c.close()
    return get_customer(uid)

def get_state(uid):
    r = get_customer(uid)
    try: return json.loads(r["state"]) if r and r["state"] else None
    except ValueError: return None

def set_state(uid, state):
    c = core.db()
    c.execute("UPDATE shop_customers SET state=? WHERE tg_id=?", (json.dumps(state) if state else None, uid))
    c.commit(); c.close()

def start_referrer(text):
    """'/start ref_123' -> 123 (the deep-link payload of an invite link)."""
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ref_") and parts[1][4:].isdigit():
        return int(parts[1][4:])
    return None

# ---------------- customer UI ----------------
B_BUY, B_TEST, B_SERVICES, B_WALLET = "🛒 خرید سرویس", "🎁 تست رایگان", "📦 سرویس‌های من", "💰 کیف پول"
B_INVITE, B_REPORT, B_GUIDE, B_ACCOUNT = "👥 دعوت دوستان", "🛠 گزارش اختلال", "📚 آموزش اتصال", "👤 حساب من"
CUSTOMER_MENU = [[B_BUY, B_TEST], [B_SERVICES, B_WALLET], [B_INVITE, B_REPORT], [B_GUIDE, B_ACCOUNT]]

MAINTENANCE_TEXT = "🛠 در حال به‌روزرسانی ربات هستیم.\nچند دقیقهٔ دیگر دوباره سر بزن 🙏"
BLOCKED_TEXT = "⛔️ دسترسی شما به این ربات محدود شده است."
SOON_TEXT = "⏳ این بخش به‌زودی فعال می‌شود."

def shop_name():
    return setting("name", "مهاجر")

def welcome_text(name):
    return ("سلام %s 👋\nبه فروشگاه <b>%s</b> خوش اومدی.\n\nاز منوی پایین یکی رو انتخاب کن 👇"
            % (html.escape(name or "دوست من"), html.escape(shop_name())))

def customer_menu(chat, text="از منوی پایین یکی رو انتخاب کن 👇"):
    say(chat, text, reply_kb(CUSTOMER_MENU))

def customer_message(chat, uid, m):
    text = (m.get("text") or "").strip()
    if text.startswith("/start"):
        set_state(uid, None)
        customer_menu(chat, welcome_text(m["from"].get("first_name"))); return
    if text in {b for row in CUSTOMER_MENU for b in row}:
        set_state(uid, None)
        say(chat, SOON_TEXT); return
    customer_menu(chat)

def customer_callback(chat, mid, uid, data, cbid):
    core.answer(cbid)

# ---------------- support staff UI ----------------
B_RECEIPTS, B_TICKETS = "🧾 رسیدها", "🎫 تیکت‌ها"
SUPPORT_MENU = [[B_RECEIPTS, B_TICKETS]]

def support_message(chat, uid, m):
    text = (m.get("text") or "").strip()
    if text in (B_RECEIPTS, B_TICKETS):
        say(chat, SOON_TEXT); return
    say(chat, "🎧 <b>پنل پشتیبانی</b>\nاز منوی پایین یکی رو انتخاب کن 👇", reply_kb(SUPPORT_MENU))

def support_callback(chat, mid, uid, data, cbid):
    core.answer(cbid)

# ---------------- entry point for every non-admin update ----------------
def on_update(up):
    if "callback_query" in up:
        cq = up["callback_query"]; msg = cq.get("message") or {}
        user = cq["from"]; uid = user["id"]
        chat = msg.get("chat", {}).get("id"); mid = msg.get("message_id")
        if rate_limited(uid) or chat is None: core.answer(cq["id"]); return
        if staff_role(uid): support_callback(chat, mid, uid, cq.get("data", ""), cq["id"]); return
        cust = touch_customer(user)
        if cust["blocked"]: core.answer(cq["id"], BLOCKED_TEXT); return
        if flag("maintenance"): core.answer(cq["id"], "🛠 در حال به‌روزرسانی هستیم"); return
        customer_callback(chat, mid, uid, cq.get("data", ""), cq["id"]); return
    m = up.get("message")
    if not m or "from" not in m or m.get("chat", {}).get("type") != "private": return
    uid = m["from"]["id"]; chat = m["chat"]["id"]
    if rate_limited(uid): return
    if staff_role(uid): support_message(chat, uid, m); return
    text = m.get("text") or ""
    cust = touch_customer(m["from"], start_referrer(text) if text.startswith("/start") else None)
    if cust["blocked"]: say(chat, BLOCKED_TEXT, {"remove_keyboard": True}); return
    if flag("maintenance"): say(chat, MAINTENANCE_TEXT); return
    customer_message(chat, uid, m)

# ---------------- admin side ----------------
B_LINKS, B_SHOP = "🔐 پنل لینک‌ها", "🛍 مدیریت فروشگاه"
ADMIN_MENU = [[B_LINKS, B_SHOP]]

def admin_start(chat):
    say(chat, "👋 سلام ادمین", reply_kb(ADMIN_MENU))
    core.send(chat, core.WELCOME, core.main_menu_kb())

def _count(sql, args=()):
    c = core.db(); n = c.execute(sql, args).fetchone()[0]; c.close(); return n

def shop_admin_text():
    return ("🛍 <b>مدیریت فروشگاه</b>\n\n👥 مشتری‌ها: <b>%d</b>\n🎧 پشتیبان‌ها: <b>%d</b>\n"
            "🛠 حالت تعمیرات: <b>%s</b>\n💾 بکاپ روزانه: <b>%s</b>"
            % (_count("SELECT COUNT(*) FROM shop_customers"), _count("SELECT COUNT(*) FROM shop_staff"),
               "روشن" if flag("maintenance") else "خاموش", "روشن" if flag("backup_auto") else "خاموش"))

def shop_admin_kb():
    return [[{"text": ("✅ خاموش کردن تعمیرات" if flag("maintenance") else "🛠 روشن کردن تعمیرات"), "callback_data": "sa:maint"}],
            [{"text": "🎧 پشتیبان‌ها", "callback_data": "sa:staff"}, {"text": "📜 لاگ کارها", "callback_data": "sa:log"}],
            [{"text": "💾 بکاپ الان", "callback_data": "sa:backup"},
             {"text": ("⏹ قطع بکاپ روزانه" if flag("backup_auto") else "🔁 بکاپ روزانه"), "callback_data": "sa:backup_auto"}],
            [{"text": "بازگشت", "callback_data": "menu"}]]

def staff_kb():
    c = core.db(); rows = c.execute("SELECT s.tg_id, c.first_name FROM shop_staff s LEFT JOIN shop_customers c "
                                     "ON c.tg_id=s.tg_id ORDER BY s.added_ts").fetchall(); c.close()
    kb = [[{"text": "❌ %s · %d" % (r["first_name"] or "پشتیبان", r["tg_id"]), "callback_data": "sa:staff_rm:%d" % r["tg_id"]}]
          for r in rows]
    kb.append([{"text": "➕ افزودن پشتیبان", "callback_data": "sa:staff_add"}])
    kb.append([{"text": "بازگشت", "callback_data": "sa:menu"}])
    return kb

STAFF_TEXT = ("🎧 <b>پشتیبان‌ها</b>\nپشتیبان فقط رسیدها و تیکت‌ها را می‌بیند؛ به تنظیمات و پلن‌ها دسترسی ندارد.\n"
              "برای حذف، روی اسمش بزن.")

def log_text(limit=15):
    c = core.db(); rows = c.execute("SELECT * FROM shop_admin_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall(); c.close()
    if not rows: return "📜 <b>لاگ کارها</b>\n\nهنوز کاری ثبت نشده."
    lines = ["• %s · <code>%s</code> · %s %s" % (time.strftime("%m/%d %H:%M", time.gmtime(r["ts"] + core.IRAN_OFFSET)),
                                                r["actor"], html.escape(r["action"]), html.escape(r["target"] or ""))
             for r in rows]
    return "📜 <b>لاگ کارها</b> (آخرین %d مورد)\n\n%s" % (limit, "\n".join(lines))

def admin_callback(chat, mid, uid, data, cbid):
    """Inline buttons of the shop admin menu; every callback_data starts with 'sa:'."""
    core.pending.pop(chat, None)
    if data == "sa:menu":
        core.answer(cbid); core.edit(chat, mid, shop_admin_text(), shop_admin_kb()); return
    if data == "sa:maint":
        on = not flag("maintenance"); set_setting("maintenance", "1" if on else "0")
        log(uid, "maintenance_on" if on else "maintenance_off")
        core.answer(cbid, "حالت تعمیرات %s شد" % ("روشن" if on else "خاموش"))
        core.edit(chat, mid, shop_admin_text(), shop_admin_kb()); return
    if data == "sa:backup_auto":
        on = not flag("backup_auto"); set_setting("backup_auto", "1" if on else "0")
        log(uid, "backup_auto_on" if on else "backup_auto_off")
        core.answer(cbid); core.edit(chat, mid, shop_admin_text(), shop_admin_kb()); return
    if data == "sa:backup":
        core.answer(cbid, "در حال ارسال بکاپ…")
        ok = send_backup(chat); log(uid, "backup_sent" if ok else "backup_failed")
        if not ok: core.send(chat, "❌ ارسال بکاپ ناموفق بود.")
        return
    if data == "sa:log":
        core.answer(cbid); core.edit(chat, mid, log_text(), [[{"text": "بازگشت", "callback_data": "sa:menu"}]]); return
    if data == "sa:staff":
        core.answer(cbid); core.edit(chat, mid, STAFF_TEXT, staff_kb()); return
    if data == "sa:staff_add":
        core.pending[chat] = {"stage": "sa_staff_add"}; core.answer(cbid)
        core.edit(chat, mid, "آیدی عددی پشتیبان را بفرست، یا یک پیام از او را اینجا فوروارد کن:",
                  [[{"text": "انصراف", "callback_data": "sa:staff"}]]); return
    if data.startswith("sa:staff_rm:"):
        tid = int(data.rsplit(":", 1)[1])
        c = core.db(); c.execute("DELETE FROM shop_staff WHERE tg_id=?", (tid,)); c.commit(); c.close()
        log(uid, "staff_remove", tid)
        core.answer(cbid, "حذف شد"); core.edit(chat, mid, STAFF_TEXT, staff_kb()); return
    core.answer(cbid)

def _forwarded_user_id(m):
    origin = m.get("forward_origin") or {}
    if origin.get("type") == "user": return origin.get("sender_user", {}).get("id")
    return (m.get("forward_from") or {}).get("id")

def admin_message(chat, uid, m):
    """Admin reply-keyboard buttons and shop input steps. Returns True when handled."""
    text = (m.get("text") or "").strip()
    if text == B_LINKS:
        core.pending.pop(chat, None); core.send(chat, core.WELCOME, core.main_menu_kb()); return True
    if text == B_SHOP:
        core.pending.pop(chat, None); core.send(chat, shop_admin_text(), shop_admin_kb()); return True
    st = core.pending.get(chat)
    if st and st.get("stage") == "sa_staff_add":
        tid = _forwarded_user_id(m) or (int(text) if text.isdigit() else None)
        if not tid:
            core.send(chat, "آیدی عددی بفرست (مثلاً <code>123456789</code>) یا پیامی از او فوروارد کن:"); return True
        if tid in owner_ids():
            core.send(chat, "این شخص خودش ادمین است.", staff_kb()); core.pending.pop(chat, None); return True
        c = core.db()
        c.execute("INSERT INTO shop_staff(tg_id,role,added_ts,added_by) VALUES(?,?,?,?) "
                  "ON CONFLICT(tg_id) DO UPDATE SET role=excluded.role", (tid, ROLE_SUPPORT, int(time.time()), uid))
        c.commit(); c.close()
        core.pending.pop(chat, None); log(uid, "staff_add", tid)
        core.send(chat, "✅ <code>%d</code> پشتیبان شد. کافیست ربات را استارت کند.\n\n%s" % (tid, STAFF_TEXT), staff_kb())
        return True
    return False

# ---------------- backups ----------------
def make_backup():
    """Consistent snapshot of the live DB (sqlite online backup; safe under WAL writers)."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        src = core.db(); dst = sqlite3.connect(path)
        try: src.backup(dst)
        finally: dst.close(); src.close()
        with open(path, "rb") as f: return f.read()
    finally:
        os.unlink(path)

def send_backup(chat):
    name = "mohajer-%s.db" % core.day_key()
    r = upload("sendDocument", chat, "document", name, make_backup(),
               caption="💾 بکاپ دیتابیس · %s\n⚠️ شامل اطلاعات حساس است؛ جایی منتشرش نکن." % core.day_key())
    return bool(r.get("ok"))

BACKUP_HOUR = 4   # Iran time; quiet hours

def backup_due(now=None):
    now = time.time() if now is None else now
    hour = time.gmtime(now + core.IRAN_OFFSET).tm_hour
    return flag("backup_auto") and hour >= BACKUP_HOUR and setting("backup_day") != core.day_key(now)

def backup_loop():
    while True:
        try:
            if backup_due():
                set_setting("backup_day", core.day_key())
                for a in owner_ids(): send_backup(a)
        except Exception as e:
            print("backup err", e, flush=True)
        time.sleep(600)

def start_threads():
    threading.Thread(target=backup_loop, daemon=True).start()
