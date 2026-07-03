#!/usr/bin/env python3
# Mohajer - stdlib-only Telegram admin panel (no pip deps). Mints multi-protocol sub links:
# VLESS/VMess/Trojan over WebSocket (TLS + no-TLS, fronted by Cloudflare) + VLESS-XHTTP.
# Per-user quota + expiry are enforced live via `xray api adu/rmu/statsquery` (no xray restart).
# All config comes from bot.env (see config/bot.env.example). Single-file, runs under systemd.
import os, re, sys, json, time, html, base64, sqlite3, secrets, threading, subprocess
import uuid as uuidlib
import urllib.request, urllib.parse, ssl
import http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env

ENV = load_env(os.environ.get("DPBOT_ENV", "/opt/dpbot/bot.env"))
TOKEN       = ENV.get("BOT_TOKEN", "")
ADMIN_IDS   = set(int(x) for x in ENV.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit())
XRAY_BIN    = ENV.get("XRAY_BIN", "/usr/local/bin/xray")
XRAY_API    = ENV.get("XRAY_API", "127.0.0.1:10085")
DOMAIN      = ENV.get("DOMAIN", "cdn.delplayer.ir")
SUB_DIR     = ENV.get("SUB_DIR", "/opt/dpsub")
SUB_BASE    = ENV.get("SUB_BASE_URL", "https://cdn.delplayer.ir")
DB_PATH     = ENV.get("DB", "/opt/dpbot/dpbot.db")
POLL        = int(ENV.get("POLL_SECONDS", "30"))
ADMIN_PORT  = int(ENV.get("ADMIN_PORT", "8091"))
ENDPOINTS   = json.loads(ENV.get("ENDPOINTS", "[]"))
DEFAULT_IPS = [x.strip() for x in ENV.get("IPS", "104.16.96.1,104.21.96.1,104.19.96.1").split(",") if x.strip()]
GB = 1024 ** 3
IRAN_OFFSET = 3 * 3600 + 30 * 60  # UTC+03:30; Iran has no DST since 2022
GRACE_SECONDS = 48 * 3600  # after quota/time runs out, keep the link (disabled) this long for renewal, then auto-delete

def day_key(ts=None):
    if ts is None:
        ts = time.time()
    return time.strftime("%Y-%m-%d", time.gmtime(ts + IRAN_OFFSET))
API_URL = "https://api.telegram.org/bot%s/" % TOKEN
SSLCTX = ssl.create_default_context()

# ---------------- db ----------------
def db():
    c = sqlite3.connect(DB_PATH, timeout=15); c.row_factory = sqlite3.Row; return c

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        token TEXT PRIMARY KEY, uuid TEXT, email TEXT UNIQUE, label TEXT,
        limit_bytes INTEGER, expiry_ts INTEGER, created_ts INTEGER,
        base_bytes INTEGER DEFAULT 0, last_raw INTEGER DEFAULT 0, used_bytes INTEGER DEFAULT 0,
        disabled_ts INTEGER DEFAULT 0)""")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS usage_daily(
        token TEXT, day TEXT, start_used INTEGER, end_used INTEGER,
        PRIMARY KEY(token, day))""")
    cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "disabled_ts" not in cols:  # migrate existing DBs
        c.execute("ALTER TABLE users ADD COLUMN disabled_ts INTEGER DEFAULT 0")
    c.commit(); c.close()

def meta_get(k, d=None):
    c = db(); r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone(); c.close()
    return r["v"] if r else d

def meta_set(k, v):
    c = db(); c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    c.commit(); c.close()

# ---------------- telegram ----------------
def tg(method, **params):
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(API_URL + method, data=data), timeout=60, context=SSLCTX) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print("tg err", method, e, flush=True); return {}

def send(chat, text, kb=None):
    p = dict(chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview="true")
    if kb is not None: p["reply_markup"] = {"inline_keyboard": kb}
    return tg("sendMessage", **p)

def edit(chat, mid, text, kb=None):
    p = dict(chat_id=chat, message_id=mid, text=text, parse_mode="HTML", disable_web_page_preview="true")
    if kb is not None: p["reply_markup"] = {"inline_keyboard": kb}
    return tg("editMessageText", **p)

def answer(cb_id, text=None):
    p = {"callback_query_id": cb_id}
    if text: p["text"] = text
    tg("answerCallbackQuery", **p)

# ---------------- xray api (multi-endpoint) ----------------
def ep_email(token, tag): return "u_%s.%s" % (token, tag)

def _adu(ep, secret, email):
    if "reality" in ep:
        r = ep["reality"]
        cl = {"id": secret, "email": email, "level": 0}
        if r.get("flow"): cl["flow"] = r["flow"]
        settings = {"clients": [cl], "decryption": "none"}
        stream = {"network": "tcp", "security": "reality", "realitySettings": {
            "show": False, "dest": r["sni"] + ":443", "xver": 0,
            "serverNames": [r["sni"]], "privateKey": r["priv"], "shortIds": [r["sid"]]}}
        ib = {"tag": ep["tag"], "listen": "0.0.0.0", "port": ep["port"], "protocol": "vless", "settings": settings, "streamSettings": stream}
    else:
        proto, net = ep["proto"], ep["net"]
        stream = {"network": net, "security": "none"}
        if net == "ws":     stream["wsSettings"] = {"path": ep["path"]}
        elif net == "xhttp": stream["xhttpSettings"] = {"path": ep["path"], "mode": "auto"}
        if proto == "trojan":  settings = {"clients": [{"password": secret, "email": email, "level": 0}]}
        elif proto == "vmess": settings = {"clients": [{"id": secret, "email": email, "level": 0}]}
        else:                  settings = {"clients": [{"id": secret, "email": email, "level": 0}], "decryption": "none"}
        ib = {"tag": ep["tag"], "listen": "127.0.0.1", "port": ep["port"], "protocol": proto, "settings": settings, "streamSettings": stream}
    f = "/tmp/dpbot_adu_%s.json" % email.replace("/", "_")
    open(f, "w").write(json.dumps({"inbounds": [ib]}))
    try:
        r = subprocess.run([XRAY_BIN, "api", "adu", "--server=%s" % XRAY_API, f], capture_output=True, text=True, timeout=15)
        out = (r.stdout + r.stderr).lower()
    except Exception as e:
        out = str(e).lower()
    finally:
        try: os.remove(f)
        except Exception: pass
    return ("add user:" in out) or ("already" in out) or ("exists" in out)

def xr_add_user(token, secret):
    ok = True
    for ep in ENDPOINTS:
        if not _adu(ep, secret, ep_email(token, ep["tag"])): ok = False
    return ok

def xr_remove_user(token):
    for ep in ENDPOINTS:
        try:
            subprocess.run([XRAY_BIN, "api", "rmu", "--server=%s" % XRAY_API, "-tag=%s" % ep["tag"], ep_email(token, ep["tag"])],
                           capture_output=True, text=True, timeout=15)
        except Exception as e:
            print("rmu err", e, flush=True)

def xr_usage(token):
    try:
        r = subprocess.run([XRAY_BIN, "api", "statsquery", "--server=%s" % XRAY_API, "-pattern", "user>>>u_%s" % token],
                           capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout or "{}")
        return sum(int(s.get("value", 0)) for s in (d.get("stat") or []))
    except Exception:
        return 0

def xray_pid():
    try:
        return subprocess.run(["systemctl", "show", "xray", "-p", "MainPID", "--value"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""

# ---------------- sub links ----------------
def sub_path(token): return os.path.join(SUB_DIR, "sub-u-%s" % token)
def sub_url(token):  return "%s/sub-u-%s" % (SUB_BASE, token)

def _ws_link(ep, secret, ip, port, sec):
    proto, net, H = ep["proto"], ep["net"], DOMAIN
    qp = urllib.parse.quote(ep["path"], safe="")
    tls_on = (sec == "tls")
    base = ep["label"] if tls_on else (ep["label"].replace("-WS", "").replace("-XHTTP", "") + "-noTLS")
    nm = urllib.parse.quote("%s · %s" % (base, ip))
    sni = ("&sni=%s" % H) if tls_on else ""
    secp = "tls" if tls_on else "none"
    if proto == "vless":
        extra = "&mode=auto" if net == "xhttp" else ""
        return "vless://%s@%s:%s?encryption=none&security=%s&type=%s&host=%s%s&path=%s%s#%s" % (secret, ip, port, secp, net, H, sni, qp, extra, nm)
    if proto == "trojan":
        return "trojan://%s@%s:%s?security=%s%s&type=%s&host=%s&path=%s#%s" % (secret, ip, port, secp, sni, net, H, qp, nm)
    if proto == "vmess":
        j = {"v": "2", "ps": "%s · %s" % (base, ip), "add": ip, "port": str(port), "id": secret, "aid": "0", "scy": "auto",
             "net": net, "type": "none", "host": H, "path": ep["path"], "tls": ("tls" if tls_on else ""), "sni": (H if tls_on else "")}
        return "vmess://" + base64.b64encode(json.dumps(j).encode()).decode()
    return ""

def _reality_link(ep, secret):
    r = ep["reality"]
    nm = urllib.parse.quote("REALITY · مستقیم")
    flow = ("&flow=%s" % r["flow"]) if r.get("flow") else ""
    return ("vless://%s@%s:%s?encryption=none&security=reality&pbk=%s&sni=%s&fp=%s&sid=%s&type=tcp%s#%s"
            % (secret, r["addr"], r["port"], r["pbk"], r["sni"], r["fp"], r["sid"], flow, nm))

def get_ips():
    v = meta_get("clean_ips")
    if v:
        ips = [x.strip() for x in v.split(",") if x.strip()]
        if ips: return ips
    return DEFAULT_IPS

def set_ips(ips):
    meta_set("clean_ips", ",".join(ips))

def write_sub(token, secret, label):
    ips = get_ips() or DEFAULT_IPS; links = []; gi = 0
    for ep in ENDPOINTS:
        for port in ep.get("tls_ports", []):
            links.append(_ws_link(ep, secret, ips[gi % len(ips)], port, "tls")); gi += 1
        for port in ep.get("notls_ports", []):
            links.append(_ws_link(ep, secret, ips[gi % len(ips)], port, "none")); gi += 1
    open(sub_path(token), "w").write(base64.b64encode("\n".join(l for l in links if l).encode()).decode())

def regenerate_all_subs():
    c = db(); rows = c.execute("SELECT token,uuid,label FROM users").fetchall(); c.close()
    for u in rows: write_sub(u["token"], u["uuid"], u["label"])

def del_sub(token):
    try: os.remove(sub_path(token))
    except Exception: pass

# ---------------- helpers ----------------
def fmt_bytes(b):
    b = float(b)
    if b <= 0: return "0"
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024: return "%.1f %s" % (b, u)
        b /= 1024
    return "%.1f PB" % b

def human_limit(lb): return "نامحدود" if lb <= 0 else fmt_bytes(lb)

def human_expiry(ts):
    if ts <= 0: return "نامحدود"
    left = ts - int(time.time())
    if left <= 0: return "منقضی"
    d = left // 86400; h = (left % 86400) // 3600
    return ("%d روز و %d ساعت" % (d, h)) if d else ("%d ساعت" % h)

def is_admin(uid):
    if ADMIN_IDS: return uid in ADMIN_IDS
    a = meta_get("admin_id"); return a is not None and int(a) == uid

# ---------------- core ops ----------------
def create_user(vol_gb, dur_days, label=None):
    token = secrets.token_hex(8)
    secret = str(uuidlib.uuid4())
    label = label or ("link-%s" % token[:6])
    limit_bytes = int(vol_gb * GB) if vol_gb and vol_gb > 0 else 0
    expiry_ts = int(time.time()) + int(dur_days) * 86400 if dur_days and dur_days > 0 else 0
    if not xr_add_user(token, secret): return None
    write_sub(token, secret, label)
    c = db()
    c.execute("""INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,base_bytes,last_raw,used_bytes)
                 VALUES(?,?,?,?,?,?,?,0,0,0)""",
              (token, secret, "u_" + token, label, limit_bytes, expiry_ts, int(time.time())))
    c.commit(); c.close()
    return token

def delete_user(token):
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    if u:
        xr_remove_user(token); del_sub(token)
        c.execute("DELETE FROM users WHERE token=?", (token,)); c.commit()
    c.close()

def exhaust_reason(u, now=None):
    now = now or int(time.time())
    if (u["limit_bytes"] or 0) > 0 and u["used_bytes"] >= u["limit_bytes"]: return "حجم تمام شد"
    if (u["expiry_ts"] or 0) > 0 and now >= u["expiry_ts"]: return "زمان تمام شد"
    return None

def disable_user(token):
    # exhausted: stop service (remove from xray) but KEEP the row + sub file so it can be renewed within the grace window
    xr_remove_user(token)
    c = db(); c.execute("UPDATE users SET disabled_ts=? WHERE token=?", (int(time.time()), token)); c.commit(); c.close()

def reenable_user(token):
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if not u: return
    xr_add_user(token, u["uuid"]); write_sub(token, u["uuid"], u["label"])
    c = db(); c.execute("UPDATE users SET disabled_ts=0 WHERE token=?", (token,)); c.commit(); c.close()

def maybe_reenable(token):
    # after an extend: if it was disabled but now has quota/time again, bring it back live immediately
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if u and u["disabled_ts"] and not exhaust_reason(u):
        reenable_user(token); return True
    return False

def refresh_usage(token):
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    if not u: c.close(); return
    raw = xr_usage(token); base = u["base_bytes"]; last = u["last_raw"]
    if raw < last: base += last
    used = base + raw
    c.execute("UPDATE users SET base_bytes=?,last_raw=?,used_bytes=? WHERE token=?", (base, raw, used, token))
    c.commit(); c.close()

def xr_usage_all():
    # ONE statsquery for all users -> {token: bytes} (avoids N forks per poll)
    try:
        r = subprocess.run([XRAY_BIN, "api", "statsquery", "--server=%s" % XRAY_API, "-pattern", "user>>>u_"],
                           capture_output=True, text=True, timeout=20)
        d = json.loads(r.stdout or "{}"); tot = {}
        for s in (d.get("stat") or []):
            nm = s.get("name", "")
            if not nm.startswith("user>>>u_"): continue
            tk = nm[len("user>>>u_"):].split(".", 1)[0].split(">>>", 1)[0]
            tot[tk] = tot.get(tk, 0) + int(s.get("value", 0))
        return tot
    except Exception:
        return {}

def refresh_all_usage():
    raws = xr_usage_all(); c = db(); today = day_key()
    for u in c.execute("SELECT token,base_bytes,last_raw FROM users").fetchall():
        raw = raws.get(u["token"], 0); base = u["base_bytes"]
        if raw < u["last_raw"]: base += u["last_raw"]
        used = base + raw
        c.execute("UPDATE users SET base_bytes=?,last_raw=?,used_bytes=? WHERE token=?", (base, raw, used, u["token"]))
        record_daily(c, u["token"], used, today)
    prune_daily(c)
    c.commit(); c.close()

def record_daily(c, token, used, day):
    r = c.execute("SELECT start_used,end_used FROM usage_daily WHERE token=? AND day=?", (token, day)).fetchone()
    if r is None:
        c.execute("INSERT INTO usage_daily(token,day,start_used,end_used) VALUES(?,?,?,?)", (token, day, used, used))
    elif used > r["end_used"]:
        c.execute("UPDATE usage_daily SET end_used=? WHERE token=? AND day=?", (used, token, day))

def prune_daily(c, keep_days=30):
    cutoff = day_key(time.time() - keep_days * 86400)
    c.execute("DELETE FROM usage_daily WHERE day < ?", (cutoff,))

def panel_usage_summary():
    c = db(); day = day_key()
    total = c.execute("SELECT COALESCE(SUM(used_bytes),0) v FROM users").fetchone()["v"]
    today = c.execute("SELECT COALESCE(SUM(max(end_used-start_used,0)),0) v FROM usage_daily WHERE day=?", (day,)).fetchone()["v"]
    c.close(); return int(total), int(today)

def resync_all():
    c = db(); rows = c.execute("SELECT token,uuid,label,disabled_ts FROM users").fetchall(); c.close()
    for u in rows:
        if u["disabled_ts"]: continue   # in 48h grace — keep it out of xray (stays renewable)
        xr_add_user(u["token"], u["uuid"]); write_sub(u["token"], u["uuid"], u["label"])

def notify_admin(text):
    a = (next(iter(ADMIN_IDS)) if ADMIN_IDS else meta_get("admin_id"))
    if a: send(int(a), text)

# ---------------- UI ----------------
VOLS = [("10GB", 10), ("30GB", 30), ("50GB", 50), ("100GB", 100), ("200GB", 200)]
DURS = [("۱ روز", 1), ("۷ روز", 7), ("۳۰ روز", 30), ("۶۰ روز", 60), ("۹۰ روز", 90)]
pending = {}

def main_menu_kb():
    return [[{"text": "➕ ساخت لینک جدید", "callback_data": "new"}],
            [{"text": "📋 لیست لینک‌ها", "callback_data": "list"}],
            [{"text": "🌐 آی‌پی‌های تمیز", "callback_data": "ips"}]]

def _grid(items, cb):
    rows, r = [], []
    for label, val in items:
        r.append({"text": label, "callback_data": "%s:%s" % (cb, val)})
        if len(r) == 3: rows.append(r); r = []
    if r: rows.append(r)
    return rows

def vol_kb():
    rows = _grid(VOLS, "vol")
    rows.append([{"text": "♾ نامحدود", "callback_data": "vol:0"}, {"text": "✏️ دلخواه", "callback_data": "vol:custom"}])
    rows.append([{"text": "بازگشت", "callback_data": "menu"}]); return rows

def dur_kb():
    rows = _grid(DURS, "dur")
    rows.append([{"text": "♾ نامحدود", "callback_data": "dur:0"}, {"text": "✏️ دلخواه", "callback_data": "dur:custom"}])
    rows.append([{"text": "بازگشت", "callback_data": "new"}]); return rows

def list_kb():
    c = db(); rows = c.execute("SELECT * FROM users ORDER BY created_ts DESC").fetchall(); c.close()
    kb = [[{"text": "%s %s · %s/%s · %s" % (("⏸" if u["disabled_ts"] else "🔗"), u["label"], fmt_bytes(u["used_bytes"]), human_limit(u["limit_bytes"]), human_expiry(u["expiry_ts"])),
            "callback_data": "u:%s" % u["token"]}] for u in rows]
    kb.append([{"text": "بازگشت", "callback_data": "menu"}]); return kb

def result_text(token):
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    return ("✅ <b>لینک ساخته شد</b>\n\n🏷 نام: <code>%s</code>\n📦 حجم: %s\n⏳ مدت: %s\n"
            "🧩 شامل: VLESS/VMess/Trojan (با و بدون TLS) + VLESS-XHTTP\n\n🔗 لینک ساب:\n<code>%s</code>\n\n"
            "(در مرورگر باز کنی، صفحه‌ی کپی + نوار حجم/زمان می‌آید)"
            % (html.escape(u["label"]), human_limit(u["limit_bytes"]), human_expiry(u["expiry_ts"]), sub_url(token)))

def detail_text(u):
    status = ""
    dts = u["disabled_ts"] if "disabled_ts" in u.keys() else 0
    if dts:
        left_h = max(0, (dts + GRACE_SECONDS - int(time.time())) // 3600)
        status = "⏸ <b>غیرفعال شد</b> (%s) — تا ~%d ساعت دیگر قابل تمدید است، وگرنه خودکار حذف می‌شود.\n\n" % (exhaust_reason(u) or "اتمام", left_h)
    return ("%s🔗 <b>%s</b>\n\n📦 مصرف: %s از %s\n⏳ %s\n🆔 <code>u_%s</code>\n\n🔗 <code>%s</code>"
            % (status, html.escape(u["label"]), fmt_bytes(u["used_bytes"]), human_limit(u["limit_bytes"]),
               human_expiry(u["expiry_ts"]), u["token"], sub_url(u["token"])))

def detail_kb(token):
    return [[{"text": "➕ حجم", "callback_data": "av:%s" % token}, {"text": "➕ زمان", "callback_data": "at:%s" % token}],
            [{"text": "🔄 بروزرسانی مصرف", "callback_data": "u:%s" % token}],
            [{"text": "🗑 حذف لینک", "callback_data": "del:%s" % token}],
            [{"text": "بازگشت به لیست", "callback_data": "list"}]]

VOLS_ADD = [("+10GB", 10), ("+30GB", 30), ("+50GB", 50), ("+100GB", 100), ("+200GB", 200)]
DURS_ADD = [("+۷ روز", 7), ("+۳۰ روز", 30), ("+۶۰ روز", 60), ("+۹۰ روز", 90)]

def _add_kb(token, items, cb):
    rows, r = [], []
    for label, v in items:
        r.append({"text": label, "callback_data": "%s:%s:%d" % (cb, token, v)})
        if len(r) == 3: rows.append(r); r = []
    if r: rows.append(r)
    rows.append([{"text": "♾ نامحدود کن", "callback_data": "%s:%s:unlim" % (cb, token)},
                 {"text": "✏️ دلخواه", "callback_data": "%s:%s:custom" % (cb, token)}])
    rows.append([{"text": "بازگشت", "callback_data": "u:%s" % token}])
    return rows

def addvol_kb(token):  return _add_kb(token, VOLS_ADD, "avd")
def addtime_kb(token): return _add_kb(token, DURS_ADD, "atd")

def extend_volume(token, gb):
    c = db(); u = c.execute("SELECT limit_bytes FROM users WHERE token=?", (token,)).fetchone()
    if u:
        c.execute("UPDATE users SET limit_bytes=? WHERE token=?", ((u["limit_bytes"] or 0) + int(float(gb) * GB), token)); c.commit()
    c.close()

def extend_time(token, days):
    c = db(); u = c.execute("SELECT expiry_ts FROM users WHERE token=?", (token,)).fetchone()
    if u:
        now = int(time.time()); base = u["expiry_ts"] if (u["expiry_ts"] or 0) > now else now
        c.execute("UPDATE users SET expiry_ts=? WHERE token=?", (base + int(days) * 86400, token)); c.commit()
    c.close()

def set_unlimited(token, field):
    c = db(); c.execute("UPDATE users SET %s=0 WHERE token=?" % field, (token,)); c.commit(); c.close()

WELCOME = "🔐 <b>پنل Mohajer</b>\nیکی را انتخاب کن:"

def route_cb(chat, mid, data, cbid):
    if data == "menu":
        pending.pop(chat, None); answer(cbid); edit(chat, mid, WELCOME, main_menu_kb()); return
    if data == "ips":
        answer(cbid); ips = get_ips()
        txt = "🌐 <b>آی‌پی‌های تمیز کلادفلر</b>\nدر کانفیگ همه‌ی لینک‌ها استفاده می‌شوند:\n\n" + "\n".join("• <code>%s</code>" % i for i in ips)
        edit(chat, mid, txt, [[{"text": "✏️ ویرایش لیست", "callback_data": "ips_edit"}], [{"text": "بازگشت", "callback_data": "menu"}]]); return
    if data == "ips_edit":
        pending[chat] = {"stage": "ips_edit"}; answer(cbid)
        edit(chat, mid, "آی‌پی‌های تمیز را بفرست (با کاما یا هر خط یکی):\n<code>104.16.96.1, 104.21.96.1, 104.19.96.1</code>"); return
    if data == "new":
        pending[chat] = {"stage": "vol"}; answer(cbid); edit(chat, mid, "📦 حجم لینک را انتخاب کن:", vol_kb()); return
    if data.startswith("vol:"):
        v = data.split(":", 1)[1]
        if v == "custom":
            pending[chat] = {"stage": "vol_custom"}; answer(cbid); edit(chat, mid, "عدد حجم را به <b>گیگابایت</b> بفرست (مثلاً 25):"); return
        pending[chat] = {"stage": "dur", "vol_gb": float(v)}; answer(cbid)
        edit(chat, mid, "حجم: %s ✅\n⏳ مدت زمان را انتخاب کن:" % ("نامحدود" if float(v) == 0 else "%sGB" % v), dur_kb()); return
    if data.startswith("dur:"):
        d = data.split(":", 1)[1]; st = pending.get(chat, {})
        if d == "custom":
            st["stage"] = "dur_custom"; pending[chat] = st; answer(cbid); edit(chat, mid, "تعداد <b>روز</b> را بفرست (مثلاً 45):"); return
        st["dur_days"] = int(d); st["stage"] = "name"; pending[chat] = st; answer(cbid)
        edit(chat, mid, "🏷 یک نام برای این لینک بفرست (مثلاً اسم مشتری):", [[{"text": "⏭ بدون نام", "callback_data": "noname"}]])
        return
    if data == "noname":
        st = pending.get(chat, {}); pending.pop(chat, None); answer(cbid, "در حال ساخت…")
        token = create_user(st.get("vol_gb", 0), st.get("dur_days", 0))
        if token: edit(chat, mid, result_text(token), [[{"text": "بازگشت به منو", "callback_data": "menu"}]])
        else:     edit(chat, mid, "❌ خطا در ساخت کاربر.", main_menu_kb())
        return
    if data.startswith("avd:") or data.startswith("atd:"):
        kind, token, val = data.split(":"); is_vol = (kind == "avd")
        if val == "custom":
            pending[chat] = {"stage": ("addvol_custom" if is_vol else "addtime_custom"), "token": token}; answer(cbid)
            edit(chat, mid, "عدد <b>%s</b> برای افزودن را بفرست:" % ("حجم (GB)" if is_vol else "روز")); return
        if val == "unlim": set_unlimited(token, "limit_bytes" if is_vol else "expiry_ts")
        elif is_vol:       extend_volume(token, val)
        else:              extend_time(token, val)
        answer(cbid, "بروز شد ✅"); refresh_usage(token); maybe_reenable(token)
        c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
        if u: edit(chat, mid, detail_text(u), detail_kb(token))
        return
    if data.startswith("av:"):
        token = data[3:]; answer(cbid); edit(chat, mid, "📦 چقدر حجم اضافه شود؟", addvol_kb(token)); return
    if data.startswith("at:"):
        token = data[3:]; answer(cbid); edit(chat, mid, "⏳ چقدر زمان اضافه شود؟", addtime_kb(token)); return
    if data == "list":
        answer(cbid); c = db(); n = c.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]; c.close()
        if n:
            total, today = panel_usage_summary()
            head = "📋 لینک‌های فعال:\n📊 مصرف کل: %s · امروز: %s" % (fmt_bytes(total), fmt_bytes(today))
        else:
            head = "هنوز لینکی نساخته‌ای. با ➕ شروع کن."
        edit(chat, mid, head, list_kb()); return
    if data.startswith("u:"):
        token = data[2:]; refresh_usage(token)
        c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
        if not u: answer(cbid, "یافت نشد"); edit(chat, mid, "📋 لینک‌ها:", list_kb()); return
        answer(cbid); edit(chat, mid, detail_text(u), detail_kb(token)); return
    if data.startswith("del:"):
        delete_user(data[4:]); answer(cbid, "حذف شد 🗑"); edit(chat, mid, "📋 لینک‌ها:", list_kb()); return
    answer(cbid)

def handle_update(up):
    if "callback_query" in up:
        cq = up["callback_query"]; uid = cq["from"]["id"]
        chat = cq["message"]["chat"]["id"]; mid = cq["message"]["message_id"]
        if not is_admin(uid): answer(cq["id"], "⛔️ اجازه نداری"); return
        route_cb(chat, mid, cq["data"], cq["id"]); return
    if "message" not in up: return
    m = up["message"]; uid = m["from"]["id"]; chat = m["chat"]["id"]; text = m.get("text", "")
    if (not ADMIN_IDS) and meta_get("admin_id") is None and text.startswith("/start"):
        meta_set("admin_id", uid)
    if not is_admin(uid): send(chat, "⛔️ این ربات خصوصی است."); return
    st = pending.get(chat)
    if st and st.get("stage") == "ips_edit":
        toks = [t.split(":")[0].strip() for t in re.split(r"[\s,]+", text.strip()) if t.strip()]
        valid = [t for t in toks if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", t) and all(0 <= int(o) <= 255 for o in t.split("."))]
        pending.pop(chat, None)
        if not valid:
            send(chat, "❌ هیچ IP معتبری پیدا نشد. مثل <code>104.16.96.1, 104.21.96.1</code> بفرست.", main_menu_kb()); return
        set_ips(valid); regenerate_all_subs()
        send(chat, "✅ <b>%d آی‌پی</b> ذخیره و همه‌ی لینک‌ها بروز شدند:\n%s\n\nمشتری‌ها فقط کافیست Update بزنند." % (len(valid), "\n".join("• <code>%s</code>" % i for i in valid)), main_menu_kb()); return
    if st and st.get("stage") in ("addvol_custom", "addtime_custom"):
        is_vol = st["stage"] == "addvol_custom"; token = st["token"]
        try: n = float(text.replace(",", ".")) if is_vol else int(text)
        except Exception: send(chat, "یک عدد بفرست:"); return
        (extend_volume if is_vol else extend_time)(token, n); pending.pop(chat, None); refresh_usage(token); maybe_reenable(token)
        c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
        if u: send(chat, "✅ بروز شد.\n\n" + detail_text(u), detail_kb(token))
        return
    if st and st.get("stage") == "vol_custom":
        try: gb = float(text.replace(",", "."))
        except Exception: send(chat, "یک عدد بفرست (GB):"); return
        st["vol_gb"] = gb; st["stage"] = "dur"; pending[chat] = st
        send(chat, "حجم: %sGB ✅\nحالا مدت را انتخاب کن:" % gb, dur_kb()); return
    if st and st.get("stage") == "dur_custom":
        try: d = int(text)
        except Exception: send(chat, "یک عدد بفرست (روز):"); return
        st["dur_days"] = d; st["stage"] = "name"; pending[chat] = st
        send(chat, "🏷 یک نام برای این لینک بفرست (مثلاً اسم مشتری):", [[{"text": "⏭ بدون نام", "callback_data": "noname"}]]); return
    if st and st.get("stage") == "name":
        token = create_user(st.get("vol_gb", 0), st.get("dur_days", 0), label=(text.strip()[:40] or None)); pending.pop(chat, None)
        send(chat, result_text(token) if token else "❌ خطا در ساخت کاربر.",
             [[{"text": "بازگشت به منو", "callback_data": "menu"}]] if token else main_menu_kb()); return
    if text.startswith("/start"):
        pending.pop(chat, None); send(chat, WELCOME, main_menu_kb()); return
    send(chat, "از دکمه‌ها استفاده کن 👇", main_menu_kb())

def enforcer():
    last_pid = meta_get("xray_pid")
    while True:
        try:
            pid = xray_pid()
            if pid and pid != "0" and pid != last_pid:
                resync_all(); last_pid = pid; meta_set("xray_pid", pid)
            refresh_all_usage()
            c = db(); rows = c.execute("SELECT token,uuid,used_bytes,limit_bytes,expiry_ts,label,disabled_ts FROM users").fetchall(); c.close()
            now = int(time.time())
            for cur in rows:
                reason = exhaust_reason(cur, now)
                if reason:
                    if not cur["disabled_ts"]:                       # just ran out -> disable + notify, start 48h grace
                        disable_user(cur["token"])
                        notify_admin("⏸ لینک «%s» غیرفعال شد (%s).\nتا ۴۸ ساعت قابل تمدید است؛ بعد از آن خودکار حذف می‌شود." % (cur["label"], reason))
                    elif now - cur["disabled_ts"] >= GRACE_SECONDS:  # grace over -> delete + notify
                        delete_user(cur["token"])
                        notify_admin("🗑 لینک «%s» پس از ۴۸ ساعت مهلتِ تمدید، خودکار حذف شد." % cur["label"])
                elif cur["disabled_ts"]:                             # got renewed -> bring back live + notify
                    reenable_user(cur["token"])
                    notify_admin("▶️ لینک «%s» تمدید شد و دوباره فعال شد." % cur["label"])
        except Exception as e:
            print("enforcer err", e, flush=True)
        time.sleep(POLL)

# ================= ADMIN PANEL (web) =================
LOGIN_TTL = 600      # one-time login link lifetime (s)
SESS_TTL  = 86400    # session cookie lifetime (s)
_login_tokens = {}   # token -> expires_ts
_sessions = {}       # sid -> {"exp": ts, "csrf": str}

def _prune_auth(now):
    for k in [k for k, v in _login_tokens.items() if v <= now]: _login_tokens.pop(k, None)
    for k in [k for k, s in _sessions.items() if s["exp"] <= now]: _sessions.pop(k, None)

def mint_login(now=None):
    now = now or int(time.time()); _prune_auth(now)
    tok = secrets.token_urlsafe(24); _login_tokens[tok] = now + LOGIN_TTL; return tok

def consume_login(tok, now=None):
    now = now or int(time.time())
    exp = _login_tokens.pop(tok, None)
    return bool(exp and exp > now)

def new_session(now=None):
    now = now or int(time.time())
    sid = secrets.token_urlsafe(24); csrf = secrets.token_urlsafe(16)
    _sessions[sid] = {"exp": now + SESS_TTL, "csrf": csrf}
    return sid, csrf

def session_csrf(sid, now=None):
    now = now or int(time.time())
    s = _sessions.get(sid) if sid else None
    if not s or s["exp"] <= now:
        if sid: _sessions.pop(sid, None)
        return None
    return s["csrf"]

def cookie_sid(cookie_header):
    try:
        c = http.cookies.SimpleCookie(); c.load(cookie_header or "")
        return c["mj_sess"].value if "mj_sess" in c else None
    except Exception:
        return None

def daily_series(days=7, token=None, now=None):
    base = now or time.time()
    keys = [day_key(base - (days - 1 - i) * 86400) for i in range(days)]
    c = db()
    if token:
        rows = c.execute("SELECT day, max(end_used-start_used,0) v FROM usage_daily WHERE token=? AND day>=?",
                         (token, keys[0])).fetchall()
    else:
        rows = c.execute("SELECT day, SUM(max(end_used-start_used,0)) v FROM usage_daily WHERE day>=? GROUP BY day",
                         (keys[0],)).fetchall()
    c.close()
    m = {r["day"]: int(r["v"] or 0) for r in rows}
    return [(k, m.get(k, 0)) for k in keys]

def users_overview():
    c = db(); today = day_key()
    rows = c.execute("SELECT token,label,used_bytes,limit_bytes,expiry_ts,disabled_ts,created_ts FROM users ORDER BY created_ts DESC").fetchall()
    daily = {r["token"]: int(r["v"] or 0) for r in
             c.execute("SELECT token, max(end_used-start_used,0) v FROM usage_daily WHERE day=?", (today,)).fetchall()}
    c.close()
    out = []
    for r in rows:
        d = dict(r); d["today"] = daily.get(r["token"], 0); out.append(d)
    return out

ADMIN_CSS = ("body{font-family:-apple-system,Segoe UI,Roboto,Tahoma,sans-serif;background:#0e1014;color:#e8eaed;"
             "margin:0;padding:16px;line-height:1.6}a{color:#5b9dff}.wrap{max-width:820px;margin:0 auto}"
             "h1{font-size:19px}h2{font-size:15px;color:#aeb4bf}.card{background:#14181f;border:1px solid #222836;"
             "border-radius:12px;padding:14px;margin:12px 0}table{width:100%;border-collapse:collapse;font-size:13px}"
             "th,td{text-align:right;padding:8px 6px;border-bottom:1px solid #222836}"
             "svg rect{fill:#2563eb}.btn{display:inline-block;background:#2563eb;color:#fff;border:0;border-radius:8px;"
             "padding:9px 14px;font-size:13px;cursor:pointer;text-decoration:none}.btn.g{background:#1b2030;color:#cdd2db}"
             "input,form{margin:4px 0}input[type=text],input[type=number]{background:#0e1014;border:1px solid #2a3140;"
             "color:#e8eaed;border-radius:8px;padding:8px;width:120px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}"
             "code{background:#0e1014;padding:2px 6px;border-radius:6px;word-break:break-all}")

def _page(title, inner):
    return ("<!doctype html><html lang=fa dir=rtl><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><title>%s</title>"
            "<style>%s</style></head><body><div class=wrap>%s</div></body></html>" % (html.escape(title), ADMIN_CSS, inner))

def _html(page):
    return 200, {"Content-Type": "text/html; charset=utf-8"}, page.encode("utf-8")

def svg_bars(series, w=780, h=90):
    mx = max([v for _, v in series] + [1]); n = len(series) or 1; bw = w / n; bars = ""
    for i, (lab, v) in enumerate(series):
        bh = (v / mx) * (h - 4); y = h - bh
        bars += '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2"><title>%s: %s</title></rect>' % (
            i * bw + 2, y, bw - 4, bh, html.escape(lab), fmt_bytes(v))
    return '<svg viewBox="0 0 %d %d" width="100%%" height="%d" preserveAspectRatio="none">%s</svg>' % (w, h, h, bars)

def render_expired():
    return _page("منقضی", "<h1>لینک منقضی شد</h1><p>برای ورود دوباره، در ربات دستور <code>/admin</code> را بزن.</p>")

def render_dashboard():
    total, today = panel_usage_summary()
    ov = users_overview()
    active = sum(1 for u in ov if not u["disabled_ts"]); disabled = len(ov) - active
    chart = svg_bars(daily_series(7))
    head = ("<h1>📊 پنل Mohajer</h1><div class=card><div class=row>"
            "<div>مصرف کل: <b>%s</b></div><div>امروز: <b>%s</b></div>"
            "<div>لینک‌ها: <b>%d</b> (فعال %d / غیرفعال %d)</div></div>"
            "<h2>مصرف ۷ روز اخیر</h2>%s</div>" % (fmt_bytes(total), fmt_bytes(today), len(ov), active, disabled, chart))
    rows = "".join(
        "<tr><td><a href='/a/user?token=%s'>%s%s</a></td><td>%s / %s</td><td>%s</td><td>%s</td></tr>" % (
            u["token"], ("⏸ " if u["disabled_ts"] else ""), html.escape(u["label"]),
            fmt_bytes(u["used_bytes"]), human_limit(u["limit_bytes"]), fmt_bytes(u["today"]), human_expiry(u["expiry_ts"]))
        for u in ov) or "<tr><td colspan=4>لینکی نیست</td></tr>"
    table = ("<div class=card><div class=row style='justify-content:space-between'><h2>کاربران</h2>"
             "<a class=btn href='/a/new'>➕ لینک جدید</a></div>"
             "<table><tr><th>نام</th><th>مصرف/سقف</th><th>امروز</th><th>انقضا</th></tr>%s</table></div>" % rows)
    return _page("پنل", head + table)

def _form(action, fields, csrf, btn, cls="btn"):
    inner = "".join(fields) + "<input type=hidden name=csrf value='%s'>" % csrf
    return "<form method=post action='%s' class=row>%s<button class='%s'>%s</button></form>" % (action, inner, cls, btn)

def render_user(token, csrf):
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if not u:
        return _page("یافت نشد", "<h1>یافت نشد</h1><a href='/a/'>بازگشت</a>")
    today_u = daily_series(1, token=token)[-1][1]
    chart = svg_bars(daily_series(30, token=token))
    tk = "<input type=hidden name=token value='%s'>" % token
    forms = (
        _form("/a/addvol", [tk, "<input type=number name=gb placeholder='GB'>"], csrf, "➕ حجم") +
        _form("/a/addtime", [tk, "<input type=number name=days placeholder='روز'>"], csrf, "➕ زمان") +
        _form("/a/rename", [tk, "<input type=text name=name placeholder='نام'>"], csrf, "✏️ نام") +
        _form("/a/unlimit", [tk, "<input type=hidden name=field value=limit_bytes>"], csrf, "♾ حجم نامحدود", "btn g") +
        _form("/a/unlimit", [tk, "<input type=hidden name=field value=expiry_ts>"], csrf, "♾ زمان نامحدود", "btn g"))
    dele = "<a class='btn g' href='/a/del?token=%s' style='background:#dc2626;color:#fff'>🗑 حذف لینک</a>" % token
    body = ("<h1>%s%s</h1><p><a href='/a/'>← داشبورد</a></p>"
            "<div class=card>مصرف: <b>%s</b> از %s · امروز: %s · انقضا: %s<br>لینک: <code>%s</code></div>"
            "<div class=card><h2>۳۰ روز اخیر</h2>%s</div>"
            "<div class=card><h2>عملیات</h2>%s<div style='margin-top:10px'>%s</div></div>" % (
                ("⏸ " if u["disabled_ts"] else ""), html.escape(u["label"]),
                fmt_bytes(u["used_bytes"]), human_limit(u["limit_bytes"]), fmt_bytes(today_u),
                human_expiry(u["expiry_ts"]), sub_url(token), chart, forms, dele))
    return _page("کاربر", body)

def render_new(csrf):
    f = _form("/a/new", ["<input type=number name=gb placeholder='حجم GB (۰=نامحدود)'>",
                         "<input type=number name=days placeholder='روز (۰=نامحدود)'>",
                         "<input type=text name=name placeholder='نام'>"], csrf, "ساخت")
    return _page("لینک جدید", "<h1>➕ لینک جدید</h1><p><a href='/a/'>← داشبورد</a></p><div class=card>%s</div>" % f)

def render_delconfirm(token, csrf):
    f = _form("/a/delete", ["<input type=hidden name=token value='%s'>" % token,
                            "<input type=hidden name=confirm value=yes>"], csrf, "بله، حذف کن")
    return _page("حذف", "<h1>حذف لینک؟</h1><p>این کار برگشت‌ناپذیر است.</p><div class=card>%s "
                        "<a class='btn g' href='/a/user?token=%s'>انصراف</a></div>" % (f, token))

def route_admin(method, path, query, cookie_header, body, now=None):
    now = now or int(time.time())
    if path.startswith("/a/login/"):
        if consume_login(path[len("/a/login/"):], now):
            sid, _ = new_session(now)
            ck = "mj_sess=%s; HttpOnly; Secure; SameSite=Strict; Path=/a; Max-Age=%d" % (sid, SESS_TTL)
            return 302, {"Location": "/a/", "Set-Cookie": ck}, b""
        return 200, {"Content-Type": "text/html; charset=utf-8"}, render_expired().encode("utf-8")
    csrf = session_csrf(cookie_sid(cookie_header), now)
    if not csrf:
        return 200, {"Content-Type": "text/html; charset=utf-8"}, render_expired().encode("utf-8")
    if method == "GET":
        if path in ("/a", "/a/"):      return _html(render_dashboard())
        if path == "/a/user":          return _html(render_user(query.get("token", [""])[0], csrf))
        if path == "/a/new":           return _html(render_new(csrf))
        if path == "/a/del":           return _html(render_delconfirm(query.get("token", [""])[0], csrf))
        return 404, {"Content-Type": "text/plain"}, b"not found"
    return route_admin_post(method, path, query, csrf, body, now)

def main():
    if not TOKEN: print("no BOT_TOKEN", flush=True); sys.exit(1)
    init_db(); tg("deleteWebhook")
    threading.Thread(target=enforcer, daemon=True).start()
    print("dpbot started; endpoints=%d" % len(ENDPOINTS), flush=True)
    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
            if offset: params["offset"] = offset
            r = tg("getUpdates", **params)
            for up in r.get("result", []):
                offset = up["update_id"] + 1
                try: handle_update(up)
                except Exception as e: print("handle err", e, flush=True)
        except Exception as e:
            print("poll err", e, flush=True); time.sleep(3)

if __name__ == "__main__":
    main()
