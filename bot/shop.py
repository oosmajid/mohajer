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
    _add_cols(c, "shop_customers", [("test_used_ts", "INTEGER NOT NULL DEFAULT 0")])
    # Which customer owns which mohajer link (users.token). kind = test | paid.
    c.execute("""CREATE TABLE IF NOT EXISTS shop_services(
        token TEXT PRIMARY KEY, tg_id INTEGER NOT NULL, kind TEXT NOT NULL,
        created_ts INTEGER NOT NULL, followup_sent INTEGER NOT NULL DEFAULT 0, feedback TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS shop_services_owner ON shop_services(tg_id)")
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

def flag(key, default="0"):
    return setting(key, default) == "1"

# ---------------- formatting ----------------
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

def fa(x):
    return str(x).translate(_FA_DIGITS)

def jalali(ts):
    """Unix ts -> (year, month, day) in the Persian calendar, Iran time."""
    g = time.gmtime(ts + core.IRAN_OFFSET); gy, gm, gd = g.tm_year, g.tm_mon, g.tm_mday
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053); days %= 12053
    jy += 4 * (days // 1461); days %= 1461
    if days > 365:
        jy += (days - 1) // 365; days = (days - 1) % 365
    if days < 186: jm, jd = 1 + days // 31, 1 + days % 31
    else: jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd

def fa_date(ts):
    return fa("%d/%02d/%02d" % jalali(ts))

def fa_size(mb):
    return fa("%g گیگ" % (mb / 1024)) if mb >= 1024 else fa("%d مگ" % mb)

def fa_hours(h):
    return fa("%g روز" % (h / 24)) if h >= 24 and h % 24 == 0 else fa("%d ساعت" % h)

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

def link_kb(token, extra=None):
    """Open-in-browser page = QR + copy + one-tap import into Happ/v2rayNG (subserver.py)."""
    rows = [[{"text": "📲 باز کردن صفحهٔ اتصال", "url": core.sub_url(token)}],
            [{"text": "📚 آموزش اتصال", "callback_data": "c:guide"}]]
    return rows + (extra or [])

# --- free test ---
TEST_DEFAULT_MB, TEST_DEFAULT_HOURS = 500, 24

def test_mb():    return int(setting("test_mb", TEST_DEFAULT_MB))
def test_hours(): return int(setting("test_hours", TEST_DEFAULT_HOURS))
def test_enabled(): return flag("test_enabled", "1")

def test_offer(chat, uid):
    cust = get_customer(uid)
    if cust["test_used_ts"]:
        core.send(chat, "🎁 تست رایگانت رو قبلاً گرفتی (%s).\nبرای ادامه می‌تونی یه سرویس بخری 👇" % fa_date(cust["test_used_ts"]),
                  [[{"text": "🛒 خرید سرویس", "callback_data": "c:buy"}]]); return
    if not test_enabled():
        say(chat, "🎁 فعلاً تست رایگان نداریم؛ به‌زودی دوباره فعال می‌شه."); return
    core.send(chat, "🎁 <b>تست رایگان</b>\n\n📦 %s · ⏳ %s\nهر حساب فقط یک بار می‌تونه تست بگیره."
              % (fa_size(test_mb()), fa_hours(test_hours())),
              [[{"text": "✅ دریافت تست", "callback_data": "c:test"}]])

def claim_test(chat, mid, uid, first_name):
    """One test per Telegram account: the claim is an atomic UPDATE, undone if minting fails."""
    if not test_enabled():
        core.edit(chat, mid, "🎁 فعلاً تست رایگان نداریم."); return
    now = int(time.time())
    c = core.db()
    claimed = c.execute("UPDATE shop_customers SET test_used_ts=? WHERE tg_id=? AND test_used_ts=0", (now, uid)).rowcount
    c.commit(); c.close()
    if not claimed:
        core.edit(chat, mid, "🎁 تست رایگانت رو قبلاً گرفتی."); return
    token = None
    try: token = core.create_user(test_mb() / 1024, test_hours() / 24, label=("تست %s" % (first_name or uid))[:40])
    except Exception as e: print("shop test err", e, flush=True)
    c = core.db()
    if token:
        c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts) VALUES(?,?,'test',?)", (token, uid, now))
    else:
        c.execute("UPDATE shop_customers SET test_used_ts=0 WHERE tg_id=?", (uid,))
    c.commit(); c.close()
    if not token:
        core.edit(chat, mid, "❌ ساخت تست الان ممکن نشد؛ چند دقیقهٔ دیگه دوباره امتحان کن."); return
    core.edit(chat, mid, "✅ <b>تست رایگانت آماده‌ست!</b>\n📦 %s · ⏳ %s\n\n🔗 <code>%s</code>\n\n"
              "روی «باز کردن صفحهٔ اتصال» بزن تا با یک لمس به برنامه اضافه بشه."
              % (fa_size(test_mb()), fa_hours(test_hours()), core.sub_url(token)), link_kb(token))

# --- follow-up before a test ends ---
FOLLOWUP_HOURS, FOLLOWUP_USED = 3, 0.8   # ask when <3h are left or 80% of the data is used

def followup_due(u, now):
    if u["disabled_ts"]: return True
    if u["expiry_ts"] and u["expiry_ts"] - now <= FOLLOWUP_HOURS * 3600: return True
    return bool(u["limit_bytes"]) and core.current_usage(u) >= FOLLOWUP_USED * u["limit_bytes"]

def send_test_followups(now=None):
    now = int(time.time()) if now is None else now
    c = core.db()
    rows = c.execute("SELECT s.token, s.tg_id, u.token AS live, u.disabled_ts, u.expiry_ts, u.limit_bytes, "
                     "u.used_bytes, u.usage_reset_bytes FROM shop_services s LEFT JOIN users u ON u.token=s.token "
                     "WHERE s.kind='test' AND s.followup_sent=0").fetchall()
    c.close()
    for r in rows:
        if r["live"] and not followup_due(r, now): continue
        c = core.db()   # mark first: a crash must never make us ask twice
        c.execute("UPDATE shop_services SET followup_sent=1 WHERE token=?", (r["token"],)); c.commit(); c.close()
        if not r["live"]: continue   # link already gone; too late to ask
        core.send(r["tg_id"], "⏳ تست رایگانت داره تموم می‌شه.\nراضی بودی؟",
                  [[{"text": "👍 عالی بود", "callback_data": "c:fb:good:%s" % r["token"]},
                    {"text": "👎 مشکل داشتم", "callback_data": "c:fb:bad:%s" % r["token"]}]])

def record_feedback(chat, mid, uid, data):
    _, _, kind, token = data.split(":", 3)
    c = core.db()
    done = c.execute("UPDATE shop_services SET feedback=? WHERE token=? AND tg_id=? AND feedback IS NULL",
                     (kind, token, uid)).rowcount
    c.commit(); c.close()
    if kind == "good":
        core.edit(chat, mid, "😍 خوشحالیم که راضی بودی!\nبا خرید سرویس، همین کیفیت رو با حجم و زمان بیشتر داری.",
                  [[{"text": "🛒 خرید سرویس", "callback_data": "c:buy"}]])
    else:
        core.edit(chat, mid, "😔 متأسفیم که اذیت شدی.\nبگو چی شد تا بررسی کنیم؛ معمولاً با یه تنظیم ساده درست می‌شه.",
                  [[{"text": "🛠 گزارش اختلال", "callback_data": "c:report"}],
                   [{"text": "📚 آموزش اتصال", "callback_data": "c:guide"}]])
        if done:
            cust = get_customer(uid)
            for a in owner_ids():
                core.send(a, "👎 یک مشتری از تست رایگان راضی نبود: %s · <code>%d</code>"
                          % (html.escape(cust["first_name"] or ""), uid))

# --- account ---
def account_text(uid):
    cust = get_customer(uid)
    c = core.db()
    paid = c.execute("SELECT COUNT(*) FROM shop_services WHERE tg_id=? AND kind='paid'", (uid,)).fetchone()[0]
    c.close()
    return ("👤 <b>حساب من</b>\n\n🙂 %s\n🆔 آیدی عددی: <code>%d</code>\n📅 عضویت: %s\n🛍 تعداد خرید: %s\n🎁 تست رایگان: %s"
            % (html.escape(cust["first_name"] or "-"), uid, fa_date(cust["joined_ts"]), fa(paid),
               ("گرفته‌شده" if cust["test_used_ts"] else "هنوز نگرفتی")))

# --- connection guide ---
GUIDE_APPS = [   # same Happ builds the subscriber page links to (subserver.HAPP_APPS)
    ("android", "📱 اندروید", "https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk"),
    ("ios", "🍏 آیفون", "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"),
    ("windows", "💻 ویندوز", "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe"),
    ("mac", "🖥 مک", "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg"),
]

def guide_kb():
    rows = [[{"text": t, "callback_data": "c:guide:%s" % k} for k, t, _ in GUIDE_APPS[i:i + 2]] for i in (0, 2)]
    return rows

GUIDE_TEXT = "📚 <b>آموزش اتصال</b>\nدستگاهت رو انتخاب کن:"

def guide_steps(key):
    _, title, url = next(g for g in GUIDE_APPS if g[0] == key)
    return ("📚 <b>آموزش اتصال · %s</b>\n\n"
            "۱. برنامهٔ <b>Happ</b> رو از دکمهٔ پایین نصب کن.\n"
            "۲. از «📦 سرویس‌های من» روی «📲 باز کردن صفحهٔ اتصال» بزن.\n"
            "۳. توی صفحه‌ای که باز می‌شه «افزودن به Happ» رو بزن.\n"
            "۴. توی Happ دکمهٔ اتصال رو بزن ✅\n\n"
            "💡 اگه وصل نشد، توی Happ گزینهٔ آپدیت اشتراک رو بزن." % title,
            [[{"text": "⬇️ دانلود Happ", "url": url}], [{"text": "بازگشت", "callback_data": "c:guides"}]])

# --- routing ---
def customer_message(chat, uid, m):
    text = (m.get("text") or "").strip()
    if text.startswith("/start"):
        set_state(uid, None)
        customer_menu(chat, welcome_text(m["from"].get("first_name"))); return
    if text in {b for row in CUSTOMER_MENU for b in row}:
        set_state(uid, None)
        if text == B_TEST: test_offer(chat, uid); return
        if text == B_ACCOUNT: core.send(chat, account_text(uid)); return
        if text == B_GUIDE: core.send(chat, GUIDE_TEXT, guide_kb()); return
        say(chat, SOON_TEXT); return
    customer_menu(chat)

def customer_callback(chat, mid, uid, data, cbid, user=None):
    if data == "c:test":
        core.answer(cbid); claim_test(chat, mid, uid, (user or {}).get("first_name")); return
    if data == "c:guide":
        core.answer(cbid); core.send(chat, GUIDE_TEXT, guide_kb()); return   # keeps the link message above
    if data == "c:guides":
        core.answer(cbid); core.edit(chat, mid, GUIDE_TEXT, guide_kb()); return
    if data.startswith("c:guide:") and data[8:] in {g[0] for g in GUIDE_APPS}:
        core.answer(cbid); text, kb = guide_steps(data[8:]); core.edit(chat, mid, text, kb); return
    if data.startswith("c:fb:") and data.count(":") == 3:
        core.answer(cbid); record_feedback(chat, mid, uid, data); return
    if data in ("c:buy", "c:report"):
        core.answer(cbid, SOON_TEXT); return
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
        customer_callback(chat, mid, uid, cq.get("data", ""), cq["id"], user); return
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
            [{"text": "🎁 تست رایگان", "callback_data": "sa:test"}],
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

TEST_MB_CHOICES = (200, 500, 1024, 2048)
TEST_HOUR_CHOICES = (6, 12, 24, 48)

def test_admin_text():
    c = core.db(); given = c.execute("SELECT COUNT(*) FROM shop_services WHERE kind='test'").fetchone()[0]
    liked = c.execute("SELECT COUNT(*) FROM shop_services WHERE kind='test' AND feedback='good'").fetchone()[0]
    disliked = c.execute("SELECT COUNT(*) FROM shop_services WHERE kind='test' AND feedback='bad'").fetchone()[0]
    c.close()
    return ("🎁 <b>تست رایگان</b>\n\nوضعیت: <b>%s</b>\nحجم: <b>%s</b> · زمان: <b>%s</b>\n\n"
            "تست‌های داده‌شده: %s · 👍 %s · 👎 %s\n\nهر حساب تلگرام فقط یک بار تست می‌گیرد."
            % ("روشن" if test_enabled() else "خاموش", fa_size(test_mb()), fa_hours(test_hours()),
               fa(given), fa(liked), fa(disliked)))

def test_admin_kb():
    mark = lambda on, t: ("✅ " + t) if on else t
    return [[{"text": ("⏸ خاموش کردن تست" if test_enabled() else "▶️ روشن کردن تست"), "callback_data": "sa:test_toggle"}],
            [{"text": mark(mb == test_mb(), fa_size(mb)), "callback_data": "sa:test_mb:%d" % mb} for mb in TEST_MB_CHOICES],
            [{"text": mark(h == test_hours(), fa_hours(h)), "callback_data": "sa:test_hours:%d" % h} for h in TEST_HOUR_CHOICES],
            [{"text": "بازگشت", "callback_data": "sa:menu"}]]

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
    if data == "sa:test":
        core.answer(cbid); core.edit(chat, mid, test_admin_text(), test_admin_kb()); return
    if data == "sa:test_toggle":
        on = not test_enabled(); set_setting("test_enabled", "1" if on else "0")
        log(uid, "test_on" if on else "test_off")
        core.answer(cbid); core.edit(chat, mid, test_admin_text(), test_admin_kb()); return
    for key, choices in (("test_mb", TEST_MB_CHOICES), ("test_hours", TEST_HOUR_CHOICES)):
        if data.startswith("sa:%s:" % key):
            v = int(data.rsplit(":", 1)[1])
            if v in choices and str(v) != setting(key):
                set_setting(key, v); log(uid, key, v)
                core.edit(chat, mid, test_admin_text(), test_admin_kb())
            core.answer(cbid); return
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

# ---------------- background ticker ----------------
TICK_SECONDS = 60

def tick():
    if backup_due():
        set_setting("backup_day", core.day_key())
        for a in owner_ids(): send_backup(a)
    send_test_followups()

def ticker():
    while True:
        try: tick()
        except Exception as e: print("shop tick err", e, flush=True)
        time.sleep(TICK_SECONDS)

def start_threads():
    threading.Thread(target=ticker, daemon=True).start()
