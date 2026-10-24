# Mohajer shop - the customer storefront of the Telegram bot (stdlib only).
# bot.py keeps answering its admins exactly as before and hands every other update
# here: customers (buy / test / wallet / services / referrals / tickets) and support
# staff. This module reuses bot.py's core (db, tg, create_user, enforcer state ...)
# through `core`, which bot.py binds once at import time (see bind()).
# Roadmap and feature list: docs/SHOP.md.
import os, re, json, math, time, html, base64, binascii, sqlite3, secrets, tempfile, threading, urllib.request, urllib.parse, urllib.error

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
                                    ("wallet", "INTEGER NOT NULL DEFAULT 0"),    # toman; only wallet_apply() changes it
                                    ("notify", "INTEGER NOT NULL DEFAULT 1")])   # customer wants usage/expiry warnings
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
    _add_cols(c, "shop_orders", [("pay_method", "TEXT NOT NULL DEFAULT 'card'"),    # card | wallet
                                 ("discount", "INTEGER NOT NULL DEFAULT 0"),         # toman off the list price
                                 ("delivered_ts", "INTEGER")])                       # set exactly once by fulfill()
    _add_cols(c, "shop_services", [("plan_id", "INTEGER"),                          # plan used for auto-renew
                                   ("auto_renew", "INTEGER NOT NULL DEFAULT 0"),
                                   ("auto_note_ts", "INTEGER NOT NULL DEFAULT 0"),  # last "not enough balance" note
                                   ("warn_flags", "INTEGER NOT NULL DEFAULT 0")])   # WARN_* already sent this period
    c.execute("CREATE INDEX IF NOT EXISTS shop_orders_status ON shop_orders(status)")
    c.execute("CREATE INDEX IF NOT EXISTS shop_services_kind ON shop_services(kind, followup_sent)")
    # The wallet can never go negative, even if some future code path bypasses wallet_apply().
    c.execute("""CREATE TRIGGER IF NOT EXISTS shop_wallet_nonneg BEFORE UPDATE OF wallet ON shop_customers
        WHEN NEW.wallet < 0 BEGIN SELECT RAISE(ABORT, 'negative wallet'); END""")
    # Support tickets. status: open (waiting for staff) -> answered (waiting for customer) -> closed.
    c.execute("""CREATE TABLE IF NOT EXISTS shop_tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL, token TEXT, category TEXT NOT NULL,
        carrier TEXT, status TEXT NOT NULL, rating INTEGER, created_ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS shop_tickets_status ON shop_tickets(status)")
    c.execute("""CREATE TABLE IF NOT EXISTS shop_ticket_msgs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, sender INTEGER NOT NULL,
        role TEXT NOT NULL, text TEXT, file TEXT, ts INTEGER NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS shop_orders_owner ON shop_orders(tg_id)")
    c.execute("CREATE INDEX IF NOT EXISTS shop_ticket_msgs_ticket ON shop_ticket_msgs(ticket_id)")
    c.execute("CREATE INDEX IF NOT EXISTS shop_tickets_owner ON shop_tickets(tg_id)")
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

AMOUNT_MAX = 10 ** 9   # toman; anything above is a typo, never money

def parse_amount(text):
    """'150000' / '150,000' / '۱۵۰٬۰۰۰' / '150.000 تومان' -> 150000. Only a plain integer or one
    with 3-digit thousands separators is accepted; '1.5', '150000.5' or '100k' are None (a silently
    mangled amount is worse than asking again)."""
    s = to_ascii(text).replace("تومان", "").replace("ت", "").strip()
    if not re.fullmatch(r"\d+|\d{1,3}([,٬،.]\d{3})+", s): return None
    n = int(re.sub(r"[^0-9]", "", s))
    return n if 0 < n <= AMOUNT_MAX else None

def _int(s, maxlen=15):
    """ASCII-decimal string -> int, else None. ('²'.isdigit() is True, int('²') raises.)"""
    s = to_ascii(s or "").strip()
    return int(s) if s and len(s) <= maxlen and s.isascii() and s.isdigit() else None

def fa_bytes(b):
    b = max(0, int(b or 0))
    if b >= core.GB: return fa("%.1f گیگ" % (b / core.GB)).replace(".۰ ", " ")
    return fa("%d مگ" % (b // (1024 * 1024)))

def fa_left(ts, now=None):
    if not ts: return "نامحدود"
    left = ts - (int(time.time()) if now is None else now)
    if left <= 0: return "تمام شده"
    d, h = left // 86400, (left % 86400) // 3600
    return fa("%d روز و %d ساعت" % (d, h) if d else ("%d ساعت" % h if h else "%d دقیقه" % max(1, left // 60)))

def bar(frac, width=10):
    n = max(0, min(width, int(round(frac * width))))
    return "▰" * n + "▱" * (width - n)

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
        cur = c.execute("SELECT first_name,username,last_seen FROM shop_customers WHERE tg_id=?", (uid,)).fetchone()
        name, uname = user.get("first_name", ""), user.get("username")
        if cur:
            if (cur["first_name"], cur["username"]) != (name, uname) or now - cur["last_seen"] >= 600:
                if uname:   # a username can move to another person; only the newest owner keeps it
                    c.execute("UPDATE shop_customers SET username=NULL WHERE lower(username)=? AND tg_id<>?", (uname.lower(), uid))
                c.execute("UPDATE shop_customers SET first_name=?,username=?,last_seen=? WHERE tg_id=?", (name, uname, now, uid))
        else:
            if uname:
                c.execute("UPDATE shop_customers SET username=NULL WHERE lower(username)=?", (uname.lower(),))
            if referrer == uid or not (referrer and c.execute(
                    "SELECT 1 FROM shop_customers WHERE tg_id=?", (referrer,)).fetchone()):
                referrer = None   # only a real, different, existing customer can be the inviter
            c.execute("INSERT INTO shop_customers(tg_id,first_name,username,joined_ts,last_seen,referrer_id) "
                      "VALUES(?,?,?,?,?,?)", (uid, name, uname, now, now, referrer))
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
    if len(parts) == 2 and parts[1].startswith("ref_"):
        return _int(parts[1][4:])
    return None

# ---------------- customer UI ----------------
B_BUY, B_TEST, B_SERVICES, B_WALLET = "🛒 خرید سرویس", "🎁 تست رایگان", "📦 سرویس‌های من", "💰 کیف پول"
B_INVITE, B_REPORT, B_GUIDE, B_ACCOUNT = "👥 دعوت دوستان", "🛠 گزارش اختلال", "📚 آموزش اتصال", "👤 حساب من"
B_RULES = "📜 قوانین"
CUSTOMER_MENU = [[B_BUY, B_TEST], [B_SERVICES, B_WALLET], [B_INVITE, B_REPORT], [B_GUIDE, B_ACCOUNT], [B_RULES]]
RULES_TEXT = ("📜 <b>قوانین استفاده از سرویس</b>\n\n"
              "• اختلال، قطعی یا محدودیت‌های اینترنت و اپراتورها خارج از کنترل ماست و مسئولیت آن‌ها را نمی‌پذیریم.\n\n"
              "• مشکلات نرم‌افزارهای اتصال یا تنظیمات دستگاه کاربر خارج از مسئولیت ماست.\n\n"
              "• برای راهنمایی یا بررسی مشکل سرویس، از بخش «گزارش اختلال» استفاده کنید.")

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
            [{"text": "📥 دریافت کانفیگ", "callback_data": "c:configs:%s:0" % token}],
            [{"text": "📚 آموزش اتصال", "callback_data": "c:guide"}]]
    return rows + (extra or [])

def send_configs(chat, uid, token, index=0):
    """Read the existing subscription locally; never require the customer's browser to open it."""
    if not owned_service(uid, token):
        core.send(chat, "این سرویس پیدا نشد."); return
    try:
        with open(core.sub_path(token), "rb") as f:
            raw = f.read()
        links = [line.strip() for line in base64.b64decode(raw, validate=True).decode("utf-8").splitlines()
                 if line.strip()]
    except (OSError, ValueError, binascii.Error):
        core.send(chat, "دریافت کانفیگ فعلاً ممکن نیست؛ دوباره امتحان کنید یا به پشتیبانی پیام بدهید."); return
    if not 0 <= index < len(links):
        core.send(chat, "کانفیگی برای نمایش پیدا نشد."); return
    head = "📥 کانفیگ %s از %s\nمتن زیر را کپی و در برنامهٔ اتصال وارد کنید." % (fa(index + 1), fa(len(links)))
    text = head + "\n\n<code>%s</code>" % html.escape(links[index])
    nav = []
    if index: nav.append({"text": "قبلی", "callback_data": "c:configs:%s:%d" % (token, index - 1)})
    if index + 1 < len(links): nav.append({"text": "بعدی", "callback_data": "c:configs:%s:%d" % (token, index + 1)})
    kb = ([nav] if nav else []) + [[{"text": "بازگشت به سرویس", "callback_data": "c:svc:%s" % token}]]
    if len(text) > 3800:
        result = upload("sendDocument", chat, "document", "config.txt", links[index].encode("utf-8"), head)
        core.send(chat, "کانفیگ در فایل بالا قرار دارد." if result.get("ok") else "ارسال فایل ناموفق بود؛ دوباره امتحان کنید.", kb)
    else:
        core.send(chat, text, kb)

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
              % (fa_size(test_mb()), fa_hours(test_hours()), html.escape(core.sub_url(token))), link_kb(token))

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
    if kind not in ("good", "bad"): return
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
                  "WHERE s.token=? AND s.tg_id=? AND u.pending_delete=0", (token, uid)).fetchone()
    c.close(); return r

# --- referral discount ---
REF_REWARD_DEFAULT, REF_DISCOUNT_DEFAULT = 10, 10   # percent
REF_REWARD_CHOICES, REF_DISCOUNT_CHOICES = (0, 5, 10, 15, 20), (0, 5, 10, 15, 20)

def ref_reward_pct():   return int(setting("ref_reward", REF_REWARD_DEFAULT))
def ref_discount_pct(): return int(setting("ref_discount", REF_DISCOUNT_DEFAULT))

def has_purchased(uid):
    c = core.db()
    n = c.execute("SELECT COUNT(*) FROM shop_orders WHERE tg_id=? AND status IN ('approved','review') AND kind!='topup'",
                  (uid,)).fetchone()[0]
    c.close(); return n > 0

def first_buy_discount(uid):
    """Percent off for a customer who joined through an invite and has not bought yet."""
    cust = get_customer(uid)
    if not cust or not cust["referrer_id"] or has_purchased(uid): return 0
    return ref_discount_pct()

def priced(uid, plan):
    """The plan as this customer pays for it (dict): price after any first-purchase discount."""
    p = dict(plan); p["list_price"] = p.get("list_price", p["price"])
    d = first_buy_discount(uid)
    p["price"] = p["list_price"] * (100 - d) // 100
    p["discount"] = p["list_price"] - p["price"]
    return p

def price_text(p):
    return ("<s>%s</s> %s" % (fa("{:,}".format(p["list_price"]).replace(",", "٬")), toman(p["price"]))
            if p.get("discount") else toman(p["price"]))

def plans_view(uid, token=None):
    plans = [priced(uid, p) for p in list_plans()]
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
    if plans[0]["discount"]:
        head += "\n\n🎁 چون با دعوت دوستت اومدی، <b>%s٪ تخفیف</b> روی اولین خریدت داری (قیمت‌ها با تخفیفه)." % fa(first_buy_discount(uid))
    return head + ("\n\n" + "\n".join(per_gb) if per_gb else ""), kb

def send_plans(chat, uid, token=None, mid=None):
    text, kb = plans_view(uid, token)
    if mid: core.edit(chat, mid, text, kb)
    else: core.send(chat, text, kb)

def invoice_view(plan, token=None, bal=0):
    suffix = (":" + token) if token else ""
    text = ("🧾 <b>پیش‌فاکتور</b>\n\n📦 %s\n%s💰 مبلغ: <b>%s</b>%s\n👛 موجودی کیف پول: %s"
            % (plan_title(plan["gb"], plan["days"]), "🔄 تمدید سرویس فعلی\n" if token else "", price_text(plan),
               " (🎁 تخفیف دعوت)" if plan.get("discount") else "", toman(bal)))
    kb = []
    if bal >= plan["price"]:
        kb.append([{"text": "💰 پرداخت از کیف پول (فوری)", "callback_data": "c:wpay:%d%s" % (plan["id"], suffix)}])
    elif bal > 0:
        short = -(-(plan["price"] - bal) // 1000) * 1000          # round the shortfall up to 1,000
        short = max(short, TOPUP_MIN)
        text += "\n\nبا شارژ %s می‌تونی از کیف پول پرداخت کنی." % toman(short)
        kb.append([{"text": "➕ شارژ %s" % toman(short), "callback_data": "c:topup_amt:%d" % short}])
    kb += [[{"text": "💳 پرداخت کارت‌به‌کارت", "callback_data": "c:pay:%d%s" % (plan["id"], suffix)}],
           [{"text": B_RULES, "callback_data": "c:rules"}],
           [{"text": "بازگشت", "callback_data": "c:plans%s" % suffix}]]
    return text, kb

ORDER_TTL = 3600            # an invoice waits this long for its receipt
LATE_RECEIPT = 24 * 3600    # a receipt for an expired invoice is still accepted (people pay late)
OPEN = ("awaiting", "review")

def get_order(oid):
    c = core.db(); r = c.execute("SELECT * FROM shop_orders WHERE id=?", (oid,)).fetchone(); c.close(); return r

def create_order(uid, kind, price, plan=None, token=None, now=None):
    """Invoice at the exact quoted price, with no surcharge. Cancel older unpaid invoices."""
    now = int(time.time()) if now is None else now
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        cards = c.execute("SELECT id FROM shop_cards WHERE active=1").fetchall()
        if not cards: c.rollback(); return None
        c.execute("UPDATE shop_orders SET status='cancelled' WHERE tg_id=? AND status='awaiting'", (uid,))
        amount = price
        oid = c.execute("INSERT INTO shop_orders(tg_id,kind,token,plan_id,gb,days,price,amount,card_id,status,created_ts,discount) "
                        "VALUES(?,?,?,?,?,?,?,?,?,'awaiting',?,?)",
                        (uid, kind, token, plan["id"] if plan else None, plan["gb"] if plan else 0,
                         plan["days"] if plan else 0, price, amount, secrets.choice(cards)[0], now,
                         (plan.get("discount", 0) if isinstance(plan, dict) else 0) if plan else 0)).lastrowid
        c.commit()
    finally:
        c.close()
    return get_order(oid)

def plan_order(uid, plan, token=None):
    return create_order(uid, "renew" if token else "buy", priced(uid, plan)["price"], priced(uid, plan), token)

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
RECEIPT_WANTED = "awaiting"   # plus expired/rejected invoices still inside LATE_RECEIPT (people pay late, re-send clearer photos)

def receipt_candidates(c, uid, now):
    """The invoice(s) a receipt can pay for: the open one plus every expired, rejected or
    auto-cancelled one of the last 24h (people pay late, or pay invoice A and then start B);
    with several the customer is asked which one."""
    return c.execute("SELECT * FROM shop_orders WHERE tg_id=? AND pay_method='card' AND (status='awaiting' OR "
                     "(status IN ('expired','rejected','cancelled') AND created_ts>=?)) ORDER BY id DESC LIMIT 5",
                     (uid, now - LATE_RECEIPT)).fetchall()

def has_open_invoice(uid, now=None):
    c = core.db(); rows = receipt_candidates(c, uid, int(time.time()) if now is None else now); c.close()
    return bool(rows)

def receive_receipt(chat, uid, file_ref, file_uid, now=None, oid=None):
    """Attach a receipt image to the customer's open invoice and send it to the reviewers. With
    several open invoices the customer is asked which one it pays for (oid comes back via c:rcpt)."""
    now = int(time.time()) if now is None else now
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        rows = receipt_candidates(c, uid, now)
        if oid is not None: rows = [r for r in rows if r["id"] == oid]
        if not rows:
            c.rollback(); set_state(uid, None)
            core.send(chat, "📸 الان سفارش پرداخت‌نشده‌ای نداری.\nاول از «🛒 خرید سرویس» یه پلن انتخاب کن.",
                      [[{"text": "🛒 خرید سرویس", "callback_data": "c:buy"}]]); return
        if c.execute("SELECT 1 FROM shop_orders WHERE receipt_uid=?", (file_uid,)).fetchone():
            c.rollback(); say(chat, "⚠️ این رسید قبلاً ارسال شده. اگه واریز جدیدی داشتی، عکس رسید همون رو بفرست."); return
        if len(rows) > 1:
            c.rollback(); set_state(uid, {"stage": "rcpt_pick", "file": file_ref, "uid": file_uid})
            core.send(chat, "📸 این رسید برای کدوم سفارشه؟ (مبلغ روی رسید رو با فاکتور مطابقت بده)",
                      [[{"text": "#%d · %s · %s%s" % (r["id"], ORDER_KINDS.get(r["kind"], r["kind"]), toman(r["amount"]),
                                                     "" if r["status"] == "awaiting" else " (قبلی)"),
                         "callback_data": "c:rcpt:%d" % r["id"]}] for r in rows]); return
        o = rows[0]
        c.execute("UPDATE shop_orders SET status='review',receipt_file=?,receipt_uid=?,receipt_ts=?,note=NULL,"
                  "decided_ts=NULL,decided_by=NULL WHERE id=?", (file_ref, file_uid, now, o["id"]))
        c.commit()
    finally:
        c.close()
    set_state(uid, None)
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
            [{"text": "✏️ اصلاح مبلغ واریزی (فقط مالک)", "callback_data": "r:amt:%d" % o["id"]}]]

PAID_MAX_FACTOR = 2   # a corrected "paid" amount may be at most twice the invoice; more is a typo, never money

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

def _extend_link(c, token, gb, days, now):
    """Add a plan to a live link inside the caller's transaction. An unlimited dimension stays
    unlimited (a plan never downgrades a link); a limited one gets the plan on top of what is
    really used, so a link that overshot its quota still receives the full plan."""
    u = c.execute("SELECT * FROM users WHERE token=? AND pending_delete=0", (token,)).fetchone()
    if not u: return False
    limit, expiry = u["limit_bytes"] or 0, u["expiry_ts"] or 0
    if not gb: limit = 0
    elif limit: limit = max(limit, core.current_usage(u)) + int(float(gb) * core.GB)
    if not days: expiry = 0
    elif expiry: expiry = max(expiry, now) + int(float(days) * 86400)
    c.execute("UPDATE users SET limit_bytes=?,expiry_ts=? WHERE token=?", (limit, expiry, token))
    return True

def _after_renew(token):
    """Best-effort xray side of a renewal. If it fails the enforcer re-enables the link on its next
    pass (it has quota again), so nothing here may undo the delivery."""
    try: core.refresh_usage(token)
    except Exception as e: print("shop refresh err", e, flush=True)
    try: core.maybe_reenable(token)
    except Exception as e: print("shop reenable err", e, flush=True)

def link_label(o, cust):
    name = re.sub(r"[<>&]", "", (cust["first_name"] if cust else "") or "") or str(o["tg_id"])
    return ("%s · %s" % (name, plan_title(o["gb"], o["days"])))[:33] + " #%d" % o["id"]

def fulfill(o):
    """Deliver an approved order EXACTLY ONCE (idempotent on shop_orders.delivered_ts): credit the
    wallet (topup), extend the customer's live link (renew) or mint a new one (buy). Returns the
    token ('wallet' for a top-up), or None when nothing could be delivered - and then nothing
    was changed, so the caller may refund."""
    now = int(time.time())
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute("SELECT delivered_ts, token FROM shop_orders WHERE id=? AND status='approved'", (o["id"],)).fetchone()
        if not cur: c.rollback(); return None
        if cur["delivered_ts"]: c.rollback(); return cur["token"]
        if o["kind"] == "topup":
            if not c.execute("SELECT 1 FROM shop_wallet_tx WHERE kind='topup' AND ref=?", ("order:%d" % o["id"],)).fetchone():
                wallet_apply(c, o["tg_id"], paid_of(o), "topup", ref="order:%d" % o["id"])
            c.execute("UPDATE shop_orders SET delivered_ts=?,token='wallet' WHERE id=?", (now, o["id"]))
            c.commit(); return "wallet"
        token = o["token"]
        if o["kind"] == "renew" and token and _extend_link(c, token, o["gb"], o["days"], now):
            c.execute("UPDATE shop_services SET kind='paid',plan_id=COALESCE(?,plan_id),auto_note_ts=0,warn_flags=0 WHERE token=?",
                      (o["plan_id"], token))
            c.execute("UPDATE shop_orders SET delivered_ts=? WHERE id=?", (now, o["id"]))
            c.commit()
            _after_renew(token)
            try: reset_warnings(token)
            except Exception as e: print("shop reset_warnings err", e, flush=True)
            return token
        c.rollback()
    except InsufficientFunds:
        c.rollback(); return None                                     # customer row vanished
    finally:
        c.close()
    # A buy - or a renewal whose link is gone (deleted after its grace period): mint a new link.
    cust = get_customer(o["tg_id"]); label = link_label(o, cust)
    c = core.db(); prev = c.execute("SELECT token FROM users WHERE label=?", (label,)).fetchone(); c.close()
    token = prev["token"] if prev else core.create_user(o["gb"], o["days"], label=label)   # adopt an unrecorded mint
    if not token: return None
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO shop_services(token,tg_id,kind,created_ts,plan_id) VALUES(?,?,'paid',?,?)",
                  (token, o["tg_id"], now, o["plan_id"]))
        c.execute("UPDATE shop_orders SET token=?,delivered_ts=? WHERE id=? AND delivered_ts IS NULL", (token, now, o["id"]))
        c.commit()
    finally:
        c.close()
    try: reset_warnings(token)
    except Exception as e: print("shop reset_warnings err", e, flush=True)
    return token

def reset_warnings(token):
    """New period after a buy/renewal: warnings that already apply (a 1-day plan is '<1 day left'
    from the start) count as seen, so the customer is not warned right after paying."""
    c = core.db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    if u:
        c.execute("UPDATE shop_services SET warn_flags=? WHERE token=?", (due_warnings(u, int(time.time())) & ~WARN_ENDED, token))
        c.commit()
    c.close()

_deliver_lock = threading.Lock()   # the update loop and the ticker both deliver; never the same order twice at once

DELIVER_HEADS = {"card": "🎉 <b>پرداختت تأیید شد!</b>", "wallet": "🎉 <b>خریدت انجام شد!</b>", "auto": "🔁 <b>تمدید خودکار انجام شد</b>"}

def deliver(oid, actor=None):
    """fulfill() an approved order, then tell the customer, pay the referral and refresh the
    reviewers' copies. Safe to call again: a delivered order is never delivered twice, and a
    failed one is retried by the ticker (retry_deliveries)."""
    with _deliver_lock:
        o = get_order(oid)
        if not o or o["status"] != "approved": return None
        if o["delivered_ts"]: return o["token"]
        token = None
        try: token = fulfill(o)
        except Exception as e: print("shop fulfill err", oid, e, flush=True)
        if not token: return None
    # Everything below is best effort: the delivery is committed, a hiccup here must not undo or repeat it.
    o = get_order(oid)
    for step in (lambda: log(actor, "order_deliver", oid, paid_of(o)),
                 lambda: _after_deliver(o, token)):
        try: step()
        except Exception as e: print("shop deliver step err", oid, e, flush=True)
    return o["token"] or token

def _after_deliver(o, token):
    oid = o["id"]
    extra = paid_of(o) - o["price"] if o["kind"] != "topup" and o["pay_method"] == "card" else 0
    if extra > 0: wallet_change(o["tg_id"], extra, "overpay", ref="order:%d" % oid)
    if o["kind"] == "topup":
        core.send(o["tg_id"], "💰 <b>کیف پولت %s شارژ شد!</b>\nموجودی: <b>%s</b>" % (toman(paid_of(o)), toman(balance(o["tg_id"]))),
                  [[{"text": "🛒 خرید سرویس", "callback_data": "c:buy"}]])
    else:
        head = DELIVER_HEADS.get(o["pay_method"], DELIVER_HEADS["card"])
        if o["pay_method"] == "auto": head += " (%s از کیف پولت)" % toman(o["price"])
        deliver_message(o, token, head, extra)
        reward_inviter(o)
    if o["pay_method"] == "card": refresh_review(oid)

def approve(oid, uid):
    """review -> approved exactly once, then deliver. Returns 'ok' | 'done' | 'short' | 'pending'.
    An approved order never goes back to review: delivery is idempotent and retried by the ticker,
    so a reviewer can never hand out the same plan twice."""
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
    log(uid, "order_approve", oid, paid_of(o))
    if deliver(oid, uid): return "ok"
    refresh_review(oid)
    for a in owner_ids():
        core.send(a, "⚠️ سفارش #%d تأیید شد ولی ساخت سرویس الان انجام نشد (xray؟). هر چند دقیقه دوباره تلاش می‌شود." % oid)
    return "pending"

RETRY_AFTER, RETRY_ALERT_EVERY, RETRY_GIVE_UP = 120, 6 * 3600, 24 * 3600

def retry_deliveries(now=None):
    """Approved-but-undelivered orders (a crash between approval and delivery, xray down): deliver
    again; the owners hear about a stuck order at most every few hours."""
    now = int(time.time()) if now is None else now
    c = core.db()
    rows = c.execute("SELECT id, tg_id, price, pay_method, decided_ts FROM shop_orders WHERE status='approved' "
                     "AND delivered_ts IS NULL AND decided_ts<?", (now - RETRY_AFTER,)).fetchall()
    c.close()
    for r in rows:
        if deliver(r["id"]): continue
        if r["pay_method"] in ("wallet", "auto") and now - r["decided_ts"] >= RETRY_GIVE_UP:
            if refund_undelivered(r["id"], r["tg_id"], r["price"]):   # a day without a link: the customer gets the money back
                core.send(r["tg_id"], "❌ ساخت سرویس سفارش #%d ممکن نشد؛ مبلغ به کیف پولت برگشت. ببخشید 🙏" % r["id"])
                for a in owner_ids(): core.send(a, "↩️ سفارش کیف‌پولی #%d بعد از ۲۴ ساعت تحویل نشد و بازپرداخت شد." % r["id"])
            continue
        last = int(setting("stuck_alert_%d" % r["id"], 0))
        if now - last >= RETRY_ALERT_EVERY:
            set_setting("stuck_alert_%d" % r["id"], now)
            for a in owner_ids():
                core.send(a, "⚠️ سفارش #%d هنوز تحویل نشده (%s ساعت). وضعیت xray را بررسی کن؛ تلاش خودکار ادامه دارد."
                          % (r["id"], fa((now - r["decided_ts"]) // 3600)))

def deliver_message(o, token, head, extra=0):
    body = "سرویست تمدید شد ✅" if o["kind"] == "renew" and o["token"] == token else "سرویس جدیدت فعال شد ✅"
    if o["kind"] == "renew" and o["token"] != token:
        body = "لینک قبلیت حذف شده بود؛ یه سرویس جدید برات ساختیم ✅\n⚠️ لینک جدید رو جایگزین قبلی کن."
    text = "%s\n%s\n📦 %s\n\n🔗 <code>%s</code>" % (head, body, plan_title(o["gb"], o["days"]), html.escape(core.sub_url(token)))
    c = core.db(); u = c.execute("SELECT disabled_ts FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if u and u["disabled_ts"]: text += "\n\n⏳ فعال‌سازی لینک تا چند دقیقهٔ دیگه خودکار انجام می‌شه."
    if extra > 0: text += "\n\n💰 %s اضافه واریز کرده بودی؛ به کیف پولت برگشت." % toman(extra)
    core.send(o["tg_id"], text, service_kb(token))

def auto_kb(token):
    c = core.db(); r = c.execute("SELECT auto_renew FROM shop_services WHERE token=?", (token,)).fetchone(); c.close()
    on = bool(r and r[0])
    return [[{"text": "🔁 تمدید خودکار: %s" % ("روشن ✅" if on else "خاموش"), "callback_data": "c:auto:%s" % token}]]

DUP_WINDOW = 60   # a second identical wallet purchase within this many seconds is a double tap, not a new order

def wallet_buy(uid, plan, token=None, kind="purchase"):
    """Pay a plan from the wallet: debit + approved order in one transaction, then deliver. If
    nothing could be delivered the debit is refunded. Returns (status, order, token);
    status ok | funds | dup | gone | failed."""
    now = int(time.time()); plan = priced(uid, plan); method = "auto" if kind == "auto" else "wallet"
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        dup = c.execute("SELECT id FROM shop_orders WHERE tg_id=? AND pay_method IN ('wallet','auto') AND plan_id=? AND kind=? "
                        "AND (kind='buy' OR token=?) AND status='approved' AND created_ts>?",
                        (uid, plan["id"], "renew" if token else "buy", token, now - DUP_WINDOW)).fetchone()
        if dup: c.rollback(); return "dup", get_order(dup["id"]), None
        if token and not c.execute("SELECT 1 FROM shop_services s JOIN users u ON u.token=s.token WHERE s.token=? AND s.tg_id=? "
                                   "AND u.pending_delete=0", (token, uid)).fetchone():
            c.rollback(); return "gone", None, None
        oid = c.execute("INSERT INTO shop_orders(tg_id,kind,token,plan_id,gb,days,price,amount,status,created_ts,decided_ts,"
                        "decided_by,pay_method,discount) VALUES(?,?,?,?,?,?,?,?,'approved',?,?,?,?,?)",
                        (uid, "renew" if token else "buy", token, plan["id"], plan["gb"], plan["days"], plan["price"],
                         plan["price"], now, now, uid, method, plan["discount"])).lastrowid
        wallet_apply(c, uid, -plan["price"], kind, ref="order:%d" % oid)
        c.commit()
    except InsufficientFunds:
        c.rollback(); return "funds", None, None
    finally:
        c.close()
    tok = deliver(oid, uid)
    if not tok and refund_undelivered(oid, uid, plan["price"]):   # nothing was delivered: give the money back
        return "failed", get_order(oid), None
    return "ok", get_order(oid), get_order(oid)["token"]

def refund_undelivered(oid, uid, amount):
    """approved+undelivered -> failed and refunded, atomically; False if it was (or is being) delivered."""
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if not c.execute("UPDATE shop_orders SET status='failed' WHERE id=? AND status='approved' AND delivered_ts IS NULL", (oid,)).rowcount:
            c.rollback(); return False
        wallet_apply(c, uid, amount, "refund", ref="order:%d" % oid)
        c.commit(); return True
    except sqlite3.IntegrityError:
        c.rollback(); return False
    finally:
        c.close()

def wallet_pay(chat, mid, uid, plan_id, token=None):
    plan = get_plan(plan_id)
    if not plan or not plan["active"]:
        core.edit(chat, mid, "این پلن دیگه موجود نیست.", [[{"text": "🛒 پلن‌ها", "callback_data": "c:plans"}]]); return
    if token and not owned_service(uid, token):
        core.edit(chat, mid, "این سرویس پیدا نشد."); return
    status, o, tok = wallet_buy(uid, plan, token)
    if status == "funds":
        text, kb = invoice_view(priced(uid, plan), token, balance(uid)); core.edit(chat, mid, "⚠️ موجودی کافی نیست.\n\n" + text, kb); return
    if status == "dup":
        core.edit(chat, mid, "✅ همین خرید چند لحظه پیش انجام شد؛ لینکش رو بالاتر فرستادیم."); return
    if status == "gone":
        core.edit(chat, mid, "این سرویس دیگه وجود نداره؛ می‌تونی یه سرویس جدید بخری.", [[{"text": "🛒 خرید سرویس", "callback_data": "c:buy"}]]); return
    if status == "failed":
        core.edit(chat, mid, "❌ ساخت سرویس الان ممکن نشد؛ مبلغ به کیف پولت برگشت. چند دقیقهٔ دیگه دوباره امتحان کن."); return
    core.edit(chat, mid, "✅ از کیف پولت %s کم شد. موجودی: %s" % (toman(o["price"]), toman(balance(uid))))

def reward_inviter(o):
    """The inviter earns a share of the invitee's FIRST real purchase. ref='ref:<invitee>' is
    unique in the ledger, so it can only ever be paid once per invited customer."""
    cust = get_customer(o["tg_id"])
    if not cust or not cust["referrer_id"] or o["kind"] == "topup": return
    c = core.db()
    n = c.execute("SELECT COUNT(*) FROM shop_orders WHERE tg_id=? AND status='approved' AND delivered_ts IS NOT NULL "
                  "AND kind!='topup'", (o["tg_id"],)).fetchone()[0]
    inviter_bought = c.execute("SELECT 1 FROM shop_orders WHERE tg_id=? AND status='approved' AND delivered_ts IS NOT NULL "
                               "AND kind!='topup'", (cust["referrer_id"],)).fetchone()
    c.close()
    amount = o["price"] * ref_reward_pct() // 100
    if n != 1 or amount <= 0 or not inviter_bought: return   # a reward only ever goes to a paying customer
    bal = wallet_change(cust["referrer_id"], amount, "referral", ref="ref:%d" % o["tg_id"])
    if bal is not None:
        core.send(cust["referrer_id"], "🎉 دوستت %s اولین خریدش رو انجام داد!\n💰 %s پاداش به کیف پولت اضافه شد. موجودی: <b>%s</b>"
                  % (html.escape(cust["first_name"] or ""), toman(amount), toman(bal)))

def bot_username():
    name = setting("bot_username")
    if not name:
        name = ((core.tg("getMe") or {}).get("result") or {}).get("username")
        if name: set_setting("bot_username", name)
    return name or "bot"

def invite_link(uid):
    return "https://t.me/%s?start=ref_%d" % (bot_username(), uid)

def invite_stats(uid):
    c = core.db()
    invited = c.execute("SELECT COUNT(*) FROM shop_customers WHERE referrer_id=?", (uid,)).fetchone()[0]
    buyers = c.execute("SELECT COUNT(DISTINCT o.tg_id) FROM shop_orders o JOIN shop_customers c ON c.tg_id=o.tg_id "
                       "WHERE c.referrer_id=? AND o.status='approved' AND o.delivered_ts IS NOT NULL "
                       "AND o.kind IN ('buy','renew')", (uid,)).fetchone()[0]
    earned = c.execute("SELECT COALESCE(SUM(delta),0) FROM shop_wallet_tx WHERE tg_id=? AND kind='referral'", (uid,)).fetchone()[0]
    c.close(); return invited, buyers, earned

def share_text():
    d = ref_discount_pct()
    return ("🔐 من از %s وی‌پی‌ان می‌گیرم و راضی‌ام. با لینک من بیا%s 👇"
            % (html.escape(shop_name()), ("، %s٪ تخفیف اولین خرید داری" % fa(d)) if d else ""))

def invite_view(uid):
    link = invite_link(uid); invited, buyers, earned = invite_stats(uid)
    r, d = ref_reward_pct(), ref_discount_pct()
    rules = []
    if r: rules.append("🎁 تو: <b>%s٪</b> مبلغ اولین خریدش به کیف پولت" % fa(r))
    if d: rules.append("🎁 دوستت: <b>%s٪ تخفیف</b> اولین خرید" % fa(d))
    text = ("👥 <b>دعوت دوستان</b>\n\n" +
            ("هر دوستی که با لینک تو بیاد و خرید کنه:\n%s\n\n" % "\n".join(rules) if rules else "") +
            "🔗 لینک دعوت تو:\n<code>%s</code>\n\n📊 دعوت‌شده‌ها: %s · خریدارها: %s\n💰 پاداش تا الان: %s"
            % (link, fa(invited), fa(buyers), toman(earned)))
    kb = [[{"text": "📤 فرستادن برای دوستان", "url": "https://t.me/share/url?url=%s&text=%s"
            % (urllib.parse.quote(link, safe=""), urllib.parse.quote(share_text(), safe=""))}],
          [{"text": "📋 پیام آماده برای فوروارد", "callback_data": "c:ref_msg"}]]
    return text, kb

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

review_pending = {}   # reviewer uid -> {"kind": "amount" | "reason", "oid": id, "ts": when}; staff only, tiny
PENDING_TTL = 600     # a typed-answer prompt for staff expires after this; stale prompts must never eat a later message

def _pending_get(d, uid, now=None):
    st = d.get(uid)
    if st and (now or time.time()) - st.get("ts", 0) > PENDING_TTL:
        d.pop(uid, None); return None
    return st

def clear_admin_state(chat, uid):
    """Forget every shop prompt for an admin/support member (called on any command, menu button or
    unrelated callback, so no typed text can be swallowed by an old prompt)."""
    review_pending.pop(uid, None); ticket_pending.pop(uid, None)
    st = core.pending.get(chat)
    if st and str(st.get("stage", "")).startswith("sa_"): core.pending.pop(chat, None)

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
    oid = _int(parts[2]) if len(parts) >= 3 else None
    if oid is None: core.answer(cbid); return
    action = parts[1]
    o = get_order(oid)
    if not o: core.answer(cbid, "سفارش پیدا نشد"); return
    owner = uid in owner_ids()
    if action == "q":
        core.answer(cbid); send_review(oid, to=[chat]); return
    if o["status"] != "review":
        core.answer(cbid, "این سفارش قبلاً بررسی شده"); refresh_review(oid); return
    if action == "ok":
        # Support may only approve an untouched invoice (exact amount); anything that moves money
        # beyond the invoice (a corrected amount -> wallet credit) needs an owner.
        if not owner and o["paid_amount"] is not None:
            core.answer(cbid, "مبلغ این سفارش اصلاح شده؛ فقط مالک می‌تواند تأییدش کند"); return
        res = approve(oid, uid)
        core.answer(cbid, {"ok": "✅ تأیید شد و سرویس ارسال شد", "done": "این سفارش قبلاً بررسی شده",
                           "short": "مبلغ واریزی کمتر از فاکتور است؛ رد کن یا مبلغ را اصلاح کن",
                           "pending": "✅ تأیید شد؛ ساخت سرویس کمی طول می‌کشد و خودکار تکرار می‌شود"}[res]); return
    if action == "amt" and not owner:
        core.answer(cbid, "اصلاح مبلغ فقط برای مالک ممکن است؛ در صورت اختلاف، رد کن یا به مالک بگو"); return
    if action == "no":
        core.answer(cbid)
        kb = [[{"text": r, "callback_data": "r:nor:%d:%d" % (oid, i)}] for i, r in enumerate(REJECT_REASONS)]
        kb += [[{"text": "✏️ دلیل دیگر", "callback_data": "r:nox:%d" % oid}], [{"text": "بازگشت", "callback_data": "r:back:%d" % oid}]]
        core.tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": kb}); return
    if action == "nor" and len(parts) == 4 and _int(parts[3]) is not None and int(parts[3]) < len(REJECT_REASONS):
        reject(oid, uid, REJECT_REASONS[int(parts[3])]); core.answer(cbid, "رد شد"); return
    if action == "back":
        review_pending.pop(uid, None); core.answer(cbid)
        core.tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": review_kb(o)}); return
    if action in ("nox", "amt"):
        review_pending[uid] = {"kind": "reason" if action == "nox" else "amount", "oid": oid, "ts": time.time()}; core.answer(cbid)
        core.send(chat, "دلیل رد سفارش #%d را بنویس:" % oid if action == "nox" else
                  "مبلغی که واقعاً برای سفارش #%d واریز شده را به تومان بفرست:" % oid,
                  [[{"text": "انصراف", "callback_data": "r:back:%d" % oid}]]); return
    core.answer(cbid)

def review_text(chat, uid, text):
    """Typed answers to 'other reason' / 'fix amount'. Returns True when it consumed the message."""
    st = _pending_get(review_pending, uid)
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
    if n > o["amount"] * PAID_MAX_FACTOR:
        core.send(chat, "این مبلغ بیش از %s برابر فاکتور (%s) است؛ اگر واقعاً این‌قدر واریز شده، سفارش را رد کن و "
                  "با «👤 کاربران» کیف پولش را دستی شارژ کن." % (fa(PAID_MAX_FACTOR), toman(o["amount"]))); return True
    if uid not in owner_ids(): review_pending.pop(uid, None); return True
    review_pending.pop(uid, None)
    c = core.db(); n_rows = c.execute("UPDATE shop_orders SET paid_amount=? WHERE id=? AND status='review'", (n, o["id"])).rowcount
    c.commit(); c.close()
    if not n_rows: core.send(chat, "این سفارش دیگر در انتظار بررسی نیست."); return True
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

# --- my services ---
def my_services(uid):
    c = core.db()
    rows = c.execute("SELECT u.*, s.kind, s.auto_renew FROM shop_services s JOIN users u ON u.token=s.token "
                     "WHERE s.tg_id=? AND u.pending_delete=0 ORDER BY u.created_ts DESC", (uid,)).fetchall()
    c.close(); return rows

def services_view(uid):
    rows = my_services(uid)
    if not rows:
        return ("📦 هنوز سرویسی نداری.\nمی‌تونی اول تست رایگان بگیری یا مستقیم یه سرویس بخری 👇",
                [[{"text": "🛒 خرید سرویس", "callback_data": "c:buy"}]])
    kb = [[{"text": "%s %s · %s/%s · %s" % ("⏸" if u["disabled_ts"] else "🟢", u["label"], fa_bytes(core.current_usage(u)),
                                            fa_bytes(u["limit_bytes"]) if u["limit_bytes"] else "∞", fa_left(u["expiry_ts"])),
            "callback_data": "c:svc:%s" % u["token"]}] for u in rows]
    return "📦 <b>سرویس‌های من</b>\nروی هر سرویس بزن تا جزئیات و لینک اتصالش رو ببینی:", kb

def today_usage(token):
    c = core.db()
    r = c.execute("SELECT max(end_used-start_used,0) FROM usage_daily WHERE token=? AND day=?", (token, core.day_key())).fetchone()
    c.close(); return (r[0] or 0) if r else 0

def service_kb(token):
    c = core.db(); r = c.execute("SELECT kind FROM shop_services WHERE token=?", (token,)).fetchone(); c.close()
    rows = [[{"text": "📲 باز کردن صفحهٔ اتصال", "url": core.sub_url(token)}],
            [{"text": "📥 دریافت کانفیگ", "callback_data": "c:configs:%s:0" % token}],
            [{"text": "🔄 بروزرسانی وضعیت", "callback_data": "c:svc:%s" % token},
             {"text": "➕ تمدید", "callback_data": "c:renew:%s" % token}]]
    if r and r["kind"] == "paid": rows += auto_kb(token)
    return rows + [[{"text": "📚 آموزش اتصال", "callback_data": "c:guide"}, {"text": "بازگشت", "callback_data": "c:svcs"}]]

def service_view(uid, token, now=None):
    now = int(time.time()) if now is None else now
    u = owned_service(uid, token)
    if not u: return "این سرویس دیگه وجود نداره.", [[{"text": "بازگشت", "callback_data": "c:svcs"}]]
    used, limit = core.current_usage(u), u["limit_bytes"]
    lines = ["%s <b>%s</b>" % ("⏸" if u["disabled_ts"] else "🟢", html.escape(u["label"])), ""]
    if u["disabled_ts"]:
        left_h = max(0, (u["disabled_ts"] + core.GRACE_SECONDS - now) // 3600)
        lines += ["⚠️ <b>غیرفعال شده</b>؛ تا %s ساعت دیگه با تمدید، همین لینک دوباره فعال می‌شه." % fa(left_h), ""]
    if limit:
        lines += ["📊 مصرف: %s از %s" % (fa_bytes(used), fa_bytes(limit)),
                  "%s %s٪" % (bar(used / limit), fa(min(100, int(used * 100 / limit)))),
                  "📦 باقی‌مانده: <b>%s</b>" % fa_bytes(limit - used)]
    else:
        lines.append("📊 مصرف: %s (حجم نامحدود)" % fa_bytes(used))
    lines += ["📅 مصرف امروز: %s" % fa_bytes(today_usage(token)),
              "⏳ زمان باقی‌مانده: <b>%s</b>" % fa_left(u["expiry_ts"], now) +
              (" (تا %s)" % fa_date(u["expiry_ts"]) if u["expiry_ts"] else ""),
              "", "🔗 <code>%s</code>" % html.escape(core.sub_url(token))]
    return "\n".join(lines), service_kb(token)

# --- usage / expiry warnings ---
WARN_USED80, WARN_DAYS3, WARN_DAYS1, WARN_ENDED = 1, 2, 4, 8

def due_warnings(u, now):
    """Bits that apply now, most urgent first; earlier stages are implied (no 3-day after a 1-day)."""
    if u["disabled_ts"]: return WARN_ENDED | WARN_DAYS1 | WARN_DAYS3 | WARN_USED80
    bits = 0
    if u["expiry_ts"]:
        left = u["expiry_ts"] - now
        if left <= 86400: bits |= WARN_DAYS1 | WARN_DAYS3
        elif left <= 3 * 86400: bits |= WARN_DAYS3
    if u["limit_bytes"] and core.current_usage(u) >= 0.8 * u["limit_bytes"]: bits |= WARN_USED80
    return bits

def send_service_warnings(now=None):
    """80% data, 3 days and 1 day left, and 'ended' - each at most once per renewal period.
    Test links have their own follow-up; auto-renewing links skip the early warnings."""
    now = int(time.time()) if now is None else now
    c = core.db()
    rows = c.execute("SELECT s.token, s.tg_id, s.warn_flags, s.auto_renew, c.notify, u.label, u.disabled_ts, u.expiry_ts, "
                     "u.limit_bytes, u.used_bytes, u.usage_reset_bytes FROM shop_services s JOIN users u ON u.token=s.token "
                     "JOIN shop_customers c ON c.tg_id=s.tg_id WHERE s.kind='paid' AND u.pending_delete=0 AND u.frozen=0 "
                     "AND c.blocked=0").fetchall()
    c.close()
    for r in rows:
        new = due_warnings(r, now) & ~r["warn_flags"]
        if not new: continue
        c = core.db(); c.execute("UPDATE shop_services SET warn_flags=warn_flags|? WHERE token=?", (new, r["token"])); c.commit(); c.close()
        label = html.escape(r["label"])
        if new & WARN_ENDED:
            text = "⏸ سرویس «%s» تموم شد و غیرفعال شد.\nتا %s ساعت دیگه با تمدید، همین لینک دوباره فعال می‌شه." % (
                label, fa(core.GRACE_SECONDS // 3600))
        elif r["auto_renew"] or not r["notify"]:
            continue
        elif new & WARN_DAYS1:
            text = "⏳ فقط کمتر از ۱ روز از سرویس «%s» مونده." % label
        elif new & WARN_DAYS3:
            text = "⏳ کمتر از ۳ روز از سرویس «%s» مونده." % label
        else:
            text = "📊 ۸۰٪ حجم سرویس «%s» مصرف شده." % label
        core.send(r["tg_id"], text, [[{"text": "➕ تمدید", "callback_data": "c:renew:%s" % r["token"]},
                                      {"text": "📦 جزئیات", "callback_data": "c:svc:%s" % r["token"]}]])

# --- auto-renew from the wallet ---
AUTO_BEFORE, AUTO_USED = 3600, 0.98   # renew when <1h is left, 98% of the data is used, or the link was disabled
AUTO_NOTE_EVERY = 24 * 3600           # remind about a low balance at most once a day

AUTO_GAP = 3600   # never auto-renew the same link twice within an hour, whatever the counters say

def auto_renew_due(u, now):
    """Due when the link is about to end. 'disabled' on its own is not a reason: a link that was
    just renewed stays disabled until xray re-adds it, and charging again for that would loop."""
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
                     "JOIN shop_customers c ON c.tg_id=s.tg_id WHERE s.auto_renew=1 AND s.kind='paid' AND c.blocked=0 "
                     "AND u.pending_delete=0 AND u.frozen=0 AND NOT EXISTS (SELECT 1 FROM shop_orders o WHERE o.token=s.token "
                     "AND o.kind='renew' AND o.status='approved' AND o.created_ts>?)", (now - AUTO_GAP,)).fetchall()
    c.close()
    for r in rows:
        try:
            if not auto_renew_due(r, now): continue
            renew_btn = [{"text": "🔄 تمدید دستی", "callback_data": "c:renew:%s" % r["token"]}]
            plan = get_plan(r["plan_id"]) if r["plan_id"] else None
            if not plan or not plan["active"]:
                _auto_note(r, now, "🔁 تمدید خودکار «%s» انجام نشد: پلن قبلی دیگه فروخته نمی‌شه.\nیه پلن جدید انتخاب کن 👇"
                           % html.escape(r["label"]), [renew_btn]); continue
            status, o, tok = wallet_buy(r["tg_id"], plan, r["token"], kind="auto")
            if status == "funds":
                short = max(TOPUP_MIN, -(-(plan["price"] - balance(r["tg_id"])) // 1000) * 1000)
                _auto_note(r, now, "🔁 سرویس «%s» داره تموم می‌شه ولی موجودی کیف پولت برای تمدید خودکار کافی نیست.\n"
                           "مبلغ تمدید: %s · موجودی: %s" % (html.escape(r["label"]), toman(plan["price"]), toman(balance(r["tg_id"]))),
                           [[{"text": "➕ شارژ %s" % toman(short), "callback_data": "c:topup_amt:%d" % short}], renew_btn])
            elif status == "failed":
                _auto_note(r, now, "🔁 تمدید خودکار «%s» الان انجام نشد؛ مبلغ به کیف پولت برگشت و دوباره تلاش می‌کنیم."
                           % html.escape(r["label"]), [renew_btn])
            elif status == "gone":
                c = core.db(); c.execute("UPDATE shop_services SET auto_renew=0 WHERE token=?", (r["token"],)); c.commit(); c.close()
        except Exception as e:
            print("shop auto-renew err", r["token"], e, flush=True)

TICKET_CATS = [("down", "🔴 کلاً وصل نمی‌شه"), ("slow", "🐢 سرعتش کمه"), ("carrier", "📶 فقط روی یه اینترنت مشکل داره"),
               ("pay", "💳 مشکل پرداخت"), ("other", "💬 سؤال یا موارد دیگه")]
CARRIERS = [("mci", "همراه اول"), ("irancell", "ایرانسل"), ("rightel", "رایتل"), ("fixed", "اینترنت خانگی"), ("all", "همه")]
NEEDS_CARRIER = ("down", "slow", "carrier")
CAT_NAMES, CARRIER_NAMES = dict(TICKET_CATS), dict(CARRIERS)
MAX_OPEN_TICKETS = 3

QUICK_FIX = ("🛠 <b>گزارش اختلال</b>\n\nقبل از ثبت گزارش، این‌ها رو امتحان کن؛ بیشتر مشکل‌ها همین‌جا حل می‌شن:\n\n"
             "۱. توی برنامه، اشتراک رو <b>آپدیت</b> کن.\n"
             "۲. یه کانفیگ دیگه از لیست رو انتخاب کن.\n"
             "۳. حالت هواپیما رو یه بار روشن و خاموش کن.\n"
             "۴. اگه روی یه اینترنت وصل نمی‌شی، اون یکی رو امتحان کن (مثلاً وای‌فای ↔ دیتا).")

def report_start(chat, uid, mid=None):
    set_state(uid, None)
    kb = [[{"text": "✅ درست شد", "callback_data": "c:rep_ok"}, {"text": "📝 هنوز مشکل دارم", "callback_data": "c:rep_go"}]]
    if mid: core.edit(chat, mid, QUICK_FIX, kb)
    else: core.send(chat, QUICK_FIX, kb)

def report_pick_service(chat, mid, uid):
    c = core.db()
    n = c.execute("SELECT COUNT(*) FROM shop_tickets WHERE tg_id=? AND status!='closed'", (uid,)).fetchone()[0]; c.close()
    if n >= MAX_OPEN_TICKETS:
        core.edit(chat, mid, "📝 الان %s گزارش باز داری؛ به‌زودی جواب می‌گیری 🙏" % fa(n)); return
    rows = my_services(uid)
    kb = [[{"text": "%s %s" % ("⏸" if u["disabled_ts"] else "🟢", u["label"]), "callback_data": "c:rs:%s" % u["token"]}] for u in rows]
    kb.append([{"text": "💬 سؤال عمومی / قبل از خرید", "callback_data": "c:rs:-"}])
    core.edit(chat, mid, "📝 مشکل مربوط به کدوم سرویسه؟", kb)

def report_pick_cat(chat, mid, uid, token):
    set_state(uid, {"stage": "tk_cat", "token": token})
    u = owned_service(uid, token) if token else None
    if u and u["disabled_ts"]:
        core.edit(chat, mid, "⏸ سرویس «%s» تموم شده و برای همین وصل نمی‌شه.\nبا تمدید، همین لینک دوباره فعال می‌شه." % html.escape(u["label"]),
                  [[{"text": "➕ تمدید", "callback_data": "c:renew:%s" % token}],
                   [{"text": "📝 مشکل دیگه‌ای دارم", "callback_data": "c:rcats"}]]); return
    core.edit(chat, mid, "📝 مشکل چیه؟", [[{"text": t, "callback_data": "c:rc:%s" % k}] for k, t in TICKET_CATS])

def report_text_prompt(chat, mid, uid, st):
    st["stage"] = "tk_text"; set_state(uid, st)
    core.edit(chat, mid, "✍️ مشکلت رو کوتاه بنویس (اگه اسکرین‌شات داری، همون رو بفرست).",
              [[{"text": "انصراف", "callback_data": "c:rep_cancel"}]])

def service_summary(token):
    c = core.db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if not u: return "سرویس حذف شده (<code>%s</code>)" % token
    return "%s %s · %s/%s · %s · <code>%s</code>" % (
        "⏸" if u["disabled_ts"] else "🟢", html.escape(u["label"]), fa_bytes(core.current_usage(u)),
        fa_bytes(u["limit_bytes"]) if u["limit_bytes"] else "∞", fa_left(u["expiry_ts"]), token)

def get_ticket(tid):
    c = core.db(); r = c.execute("SELECT * FROM shop_tickets WHERE id=?", (tid,)).fetchone(); c.close(); return r

CARD_MSG_CHARS, CARD_MAX_CHARS = 500, 3500   # Telegram rejects >4096 chars; a dropped card = a ticket nobody sees

def clip(text, n):
    text = text or ""
    return text if len(text) <= n else text[:n - 1] + "…"

def ticket_card(t, history=3):
    cust = get_customer(t["tg_id"])
    c = core.db()
    msgs = c.execute("SELECT * FROM shop_ticket_msgs WHERE ticket_id=? ORDER BY id DESC LIMIT ?", (t["id"], history)).fetchall()
    c.close()
    status = {"open": "🟠 منتظر پاسخ", "answered": "🔵 پاسخ داده شد", "closed": "⚪️ بسته"}[t["status"]]
    lines = ["🎫 <b>تیکت #%d</b> · %s" % (t["id"], status),
             "📝 %s%s" % (CAT_NAMES.get(t["category"], t["category"]), (" · 📶 " + CARRIER_NAMES.get(t["carrier"], "")) if t["carrier"] else ""),
             "👤 %s · <code>%d</code>" % (html.escape(cust["first_name"] or "-") if cust else "-", t["tg_id"]),
             "📦 %s" % (service_summary(t["token"]) if t["token"] else "سؤال عمومی"), ""]
    for m in reversed(msgs):
        who = "👤" if m["role"] == "customer" else "🎧"
        lines.append("%s %s%s" % (who, html.escape(clip(m["text"], CARD_MSG_CHARS)), " 📎" if m["file"] else ""))
    if t["rating"] is not None: lines += ["", "رضایت: %s" % ("👍" if t["rating"] else "👎")]
    text = "\n".join(lines)
    while len(text) > CARD_MAX_CHARS and len(lines) > 6:   # drop the oldest message until it fits
        del lines[5]; text = "\n".join(lines)
    return text

CANNED = [("🔄 آپدیت اشتراک", "لطفاً توی برنامه اشتراک رو آپدیت کن و یه کانفیگ دیگه رو امتحان کن 🙏"),
          ("✅ برطرف شد", "مشکل از سمت ما بود و برطرف شد ✅ اشتراک رو آپدیت کن و دوباره وصل شو."),
          ("🔍 در حال بررسی", "داریم بررسی می‌کنیم؛ به محض نتیجه خبرت می‌کنیم 🙏"),
          ("🧾 شمارهٔ پیگیری", "لطفاً شمارهٔ پیگیری یا عکس رسید واریز رو بفرست تا بررسی کنیم.")]

def ticket_kb(t):
    if t["status"] == "closed": return [[{"text": "🔓 باز کردن دوباره", "callback_data": "t:o:%d" % t["id"]}]]
    return ([[{"text": "✍️ پاسخ", "callback_data": "t:r:%d" % t["id"]}, {"text": "✅ بستن", "callback_data": "t:x:%d" % t["id"]}]] +
            [[{"text": CANNED[i][0], "callback_data": "t:c:%d:%d" % (t["id"], i)},
              {"text": CANNED[i + 1][0], "callback_data": "t:c:%d:%d" % (t["id"], i + 1)}] for i in (0, 2)])

def notify_staff_ticket(tid, file=None, head=""):
    t = get_ticket(tid)
    for chat in reviewers():
        r = core.send(chat, head + ticket_card(t), ticket_kb(t))
        if not (r or {}).get("ok"):   # never lose a ticket silently: fall back to the bare header
            core.send(chat, head + ticket_card(t, history=0), ticket_kb(t))
        if file: send_file(chat, file, "📎 پیوست تیکت #%d" % tid)

def send_file(chat, file, caption):
    kind, fid = file.split(":", 1)
    core.tg("sendPhoto" if kind == "photo" else "sendDocument", chat_id=chat,
            **{("photo" if kind == "photo" else "document"): fid}, caption=caption)

MSG_CHARS, MAX_TICKET_MSGS, MAX_DAILY_MSGS = 1500, 40, 30   # per message, per ticket, per customer per day

def add_ticket_msg(c, tid, sender, role, text, file=None):
    now = int(time.time())
    c.execute("INSERT INTO shop_ticket_msgs(ticket_id,sender,role,text,file,ts) VALUES(?,?,?,?,?,?)",
              (tid, sender, role, clip(text, MSG_CHARS), file, now))
    c.execute("UPDATE shop_tickets SET status=?,updated_ts=? WHERE id=?",
              ("answered" if role == "staff" else "open", now, tid))

def customer_msg_quota(c, uid, tid, now):
    """'ok' | 'ticket' (this ticket is full) | 'day' (too many messages today); keeps the DB small on a 512MB box."""
    if tid and c.execute("SELECT COUNT(*) FROM shop_ticket_msgs WHERE ticket_id=? AND role='customer'", (tid,)).fetchone()[0] >= MAX_TICKET_MSGS:
        return "ticket"
    if c.execute("SELECT COUNT(*) FROM shop_ticket_msgs WHERE sender=? AND role='customer' AND ts>?", (uid, now - 86400)).fetchone()[0] >= MAX_DAILY_MSGS:
        return "day"
    return "ok"

def create_ticket(chat, uid, st, text, file):
    now = int(time.time())
    c = core.db()
    try:
        c.execute("BEGIN IMMEDIATE")   # the open-ticket cap is enforced here, where the row is written
        n = c.execute("SELECT COUNT(*) FROM shop_tickets WHERE tg_id=? AND status!='closed'", (uid,)).fetchone()[0]
        if n >= MAX_OPEN_TICKETS or customer_msg_quota(c, uid, None, now) != "ok":
            c.rollback(); set_state(uid, None)
            say(chat, "📝 الان %s گزارش باز داری؛ به‌زودی جواب می‌گیری 🙏" % fa(n) if n >= MAX_OPEN_TICKETS else
                "⏳ امروز پیام‌های زیادی فرستادی؛ فردا دوباره امتحان کن 🙏"); return
        tid = c.execute("INSERT INTO shop_tickets(tg_id,token,category,carrier,status,created_ts,updated_ts) VALUES(?,?,?,?,'open',?,?)",
                        (uid, st.get("token"), st["cat"], st.get("carrier"), now, now)).lastrowid
        add_ticket_msg(c, tid, uid, "customer", text, file); c.commit()
    finally:
        c.close()
    set_state(uid, None)
    say(chat, "✅ گزارشت ثبت شد (تیکت #%s).\nبه‌زودی همین‌جا جواب می‌گیری 🙏" % fa(tid))
    notify_staff_ticket(tid, file)
    carrier_alert(st.get("carrier"), st["cat"], now)

CARRIER_ALERT_N, CARRIER_ALERT_WINDOW, CARRIER_ALERT_EVERY = 3, 1800, 3600

def carrier_alert(carrier, cat, now):
    """Several reports from one carrier in a short time usually means an outage, not a customer issue."""
    if cat not in NEEDS_CARRIER or not carrier or carrier == "all": return
    c = core.db()
    n = c.execute("SELECT COUNT(DISTINCT tg_id) FROM shop_tickets WHERE carrier=? AND category IN ('down','slow','carrier') "
                  "AND created_ts>=?", (carrier, now - CARRIER_ALERT_WINDOW)).fetchone()[0]
    c.close()
    if n < CARRIER_ALERT_N or now - int(setting("carrier_alert_" + carrier, 0)) < CARRIER_ALERT_EVERY: return
    set_setting("carrier_alert_" + carrier, now)
    for a in owner_ids():
        core.send(a, "📶 <b>%s گزارش از %s</b> در نیم ساعت اخیر؛ احتمالاً اختلال عمومی است." % (fa(n), CARRIER_NAMES[carrier]),
                  [[{"text": "📢 اعلان اختلال", "callback_data": "sa:outage"}]])

def customer_ticket_input(chat, uid, m, st):
    """Text or screenshot while a ticket step waits for it. Returns True when consumed."""
    if st.get("stage") not in ("tk_text", "tk_reply"): return False
    text = (m.get("text") or m.get("caption") or "").strip()
    rf = receipt_file(m); file = rf[0] if rf else None
    if not text and not file:
        say(chat, "یه توضیح کوتاه یا اسکرین‌شات بفرست:"); return True
    if st["stage"] == "tk_text":
        create_ticket(chat, uid, st, text, file); return True
    t = get_ticket(st["id"])
    if not t or t["tg_id"] != uid:
        set_state(uid, None); return False
    if t["status"] == "closed":   # only staff reopen a ticket; a stale state must not do it
        set_state(uid, None)
        say(chat, "این تیکت بسته شده. اگه هنوز مشکل داری، از «🛠 گزارش اختلال» یه گزارش جدید ثبت کن."); return True
    now = int(time.time())
    c = core.db(); q = customer_msg_quota(c, uid, t["id"], now)
    if q != "ok":
        c.close(); set_state(uid, None)
        say(chat, "این تیکت پر شده؛ منتظر پاسخ پشتیبانی بمون 🙏" if q == "ticket" else "⏳ امروز پیام‌های زیادی فرستادی؛ فردا دوباره امتحان کن 🙏")
        return True
    add_ticket_msg(c, t["id"], uid, "customer", text, file); c.commit(); c.close()
    set_state(uid, None)
    say(chat, "✅ پیامت به تیکت #%s اضافه شد." % fa(t["id"]))
    notify_staff_ticket(t["id"], file, "💬 پیام جدید مشتری\n")
    return True

def customer_ticket_kb(tid):
    return [[{"text": "✍️ جواب دادن", "callback_data": "c:tk:%d" % tid},
             {"text": "✅ مشکلم حل شد", "callback_data": "c:tkok:%d" % tid}]]

def close_ticket(tid):
    c = core.db()
    n = c.execute("UPDATE shop_tickets SET status='closed',updated_ts=? WHERE id=? AND status!='closed'", (int(time.time()), tid)).rowcount
    c.commit(); c.close(); return n

ticket_pending = {}   # staff uid -> ticket id awaiting a typed reply; staff only, tiny

def staff_reply(uid, tid, text):
    """Returns True when the customer got the reply, False otherwise (closed ticket, blocked bot)."""
    t = get_ticket(tid)
    if not t or t["status"] == "closed": return False
    text = clip(text, MSG_CHARS)
    r = core.send(t["tg_id"], "🎧 <b>پاسخ پشتیبانی</b> · تیکت #%s\n\n%s" % (fa(tid), html.escape(text)), customer_ticket_kb(tid))
    if not (r or {}).get("ok"): return False
    c = core.db(); add_ticket_msg(c, tid, uid, "staff", text); c.commit(); c.close()
    log(uid, "ticket_reply", tid)
    return True

def ticket_callback(chat, mid, uid, data, cbid):
    """Staff buttons on tickets: t:r (reply) t:c (canned) t:x (close) t:o (reopen) t:v (view)."""
    parts = data.split(":")
    tid = _int(parts[2]) if len(parts) >= 3 else None
    if tid is None: core.answer(cbid); return
    action = parts[1]; t = get_ticket(tid)
    if not t: core.answer(cbid, "تیکت پیدا نشد"); return
    if action == "v":
        core.answer(cbid); core.send(chat, ticket_card(t, history=10), ticket_kb(t)); return
    if action == "o":
        c = core.db(); c.execute("UPDATE shop_tickets SET status='open' WHERE id=?", (tid,)); c.commit(); c.close()
        log(uid, "ticket_reopen", tid); core.answer(cbid, "دوباره باز شد")
        t = get_ticket(tid); core.edit(chat, mid, ticket_card(t), ticket_kb(t)); return
    if t["status"] == "closed": core.answer(cbid, "این تیکت بسته شده"); return
    if action == "r":
        ticket_pending[uid] = {"id": tid, "ts": time.time()}; core.answer(cbid)
        core.send(chat, "✍️ پاسخ تیکت #%d را بنویس (تا %s دقیقه؛ برای انصراف یکی از دکمه‌های منو را بزن):"
                  % (tid, fa(PENDING_TTL // 60))); return
    if action == "c" and len(parts) == 4 and _int(parts[3]) is not None and int(parts[3]) < len(CANNED):
        ok = staff_reply(uid, tid, CANNED[int(parts[3])][1]); core.answer(cbid, "فرستاده شد" if ok else "❌ به مشتری نرسید")
        t = get_ticket(tid); core.edit(chat, mid, ticket_card(t), ticket_kb(t)); return
    if action == "x":
        if close_ticket(tid):
            log(uid, "ticket_close", tid)
            core.send(t["tg_id"], "✅ تیکت #%s بسته شد.\nمشکلت حل شد؟" % fa(tid),
                      [[{"text": "👍 آره، ممنون", "callback_data": "c:tkr:%d:1" % tid},
                        {"text": "👎 نه هنوز", "callback_data": "c:tkr:%d:0" % tid}]])
        core.answer(cbid, "بسته شد"); t = get_ticket(tid); core.edit(chat, mid, ticket_card(t), ticket_kb(t)); return
    core.answer(cbid)

def ticket_text(chat, uid, text):
    """A staff member's typed reply. Returns True when consumed."""
    st = _pending_get(ticket_pending, uid)
    if not st: return False
    tid = st["id"]
    if not text.strip(): core.send(chat, "متن پاسخ را بنویس:"); return True
    ticket_pending.pop(uid, None)
    if staff_reply(uid, tid, text.strip()):
        core.send(chat, "✅ پاسخ برای مشتری فرستاده شد.\n\n" + ticket_card(get_ticket(tid)), ticket_kb(get_ticket(tid)))
    else:
        core.send(chat, "❌ پاسخ به مشتری نرسید (تیکت بسته است یا ربات را بلاک کرده).")
    return True

def tickets_view(status_filter=("open", "answered")):
    c = core.db()
    rows = c.execute("SELECT t.*, c.first_name FROM shop_tickets t LEFT JOIN shop_customers c ON c.tg_id=t.tg_id "
                     "WHERE t.status IN (%s) ORDER BY t.status='answered', t.updated_ts LIMIT 30"
                     % ",".join("?" * len(status_filter)), status_filter).fetchall()
    c.close()
    if not rows: return "🎫 تیکت بازی نیست ✨", None
    return ("🎫 <b>تیکت‌های باز</b> (%s)\n🟠 منتظر پاسخ ما · 🔵 منتظر مشتری" % fa(len(rows)),
            [[{"text": "%s #%d · %s · %s" % ("🟠" if r["status"] == "open" else "🔵", r["id"], r["first_name"] or r["tg_id"],
                                              CAT_NAMES.get(r["category"], "")), "callback_data": "t:v:%d" % r["id"]}] for r in rows])

def customer_ticket_callback(chat, mid, uid, data, cbid):
    """c:rep* / c:rs / c:rc / c:rk / c:tk* buttons. Returns True when handled."""
    parts = data.split(":")
    if data == "c:report":
        core.answer(cbid); report_start(chat, uid); return True
    if data == "c:rep_ok":
        core.answer(cbid); set_state(uid, None); core.edit(chat, mid, "😍 عالیه! اگه دوباره مشکلی بود همین‌جاییم."); return True
    if data == "c:rep_cancel":
        core.answer(cbid); set_state(uid, None); core.edit(chat, mid, "گزارش لغو شد."); return True
    if data == "c:rep_go":
        core.answer(cbid); report_pick_service(chat, mid, uid); return True
    if parts[:2] == ["c", "rs"] and len(parts) == 3:
        core.answer(cbid)
        token = None if parts[2] == "-" else parts[2]
        if token and not owned_service(uid, token): core.edit(chat, mid, "این سرویس پیدا نشد."); return True
        report_pick_cat(chat, mid, uid, token); return True
    st = get_state(uid) or {}
    if data == "c:rcats" and st.get("stage") == "tk_cat":
        core.answer(cbid)
        core.edit(chat, mid, "📝 مشکل چیه؟", [[{"text": t, "callback_data": "c:rc:%s" % k}] for k, t in TICKET_CATS]); return True
    if parts[:2] == ["c", "rc"] and len(parts) == 3 and parts[2] in CAT_NAMES:
        core.answer(cbid)
        if st.get("stage") != "tk_cat": report_start(chat, uid, mid); return True
        st["cat"] = parts[2]
        if parts[2] in NEEDS_CARRIER:
            st["stage"] = "tk_car"; set_state(uid, st)
            core.edit(chat, mid, "📶 با کدوم اینترنت مشکل داری؟",
                      [[{"text": t, "callback_data": "c:rk:%s" % k} for k, t in CARRIERS[i:i + 3]] for i in (0, 3)]); return True
        report_text_prompt(chat, mid, uid, st); return True
    if parts[:2] == ["c", "rk"] and len(parts) == 3 and parts[2] in CARRIER_NAMES:
        core.answer(cbid)
        if st.get("stage") != "tk_car": report_start(chat, uid, mid); return True
        st["carrier"] = parts[2]; report_text_prompt(chat, mid, uid, st); return True
    if parts[:2] in (["c", "tk"], ["c", "tkok"]) and len(parts) == 3 and _int(parts[2]) is not None or \
            (parts[:2] == ["c", "tkr"] and len(parts) == 4 and _int(parts[2]) is not None and parts[3] in ("0", "1")):
        t = get_ticket(int(parts[2]))
        if not t or t["tg_id"] != uid: core.answer(cbid, "تیکت پیدا نشد"); return True
        if parts[1] == "tk":
            if t["status"] == "closed": core.answer(cbid, "این تیکت بسته شده؛ اگه مشکل داری گزارش جدید ثبت کن"); return True
            core.answer(cbid); set_state(uid, {"stage": "tk_reply", "id": t["id"]})
            core.send(chat, "✍️ پیامت برای تیکت #%s رو بنویس:" % fa(t["id"])); return True
        if parts[1] == "tkok":
            core.answer(cbid, "ممنون! 🙏")
            if close_ticket(t["id"]):
                c = core.db(); c.execute("UPDATE shop_tickets SET rating=1 WHERE id=?", (t["id"],)); c.commit(); c.close()
                core.edit(chat, mid, "✅ خوشحالیم که مشکلت حل شد! تیکت #%s بسته شد." % fa(t["id"]))
                for a in reviewers(): core.send(a, "✅ مشتری تیکت #%d را بست (حل شد)." % t["id"])
            return True
        good = parts[3] == "1"
        if t["status"] != "closed": core.answer(cbid); return True   # rating is for closed tickets only
        c = core.db()
        n = c.execute("UPDATE shop_tickets SET rating=? WHERE id=? AND rating IS NULL", (1 if good else 0, t["id"])).rowcount
        if n and not good: c.execute("UPDATE shop_tickets SET status='open' WHERE id=?", (t["id"],))
        c.commit(); c.close()
        core.answer(cbid)
        if good: core.edit(chat, mid, "😍 ممنون از بازخوردت!"); return True
        if not n: core.edit(chat, mid, "نظرت قبلاً ثبت شده بود."); return True
        core.edit(chat, mid, "😔 متأسفیم. تیکت دوباره باز شد؛ اگه توضیحی داری همین الان بنویس.")
        if n:
            set_state(uid, {"stage": "tk_reply", "id": t["id"]})
            notify_staff_ticket(t["id"], head="👎 مشتری از نتیجه راضی نبود؛ تیکت دوباره باز شد\n")
        return True
    return False

# --- account ---
def account_text(uid):
    cust = get_customer(uid)
    c = core.db()
    paid = c.execute("SELECT COUNT(*) FROM shop_orders WHERE tg_id=? AND status='approved' "
                     "AND delivered_ts IS NOT NULL AND kind IN ('buy','renew')", (uid,)).fetchone()[0]
    c.close()
    return ("👤 <b>حساب من</b>\n\n🙂 %s\n🆔 آیدی عددی: <code>%d</code>\n📅 عضویت: %s\n💰 موجودی: %s\n🛍 تعداد خرید: %s\n🎁 تست رایگان: %s"
            % (html.escape(cust["first_name"] or "-"), uid, fa_date(cust["joined_ts"]), toman(cust["wallet"]), fa(paid),
               ("گرفته‌شده" if cust["test_used_ts"] else "هنوز نگرفتی")))

def account_kb(uid):
    on = get_customer(uid)["notify"]
    return [[{"text": "🔔 اعلان‌های حجم و زمان: %s" % ("روشن ✅" if on else "خاموش"), "callback_data": "c:notify"}]]

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
    menu_tap = text.startswith("/start") or text in {b for row in CUSTOMER_MENU for b in row}
    rf = receipt_file(m)
    # An image while an invoice is open is a receipt - even if a ticket prompt is still pending.
    if rf and not menu_tap and has_open_invoice(uid): receive_receipt(chat, uid, *rf); return
    if not menu_tap and customer_ticket_input(chat, uid, m, get_state(uid) or {}): return
    if rf: receive_receipt(chat, uid, *rf); return
    if text.startswith("/start"):
        set_state(uid, None)
        customer_menu(chat, welcome_text(m["from"].get("first_name"))); return
    if text in {b for row in CUSTOMER_MENU for b in row}:
        set_state(uid, None)
        if text == B_BUY: send_plans(chat, uid); return
        if text == B_WALLET: core.send(chat, *wallet_view(uid)); return
        if text == B_SERVICES: core.send(chat, *services_view(uid)); return
        if text == B_INVITE: core.send(chat, *invite_view(uid)); return
        if text == B_REPORT: report_start(chat, uid); return
        if text == B_TEST: test_offer(chat, uid); return
        if text == B_ACCOUNT: core.send(chat, account_text(uid), account_kb(uid)); return
        if text == B_GUIDE: core.send(chat, GUIDE_TEXT, guide_kb()); return
        if text == B_RULES: core.send(chat, RULES_TEXT); return
        say(chat, SOON_TEXT); return
    st = get_state(uid) or {}
    if st.get("stage") == "topup_amount":
        n = parse_amount(text)
        if not n:
            core.send(chat, "مبلغ رو به تومان بفرست، مثلاً <code>300000</code>:"); return
        set_state(uid, None); start_topup(chat, uid, n); return
    customer_menu(chat)

def customer_callback(chat, mid, uid, data, cbid, user=None):
    if data == "c:rules":
        core.answer(cbid); core.send(chat, RULES_TEXT); return
    config_parts = data.split(":")
    if config_parts[:2] == ["c", "configs"] and len(config_parts) == 4 and _int(config_parts[3]) is not None:
        core.answer(cbid); send_configs(chat, uid, config_parts[2], int(config_parts[3])); return
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
    if parts[:2] == ["c", "rcpt"] and len(parts) == 3 and _int(parts[2]) is not None:
        st = get_state(uid) or {}
        core.answer(cbid)
        if st.get("stage") != "rcpt_pick": core.edit(chat, mid, "این انتخاب دیگه معتبر نیست؛ عکس رسید رو دوباره بفرست."); return
        core.tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
        receive_receipt(chat, uid, st["file"], st["uid"], oid=int(parts[2])); return
    if len(parts) > 1 and parts[1] in ("buy", "renew", "plans", "plan", "pay", "wpay", "topup", "topup_custom", "topup_amt"):
        st = get_state(uid) or {}
        if str(st.get("stage", "")).startswith(("tk_", "rcpt")): set_state(uid, None)   # leaving the ticket flow
    if data == "c:buy":
        core.answer(cbid); send_plans(chat, uid); return
    if parts[:2] == ["c", "renew"] and len(parts) == 3:
        core.answer(cbid)
        if owned_service(uid, parts[2]): send_plans(chat, uid, parts[2])
        else: core.send(chat, "این سرویس دیگه وجود نداره؛ می‌تونی یه سرویس جدید بخری.", [[{"text": "🛒 خرید سرویس", "callback_data": "c:buy"}]])
        return
    if parts[:2] == ["c", "plans"]:
        core.answer(cbid); send_plans(chat, uid, parts[2] if len(parts) == 3 else None, mid); return
    if parts[:2] in (["c", "plan"], ["c", "pay"], ["c", "wpay"]) and len(parts) in (3, 4) and _int(parts[2]) is not None:
        core.answer(cbid)
        if parts[1] == "pay": start_payment(chat, mid, uid, int(parts[2]), token); return
        if parts[1] == "wpay": wallet_pay(chat, mid, uid, int(parts[2]), token); return
        plan = get_plan(int(parts[2]))
        if not plan or not plan["active"]: send_plans(chat, uid, token, mid); return
        text, kb = invoice_view(priced(uid, plan), token, balance(uid)); core.edit(chat, mid, text, kb); return
    if parts[:2] == ["c", "cancel"] and len(parts) == 3 and _int(parts[2]) is not None:
        core.answer(cbid); cancel_order(chat, mid, uid, int(parts[2])); return
    if data == "c:wallet":
        core.answer(cbid); core.edit(chat, mid, *wallet_view(uid)); return
    if data == "c:topup":
        core.answer(cbid); core.edit(chat, mid, *topup_view()); return
    if data == "c:topup_custom":
        core.answer(cbid); set_state(uid, {"stage": "topup_amount"})
        core.edit(chat, mid, "✏️ مبلغ شارژ رو به تومان بفرست (حداقل %s):" % toman(TOPUP_MIN),
                  [[{"text": "انصراف", "callback_data": "c:wallet"}]]); return
    if parts[:2] == ["c", "topup_amt"] and len(parts) == 3 and _int(parts[2]) is not None:
        core.answer(cbid); start_topup(chat, uid, int(parts[2]), mid); return
    if data == "c:tx":
        core.answer(cbid); lines = tx_lines(uid, 30)
        core.edit(chat, mid, "📜 <b>تراکنش‌ها</b> (آخرین ۳۰ مورد)\n\n" + ("\n".join(lines) or "هنوز تراکنشی نداری."),
                  [[{"text": "بازگشت", "callback_data": "c:wallet"}]]); return
    if data == "c:ref_msg":
        core.answer(cbid); core.send(chat, "%s\n%s" % (share_text(), invite_link(uid))); return
    if data == "c:svcs":
        core.answer(cbid); core.edit(chat, mid, *services_view(uid)); return
    if parts[:2] == ["c", "svc"] and len(parts) == 3:
        core.answer(cbid, "بروز شد")   # counters come from the enforcer's own refresh (every POLL seconds); no xray call here
        core.edit(chat, mid, *service_view(uid, parts[2])); return
    if data == "c:notify":
        c = core.db(); c.execute("UPDATE shop_customers SET notify=1-notify WHERE tg_id=?", (uid,)); c.commit(); c.close()
        core.answer(cbid, "اعلان‌ها روشن شد" if get_customer(uid)["notify"] else "اعلان‌ها خاموش شد")
        core.edit(chat, mid, account_text(uid), account_kb(uid)); return
    if parts[:2] == ["c", "auto"] and len(parts) == 3:
        if not owned_service(uid, parts[2]): core.answer(cbid, "این سرویس پیدا نشد"); return
        c = core.db()
        c.execute("UPDATE shop_services SET auto_renew=1-auto_renew,auto_note_ts=0 WHERE token=? AND tg_id=? AND kind='paid'", (parts[2], uid))
        c.commit(); on = c.execute("SELECT auto_renew FROM shop_services WHERE token=?", (parts[2],)).fetchone()[0]; c.close()
        core.answer(cbid, "تمدید خودکار روشن شد؛ موقع تموم شدن، از کیف پولت تمدید می‌شه" if on else "تمدید خودکار خاموش شد")
        core.tg("editMessageReplyMarkup", chat_id=chat, message_id=mid,
                reply_markup={"inline_keyboard": service_kb(parts[2])}); return
    if (data.startswith(("c:rep", "c:rs:", "c:rc", "c:rk:", "c:tk")) and
            customer_ticket_callback(chat, mid, uid, data, cbid)): return
    core.answer(cbid)

# ---------------- support staff UI ----------------
B_RECEIPTS, B_TICKETS = "🧾 رسیدها", "🎫 تیکت‌ها"
SUPPORT_MENU = [[B_RECEIPTS, B_TICKETS]]

def support_message(chat, uid, m):
    text = (m.get("text") or "").strip()
    if text == B_RECEIPTS:
        clear_admin_state(chat, uid); t, kb = queue_view(); core.send(chat, t, kb); return
    if text == B_TICKETS:
        clear_admin_state(chat, uid); t, kb = tickets_view(); core.send(chat, t, kb); return
    if text.startswith("/"): clear_admin_state(chat, uid)
    elif review_text(chat, uid, text): return
    elif ticket_text(chat, uid, text): return
    say(chat, "🎧 <b>پنل پشتیبانی</b>\nاز منوی پایین یکی رو انتخاب کن 👇", reply_kb(SUPPORT_MENU))

def support_callback(chat, mid, uid, data, cbid):
    if data.startswith("r:"): review_callback(chat, mid, uid, data, cbid); return
    if data.startswith("t:"): ticket_callback(chat, mid, uid, data, cbid); return
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
    if flag("maintenance"): say(chat, MAINTENANCE_TEXT); return
    is_new = get_customer(uid) is None
    cust = touch_customer(m["from"], start_referrer(text) if text.startswith("/start") else None)
    if cust["blocked"]: say(chat, BLOCKED_TEXT, {"remove_keyboard": True}); return
    if is_new and cust["referrer_id"]:
        core.send(cust["referrer_id"], "👋 %s با لینک دعوت تو عضو شد! بعد از اولین خریدش پاداشت می‌رسه 🎁"
                  % html.escape(cust["first_name"] or "یه نفر"))
    customer_message(chat, uid, m)

# ---------------- admin side ----------------
B_LINKS, B_SHOP, B_WEB = "🔗 لینک‌ها", "🛍 فروشگاه", "🌐 پنل وب"
ADMIN_MENU = [[B_LINKS, B_SHOP], [B_RECEIPTS, B_TICKETS], [B_WEB]]
ADMIN_BUTTONS = {b for row in ADMIN_MENU for b in row}

def admin_start(chat):
    say(chat, "👋 سلام ادمین\n🔗 لینک‌ها = همان پنل قبلی (ساخت، لیست، آدرس‌های CDN) · 🛍 فروشگاه = پلن‌ها، کارت‌ها، مشتری‌ها\n"
        "🌐 پنل وب = لینک ورود به پنل مدیریت (همان /admin)", reply_kb(ADMIN_MENU))
    core.send(chat, core.WELCOME, core.main_menu_kb())

def web_login(chat):
    tok = core.mint_login()
    core.send(chat, "🔐 لینک ورود به پنل (۱۰ دقیقه اعتبار، یک‌بار مصرف):\n<code>%s/a/login/%s</code>" % (core.SUB_BASE, tok))

def _count(sql, args=()):
    c = core.db(); n = c.execute(sql, args).fetchone()[0]; c.close(); return n

def shop_admin_text():
    return ("🛍 <b>مدیریت فروشگاه</b>\n\n🛍 خریداران: <b>%d</b>\n🎧 پشتیبان‌ها: <b>%d</b>\n"
            "🛠 حالت تعمیرات: <b>%s</b>\n💾 بکاپ روزانه: <b>%s</b>"
            % (_count("SELECT COUNT(DISTINCT tg_id) FROM shop_orders WHERE status='approved' "
                      "AND delivered_ts IS NOT NULL AND kind IN ('buy','renew')"), _count("SELECT COUNT(*) FROM shop_staff"),
               "روشن" if flag("maintenance") else "خاموش", "روشن" if flag("backup_auto") else "خاموش"))

def shop_admin_kb():
    return [[{"text": ("✅ خاموش کردن تعمیرات" if flag("maintenance") else "🛠 روشن کردن تعمیرات"), "callback_data": "sa:maint"}],
            [{"text": "🧾 رسیدهای در انتظار (%s)" % fa(_count("SELECT COUNT(*) FROM shop_orders WHERE status='review'")),
              "callback_data": "sa:queue"},
             {"text": "🎫 تیکت‌ها (%s)" % fa(_count("SELECT COUNT(*) FROM shop_tickets WHERE status='open'")),
              "callback_data": "sa:tickets"}],
            [{"text": "📦 پلن‌ها", "callback_data": "sa:plans"}, {"text": "💳 کارت‌ها", "callback_data": "sa:cards"}],
            [{"text": "👤 کاربران", "callback_data": "sa:users"}, {"text": "🎁 تست رایگان", "callback_data": "sa:test"}],
            [{"text": "👥 دعوت دوستان", "callback_data": "sa:ref"}],
            [{"text": "✅ اعلام رفع اختلال" if setting("outage") else "📢 اعلان اختلال", "callback_data": "sa:outage"}],
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
    tid = _forwarded_user_id(m) or _int(text)
    t = to_ascii(text).strip()
    c = core.db()
    if tid: r = c.execute("SELECT * FROM shop_customers WHERE tg_id=?", (tid,)).fetchone()
    else: r = c.execute("SELECT * FROM shop_customers WHERE lower(username)=? ORDER BY last_seen DESC",
                        (t.lstrip("@").lower(),)).fetchone()
    c.close(); return r

def user_card(tid):
    cust = get_customer(tid)
    c = core.db()
    services = c.execute("SELECT COUNT(*) FROM shop_services s JOIN users u ON u.token=s.token WHERE s.tg_id=?", (tid,)).fetchone()[0]
    bought = c.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM shop_orders WHERE tg_id=? AND status='approved' "
                       "AND delivered_ts IS NOT NULL AND kind IN ('buy','renew')", (tid,)).fetchone()
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

def run_bg(fn):
    """Broadcasts run off the update loop so the bot keeps answering (tests replace this)."""
    threading.Thread(target=fn, daemon=True).start()

def active_customers():
    c = core.db()
    ids = [r[0] for r in c.execute("SELECT DISTINCT s.tg_id FROM shop_services s JOIN users u ON u.token=s.token "
                                   "JOIN shop_customers c ON c.tg_id=s.tg_id WHERE s.kind='paid' AND u.pending_delete=0 "
                                   "AND u.disabled_ts=0 AND c.blocked=0")]
    c.close(); return ids

_bc_lock = threading.Lock()   # one broadcast at a time; start/end of an outage never interleave

def broadcast(ids, text, kb=None, pause=0.05, locked=False):
    """Returns (sent, failed). Blocked/deleted users count as failed; a 429 waits and retries once.
    One broadcast runs at a time; with locked=True the caller already holds _bc_lock and it is released here."""
    sent = failed = 0
    if not locked: _bc_lock.acquire()
    try:
        for i in ids:
            r = core.send(i, text, kb) or {}   # core.tg returns Telegram's error body, so 429 is visible here
            if not r.get("ok") and r.get("error_code") == 429:
                time.sleep(min(60, int((r.get("parameters") or {}).get("retry_after", 5)))); r = core.send(i, text, kb) or {}
            if r.get("ok"): sent += 1
            else: failed += 1
            if pause: time.sleep(pause)   # stay under Telegram's ~30 msg/s
    finally:
        _bc_lock.release()
    return sent, failed

def start_outage(uid, chat, text):
    if not _bc_lock.acquire(blocking=False): core.send(chat, "⏳ ارسال قبلی هنوز تمام نشده؛ چند لحظه بعد دوباره بزن."); return
    ids = active_customers()
    set_setting("outage", json.dumps({"ids": ids, "ts": int(time.time())})); log(uid, "outage_start", len(ids), text[:100])
    msg = "⚠️ <b>اطلاعیهٔ اختلال</b>\n\n%s\n\nداریم روش کار می‌کنیم؛ به محض رفع، همین‌جا خبرت می‌کنیم 🙏" % html.escape(text)
    def go():
        n, bad = broadcast(ids, msg, locked=True)
        core.send(chat, "📢 اعلان اختلال برای %s نفر فرستاده شد%s." % (fa(n), (" (%s نفر دریافت نکردند)" % fa(bad)) if bad else ""))
    run_bg(go)

def end_outage(uid, chat):
    if not _bc_lock.acquire(blocking=False): core.send(chat, "⏳ ارسال اعلان اختلال هنوز تمام نشده؛ چند لحظه بعد دوباره بزن."); return
    rec = json.loads(setting("outage") or "{}")
    set_setting("outage", ""); log(uid, "outage_end", len(rec.get("ids", [])))
    ids = rec.get("ids", [])
    def go():
        n, bad = broadcast(ids, "✅ <b>اختلال برطرف شد</b>\nاگه هنوز وصل نمی‌شی، توی برنامه اشتراک رو آپدیت کن 🙏",
                           [[{"text": "📚 آموزش اتصال", "callback_data": "c:guide"}]], locked=True)
        core.send(chat, "✅ پیام رفع اختلال برای %s نفر فرستاده شد%s." % (fa(n), (" (%s نفر دریافت نکردند)" % fa(bad)) if bad else ""))
    run_bg(go)

def ref_admin():
    c = core.db()
    invited = c.execute("SELECT COUNT(*) FROM shop_customers WHERE referrer_id IS NOT NULL").fetchone()[0]
    buyers = c.execute("SELECT COUNT(DISTINCT o.tg_id) FROM shop_orders o JOIN shop_customers c ON c.tg_id=o.tg_id "
                       "WHERE c.referrer_id IS NOT NULL AND o.status='approved' AND o.delivered_ts IS NOT NULL "
                       "AND o.kind IN ('buy','renew')").fetchone()[0]
    paid = c.execute("SELECT COALESCE(SUM(delta),0) FROM shop_wallet_tx WHERE kind='referral'").fetchone()[0]
    discounts = c.execute("SELECT COALESCE(SUM(discount),0) FROM shop_orders WHERE status='approved'").fetchone()[0]
    top = c.execute("SELECT c.referrer_id id, r.first_name, COUNT(*) n FROM shop_customers c LEFT JOIN shop_customers r "
                    "ON r.tg_id=c.referrer_id WHERE c.referrer_id IS NOT NULL GROUP BY c.referrer_id ORDER BY n DESC LIMIT 5").fetchall()
    c.close()
    mark = lambda on, t: ("✅ " + t) if on else t
    text = ("👥 <b>دعوت دوستان</b>\n\nپاداش دعوت‌کننده: <b>%s٪</b> اولین خرید دوستش (به کیف پول)\n"
            "تخفیف دعوت‌شده: <b>%s٪</b> روی اولین خرید\n\nدعوت‌شده‌ها: %s · خریدارها: %s\n"
            "پاداش پرداخت‌شده: %s · تخفیف داده‌شده: %s"
            % (fa(ref_reward_pct()), fa(ref_discount_pct()), fa(invited), fa(buyers), toman(paid), toman(discounts)))
    if top:
        text += "\n\n🏆 <b>برترین‌ها</b>\n" + "\n".join("%s. %s · <code>%d</code> · %s نفر" % (
            fa(i + 1), html.escape(t["first_name"] or "-"), t["id"], fa(t["n"])) for i, t in enumerate(top))
    kb = [[{"text": "پاداش دعوت‌کننده:", "callback_data": "sa:noop"}],
          [{"text": mark(v == ref_reward_pct(), fa("%d٪" % v)), "callback_data": "sa:ref_reward:%d" % v} for v in REF_REWARD_CHOICES],
          [{"text": "تخفیف دعوت‌شده:", "callback_data": "sa:noop"}],
          [{"text": mark(v == ref_discount_pct(), fa("%d٪" % v)), "callback_data": "sa:ref_discount:%d" % v} for v in REF_DISCOUNT_CHOICES],
          [{"text": "بازگشت", "callback_data": "sa:menu"}]]
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
    if data.startswith("t:"):
        ticket_callback(chat, mid, uid, data, cbid); return
    review_pending.pop(uid, None); ticket_pending.pop(uid, None)
    if data == "sa:tickets":
        core.answer(cbid); t, kb = tickets_view()
        core.edit(chat, mid, t, (kb or []) + [[{"text": "بازگشت", "callback_data": "sa:menu"}]]); return
    if data == "sa:queue":
        core.answer(cbid); t, kb = queue_view()
        core.edit(chat, mid, t, (kb or []) + [[{"text": "بازگشت", "callback_data": "sa:menu"}]]); return
    if data == "sa:plans":
        core.answer(cbid); core.edit(chat, mid, *plans_admin()); return
    if data == "sa:plan_add":
        core.pending[chat] = {"ts": time.time(), "stage": "sa_plan_add"}; core.answer(cbid)
        core.edit(chat, mid, PLAN_ADD_TEXT, [[{"text": "انصراف", "callback_data": "sa:plans"}]]); return
    if data == "sa:cards":
        core.answer(cbid); core.edit(chat, mid, *cards_admin()); return
    if data == "sa:card_add":
        core.pending[chat] = {"ts": time.time(), "stage": "sa_card_add"}; core.answer(cbid)
        core.edit(chat, mid, "➕ <b>کارت جدید</b>\nشمارهٔ ۱۶ رقمی و نام صاحب کارت را بفرست:\n<code>6037991234567890 فاطمه میرخلیلی</code>",
                  [[{"text": "انصراف", "callback_data": "sa:cards"}]]); return
    if data == "sa:outage":
        core.answer(cbid)
        if setting("outage"):
            core.edit(chat, mid, "✅ پیام «اختلال برطرف شد» برای همان مشتری‌هایی که اعلان گرفتند فرستاده شود؟",
                      [[{"text": "✅ بله، بفرست", "callback_data": "sa:outage_end"}], [{"text": "بازگشت", "callback_data": "sa:menu"}]])
        else:
            core.pending[chat] = {"ts": time.time(), "stage": "sa_outage"}
            core.edit(chat, mid, "📢 متن اعلان اختلال را بنویس (مثلاً «اینترنت همراه اول از ساعت ۱۸ کند شده»).\n"
                      "برای %s مشتری فعال فرستاده می‌شود." % fa(len(active_customers())),
                      [[{"text": "انصراف", "callback_data": "sa:menu"}]])
        return
    if data == "sa:outage_send":
        draft = setting("outage_draft")
        if not draft or setting("outage"): core.answer(cbid, "پیش‌نویسی نیست"); return
        set_setting("outage_draft", ""); start_outage(uid, chat, draft)
        core.answer(cbid, "در حال ارسال…"); core.edit(chat, mid, shop_admin_text(), shop_admin_kb()); return
    if data == "sa:outage_end":
        if not setting("outage"): core.answer(cbid); return
        end_outage(uid, chat); core.answer(cbid, "در حال ارسال…"); core.edit(chat, mid, shop_admin_text(), shop_admin_kb()); return
    if data == "sa:users":
        core.pending[chat] = {"ts": time.time(), "stage": "sa_user_find"}; core.answer(cbid)
        core.edit(chat, mid, USERS_PROMPT, [[{"text": "بازگشت", "callback_data": "sa:menu"}]]); return
    parts = data.split(":")
    if parts[1:2] == ["uwal"] and len(parts) == 4 and _int(parts[2]) is not None and parts[3] in ("add", "sub"):
        tid = int(parts[2])
        if not get_customer(tid): core.answer(cbid, "پیدا نشد"); return
        core.pending[chat] = {"ts": time.time(), "stage": "sa_wallet", "id": tid, "sign": 1 if parts[3] == "add" else -1}; core.answer(cbid)
        core.send(chat, "%s موجودی <code>%d</code>: مبلغ (تومان) و بعد دلیل را بفرست:\n<code>50000 بازگشت وجه سفارش ۱۲</code>"
                  % ("➕ افزایش" if parts[3] == "add" else "➖ کسر", tid)); return
    if parts[1:2] == ["ublock"] and len(parts) == 3 and _int(parts[2]) is not None:
        tid = int(parts[2]); cust = get_customer(tid)
        if not cust: core.answer(cbid, "پیدا نشد"); return
        if not cust["blocked"] and (staff_role(tid) or tid in owner_ids()):
            core.answer(cbid, "این شخص پشتیبان/ادمین است؛ اول از «🎧 پشتیبان‌ها» حذفش کن"); return
        c = core.db(); c.execute("UPDATE shop_customers SET blocked=1-blocked WHERE tg_id=?", (tid,)); c.commit(); c.close()
        log(uid, "unblock" if cust["blocked"] else "block", tid)
        core.answer(cbid, "رفع مسدودی شد" if cust["blocked"] else "مسدود شد"); core.edit(chat, mid, *user_card(tid)); return
    if len(parts) == 3 and _int(parts[2]) is not None:
        what, rid = parts[1], int(parts[2])
        if what in ("plan", "plan_act", "plan_pop", "plan_price", "plan_del"):
            p = get_plan(rid)
            if not p: core.answer(cbid, "پیدا نشد"); core.edit(chat, mid, *plans_admin()); return
            if what == "plan_price":
                core.pending[chat] = {"ts": time.time(), "stage": "sa_plan_price", "id": rid}; core.answer(cbid)
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
        def go():
            ok = send_backup(chat); log(uid, "backup_sent" if ok else "backup_failed")
            if not ok: core.send(chat, "❌ ارسال بکاپ ناموفق بود.")
        run_bg(go)   # the upload must not stall the update loop
        return
    if data == "sa:test":
        core.answer(cbid); core.edit(chat, mid, test_admin_text(), test_admin_kb()); return
    if data == "sa:test_toggle":
        on = not test_enabled(); set_setting("test_enabled", "1" if on else "0")
        log(uid, "test_on" if on else "test_off")
        core.answer(cbid); core.edit(chat, mid, test_admin_text(), test_admin_kb()); return
    if data == "sa:ref":
        core.answer(cbid); core.edit(chat, mid, *ref_admin()); return
    for key, choices, view in (("test_mb", TEST_MB_CHOICES, lambda: (test_admin_text(), test_admin_kb())),
                               ("test_hours", TEST_HOUR_CHOICES, lambda: (test_admin_text(), test_admin_kb())),
                               ("ref_reward", REF_REWARD_CHOICES, ref_admin), ("ref_discount", REF_DISCOUNT_CHOICES, ref_admin)):
        if data.startswith("sa:%s:" % key) and _int(data.rsplit(":", 1)[1]) is not None:
            v = int(data.rsplit(":", 1)[1])
            if v in choices and str(v) != setting(key):
                set_setting(key, v); log(uid, key, v)
                core.edit(chat, mid, *view())
            core.answer(cbid); return
    if data == "sa:log":
        core.answer(cbid); core.edit(chat, mid, log_text(), [[{"text": "بازگشت", "callback_data": "sa:menu"}]]); return
    if data == "sa:staff":
        core.answer(cbid); core.edit(chat, mid, STAFF_TEXT, staff_kb()); return
    if data == "sa:staff_add":
        core.pending[chat] = {"ts": time.time(), "stage": "sa_staff_add"}; core.answer(cbid)
        core.edit(chat, mid, "آیدی عددی پشتیبان را بفرست، یا یک پیام از او را اینجا فوروارد کن:",
                  [[{"text": "انصراف", "callback_data": "sa:staff"}]]); return
    if data.startswith("sa:staff_rm:") and _int(data.rsplit(":", 1)[1]) is not None:
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
    """Admin reply-keyboard buttons and shop input steps. Returns True when handled. Any command
    (/start, /admin, ...) first drops every shop prompt and is then left to bot.py."""
    text = (m.get("text") or "").strip()
    if text.startswith("/"):
        clear_admin_state(chat, uid); return False
    if text in ADMIN_BUTTONS:
        core.pending.pop(chat, None); clear_admin_state(chat, uid)
        if text == B_LINKS: core.send(chat, core.WELCOME, core.main_menu_kb())
        elif text == B_SHOP: core.send(chat, shop_admin_text(), shop_admin_kb())
        elif text == B_RECEIPTS: core.send(chat, *queue_view())
        elif text == B_TICKETS: core.send(chat, *tickets_view())
        else: web_login(chat)
        return True
    if review_text(chat, uid, text): return True
    if ticket_text(chat, uid, text): return True
    st = core.pending.get(chat)
    if st and str(st.get("stage", "")).startswith("sa_") and time.time() - st.get("ts", 0) > PENDING_TTL:
        core.pending.pop(chat, None); st = None   # an hours-old prompt must not read a random later message
    if st and st.get("stage") == "sa_outage":
        if not text: core.send(chat, "متن اعلان را بنویس:"); return True
        core.pending.pop(chat, None); set_setting("outage_draft", text[:1000])
        core.send(chat, "پیش‌نمایش:\n\n⚠️ <b>اطلاعیهٔ اختلال</b>\n\n%s\n\nبرای %s مشتری فعال بفرستم؟"
                  % (html.escape(text[:1000]), fa(len(active_customers()))),
                  [[{"text": "📢 ارسال", "callback_data": "sa:outage_send"}], [{"text": "انصراف", "callback_data": "sa:menu"}]])
        return True
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
            if (len(nums) != 3 or not math.isfinite(gb) or not math.isfinite(days) or gb < 0 or days < 0 or not price
                    or gb > 100000 or days > 3650): raise ValueError()
        except (ValueError, IndexError):
            core.send(chat, "سه عدد بفرست: حجم، روز، قیمت. مثلاً <code>30 30 150000</code>"); return True
        c = core.db()
        try:
            pid = c.execute("INSERT INTO shop_plans(gb,days,price,created_ts) VALUES(?,?,?,?)", (gb, days, price, int(time.time()))).lastrowid
            c.commit()
        finally:
            c.close()
        core.pending.pop(chat, None); log(uid, "plan_add", pid, "%g/%g/%d" % (gb, days, price))
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
        if c.execute("SELECT 1 FROM shop_cards WHERE number=?", (digits,)).fetchone():
            c.close(); core.send(chat, "این کارت قبلاً اضافه شده.", cards_admin()[1]); core.pending.pop(chat, None); return True
        cid = c.execute("INSERT INTO shop_cards(number,holder,created_ts) VALUES(?,?,?)", (digits, holder[:60], int(time.time()))).lastrowid
        c.commit(); c.close(); core.pending.pop(chat, None); log(uid, "card_add", cid, "…" + digits[-4:])
        core.send(chat, "✅ کارت اضافه شد.", cards_admin()[1]); return True
    if st and st.get("stage") == "sa_staff_add":
        tid = _forwarded_user_id(m) or _int(text)
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
BACKUP_MAX = 45 * 1024 * 1024   # Telegram bot uploads stop at 50MB

def make_backup():
    """Consistent snapshot of the live DB (sqlite online backup; safe under WAL writers) WITHOUT
    the web-panel sessions: a backup must never double as a 30-day login to the admin panel.
    Returns the bytes, or None when the file is too big for Telegram."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        src = core.db(); dst = sqlite3.connect(path)
        try:
            src.backup(dst)
            dst.execute("DELETE FROM meta WHERE k LIKE 'sess\\_%' ESCAPE '\\'")
            dst.commit(); dst.execute("VACUUM")   # really drop the deleted pages, not just unlink them
        finally:
            dst.close(); src.close()
        if os.path.getsize(path) > BACKUP_MAX: return None
        with open(path, "rb") as f: return f.read()
    finally:
        os.unlink(path)

def send_backup(chat, data=None):
    data = make_backup() if data is None else data
    if not data:   # None/b"": over Telegram's limit
        core.send(chat, "❌ بکاپ از سقف ۴۵ مگابایت تلگرام بزرگ‌تر است؛ آن را از روی سرور بردار."); return False
    name = "mohajer-%s.db" % core.day_key()
    r = upload("sendDocument", chat, "document", name, data,
               caption="💾 بکاپ دیتابیس · %s\n⚠️ شامل توکن اشتراک مشتری‌هاست؛ جایی منتشرش نکن (نشست‌های پنل وب داخلش نیست)."
                       % core.day_key())
    return bool(r.get("ok"))

BACKUP_HOUR = 4   # Iran time; quiet hours

def backup_due(now=None):
    now = time.time() if now is None else now
    hour = time.gmtime(now + core.IRAN_OFFSET).tm_hour
    return flag("backup_auto") and hour >= BACKUP_HOUR and setting("backup_day") != core.day_key(now)

# ---------------- background ticker ----------------
TICK_SECONDS = 60

KEEP_CLOSED_DAYS, KEEP_LOG_ROWS, KEEP_ORPHAN_DAYS = 90, 20000, 30

def housekeeping(now=None):
    """Once a day: forget old closed tickets and audit rows, tell owners of deleted links that
    auto-renew is off, and drop shop_services rows whose link is long gone."""
    now = int(time.time()) if now is None else now
    if setting("housekeeping_day") == core.day_key(now): return
    set_setting("housekeeping_day", core.day_key(now))
    c = core.db()
    gone = c.execute("SELECT s.token, s.tg_id, s.kind FROM shop_services s LEFT JOIN users u ON u.token=s.token "
                     "WHERE u.token IS NULL AND s.auto_renew=1").fetchall()
    c.execute("UPDATE shop_services SET auto_renew=0 WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.token=shop_services.token)")
    c.execute("DELETE FROM shop_services WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.token=shop_services.token) AND created_ts<?",
              (now - KEEP_ORPHAN_DAYS * 86400,))
    c.execute("DELETE FROM shop_ticket_msgs WHERE ticket_id IN (SELECT id FROM shop_tickets WHERE status='closed' AND updated_ts<?)",
              (now - KEEP_CLOSED_DAYS * 86400,))
    c.execute("DELETE FROM shop_tickets WHERE status='closed' AND updated_ts<?", (now - KEEP_CLOSED_DAYS * 86400,))
    c.execute("DELETE FROM shop_admin_log WHERE id < (SELECT COALESCE(MAX(id),0) FROM shop_admin_log) - ?", (KEEP_LOG_ROWS,))
    c.execute("DELETE FROM meta WHERE k LIKE 'shop_stuck_alert_%' AND k NOT IN "
              "(SELECT 'shop_stuck_alert_' || id FROM shop_orders WHERE status='approved' AND delivered_ts IS NULL)")
    c.commit(); c.close()
    for r in gone:
        if r["kind"] == "paid":
            core.send(r["tg_id"], "ℹ️ یکی از سرویس‌هات بعد از پایان مهلت تمدید حذف شد و تمدید خودکارش خاموش شد.\n"
                      "هر وقت خواستی، یه سرویس جدید بخر 👇", [[{"text": "🛒 خرید سرویس", "callback_data": "c:buy"}]])

def tick():
    """Every step on its own, so one failing job never starves the others."""
    for job in (_tick_backup, send_test_followups, expire_orders, retry_deliveries, run_auto_renewals,
                send_service_warnings, housekeeping):
        try: job()
        except Exception as e: print("shop tick err", job.__name__, e, flush=True)

def _tick_backup():
    if not backup_due(): return
    data = make_backup() or b""   # one snapshot, every owner gets it (b"" = too big: owners are told, no rebuild per owner)
    results = [send_backup(a, data) for a in owner_ids()]
    if any(results) or not data: set_setting("backup_day", core.day_key())   # a too-big file is not retried every minute either

def ticker():
    while True:
        try: tick()
        except Exception as e: print("shop tick err", e, flush=True)
        time.sleep(TICK_SECONDS)

def start_threads():
    threading.Thread(target=ticker, daemon=True).start()
