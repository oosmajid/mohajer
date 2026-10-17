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
    _add_cols(c, "shop_customers", [("test_used_ts", "INTEGER NOT NULL DEFAULT 0"),
                                    ("wallet", "INTEGER NOT NULL DEFAULT 0")])   # toman; only wallet_apply() changes it
    # Wallet ledger: every balance change, with the balance after it. (kind, ref) is unique so
    # a retried credit for the same order can never be applied twice.
    c.execute("""CREATE TABLE IF NOT EXISTS shop_wallet_tx(
        id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL, delta INTEGER NOT NULL,
        balance INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT, note TEXT, actor INTEGER, ts INTEGER NOT NULL)""")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS shop_wallet_tx_ref ON shop_wallet_tx(kind, ref) WHERE ref IS NOT NULL")
    c.execute("CREATE INDEX IF NOT EXISTS shop_wallet_tx_owner ON shop_wallet_tx(tg_id)")
    # Which customer owns which mohajer link (users.token). kind = test | paid.
    c.execute("""CREATE TABLE IF NOT EXISTS shop_services(
        token TEXT PRIMARY KEY, tg_id INTEGER NOT NULL, kind TEXT NOT NULL,
        created_ts INTEGER NOT NULL, followup_sent INTEGER NOT NULL DEFAULT 0, feedback TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS shop_services_owner ON shop_services(tg_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS shop_plans(
        id INTEGER PRIMARY KEY AUTOINCREMENT, gb REAL NOT NULL, days REAL NOT NULL,   -- 0 = unlimited
        price INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, popular INTEGER NOT NULL DEFAULT 0,
        created_ts INTEGER NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS shop_cards(
        id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT NOT NULL, holder TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, created_ts INTEGER NOT NULL)""")
    # One row per invoice. gb/days/price are a snapshot, so later plan edits never change it.
    # status: awaiting (no receipt yet) -> review -> approved | rejected; or expired | cancelled.
    c.execute("""CREATE TABLE IF NOT EXISTS shop_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL, kind TEXT NOT NULL,  -- buy | renew
        token TEXT, plan_id INTEGER, gb REAL NOT NULL, days REAL NOT NULL, price INTEGER NOT NULL,
        amount INTEGER NOT NULL, paid_amount INTEGER, card_id INTEGER, status TEXT NOT NULL,
        receipt_file TEXT, receipt_uid TEXT UNIQUE, created_ts INTEGER NOT NULL, receipt_ts INTEGER,
        decided_ts INTEGER, decided_by INTEGER, note TEXT, review_msgs TEXT)""")
    _add_cols(c, "shop_orders", [("pay_method", "TEXT NOT NULL DEFAULT 'card'")])   # card | wallet
    _add_cols(c, "shop_services", [("plan_id", "INTEGER"),                          # plan used for auto-renew
                                   ("auto_renew", "INTEGER NOT NULL DEFAULT 0"),
                                   ("auto_note_ts", "INTEGER NOT NULL DEFAULT 0")])  # last "not enough balance" note
    c.execute("CREATE INDEX IF NOT EXISTS shop_orders_status ON shop_orders(status)")
    c.execute("CREATE INDEX IF NOT EXISTS shop_orders_owner ON shop_orders(tg_id)")
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

def toman(n):
    return fa("{:,}".format(int(n)).replace(",", "٬")) + " تومان"

def fa_clock(ts):
    return fa(time.strftime("%H:%M", time.gmtime(ts + core.IRAN_OFFSET)))

_IN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

def to_ascii(s):
    """Persian/Arabic digits -> ASCII, so admins and customers can type either."""
    return (s or "").translate(_IN_DIGITS)

def parse_amount(text):
    """'150,000' / '۱۵۰٬۰۰۰' / '150.000 تومان' -> 150000; None if not a positive integer."""
    digits = "".join(ch for ch in to_ascii(text) if "0" <= ch <= "9")
    try: n = int(digits)
    except ValueError: return None
    return n if n > 0 else None

def plan_title(gb, days):
    return "%s · %s" % ("حجم نامحدود" if not gb else fa("%g گیگ" % gb),
                        "زمان نامحدود" if not days else fa("%g روز" % days))

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

# --- wallet ---
class InsufficientFunds(Exception):
    pass

def wallet_apply(c, uid, delta, kind, ref=None, note=None, actor=None):
    """Change a balance inside the caller's open transaction and record it in the ledger.
    Raises InsufficientFunds rather than going negative; returns the new balance."""
    row = c.execute("SELECT wallet FROM shop_customers WHERE tg_id=?", (uid,)).fetchone()
    if not row: raise InsufficientFunds()
    bal = row[0] + int(delta)
    if bal < 0: raise InsufficientFunds()
    c.execute("UPDATE shop_customers SET wallet=? WHERE tg_id=?", (bal, uid))
    c.execute("INSERT INTO shop_wallet_tx(tg_id,delta,balance,kind,ref,note,actor,ts) VALUES(?,?,?,?,?,?,?,?)",
              (uid, int(delta), bal, kind, ref, note, actor, int(time.time())))
    return bal

def wallet_change(uid, delta, kind, ref=None, note=None, actor=None):
    """Standalone credit/debit. Returns the new balance, or None if refused (funds / already applied)."""
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        bal = wallet_apply(c, uid, delta, kind, ref, note, actor)
        c.commit(); return bal
    except (InsufficientFunds, sqlite3.IntegrityError):
        c.rollback(); return None
    finally:
        c.close()

def balance(uid):
    r = get_customer(uid); return r["wallet"] if r else 0

TX_LABELS = {"topup": "شارژ کیف پول", "purchase": "خرید", "auto": "تمدید خودکار", "refund": "بازگشت وجه",
             "overpay": "مبلغ اضافهٔ واریزی", "admin": "تغییر توسط پشتیبانی", "referral": "پاداش دعوت"}

def tx_lines(uid, limit):
    c = core.db()
    rows = c.execute("SELECT * FROM shop_wallet_tx WHERE tg_id=? ORDER BY id DESC LIMIT ?", (uid, limit)).fetchall()
    c.close()
    return ["%s %s · %s%s · %s" % ("🟢" if r["delta"] > 0 else "🔴", fa_date(r["ts"]), "+" if r["delta"] > 0 else "−",
                                   toman(abs(r["delta"])), TX_LABELS.get(r["kind"], r["kind"]) +
                                   (" (%s)" % html.escape(r["note"]) if r["note"] and r["kind"] == "admin" else ""))
            for r in rows]

def wallet_view(uid):
    lines = tx_lines(uid, 5)
    text = "💰 <b>کیف پول</b>\n\nموجودی: <b>%s</b>" % toman(balance(uid))
    text += ("\n\n<b>آخرین تراکنش‌ها</b>\n" + "\n".join(lines)) if lines else "\n\nهنوز تراکنشی نداری."
    text += "\n\n💡 با موجودی کیف پول، خرید و تمدید فوری انجام می‌شه و منتظر تأیید رسید نمی‌مونی."
    kb = [[{"text": "➕ شارژ کیف پول", "callback_data": "c:topup"}]]
    if lines: kb.append([{"text": "📜 همهٔ تراکنش‌ها", "callback_data": "c:tx"}])
    return text, kb

TOPUP_CHOICES = (100000, 200000, 500000, 1000000)
TOPUP_MIN, TOPUP_MAX = 10000, 50000000

def topup_view():
    kb = [[{"text": toman(a), "callback_data": "c:topup_amt:%d" % a} for a in TOPUP_CHOICES[i:i + 2]] for i in (0, 2)]
    kb += [[{"text": "✏️ مبلغ دلخواه", "callback_data": "c:topup_custom"}], [{"text": "بازگشت", "callback_data": "c:wallet"}]]
    return "➕ <b>شارژ کیف پول</b>\nچقدر شارژ کنیم؟", kb

def start_topup(chat, uid, amount, mid=None):
    if not (TOPUP_MIN <= amount <= TOPUP_MAX):
        core.send(chat, "مبلغ شارژ باید بین %s و %s باشد." % (toman(TOPUP_MIN), toman(TOPUP_MAX))); return
    o = create_order(uid, "topup", amount)
    if not o:
        text, kb = "😔 پرداخت کارت‌به‌کارت موقتاً در دسترس نیست؛ کمی بعد دوباره امتحان کن.", None
        for a in owner_ids(): core.send(a, "⚠️ مشتری می‌خواست کیف پولش را شارژ کند ولی هیچ کارت فعالی تعریف نشده است.")
    else:
        text, kb = payment_text(o), [[{"text": "❌ لغو سفارش", "callback_data": "c:cancel:%d" % o["id"]}]]
    if mid: core.edit(chat, mid, text, kb)
    else: core.send(chat, text, kb)

# --- plans & buying ---
def get_plan(pid):
    c = core.db(); r = c.execute("SELECT * FROM shop_plans WHERE id=?", (pid,)).fetchone(); c.close(); return r

def list_plans(active_only=True):
    c = core.db()
    rows = c.execute("SELECT * FROM shop_plans %s ORDER BY price" % ("WHERE active=1" if active_only else "")).fetchall()
    c.close(); return rows

def owned_service(uid, token):
    """The customer's link row (users join shop_services), or None if it is not theirs / gone."""
    c = core.db()
    r = c.execute("SELECT u.* FROM shop_services s JOIN users u ON u.token=s.token "
                  "WHERE s.token=? AND s.tg_id=?", (token, uid)).fetchone()
    c.close(); return r

def plans_view(uid, token=None):
    plans = list_plans()
    if not plans:
        return "🛒 فعلاً پلنی برای فروش نداریم؛ به‌زودی برمی‌گردیم 🙏", None
    suffix = (":" + token) if token else ""
    kb = [[{"text": "%s%s · %s" % ("🔥 " if p["popular"] else "", plan_title(p["gb"], p["days"]), toman(p["price"])),
            "callback_data": "c:plan:%d%s" % (p["id"], suffix)}] for p in plans]
    per_gb = ["• %s ← هر گیگ %s" % (plan_title(p["gb"], p["days"]), toman(p["price"] / p["gb"]))
              for p in plans if p["gb"]]
    if token:
        u = owned_service(uid, token)
        head = "🔄 <b>تمدید «%s»</b>\nحجم و زمان پلن به همین سرویس اضافه می‌شه و لینکت عوض نمی‌شه:" % html.escape(u["label"] if u else "")
    else:
        head = "🛒 <b>خرید سرویس</b>\nیکی از پلن‌ها رو انتخاب کن:"
    return head + ("\n\n" + "\n".join(per_gb) if per_gb else ""), kb

def send_plans(chat, uid, token=None, mid=None):
    text, kb = plans_view(uid, token)
    if mid: core.edit(chat, mid, text, kb)
    else: core.send(chat, text, kb)

def invoice_view(plan, token=None, bal=0):
    suffix = (":" + token) if token else ""
    text = ("🧾 <b>پیش‌فاکتور</b>\n\n📦 %s\n%s💰 مبلغ: <b>%s</b>\n👛 موجودی کیف پول: %s"
            % (plan_title(plan["gb"], plan["days"]), "🔄 تمدید سرویس فعلی\n" if token else "", toman(plan["price"]), toman(bal)))
    kb = []
    if bal >= plan["price"]:
        kb.append([{"text": "💰 پرداخت از کیف پول (فوری)", "callback_data": "c:wpay:%d%s" % (plan["id"], suffix)}])
    elif bal > 0:
        short = -(-(plan["price"] - bal) // 1000) * 1000          # round the shortfall up to 1,000
        short = max(short, TOPUP_MIN)
        text += "\n\nبا شارژ %s می‌تونی از کیف پول پرداخت کنی." % toman(short)
        kb.append([{"text": "➕ شارژ %s" % toman(short), "callback_data": "c:topup_amt:%d" % short}])
    kb += [[{"text": "💳 پرداخت کارت‌به‌کارت", "callback_data": "c:pay:%d%s" % (plan["id"], suffix)}],
           [{"text": "بازگشت", "callback_data": "c:plans%s" % suffix}]]
    return text, kb

ORDER_TTL = 3600            # an invoice waits this long for its receipt
LATE_RECEIPT = 24 * 3600    # a receipt for an expired invoice is still accepted (people pay late)
OPEN = ("awaiting", "review")

def get_order(oid):
    c = core.db(); r = c.execute("SELECT * FROM shop_orders WHERE id=?", (oid,)).fetchone(); c.close(); return r

def create_order(uid, kind, price, plan=None, token=None, now=None):
    """New card-to-card invoice (kind buy | renew | topup) with a unique amount (price + a few
    random tomans) so the reviewer can match the bank SMS. Older unpaid invoices are cancelled."""
    now = int(time.time()) if now is None else now
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        cards = c.execute("SELECT id FROM shop_cards WHERE active=1").fetchall()
        if not cards: c.rollback(); return None
        c.execute("UPDATE shop_orders SET status='cancelled' WHERE tg_id=? AND status='awaiting'", (uid,))
        taken = {r[0] for r in c.execute("SELECT amount FROM shop_orders WHERE status IN ('awaiting','review')")}
        amount = price + 1 + secrets.randbelow(999)
        for _ in range(50):
            if amount not in taken: break
            amount = price + 1 + secrets.randbelow(999)
        oid = c.execute("INSERT INTO shop_orders(tg_id,kind,token,plan_id,gb,days,price,amount,card_id,status,created_ts) "
                        "VALUES(?,?,?,?,?,?,?,?,?,'awaiting',?)",
                        (uid, kind, token, plan["id"] if plan else None, plan["gb"] if plan else 0,
                         plan["days"] if plan else 0, price, amount, secrets.choice(cards)[0], now)).lastrowid
        c.commit()
    finally:
        c.close()
    return get_order(oid)

def plan_order(uid, plan, token=None):
    return create_order(uid, "renew" if token else "buy", plan["price"], plan, token)

def get_card(cid):
    c = core.db(); r = c.execute("SELECT * FROM shop_cards WHERE id=?", (cid,)).fetchone(); c.close(); return r

def payment_text(o):
    card = get_card(o["card_id"])
    return ("💳 <b>%s · سفارش #%s</b>\n\n"
            "مبلغ دقیق: <b>%s</b>\n<code>%d</code>\n\n"
            "به این کارت:\n<code>%s</code>\nبه نام <b>%s</b>\n\n"
            "⏳ مهلت پرداخت: تا ساعت %s\n"
            "📸 بعد از واریز، <b>عکس رسید</b> رو همین‌جا بفرست.\n\n"
            "⚠️ لطفاً دقیقاً همین مبلغ رو واریز کن تا پرداختت زودتر شناسایی بشه."
            % ("شارژ کیف پول" if o["kind"] == "topup" else "پرداخت کارت‌به‌کارت",
               fa(o["id"]), toman(o["amount"]), o["amount"], card["number"] if card else "-",
               html.escape(card["holder"] if card else "-"), fa_clock(o["created_ts"] + ORDER_TTL)))

def start_payment(chat, mid, uid, plan_id, token=None):
    plan = get_plan(plan_id)
    if not plan or not plan["active"]:
        core.edit(chat, mid, "این پلن دیگه موجود نیست.", [[{"text": "🛒 پلن‌ها", "callback_data": "c:plans"}]]); return
    if token and not owned_service(uid, token):
        core.edit(chat, mid, "این سرویس پیدا نشد."); return
    o = plan_order(uid, plan, token)
    if not o:
        core.edit(chat, mid, "😔 پرداخت کارت‌به‌کارت موقتاً در دسترس نیست؛ کمی بعد دوباره امتحان کن.")
        for a in owner_ids(): core.send(a, "⚠️ مشتری می‌خواست بخرد ولی هیچ کارت فعالی تعریف نشده است.")
        return
    core.edit(chat, mid, payment_text(o), [[{"text": "❌ لغو سفارش", "callback_data": "c:cancel:%d" % o["id"]}]])

def cancel_order(chat, mid, uid, oid):
    c = core.db()
    n = c.execute("UPDATE shop_orders SET status='cancelled' WHERE id=? AND tg_id=? AND status='awaiting'", (oid, uid)).rowcount
    c.commit(); c.close()
    core.edit(chat, mid, "سفارش #%s لغو شد." % fa(oid) if n else "این سفارش دیگه باز نیست.")

# --- receipts ---
def receive_receipt(chat, uid, file_ref, file_uid, now=None):
    """Attach a receipt image to the customer's open invoice and send it to the reviewers."""
    now = int(time.time()) if now is None else now
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        o = c.execute("SELECT * FROM shop_orders WHERE tg_id=? AND (status='awaiting' OR "
                      "(status='expired' AND created_ts>=?)) ORDER BY id DESC LIMIT 1", (uid, now - LATE_RECEIPT)).fetchone()
        if not o:
            c.rollback()
            core.send(chat, "📸 الان سفارش پرداخت‌نشده‌ای نداری.\nاول از «🛒 خرید سرویس» یه پلن انتخاب کن.",
                      [[{"text": "🛒 خرید سرویس", "callback_data": "c:buy"}]]); return
        if c.execute("SELECT 1 FROM shop_orders WHERE receipt_uid=?", (file_uid,)).fetchone():
            c.rollback(); say(chat, "⚠️ این رسید قبلاً ارسال شده. اگه واریز جدیدی داشتی، عکس رسید همون رو بفرست."); return
        c.execute("UPDATE shop_orders SET status='review',receipt_file=?,receipt_uid=?,receipt_ts=? WHERE id=?",
                  (file_ref, file_uid, now, o["id"]))
        c.commit()
    finally:
        c.close()
    say(chat, "✅ رسیدت رسید! بعد از بررسی خبرت می‌کنیم 🙏\nشمارهٔ سفارش: #%s" % fa(o["id"]))
    send_review(o["id"])

def reviewers():
    c = core.db(); staff = [r[0] for r in c.execute("SELECT tg_id FROM shop_staff")]; c.close()
    return owner_ids() + [s for s in staff if s not in owner_ids()]

ORDER_KINDS = {"buy": "خرید", "renew": "تمدید", "topup": "شارژ کیف پول"}

def review_caption(o):
    cust = get_customer(o["tg_id"]); card = get_card(o["card_id"])
    late = o["receipt_ts"] and o["receipt_ts"] > o["created_ts"] + ORDER_TTL
    what = ORDER_KINDS.get(o["kind"], o["kind"])
    lines = ["🧾 <b>رسید · سفارش #%d</b> (%s)%s" % (o["id"], what, " ⏰ دیرتر از مهلت" if late else ""), "",
             "👤 %s · <code>%d</code>" % (html.escape(cust["first_name"] or "-") if cust else "-", o["tg_id"])]
    if o["kind"] != "topup": lines.append("📦 %s" % plan_title(o["gb"], o["days"]))
    lines.append("💰 مبلغ فاکتور: <b>%s</b>" % toman(o["amount"]))
    if o["paid_amount"] is not None:
        lines.append("✏️ مبلغ واریزی: <b>%s</b>" % toman(o["paid_amount"]))
    lines.append("💳 کارت: %s (%s)" % (("…" + card["number"][-4:]) if card else "-", html.escape(card["holder"]) if card else "-"))
    if o["status"] == "approved": lines += ["", "✅ <b>تأیید شد</b> · <code>%s</code>" % o["decided_by"]]
    if o["status"] == "rejected": lines += ["", "❌ <b>رد شد</b>: %s · <code>%s</code>" % (html.escape(o["note"] or ""), o["decided_by"])]
    return "\n".join(lines)

def review_kb(o):
    if o["status"] != "review": return []
    return [[{"text": "✅ تأیید", "callback_data": "r:ok:%d" % o["id"]}, {"text": "❌ رد", "callback_data": "r:no:%d" % o["id"]}],
            [{"text": "✏️ اصلاح مبلغ واریزی", "callback_data": "r:amt:%d" % o["id"]}]]

def send_review(oid, to=None):
    """Post the receipt (photo/document + details + buttons) to reviewers and remember the copies."""
    o = get_order(oid); kind, file_id = o["receipt_file"].split(":", 1)
    sent = []
    for chat in (to or reviewers()):
        r = core.tg("sendPhoto" if kind == "photo" else "sendDocument", chat_id=chat,
                    **{("photo" if kind == "photo" else "document"): file_id},
                    caption=review_caption(o), parse_mode="HTML", reply_markup={"inline_keyboard": review_kb(o)})
        mid = (r.get("result") or {}).get("message_id")
        if mid: sent.append([chat, mid])
    c = core.db()
    old = json.loads(c.execute("SELECT review_msgs FROM shop_orders WHERE id=?", (oid,)).fetchone()[0] or "[]")
    c.execute("UPDATE shop_orders SET review_msgs=? WHERE id=?", (json.dumps(old + sent), oid)); c.commit(); c.close()

def refresh_review(oid):
    """Every reviewer sees the same state (who approved / why rejected) on their copy."""
    o = get_order(oid)
    for chat, mid in json.loads(o["review_msgs"] or "[]"):
        core.tg("editMessageCaption", chat_id=chat, message_id=mid, caption=review_caption(o), parse_mode="HTML",
                reply_markup={"inline_keyboard": review_kb(o)})

def paid_of(o):
    return o["paid_amount"] if o["paid_amount"] is not None else o["amount"]

def fulfill(o):
    """Deliver an approved order: credit the wallet (topup), or create/extend the customer's link.
    Returns the token ('wallet' for a top-up) or None on failure."""
    if o["kind"] == "topup":
        wallet_change(o["tg_id"], paid_of(o), "topup", ref="order:%d" % o["id"])   # None = already credited
        return "wallet"
    token = o["token"]
    c = core.db(); live = token and c.execute("SELECT 1 FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if o["kind"] == "renew" and live:
        if o["gb"]: core.extend_volume(token, o["gb"])
        else: core.set_unlimited(token, "limit_bytes")
        if o["days"]: core.extend_time(token, o["days"])
        else: core.set_unlimited(token, "expiry_ts")
        try: core.refresh_usage(token)
        except Exception as e: print("shop refresh err", e, flush=True)
        core.maybe_reenable(token)
        c = core.db(); c.execute("UPDATE shop_services SET kind='paid',plan_id=COALESCE(?,plan_id),auto_note_ts=0 WHERE token=?",
                                 (o["plan_id"], token)); c.commit(); c.close()
        return token
    # a buy - or a renewal whose link was already deleted after its grace period: mint a new one
    cust = get_customer(o["tg_id"])
    token = core.create_user(o["gb"], o["days"], label=("%s · %s" % ((cust["first_name"] if cust else "") or o["tg_id"],
                                                                      plan_title(o["gb"], o["days"])))[:40])
    if token:
        c = core.db()
        c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts,plan_id) VALUES(?,?,'paid',?,?)",
                  (token, o["tg_id"], int(time.time()), o["plan_id"]))
        c.execute("UPDATE shop_orders SET token=? WHERE id=?", (token, o["id"])); c.commit(); c.close()
    return token

def approve(oid, uid):
    """review -> approved exactly once, then deliver. Returns 'ok' | 'done' | 'short' | 'failed'."""
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        o = c.execute("SELECT * FROM shop_orders WHERE id=?", (oid,)).fetchone()
        if not o or o["status"] != "review": c.rollback(); return "done"
        if o["kind"] != "topup" and paid_of(o) < o["amount"]: c.rollback(); return "short"   # a top-up credits what was paid
        c.execute("UPDATE shop_orders SET status='approved',decided_ts=?,decided_by=? WHERE id=?", (int(time.time()), uid, oid))
        c.commit()
    finally:
        c.close()
    token = None
    try: token = fulfill(get_order(oid))
    except Exception as e: print("shop fulfill err", e, flush=True)
    if not token:   # give the order back to the queue so someone can retry
        c = core.db(); c.execute("UPDATE shop_orders SET status='review',decided_ts=NULL,decided_by=NULL WHERE id=?", (oid,))
        c.commit(); c.close(); return "failed"
    o = get_order(oid); log(uid, "order_approve", oid, paid_of(o))
    extra = paid_of(o) - o["price"] if o["kind"] != "topup" else 0
    if extra > 0: wallet_change(o["tg_id"], extra, "overpay", ref="order:%d" % oid)   # incl. the unique-amount tomans
    if o["kind"] == "topup":
        core.send(o["tg_id"], "💰 <b>کیف پولت %s شارژ شد!</b>\nموجودی: <b>%s</b>" % (toman(paid_of(o)), toman(balance(o["tg_id"]))),
                  [[{"text": "🛒 خرید سرویس", "callback_data": "c:buy"}]])
    else:
        deliver_message(o, token, "🎉 <b>پرداختت تأیید شد!</b>", extra)
    refresh_review(oid)
    return "ok"

def deliver_message(o, token, head, extra=0):
    body = "سرویست تمدید شد ✅" if o["kind"] == "renew" else "سرویس جدیدت فعال شد ✅"
    text = "%s\n%s\n📦 %s\n\n🔗 <code>%s</code>" % (head, body, plan_title(o["gb"], o["days"]), core.sub_url(token))
    if extra > 0: text += "\n\n💰 %s اضافه واریز کرده بودی؛ به کیف پولت برگشت." % toman(extra)
    core.send(o["tg_id"], text, link_kb(token, auto_kb(token)))

def auto_kb(token):
    c = core.db(); r = c.execute("SELECT auto_renew FROM shop_services WHERE token=?", (token,)).fetchone(); c.close()
    on = bool(r and r[0])
    return [[{"text": "🔁 تمدید خودکار: %s" % ("روشن ✅" if on else "خاموش"), "callback_data": "c:auto:%s" % token}]]

def wallet_buy(uid, plan, token=None, kind="purchase"):
    """Pay a plan from the wallet: debit + order row in one transaction, then deliver; a failed
    delivery is refunded. Returns (status, order, token); status ok | funds | failed."""
    now = int(time.time())
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        oid = c.execute("INSERT INTO shop_orders(tg_id,kind,token,plan_id,gb,days,price,amount,status,created_ts,decided_ts,"
                        "decided_by,pay_method) VALUES(?,?,?,?,?,?,?,?,'approved',?,?,?,'wallet')",
                        (uid, "renew" if token else "buy", token, plan["id"], plan["gb"], plan["days"], plan["price"],
                         plan["price"], now, now, uid)).lastrowid
        wallet_apply(c, uid, -plan["price"], kind, ref="order:%d" % oid)
        c.commit()
    except InsufficientFunds:
        c.rollback(); return "funds", None, None
    finally:
        c.close()
    o = get_order(oid); tok = None
    try: tok = fulfill(o)
    except Exception as e: print("shop wallet fulfill err", e, flush=True)
    if not tok:
        c = core.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            wallet_apply(c, uid, plan["price"], "refund", ref="order:%d" % oid)
            c.execute("UPDATE shop_orders SET status='failed' WHERE id=?", (oid,))
            c.commit()
        finally:
            c.close()
        return "failed", o, None
    return "ok", get_order(oid), tok

def wallet_pay(chat, mid, uid, plan_id, token=None):
    plan = get_plan(plan_id)
    if not plan or not plan["active"]:
        core.edit(chat, mid, "این پلن دیگه موجود نیست.", [[{"text": "🛒 پلن‌ها", "callback_data": "c:plans"}]]); return
    if token and not owned_service(uid, token):
        core.edit(chat, mid, "این سرویس پیدا نشد."); return
    status, o, tok = wallet_buy(uid, plan, token)
    if status == "funds":
        text, kb = invoice_view(plan, token, balance(uid)); core.edit(chat, mid, "⚠️ موجودی کافی نیست.\n\n" + text, kb); return
    if status == "failed":
        core.edit(chat, mid, "❌ ساخت سرویس الان ممکن نشد؛ مبلغ به کیف پولت برگشت. چند دقیقهٔ دیگه دوباره امتحان کن."); return
    core.edit(chat, mid, "✅ از کیف پولت %s کم شد. موجودی: %s" % (toman(plan["price"]), toman(balance(uid))))
    deliver_message(o, tok, "🎉 <b>خریدت انجام شد!</b>")

REJECT_REASONS = ["مبلغ واریزی با فاکتور یکی نیست", "رسید ناخوانا یا نامعتبر است", "واریزی به حساب ما نرسیده", "رسید تکراری است"]

def reject(oid, uid, reason):
    c = core.db()
    n = c.execute("UPDATE shop_orders SET status='rejected',note=?,decided_ts=?,decided_by=? WHERE id=? AND status='review'",
                  (reason, int(time.time()), uid, oid)).rowcount
    c.commit(); c.close()
    if not n: return False
    o = get_order(oid); log(uid, "order_reject", oid, reason)
    core.send(o["tg_id"], "❌ رسید سفارش #%s تأیید نشد.\nدلیل: %s\n\nاگه فکر می‌کنی اشتباه شده، از «🛠 گزارش اختلال» پیام بده."
              % (fa(oid), html.escape(reason)), [[{"text": "🛒 خرید دوباره", "callback_data": "c:buy"}]])
    refresh_review(oid)
    return True

review_pending = {}   # reviewer uid -> {"kind": "amount" | "reason", "oid": id}; staff only, tiny

def queue_view():
    c = core.db()
    rows = c.execute("SELECT o.*, c.first_name FROM shop_orders o LEFT JOIN shop_customers c ON c.tg_id=o.tg_id "
                     "WHERE o.status='review' ORDER BY o.receipt_ts LIMIT 30").fetchall()
    c.close()
    if not rows: return "🧾 رسیدی در انتظار بررسی نیست ✨", None
    return ("🧾 <b>رسیدهای در انتظار</b> (%s)\nروی هرکدام بزن تا رسیدش بیاد:" % fa(len(rows)),
            [[{"text": "#%d · %s · %s" % (r["id"], r["first_name"] or r["tg_id"], toman(r["amount"])),
               "callback_data": "r:q:%d" % r["id"]}] for r in rows])

def review_callback(chat, mid, uid, data, cbid):
    """Receipt buttons for owners and support staff ('r:<action>:<order>[:<arg>]')."""
    parts = data.split(":")
    if len(parts) < 3 or not parts[2].isdigit(): core.answer(cbid); return
    action, oid = parts[1], int(parts[2])
    o = get_order(oid)
    if not o: core.answer(cbid, "سفارش پیدا نشد"); return
    if action == "q":
        core.answer(cbid); send_review(oid, to=[chat]); return
    if o["status"] != "review":
        core.answer(cbid, "این سفارش قبلاً بررسی شده"); refresh_review(oid); return
    if action == "ok":
        res = approve(oid, uid)
        core.answer(cbid, {"ok": "✅ تأیید شد و سرویس ارسال شد", "done": "این سفارش قبلاً بررسی شده",
                           "short": "مبلغ واریزی کمتر از فاکتور است؛ رد کن یا مبلغ را اصلاح کن",
                           "failed": "❌ ساخت سرویس ناموفق بود؛ دوباره تأیید کن"}[res]); return
    if action == "no":
        core.answer(cbid)
        kb = [[{"text": r, "callback_data": "r:nor:%d:%d" % (oid, i)}] for i, r in enumerate(REJECT_REASONS)]
        kb += [[{"text": "✏️ دلیل دیگر", "callback_data": "r:nox:%d" % oid}], [{"text": "بازگشت", "callback_data": "r:back:%d" % oid}]]
        core.tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": kb}); return
    if action == "nor" and len(parts) == 4 and parts[3].isdigit() and int(parts[3]) < len(REJECT_REASONS):
        reject(oid, uid, REJECT_REASONS[int(parts[3])]); core.answer(cbid, "رد شد"); return
    if action == "back":
        core.answer(cbid); core.tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": review_kb(o)}); return
    if action in ("nox", "amt"):
        review_pending[uid] = {"kind": "reason" if action == "nox" else "amount", "oid": oid}; core.answer(cbid)
        core.send(chat, "دلیل رد سفارش #%d را بنویس:" % oid if action == "nox" else
                  "مبلغی که واقعاً برای سفارش #%d واریز شده را به تومان بفرست:" % oid); return
    core.answer(cbid)

def review_text(chat, uid, text):
    """Typed answers to 'other reason' / 'fix amount'. Returns True when it consumed the message."""
    st = review_pending.get(uid)
    if not st: return False
    o = get_order(st["oid"])
    if not o or o["status"] != "review":
        review_pending.pop(uid, None); core.send(chat, "این سفارش دیگر در انتظار بررسی نیست."); return True
    if st["kind"] == "reason":
        if not text.strip(): core.send(chat, "دلیل را بنویس:"); return True
        review_pending.pop(uid, None); reject(o["id"], uid, text.strip()[:200])
        core.send(chat, "❌ سفارش #%d رد شد و به مشتری خبر داده شد." % o["id"]); return True
    n = parse_amount(text)
    if not n: core.send(chat, "یک عدد بفرست (تومان)، مثلاً <code>150000</code>:"); return True
    review_pending.pop(uid, None)
    c = core.db(); c.execute("UPDATE shop_orders SET paid_amount=? WHERE id=? AND status='review'", (n, o["id"])); c.commit(); c.close()
    log(uid, "order_paid_amount", o["id"], n); refresh_review(o["id"])
    core.send(chat, "✏️ مبلغ واریزی سفارش #%d ثبت شد: %s%s" % (o["id"], toman(n),
              "" if n >= o["amount"] else "\n⚠️ کمتر از فاکتور است؛ تأیید ممکن نیست."))
    return True

def expire_orders(now=None):
    now = int(time.time()) if now is None else now
    c = core.db()
    rows = c.execute("SELECT id,tg_id FROM shop_orders WHERE status='awaiting' AND created_ts<?", (now - ORDER_TTL,)).fetchall()
    expired = [r for r in rows if c.execute("UPDATE shop_orders SET status='expired' WHERE id=? AND status='awaiting'",
                                            (r["id"],)).rowcount]
    c.commit(); c.close()
    for r in expired:
        core.send(r["tg_id"], "⌛️ مهلت پرداخت سفارش #%s تموم شد.\nاگه واریز کردی، تا ۲۴ ساعت هنوز می‌تونی عکس رسید رو بفرستی؛ "
                  "وگرنه دوباره از «🛒 خرید سرویس» سفارش بده." % fa(r["id"]))

# --- auto-renew from the wallet ---
AUTO_BEFORE, AUTO_USED = 3600, 0.98   # renew when <1h is left, 98% of the data is used, or the link was disabled
AUTO_NOTE_EVERY = 24 * 3600           # remind about a low balance at most once a day

def auto_renew_due(u, now):
    if u["disabled_ts"]: return True
    if u["expiry_ts"] and u["expiry_ts"] - now <= AUTO_BEFORE: return True
    return bool(u["limit_bytes"]) and core.current_usage(u) >= AUTO_USED * u["limit_bytes"]

def _auto_note(r, now, text, kb):
    if now - r["auto_note_ts"] < AUTO_NOTE_EVERY: return
    c = core.db(); c.execute("UPDATE shop_services SET auto_note_ts=? WHERE token=?", (now, r["token"])); c.commit(); c.close()
    core.send(r["tg_id"], text, kb)

def run_auto_renewals(now=None):
    now = int(time.time()) if now is None else now
    c = core.db()
    rows = c.execute("SELECT s.token AS token, s.tg_id, s.plan_id, s.auto_note_ts, u.label, u.disabled_ts, u.expiry_ts, "
                     "u.limit_bytes, u.used_bytes, u.usage_reset_bytes FROM shop_services s JOIN users u ON u.token=s.token "
                     "WHERE s.auto_renew=1 AND u.pending_delete=0 AND u.frozen=0").fetchall()
    c.close()
    for r in rows:
        if not auto_renew_due(r, now): continue
        renew_btn = [{"text": "🔄 تمدید دستی", "callback_data": "c:renew:%s" % r["token"]}]
        plan = get_plan(r["plan_id"]) if r["plan_id"] else None
        if not plan or not plan["active"]:
            _auto_note(r, now, "🔁 تمدید خودکار «%s» انجام نشد: پلن قبلی دیگه فروخته نمی‌شه.\nیه پلن جدید انتخاب کن 👇"
                       % html.escape(r["label"]), [renew_btn]); continue
        status, o, tok = wallet_buy(r["tg_id"], plan, r["token"], kind="auto")
        if status == "ok":
            deliver_message(o, tok, "🔁 <b>تمدید خودکار انجام شد</b> (%s از کیف پولت)" % toman(plan["price"]))
        elif status == "funds":
            short = max(TOPUP_MIN, -(-(plan["price"] - balance(r["tg_id"])) // 1000) * 1000)
            _auto_note(r, now, "🔁 سرویس «%s» داره تموم می‌شه ولی موجودی کیف پولت برای تمدید خودکار کافی نیست.\n"
                       "مبلغ تمدید: %s · موجودی: %s" % (html.escape(r["label"]), toman(plan["price"]), toman(balance(r["tg_id"]))),
                       [[{"text": "➕ شارژ %s" % toman(short), "callback_data": "c:topup_amt:%d" % short}], renew_btn])

# --- account ---
def account_text(uid):
    cust = get_customer(uid)
    c = core.db()
    paid = c.execute("SELECT COUNT(*) FROM shop_orders WHERE tg_id=? AND status='approved'", (uid,)).fetchone()[0]
    c.close()
    return ("👤 <b>حساب من</b>\n\n🙂 %s\n🆔 آیدی عددی: <code>%d</code>\n📅 عضویت: %s\n💰 موجودی: %s\n🛍 تعداد خرید: %s\n🎁 تست رایگان: %s"
            % (html.escape(cust["first_name"] or "-"), uid, fa_date(cust["joined_ts"]), toman(cust["wallet"]), fa(paid),
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
def receipt_file(m):
    """('photo:<file_id>' | 'doc:<file_id>', file_unique_id) for an image message, else None."""
    if m.get("photo"):
        p = m["photo"][-1]; return "photo:" + p["file_id"], p["file_unique_id"]
    d = m.get("document") or {}
    if (d.get("mime_type") or "").startswith("image/") or (d.get("mime_type") == "application/pdf"):
        return "doc:" + d["file_id"], d["file_unique_id"]
    return None

def customer_message(chat, uid, m):
    text = (m.get("text") or "").strip()
    rf = receipt_file(m)
    if rf: receive_receipt(chat, uid, *rf); return
    if text.startswith("/start"):
        set_state(uid, None)
        customer_menu(chat, welcome_text(m["from"].get("first_name"))); return
    if text in {b for row in CUSTOMER_MENU for b in row}:
        set_state(uid, None)
        if text == B_BUY: send_plans(chat, uid); return
        if text == B_WALLET: core.send(chat, *wallet_view(uid)); return
        if text == B_TEST: test_offer(chat, uid); return
        if text == B_ACCOUNT: core.send(chat, account_text(uid)); return
        if text == B_GUIDE: core.send(chat, GUIDE_TEXT, guide_kb()); return
        say(chat, SOON_TEXT); return
    st = get_state(uid) or {}
    if st.get("stage") == "topup_amount":
        n = parse_amount(text)
        if not n:
            core.send(chat, "مبلغ رو به تومان بفرست، مثلاً <code>300000</code>:"); return
        set_state(uid, None); start_topup(chat, uid, n); return
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
    parts = data.split(":")
    token = parts[3] if len(parts) == 4 else None
    if data == "c:buy":
        core.answer(cbid); send_plans(chat, uid); return
    if parts[:2] == ["c", "renew"] and len(parts) == 3:
        core.answer(cbid)
        if owned_service(uid, parts[2]): send_plans(chat, uid, parts[2])
        else: core.send(chat, "این سرویس دیگه وجود نداره؛ می‌تونی یه سرویس جدید بخری.", [[{"text": "🛒 خرید سرویس", "callback_data": "c:buy"}]])
        return
    if parts[:2] == ["c", "plans"]:
        core.answer(cbid); send_plans(chat, uid, parts[2] if len(parts) == 3 else None, mid); return
    if parts[:2] in (["c", "plan"], ["c", "pay"], ["c", "wpay"]) and len(parts) in (3, 4) and parts[2].isdigit():
        core.answer(cbid)
        if parts[1] == "pay": start_payment(chat, mid, uid, int(parts[2]), token); return
        if parts[1] == "wpay": wallet_pay(chat, mid, uid, int(parts[2]), token); return
        plan = get_plan(int(parts[2]))
        if not plan or not plan["active"]: send_plans(chat, uid, token, mid); return
        text, kb = invoice_view(plan, token, balance(uid)); core.edit(chat, mid, text, kb); return
    if parts[:2] == ["c", "cancel"] and len(parts) == 3 and parts[2].isdigit():
        core.answer(cbid); cancel_order(chat, mid, uid, int(parts[2])); return
    if data == "c:wallet":
        core.answer(cbid); core.edit(chat, mid, *wallet_view(uid)); return
    if data == "c:topup":
        core.answer(cbid); core.edit(chat, mid, *topup_view()); return
    if data == "c:topup_custom":
        core.answer(cbid); set_state(uid, {"stage": "topup_amount"})
        core.edit(chat, mid, "✏️ مبلغ شارژ رو به تومان بفرست (حداقل %s):" % toman(TOPUP_MIN),
                  [[{"text": "انصراف", "callback_data": "c:wallet"}]]); return
    if parts[:2] == ["c", "topup_amt"] and len(parts) == 3 and parts[2].isdigit():
        core.answer(cbid); start_topup(chat, uid, int(parts[2]), mid); return
    if data == "c:tx":
        core.answer(cbid); lines = tx_lines(uid, 30)
        core.edit(chat, mid, "📜 <b>تراکنش‌ها</b> (آخرین ۳۰ مورد)\n\n" + ("\n".join(lines) or "هنوز تراکنشی نداری."),
                  [[{"text": "بازگشت", "callback_data": "c:wallet"}]]); return
    if parts[:2] == ["c", "auto"] and len(parts) == 3:
        if not owned_service(uid, parts[2]): core.answer(cbid, "این سرویس پیدا نشد"); return
        c = core.db()
        c.execute("UPDATE shop_services SET auto_renew=1-auto_renew,auto_note_ts=0 WHERE token=? AND tg_id=?", (parts[2], uid))
        c.commit(); on = c.execute("SELECT auto_renew FROM shop_services WHERE token=?", (parts[2],)).fetchone()[0]; c.close()
        core.answer(cbid, "تمدید خودکار روشن شد؛ موقع تموم شدن، از کیف پولت تمدید می‌شه" if on else "تمدید خودکار خاموش شد")
        core.tg("editMessageReplyMarkup", chat_id=chat, message_id=mid,
                reply_markup={"inline_keyboard": link_kb(parts[2], auto_kb(parts[2]))}); return
    if data == "c:report":
        core.answer(cbid, SOON_TEXT); return
    core.answer(cbid)

# ---------------- support staff UI ----------------
B_RECEIPTS, B_TICKETS = "🧾 رسیدها", "🎫 تیکت‌ها"
SUPPORT_MENU = [[B_RECEIPTS, B_TICKETS]]

def support_message(chat, uid, m):
    text = (m.get("text") or "").strip()
    if text == B_RECEIPTS:
        review_pending.pop(uid, None); t, kb = queue_view(); core.send(chat, t, kb); return
    if text == B_TICKETS:
        say(chat, SOON_TEXT); return
    if review_text(chat, uid, text): return
    say(chat, "🎧 <b>پنل پشتیبانی</b>\nاز منوی پایین یکی رو انتخاب کن 👇", reply_kb(SUPPORT_MENU))

def support_callback(chat, mid, uid, data, cbid):
    if data.startswith("r:"): review_callback(chat, mid, uid, data, cbid); return
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
            [{"text": "🧾 رسیدهای در انتظار (%s)" % fa(_count("SELECT COUNT(*) FROM shop_orders WHERE status='review'")),
              "callback_data": "sa:queue"}],
            [{"text": "📦 پلن‌ها", "callback_data": "sa:plans"}, {"text": "💳 کارت‌ها", "callback_data": "sa:cards"}],
            [{"text": "👤 کاربران", "callback_data": "sa:users"}, {"text": "🎁 تست رایگان", "callback_data": "sa:test"}],
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

def plans_admin():
    plans = list_plans(active_only=False)
    kb = [[{"text": "%s%s%s · %s" % ("" if p["active"] else "⏸ ", "🔥 " if p["popular"] else "",
                                     plan_title(p["gb"], p["days"]), toman(p["price"])),
            "callback_data": "sa:plan:%d" % p["id"]}] for p in plans]
    kb += [[{"text": "➕ افزودن پلن", "callback_data": "sa:plan_add"}], [{"text": "بازگشت", "callback_data": "sa:menu"}]]
    return ("📦 <b>پلن‌ها</b>\nروی هر پلن بزن تا ویرایشش کنی. ⏸ = مخفی از مشتری · 🔥 = پرفروش"
            if plans else "📦 <b>پلن‌ها</b>\nهنوز پلنی نساختی."), kb

def plan_admin(p):
    return ("📦 <b>%s</b>\n💰 %s\nوضعیت: %s%s" % (plan_title(p["gb"], p["days"]), toman(p["price"]),
                                               "فعال" if p["active"] else "مخفی", " · 🔥 پرفروش" if p["popular"] else ""),
            [[{"text": "⏸ مخفی کن" if p["active"] else "▶️ فعال کن", "callback_data": "sa:plan_act:%d" % p["id"]},
              {"text": "🔥 برداشتن برچسب" if p["popular"] else "🔥 پرفروش", "callback_data": "sa:plan_pop:%d" % p["id"]}],
             [{"text": "✏️ تغییر قیمت", "callback_data": "sa:plan_price:%d" % p["id"]},
              {"text": "🗑 حذف", "callback_data": "sa:plan_del:%d" % p["id"]}],
             [{"text": "بازگشت", "callback_data": "sa:plans"}]])

PLAN_ADD_TEXT = ("➕ <b>پلن جدید</b>\nحجم (گیگ)، مدت (روز) و قیمت (تومان) را با فاصله بفرست:\n"
                 "<code>30 30 150000</code>\nبرای نامحدود، صفر بگذار.")

def cards_admin():
    c = core.db(); cards = c.execute("SELECT * FROM shop_cards ORDER BY id").fetchall(); c.close()
    kb = [[{"text": "%s …%s · %s" % ("✅" if k["active"] else "⏸", k["number"][-4:], k["holder"]), "callback_data": "sa:card_act:%d" % k["id"]},
           {"text": "🗑", "callback_data": "sa:card_del:%d" % k["id"]}] for k in cards]
    kb += [[{"text": "➕ افزودن کارت", "callback_data": "sa:card_add"}], [{"text": "بازگشت", "callback_data": "sa:menu"}]]
    return ("💳 <b>کارت‌ها</b>\nبرای هر سفارش یکی از کارت‌های فعال به‌صورت تصادفی نمایش داده می‌شود.\n"
            "روی کارت بزن تا فعال/غیرفعال شود." if cards else "💳 <b>کارت‌ها</b>\nهنوز کارتی اضافه نکردی؛ بدون کارت فعال، خرید ممکن نیست."), kb

def find_customer(m, text):
    tid = _forwarded_user_id(m)
    t = to_ascii(text).strip()
    if not tid and t.isdigit(): tid = int(t)
    c = core.db()
    if tid: r = c.execute("SELECT * FROM shop_customers WHERE tg_id=?", (tid,)).fetchone()
    else: r = c.execute("SELECT * FROM shop_customers WHERE lower(username)=?", (t.lstrip("@").lower(),)).fetchone()
    c.close(); return r

def user_card(tid):
    cust = get_customer(tid)
    c = core.db()
    services = c.execute("SELECT COUNT(*) FROM shop_services s JOIN users u ON u.token=s.token WHERE s.tg_id=?", (tid,)).fetchone()[0]
    bought = c.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM shop_orders WHERE tg_id=? AND status='approved' "
                       "AND kind!='topup'", (tid,)).fetchone()
    invited = c.execute("SELECT COUNT(*) FROM shop_customers WHERE referrer_id=?", (tid,)).fetchone()[0]
    c.close()
    text = ("👤 <b>%s</b>%s\n🆔 <code>%d</code>\n📅 عضویت: %s · آخرین بازدید: %s\n💰 موجودی: <b>%s</b>\n"
            "📦 سرویس‌های زنده: %s · 🛍 خریدها: %s (%s)\n👥 دعوت‌شده‌ها: %s%s"
            % (html.escape(cust["first_name"] or "-"), (" · @" + html.escape(cust["username"])) if cust["username"] else "",
               tid, fa_date(cust["joined_ts"]), fa_date(cust["last_seen"]), toman(cust["wallet"]), fa(services),
               fa(bought[0]), toman(bought[1]), fa(invited), "\n🚫 <b>مسدود</b>" if cust["blocked"] else ""))
    kb = [[{"text": "➕ افزایش موجودی", "callback_data": "sa:uwal:%d:add" % tid},
           {"text": "➖ کسر موجودی", "callback_data": "sa:uwal:%d:sub" % tid}],
          [{"text": "✅ رفع مسدودی" if cust["blocked"] else "🚫 مسدود کن", "callback_data": "sa:ublock:%d" % tid}],
          [{"text": "🔎 کاربر دیگر", "callback_data": "sa:users"}, {"text": "بازگشت", "callback_data": "sa:menu"}]]
    return text, kb

USERS_PROMPT = "👤 <b>کاربران</b>\nآیدی عددی یا @یوزرنیم مشتری را بفرست، یا یک پیام از او را فوروارد کن:"

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
    if data.startswith("r:"):
        review_callback(chat, mid, uid, data, cbid); return
    review_pending.pop(uid, None)
    if data == "sa:queue":
        core.answer(cbid); t, kb = queue_view()
        core.edit(chat, mid, t, (kb or []) + [[{"text": "بازگشت", "callback_data": "sa:menu"}]]); return
    if data == "sa:plans":
        core.answer(cbid); core.edit(chat, mid, *plans_admin()); return
    if data == "sa:plan_add":
        core.pending[chat] = {"stage": "sa_plan_add"}; core.answer(cbid)
        core.edit(chat, mid, PLAN_ADD_TEXT, [[{"text": "انصراف", "callback_data": "sa:plans"}]]); return
    if data == "sa:cards":
        core.answer(cbid); core.edit(chat, mid, *cards_admin()); return
    if data == "sa:card_add":
        core.pending[chat] = {"stage": "sa_card_add"}; core.answer(cbid)
        core.edit(chat, mid, "➕ <b>کارت جدید</b>\nشمارهٔ ۱۶ رقمی و نام صاحب کارت را بفرست:\n<code>6037991234567890 فاطمه میرخلیلی</code>",
                  [[{"text": "انصراف", "callback_data": "sa:cards"}]]); return
    if data == "sa:users":
        core.pending[chat] = {"stage": "sa_user_find"}; core.answer(cbid)
        core.edit(chat, mid, USERS_PROMPT, [[{"text": "بازگشت", "callback_data": "sa:menu"}]]); return
    parts = data.split(":")
    if parts[1:2] == ["uwal"] and len(parts) == 4 and parts[2].isdigit() and parts[3] in ("add", "sub"):
        tid = int(parts[2])
        if not get_customer(tid): core.answer(cbid, "پیدا نشد"); return
        core.pending[chat] = {"stage": "sa_wallet", "id": tid, "sign": 1 if parts[3] == "add" else -1}; core.answer(cbid)
        core.send(chat, "%s موجودی <code>%d</code>: مبلغ (تومان) و بعد دلیل را بفرست:\n<code>50000 بازگشت وجه سفارش ۱۲</code>"
                  % ("➕ افزایش" if parts[3] == "add" else "➖ کسر", tid)); return
    if parts[1:2] == ["ublock"] and len(parts) == 3 and parts[2].isdigit():
        tid = int(parts[2]); cust = get_customer(tid)
        if not cust: core.answer(cbid, "پیدا نشد"); return
        c = core.db(); c.execute("UPDATE shop_customers SET blocked=1-blocked WHERE tg_id=?", (tid,)); c.commit(); c.close()
        log(uid, "unblock" if cust["blocked"] else "block", tid)
        core.answer(cbid, "رفع مسدودی شد" if cust["blocked"] else "مسدود شد"); core.edit(chat, mid, *user_card(tid)); return
    if len(parts) == 3 and parts[2].isdigit():
        what, rid = parts[1], int(parts[2])
        if what in ("plan", "plan_act", "plan_pop", "plan_price", "plan_del"):
            p = get_plan(rid)
            if not p: core.answer(cbid, "پیدا نشد"); core.edit(chat, mid, *plans_admin()); return
            if what == "plan_price":
                core.pending[chat] = {"stage": "sa_plan_price", "id": rid}; core.answer(cbid)
                core.edit(chat, mid, "قیمت جدید «%s» را به تومان بفرست:" % plan_title(p["gb"], p["days"]),
                          [[{"text": "انصراف", "callback_data": "sa:plan:%d" % rid}]]); return
            if what == "plan_del":
                c = core.db(); c.execute("DELETE FROM shop_plans WHERE id=?", (rid,)); c.commit(); c.close()
                log(uid, "plan_delete", rid, plan_title(p["gb"], p["days"]))
                core.answer(cbid, "حذف شد"); core.edit(chat, mid, *plans_admin()); return
            if what in ("plan_act", "plan_pop"):
                col = "active" if what == "plan_act" else "popular"
                c = core.db(); c.execute("UPDATE shop_plans SET %s=1-%s WHERE id=?" % (col, col), (rid,)); c.commit(); c.close()
                log(uid, what, rid); p = get_plan(rid)
            core.answer(cbid); core.edit(chat, mid, *plan_admin(p)); return
        if what in ("card_act", "card_del"):
            c = core.db()
            if what == "card_act": c.execute("UPDATE shop_cards SET active=1-active WHERE id=?", (rid,))
            else: c.execute("DELETE FROM shop_cards WHERE id=?", (rid,))
            c.commit(); c.close(); log(uid, what, rid)
            core.answer(cbid); core.edit(chat, mid, *cards_admin()); return
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
    if text in (B_LINKS, B_SHOP):
        core.pending.pop(chat, None); review_pending.pop(uid, None)
        if text == B_LINKS: core.send(chat, core.WELCOME, core.main_menu_kb())
        else: core.send(chat, shop_admin_text(), shop_admin_kb())
        return True
    if review_text(chat, uid, text): return True
    st = core.pending.get(chat)
    if st and st.get("stage") == "sa_user_find":
        cust = find_customer(m, text)
        if not cust:
            core.send(chat, "مشتری پیدا نشد. آیدی عددی، @یوزرنیم یا یک پیام فورواردی بفرست:",
                      [[{"text": "بازگشت", "callback_data": "sa:menu"}]]); return True
        core.pending.pop(chat, None); core.send(chat, *user_card(cust["tg_id"])); return True
    if st and st.get("stage") == "sa_wallet":
        words = text.split(maxsplit=1)
        n = parse_amount(words[0]) if words else None
        if not n or len(words) < 2:
            core.send(chat, "مبلغ و دلیل را بفرست، مثلاً <code>50000 بازگشت وجه</code>:"); return True
        tid, delta, note = st["id"], st["sign"] * n, words[1].strip()[:100]
        bal = wallet_change(tid, delta, "admin", note=note, actor=uid)
        if bal is None:
            core.send(chat, "❌ موجودی مشتری کمتر از این مبلغ است."); return True
        core.pending.pop(chat, None); log(uid, "wallet_adjust", tid, "%+d %s" % (delta, note))
        core.send(tid, "💰 %s %s کیف پولت توسط پشتیبانی.\nدلیل: %s\nموجودی: <b>%s</b>"
                  % (toman(abs(delta)), "اضافه شد به" if delta > 0 else "کم شد از", html.escape(note), toman(bal)))
        core.send(chat, "✅ انجام شد.\n\n" + user_card(tid)[0], user_card(tid)[1]); return True
    if st and st.get("stage") == "sa_plan_add":
        nums = to_ascii(text).split()
        try:
            gb, days, price = float(nums[0]), float(nums[1]), parse_amount(nums[2])
            if len(nums) != 3 or gb < 0 or days < 0 or not price or gb > 100000 or days > 3650: raise ValueError()
        except (ValueError, IndexError):
            core.send(chat, "سه عدد بفرست: حجم، روز، قیمت. مثلاً <code>30 30 150000</code>"); return True
        c = core.db()
        pid = c.execute("INSERT INTO shop_plans(gb,days,price,created_ts) VALUES(?,?,?,?)", (gb, days, price, int(time.time()))).lastrowid
        c.commit(); c.close(); core.pending.pop(chat, None); log(uid, "plan_add", pid, "%g/%g/%d" % (gb, days, price))
        core.send(chat, "✅ پلن اضافه شد.", plans_admin()[1]); return True
    if st and st.get("stage") == "sa_plan_price":
        price = parse_amount(text)
        if not price: core.send(chat, "قیمت را به تومان بفرست، مثلاً <code>150000</code>:"); return True
        c = core.db(); c.execute("UPDATE shop_plans SET price=? WHERE id=?", (price, st["id"])); c.commit(); c.close()
        core.pending.pop(chat, None); log(uid, "plan_price", st["id"], price)
        p = get_plan(st["id"])
        if p: core.send(chat, *plan_admin(p))
        return True
    if st and st.get("stage") == "sa_card_add":
        raw = to_ascii(text).strip()
        digits = "".join(ch for ch in raw[:30] if ch.isdigit())
        holder = raw.lstrip("0123456789 -").strip()
        if len(digits) != 16 or not holder:
            core.send(chat, "شمارهٔ ۱۶ رقمی و بعد نام صاحب کارت را بفرست:\n<code>6037991234567890 فاطمه میرخلیلی</code>"); return True
        c = core.db()
        cid = c.execute("INSERT INTO shop_cards(number,holder,created_ts) VALUES(?,?,?)", (digits, holder[:60], int(time.time()))).lastrowid
        c.commit(); c.close(); core.pending.pop(chat, None); log(uid, "card_add", cid, "…" + digits[-4:])
        core.send(chat, "✅ کارت اضافه شد.", cards_admin()[1]); return True
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
    expire_orders()
    run_auto_renewals()

def ticker():
    while True:
        try: tick()
        except Exception as e: print("shop tick err", e, flush=True)
        time.sleep(TICK_SECONDS)

def start_threads():
    threading.Thread(target=ticker, daemon=True).start()
