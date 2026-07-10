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
XRAY_SERVICE = ENV.get("XRAY_SERVICE", "xray")  # systemd unit to watch for PID changes (multi-instance safe)
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
    c = sqlite3.connect(DB_PATH, timeout=15); c.row_factory = sqlite3.Row
    # WAL so the admin panel (reader) and the enforcer/main (writers) don't deadlock
    # into "database is locked"; busy_timeout waits on writer-writer contention.
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    c.execute("PRAGMA synchronous=NORMAL")
    return c

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        token TEXT PRIMARY KEY, uuid TEXT, email TEXT UNIQUE, label TEXT,
        limit_bytes INTEGER, expiry_ts INTEGER, created_ts INTEGER,
        base_bytes INTEGER DEFAULT 0, last_raw INTEGER DEFAULT 0, used_bytes INTEGER DEFAULT 0,
        disabled_ts INTEGER DEFAULT 0, frozen INTEGER DEFAULT 0)""")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS usage_daily(
        token TEXT, day TEXT, start_used INTEGER, end_used INTEGER,
        PRIMARY KEY(token, day))""")
    cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "disabled_ts" not in cols:  # migrate existing DBs
        c.execute("ALTER TABLE users ADD COLUMN disabled_ts INTEGER DEFAULT 0")
    if "frozen" not in cols:       # manual admin freeze (independent of quota/expiry disable)
        c.execute("ALTER TABLE users ADD COLUMN frozen INTEGER DEFAULT 0")
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

# ---- online presence + force-disconnect ----
# xray's rmu blocks NEW auth but never tears down an already-established session; over
# WS/CDN that session is one long-lived cloudflared<->xray localhost socket, so a removed
# user keeps working for hours until they reconnect. We can't identify a single user's
# localhost socket (xray logs the real client IP, not the socket), so to cut a live session
# we `ss -K` the carrier sockets on the port(s) the target is using: every client there
# reconnects in ~1s, valid users re-auth instantly (still in xray, no resync), the removed
# user is blocked. Needs policy.levels.0.statsUserOnline=true (also powers the panel glow).
def xr_online_map():
    # {token: set(tags)} of users with a live session right now; None if xray can't tell.
    try:
        r = subprocess.run([XRAY_BIN, "api", "statsgetallonlineusers", "--server=%s" % XRAY_API],
                           capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout or "{}")
    except Exception as e:
        print("online-map err", e, flush=True); return None
    m = {}
    for s in (data.get("users") or []):        # each entry: "user>>>u_<token>.<tag>>>>online"
        parts = s.split(">>>")
        if len(parts) >= 2 and parts[1].startswith("u_") and "." in parts[1]:
            tok, tag = parts[1][2:].split(".", 1)
            m.setdefault(tok, set()).add(tag)
    return m

def online_tags_of(token):
    m = xr_online_map()
    return None if m is None else m.get(token, set())

def ports_to_kick(online_tags):
    # None -> unknown, reset every endpoint (safe fallback); empty -> offline, nothing;
    # non-empty -> only the ports the target is actually on (spares other protocols).
    if online_tags is None:
        return sorted({ep["port"] for ep in ENDPOINTS})
    return sorted({ep["port"] for ep in ENDPOINTS if ep["tag"] in online_tags})

def force_disconnect(online_tags):
    for p in ports_to_kick(online_tags):
        try:
            subprocess.run(["ss", "-K", "dst", "127.0.0.1", "dport", "=", ":%d" % p],
                           capture_output=True, text=True, timeout=10)
        except Exception as e:
            print("kick err", e, flush=True)

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
        return subprocess.run(["systemctl", "show", XRAY_SERVICE, "-p", "MainPID", "--value"], capture_output=True, text=True, timeout=10).stdout.strip()
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

def parse_ips(text):
    # shared IP parser (Telegram + web panel): split on commas/space/newlines, drop :port, keep valid IPv4
    toks = [t.split(":")[0].strip() for t in re.split(r"[\s,]+", (text or "").strip()) if t.strip()]
    return [t for t in toks if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", t) and all(0 <= int(o) <= 255 for o in t.split("."))]

def _ep_slots(ep):
    # ordered (port, security) slots for an endpoint: TLS ports first, then no-TLS
    return [(p, "tls") for p in ep.get("tls_ports", [])] + [(p, "none") for p in ep.get("notls_ports", [])]

def get_recipe():
    # {tag: {"enabled": bool, "count": int}} — how many configs each endpoint emits.
    # Default (no override): every endpoint on, one config per slot -> reproduces legacy output.
    rec = {ep["tag"]: {"enabled": True, "count": len(_ep_slots(ep))} for ep in ENDPOINTS}
    stored = meta_get("config_recipe")
    if stored:
        try:
            for tag, v in json.loads(stored).items():
                if tag in rec:
                    rec[tag] = {"enabled": bool(v.get("enabled", True)), "count": max(0, int(v.get("count", 0)))}
        except Exception:
            pass
    return rec

def set_recipe(recipe):
    meta_set("config_recipe", json.dumps(recipe))

def write_sub(token, secret, label):
    ips = get_ips() or DEFAULT_IPS; recipe = get_recipe(); links = []; gi = 0
    for ep in ENDPOINTS:
        r = recipe.get(ep["tag"], {"enabled": True, "count": len(_ep_slots(ep))})
        slots = _ep_slots(ep)
        if not r.get("enabled") or not slots:
            continue
        for k in range(int(r.get("count", 0))):
            port, sec = slots[k % len(slots)]
            links.append(_ws_link(ep, secret, ips[gi % len(ips)], port, sec)); gi += 1
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
        tags = online_tags_of(token)     # capture BEFORE rmu (rmu clears the online stat)
        xr_remove_user(token); del_sub(token)
        c.execute("DELETE FROM users WHERE token=?", (token,)); c.commit()
        force_disconnect(tags)           # cut the live session now (no-op if it was offline)
    c.close()

def exhaust_reason(u, now=None):
    now = now or int(time.time())
    if (u["limit_bytes"] or 0) > 0 and u["used_bytes"] >= u["limit_bytes"]: return "حجم تمام شد"
    if (u["expiry_ts"] or 0) > 0 and now >= u["expiry_ts"]: return "زمان تمام شد"
    return None

def disable_user(token):
    # exhausted: stop service (remove from xray) but KEEP the row + sub file so it can be renewed within the grace window
    tags = online_tags_of(token)     # capture BEFORE rmu
    xr_remove_user(token)
    c = db(); c.execute("UPDATE users SET disabled_ts=? WHERE token=?", (int(time.time()), token)); c.commit(); c.close()
    force_disconnect(tags)           # cut the live session so quota/expiry actually takes effect now

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

def freeze_user(token):
    # manual admin freeze: cut the link NOW and keep it out of xray until unfrozen. Fully
    # independent of the quota/expiry disable — no 48h grace, and the enforcer never touches it.
    tags = online_tags_of(token)     # capture BEFORE rmu (rmu clears the online stat)
    xr_remove_user(token)
    c = db(); c.execute("UPDATE users SET frozen=1 WHERE token=?", (token,)); c.commit(); c.close()
    force_disconnect(tags)           # drop the live session immediately

def unfreeze_user(token):
    c = db(); c.execute("UPDATE users SET frozen=0 WHERE token=?", (token,)); c.commit()
    u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    # bring it back live unless it's also quota/expiry-disabled or now exhausted
    if u and not u["disabled_ts"] and not exhaust_reason(u):
        xr_add_user(token, u["uuid"]); write_sub(token, u["uuid"], u["label"])

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
    c = db(); day = day_key(); cutoff30 = day_key(time.time() - 30 * 86400)
    total = c.execute("SELECT COALESCE(SUM(used_bytes),0) v FROM users").fetchone()["v"]
    today = c.execute("SELECT COALESCE(SUM(max(end_used-start_used,0)),0) v FROM usage_daily WHERE day=?", (day,)).fetchone()["v"]
    last30 = c.execute("SELECT COALESCE(SUM(max(end_used-start_used,0)),0) v FROM usage_daily WHERE day>=?", (cutoff30,)).fetchone()["v"]
    c.close(); return int(total), int(today), int(last30)

def resync_all():
    c = db(); rows = c.execute("SELECT token,uuid,label,disabled_ts,frozen FROM users").fetchall(); c.close()
    for u in rows:
        if u["disabled_ts"] or u["frozen"]: continue   # grace or manual freeze -> keep out of xray
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
            total, today, last30 = panel_usage_summary()
            head = ("📋 لینک‌های فعال:\n📊 مصرف کل: %s · امروز: %s\n🗓 ۳۰ روز اخیر: %s"
                    % (fmt_bytes(total), fmt_bytes(today), fmt_bytes(last30)))
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
        valid = parse_ips(text)
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
    if text.startswith("/admin"):
        tok = mint_login()
        send(chat, "🔐 لینک ورود به پنل (۱۰ دقیقه اعتبار، یک‌بار مصرف):\n<code>%s/a/login/%s</code>" % (SUB_BASE, tok)); return
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
            c = db(); rows = c.execute("SELECT token,uuid,used_bytes,limit_bytes,expiry_ts,label,disabled_ts,frozen FROM users").fetchall(); c.close()
            now = int(time.time())
            for cur in rows:
                if cur["frozen"]: continue   # manually frozen -> ignore all auto disable/grace/reenable
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
    rows = c.execute("SELECT token,label,used_bytes,limit_bytes,expiry_ts,disabled_ts,frozen,created_ts FROM users ORDER BY created_ts DESC").fetchall()
    daily = {r["token"]: int(r["v"] or 0) for r in
             c.execute("SELECT token, max(end_used-start_used,0) v FROM usage_daily WHERE day=?", (today,)).fetchall()}
    c.close()
    out = []
    for r in rows:
        d = dict(r); d["today"] = daily.get(r["token"], 0); out.append(d)
    return out

ADMIN_CSS = """
:root{--paper:#F4F1E8;--card:#FFFFFF;--ink:#111111;--accent:#FFDD2D;--ok:#2FCB74;--warn:#FFB020;--dng:#FF5A47;--frz:#3FA9F5;--mut:#6B675C;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;--sans:Tahoma,"Segoe UI",-apple-system,system-ui,sans-serif}
:root[data-theme=dark]{--paper:#16150F;--card:#211F17;--ink:#F1EEE3;--mut:#9C978B}
:root[data-theme=dark] .hero{--ink:#111111;--paper:#F4F1E8;--card:#FFFFFF;--mut:#6B675C}
:root[data-theme=dark] .btn:not(.ghost):not(.danger){color:#111111}
*{box-sizing:border-box}
html,body{margin:0;max-width:100%}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.55;padding:18px 14px 48px;-webkit-font-smoothing:antialiased}
.wrap{max-width:840px;margin:0 auto}
a{color:var(--ink);text-decoration:none}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.bar{cursor:pointer}
.tt{position:fixed;display:none;background:var(--ink);color:var(--paper);border:2px solid var(--ink);padding:5px 9px;font-family:var(--mono);font-size:12px;font-weight:700;pointer-events:none;z-index:60;box-shadow:3px 3px 0 rgba(0,0,0,.28)}
.n{unicode-bidi:isolate;direction:ltr}
.eyebrow{display:inline-block;font-size:11px;font-weight:800;background:var(--ink);color:var(--paper);padding:3px 8px;margin-bottom:10px}
.top{display:flex;align-items:center;gap:10px;margin:0 2px 20px}
.brand{display:flex;align-items:center;gap:9px;font-weight:800;font-size:19px}
.dot-sig{width:15px;height:15px;background:var(--accent);border:2px solid var(--ink)}
.crumb{font-size:13px;font-weight:800}
.rightnav{margin-inline-start:auto;display:flex;align-items:center;gap:10px}
.card{background:var(--card);border:3px solid var(--ink);box-shadow:5px 5px 0 var(--ink);padding:16px;margin:0 0 18px}
.card h2{margin:0 0 12px;font-size:12px;color:var(--ink);font-weight:800}
.hero{background:var(--accent);color:var(--ink)}
.big{font-family:var(--mono);font-size:44px;font-weight:800;line-height:1.02}
.big small{font-family:var(--sans);font-size:15px;font-weight:700;margin-inline-start:6px}
.title{font-size:24px;font-weight:800;margin:2px 0}
.metrics{display:flex;gap:20px;flex-wrap:wrap;margin-top:12px}
.metric .k{font-size:11px;color:var(--ink);font-weight:800}
.metric .v{font-size:18px;font-weight:800;margin-top:3px}
.pills{display:flex;gap:8px;margin-top:14px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;color:var(--ink);background:var(--card);border:2px solid var(--ink);padding:4px 10px}
.d{width:9px;height:9px;border:2px solid var(--ink)}
.d.ok{background:var(--ok)}
.d.off{background:var(--paper)}
.chart{margin-top:16px}
svg{display:block;width:100%;color:var(--ink)}
.u{display:grid;grid-template-columns:14px 1fr 72px 84px;align-items:center;gap:10px;padding:12px;border:2px solid var(--ink);background:var(--card);color:var(--ink);margin-top:10px}
.u:first-of-type{margin-top:0}
.u:hover{transform:translate(-2px,-2px);box-shadow:4px 4px 0 var(--ink)}
.st{width:12px;height:12px;border:2px solid var(--ink);flex:0 0 auto}
.st.ok{background:var(--ok);color:var(--ok)}
.st.warn{background:var(--warn);color:var(--warn)}
.st.dng{background:var(--dng);color:var(--dng)}
.st.off{background:var(--paper);color:var(--mut)}
.st.frz{background:var(--frz);color:var(--frz)}
.st.on{filter:saturate(1.45) brightness(1.12);animation:stglow 1.5s ease-in-out infinite}
@keyframes stglow{0%,100%{box-shadow:0 0 3px 0 currentColor}50%{box-shadow:0 0 9px 2px currentColor}}
@media (prefers-reduced-motion:reduce){.st.on{animation:none;box-shadow:0 0 7px 1px currentColor}}
.switch{display:flex;align-items:center;gap:12px;cursor:pointer;user-select:none}
.switch input{position:absolute;opacity:0;width:0;height:0}
.switch .knob{position:relative;flex:0 0 auto;width:48px;height:28px;background:var(--paper);border:2px solid var(--ink);border-radius:0;transition:background .15s}
.switch .knob::after{content:"";position:absolute;top:2px;inset-inline-start:2px;width:20px;height:20px;background:var(--ink);transition:inset-inline-start .15s}
.switch input:checked+.knob{background:var(--frz)}
.switch input:checked+.knob::after{inset-inline-start:22px}
.switch input:focus-visible+.knob{outline:2px solid var(--frz);outline-offset:2px}
.switch .swtxt{display:flex;flex-direction:column;line-height:1.35}
.switch .swsub{font-size:12px;color:var(--mut)}
.switch.on .swtxt b{color:var(--frz)}
.nm{flex:1 1 auto;min-width:0;display:flex;flex-direction:column}
.nm b{font-weight:800;font-size:14px}
.nm .sub{font-size:11px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono)}
.meter{min-width:0}
.trk{display:block;height:10px;background:var(--card);border:2px solid var(--ink);overflow:hidden}
.fil{display:block;height:100%;background:var(--ink)}
.rt{font-size:12px;color:var(--mut);text-align:end;min-width:0;line-height:1.3;font-weight:700}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;font-family:inherit;font-size:13px;font-weight:800;border:3px solid var(--ink);padding:9px 14px;cursor:pointer;text-decoration:none;color:var(--ink);background:var(--accent);box-shadow:3px 3px 0 var(--ink);transition:transform .06s,box-shadow .06s}
.btn:hover{transform:translate(-1px,-1px);box-shadow:4px 4px 0 var(--ink)}
.btn:active{transform:translate(3px,3px);box-shadow:0 0 0 var(--ink)}
.btn.ghost{background:var(--card)}
.btn.danger{background:var(--dng);color:#fff}
.tbtn{display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;padding:0;font-size:17px;line-height:1;border:3px solid var(--ink);background:var(--card);color:var(--ink);cursor:pointer;box-shadow:3px 3px 0 var(--ink);transition:transform .06s,box-shadow .06s}
.tbtn:hover{transform:translate(-1px,-1px);box-shadow:4px 4px 0 var(--ink)}
.tbtn:active{transform:translate(3px,3px);box-shadow:0 0 0 var(--ink)}
input[type=text],input[type=number],textarea{background:var(--card);border:3px solid var(--ink);color:var(--ink);border-radius:0;padding:9px 11px;font-family:inherit;font-size:14px;flex:1 1 130px;min-width:0;max-width:280px;outline:none}
input[type=number]{max-width:96px}
textarea{width:100%;max-width:100%;font-family:var(--mono);resize:vertical}
input::placeholder,textarea::placeholder{color:#9a958a}
input:focus,textarea:focus{box-shadow:3px 3px 0 var(--accent)}
input[type=checkbox]{width:20px;height:20px;accent-color:var(--ink);flex:0 0 auto}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
form.row{margin:0 0 8px}
.grid{display:grid;gap:10px}
.eprow{display:flex;align-items:center;gap:10px;justify-content:space-between;border:2px solid var(--ink);padding:10px 12px;background:var(--card)}
.eplabel{display:flex;align-items:center;gap:10px;flex:1;min-width:0;cursor:pointer}
.eplabel b{font-weight:800}
.eptag{display:block;font-size:11px;color:var(--mut);font-family:var(--mono);margin-top:2px}
.hint{font-size:12px;color:var(--mut);font-weight:700;margin:0 0 6px}
code{font-family:var(--mono);background:var(--paper);border:2px solid var(--ink);padding:6px 8px;word-break:break-all;font-size:12px;color:var(--ink);display:block}
:focus-visible{outline:3px solid var(--ink);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

def _page(title, inner):
    return ("<!doctype html><html lang=fa dir=rtl><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta name=color-scheme content='light dark'><title>%s</title>"
            "<script>(function(){try{var t=localStorage.getItem('mj-theme')||((window.matchMedia&&matchMedia('(prefers-color-scheme:dark)').matches)?'dark':'light');document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>"
            "<style>%s</style></head><body><div class=wrap>%s</div><div id=tt class=tt></div>"
            "<script>function toggleTheme(){var h=document.documentElement,d=h.getAttribute('data-theme')==='dark'?'light':'dark';h.setAttribute('data-theme',d);try{localStorage.setItem('mj-theme',d);}catch(e){}_syncTheme();}"
            "function _syncTheme(){var b=document.getElementById('themebtn');if(b)b.textContent=document.documentElement.getAttribute('data-theme')==='dark'?'☀️':'🌙';}_syncTheme();"
            "(function(){var t=document.getElementById('tt');document.addEventListener('click',function(e){"
            "var b=e.target.closest&&e.target.closest('.bar');if(b){t.textContent=b.getAttribute('data-t')+' — '+b.getAttribute('data-v');"
            "t.style.display='block';var w=t.offsetWidth;t.style.left=Math.max(6,Math.min(e.clientX-w/2,window.innerWidth-w-6))+'px';"
            "t.style.top=Math.max(6,e.clientY-40)+'px';}else{t.style.display='none';}});})();</script>"
            "</body></html>" % (html.escape(title), ADMIN_CSS, inner))

def _html(page):
    return 200, {"Content-Type": "text/html; charset=utf-8"}, page.encode("utf-8")

def _top(crumb="", csrf=None):
    right = ("<span class=crumb>%s</span>" % crumb) if crumb else ""
    if csrf:
        right += ("<form method=post action='/a/logout' style='margin:0'>"
                  "<input type=hidden name=csrf value='%s'>"
                  "<button class='btn ghost'>خروج</button></form>") % csrf
    right += ("<button id=themebtn type=button class=tbtn onclick=\"toggleTheme()\" "
              "aria-label='تغییر تم' title='تغییر تم'>🌙</button>")
    return ("<header class=top><span class=brand><span class=dot-sig></span>Mohajer</span>"
            "<span class=rightnav>%s</span></header>" % right)

def _metric_big(b):
    s = fmt_bytes(b); p = s.rsplit(" ", 1)
    return ("%s<small>%s</small>" % (p[0], p[1])) if len(p) == 2 else s

def svg_bars(series, w=760, h=96):
    vals = [v for _, v in series]; mx = max(vals + [1]); n = len(series) or 1; bw = w / n; bars = ""
    for i, (lab, v) in enumerate(series):
        bh = max(3.0, (v / mx) * (h - 6)); val = fmt_bytes(v)
        bars += ('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="currentColor" opacity="%s"></rect>'
                 ) % (i * bw + 3, h - bh, max(3.0, bw - 6), bh, ("1" if v > 0 else "0.18"))
        # full-height transparent hit target -> tappable on touch; carries the value for the tooltip
        bars += ('<rect class="bar" x="%.1f" y="0" width="%.1f" height="%d" fill="transparent" data-t="%s" data-v="%s">'
                 '<title>%s: %s</title></rect>') % (i * bw, bw, h, html.escape(lab), html.escape(val), html.escape(lab), val)
    return ('<svg viewBox="0 0 %d %d" width="100%%" height="%d" preserveAspectRatio="none" role="img" aria-label="نمودار مصرف">'
            '%s</svg>') % (w, h, h, bars)

def render_expired():
    return _page("منقضی", _top() + "<div class='card hero' style='text-align:center;padding:34px 18px'>"
                 "<div class=eyebrow>دسترسی</div><div class=title>لینک منقضی شد</div>"
                 "<p style='color:var(--mut);margin:6px 0 0'>برای ورود دوباره، در ربات دستور <code>/admin</code> را بزن.</p></div>")

def render_loggedout():
    return _page("خروج", _top() + "<div class='card hero' style='text-align:center;padding:34px 18px'>"
                 "<div class=eyebrow>خروج</div><div class=title>با موفقیت خارج شدی</div>"
                 "<p style='color:var(--mut);margin:6px 0 0'>برای ورودِ دوباره، در ربات دستور <code>/admin</code> را بزن.</p></div>")

def _user_row(u, online=False):
    lim = u["limit_bytes"] or 0; used = u["used_bytes"] or 0; dis = u["disabled_ts"]
    if lim > 0:
        pct = min(100, int(used * 100 / lim))
        st = "dng" if dis else ("warn" if pct >= 90 else "ok")
        fill = "<span class=fil style='width:%d%%'></span>" % pct
    else:
        st = "off" if dis else "ok"
        fill = "<span class=fil style='width:100%;opacity:.3'></span>"
    if u["frozen"]: st = "frz"          # manual freeze -> icy square, overrides quota colour
    if online and not u["frozen"]: st += " on"   # live now -> pulsing halo in the square's own colour
    return ("<a class=u href='/a/user?token=%s'><span class='st %s'></span>"
            "<span class=nm><b>%s</b><span class=sub><span class=n>%s / %s</span></span></span>"
            "<span class=meter><span class=trk>%s</span></span>"
            "<span class=rt><span class=n>%s</span><br>%s</span></a>") % (
        u["token"], st, html.escape(u["label"]),
        fmt_bytes(used), human_limit(lim), fill,
        fmt_bytes(u["today"]), human_expiry(u["expiry_ts"]))

def render_dashboard(csrf):
    total, today, last30 = panel_usage_summary()
    ov = users_overview()
    active = sum(1 for u in ov if not u["disabled_ts"]); disabled = len(ov) - active
    chart = svg_bars(daily_series(7))
    hero = ("<div class='card hero'><div class=eyebrow>مصرف امروز</div><div class=big><span class=n>%s</span></div>"
            "<div class=metrics><div class=metric><div class=k>کل</div><div class='v mono'><span class=n>%s</span></div></div>"
            "<div class=metric><div class=k>۳۰ روز اخیر</div><div class='v mono'><span class=n>%s</span></div></div></div>"
            "<div class=pills><span class=pill><span class='d ok'></span>%d فعال</span>"
            "<span class=pill><span class='d off'></span>%d غیرفعال</span></div>"
            "<div class=chart><div class=eyebrow>۷ روز اخیر</div>%s</div></div>") % (
        _metric_big(today), fmt_bytes(total), fmt_bytes(last30), active, disabled, chart)
    onmap = xr_online_map() or {}
    rows = "".join(_user_row(u, u["token"] in onmap) for u in ov) or "<div class=u><span class=nm style='color:var(--mut)'>هنوز لینکی نساخته‌ای</span></div>"
    users = ("<div class=card><div class=row style='justify-content:space-between;margin-bottom:8px'>"
             "<h2 style='margin:0'>لینک‌ها</h2><span class=row>"
             "<a class='btn ghost' href='/a/config'>⚙ پیکربندی</a>"
             "<a class=btn href='/a/new'>+ لینک جدید</a></span></div>%s</div>") % rows
    return _page("پنل", _top("", csrf) + hero + users)

def _form(action, fields, csrf, btn, cls="btn"):
    inner = "".join(fields) + "<input type=hidden name=csrf value='%s'>" % csrf
    return "<form method=post action='%s' class=row>%s<button class='%s'>%s</button></form>" % (action, inner, cls, btn)

def render_user(token, csrf):
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if not u:
        return _page("یافت نشد", _top("<a href='/a/'>← داشبورد</a>", csrf) + "<div class=card>لینکی با این شناسه پیدا نشد.</div>")
    today_u = daily_series(1, token=token)[-1][1]
    chart = svg_bars(daily_series(30, token=token))
    dis = bool(u["disabled_ts"]); frz = bool(u["frozen"])
    st = "frz" if frz else ("dng" if dis else "ok")
    stlabel = "فریز موقت" if frz else ("غیرفعال" if dis else "فعال")
    tk = "<input type=hidden name=token value='%s'>" % token
    frz_card = (
        "<div class=card><form method=post action='/a/freeze' id=frzf style='margin:0'>%s"
        "<input type=hidden name=csrf value='%s'>"
        "<label class='switch%s'>"
        "<input type=checkbox name=on onchange=\"document.getElementById('frzf').submit()\"%s>"
        "<span class=knob></span>"
        "<span class=swtxt><b>فریز موقت</b><span class=swsub>%s</span></span></label>"
        "</form></div>") % (
        tk, csrf, (" on" if frz else ""), (" checked" if frz else ""),
        ("اتصال قطع است — برای فعال‌سازیِ دوباره تیک را بردار" if frz
         else "با زدنِ تیک، این کانفیگ فوراً قطع و تا برداشتنِ تیک غیرفعال می‌ماند"))
    forms = (
        _form("/a/addvol", [tk, "<input type=number name=gb placeholder='حجم (گیگ)'>"], csrf, "افزودن حجم") +
        _form("/a/addtime", [tk, "<input type=number name=days placeholder='مدت (روز)'>"], csrf, "افزودن زمان") +
        _form("/a/rename", [tk, "<input type=text name=name placeholder='نام تازه'>"], csrf, "تغییر نام") +
        _form("/a/unlimit", [tk, "<input type=hidden name=field value=limit_bytes>"], csrf, "حجم نامحدود", "btn ghost") +
        _form("/a/unlimit", [tk, "<input type=hidden name=field value=expiry_ts>"], csrf, "زمان نامحدود", "btn ghost"))
    dele = "<a class='btn danger' href='/a/del?token=%s'>حذف لینک</a>" % token
    hero = ("<div class='card hero'><div class=eyebrow><span class='st %s' style='display:inline-block;margin-inline-start:6px;vertical-align:middle'></span>%s</div>"
            "<div class=title>%s</div>"
            "<div class=metrics><div class=metric><div class=k>مصرف</div><div class=v><span class=n>%s / %s</span></div></div>"
            "<div class=metric><div class=k>امروز</div><div class='v mono'><span class=n>%s</span></div></div>"
            "<div class=metric><div class=k>انقضا</div><div class=v>%s</div></div></div>"
            "<div class=chart><div class=eyebrow>۳۰ روز اخیر</div>%s</div></div>") % (
        st, stlabel, html.escape(u["label"]),
        fmt_bytes(u["used_bytes"]), human_limit(u["limit_bytes"]), fmt_bytes(today_u),
        human_expiry(u["expiry_ts"]), chart)
    link = "<div class=card><h2>لینک اشتراک</h2><code>%s</code></div>" % sub_url(token)
    actions = "<div class=card><h2>مدیریت</h2><div class=grid>%s</div><div style='margin-top:10px'>%s</div></div>" % (forms, dele)
    return _page("کاربر", _top("<a href='/a/'>← داشبورد</a>", csrf) + hero + frz_card + link + actions)

def render_new(csrf):
    f = _form("/a/new", ["<input type=number name=gb placeholder='حجم (گیگ) — ۰ = نامحدود'>",
                         "<input type=number name=days placeholder='مدت (روز) — ۰ = نامحدود'>",
                         "<input type=text name=name placeholder='نام مشتری'>"], csrf, "ساخت لینک")
    return _page("لینک جدید", _top("<a href='/a/'>← داشبورد</a>", csrf) +
                 "<div class=card><h2>لینک جدید</h2>%s</div>" % f)

def render_delconfirm(token, csrf):
    f = _form("/a/delete", ["<input type=hidden name=token value='%s'>" % token,
                            "<input type=hidden name=confirm value=yes>"], csrf, "بله، حذف کن", "btn danger")
    return _page("حذف", _top("<a href='/a/user?token=%s'>← بازگشت</a>" % token, csrf) +
                 "<div class=card><h2>حذف لینک</h2><p style='color:var(--mut);margin:0 0 12px'>"
                 "این کار برگشت‌ناپذیر است؛ لینک و کانفیگ‌های این مشتری حذف می‌شوند.</p>"
                 "<div class=row>%s<a class='btn ghost' href='/a/user?token=%s'>انصراف</a></div></div>" % (f, token))

def render_config(csrf):
    recipe = get_recipe(); ips = get_ips(); rows = ""
    for ep in ENDPOINTS:
        tag = ep["tag"]; r = recipe.get(tag, {"enabled": True, "count": 0}); nports = len(_ep_slots(ep))
        rows += ("<div class=eprow>"
                 "<label class=eplabel><input type=checkbox name='en_%s'%s>"
                 "<span><b>%s</b><span class=eptag>%s · %d پورت</span></span></label>"
                 "<input type=number name='cnt_%s' value='%d' min=0 aria-label='تعداد %s'>"
                 "</div>") % (tag, (" checked" if r["enabled"] else ""), html.escape(ep.get("label", tag)),
                              html.escape(tag), nports, tag, r["count"], html.escape(tag))
    body = ("<form method=post action='/a/config' class=grid>"
            "<h2>نوع و تعداد کانفیگ‌ها</h2>"
            "<p class=hint>تعداد سقفی ندارد؛ بیشتر از تعداد پورت، روی آی‌پی‌های تمیز پخش می‌شود.</p>%s"
            "<h2 style='margin-top:16px'>آی‌پی‌های تمیز کلادفلر</h2>"
            "<textarea name=ips rows=4 placeholder='104.16.96.1, 104.21.96.1'>%s</textarea>"
            "<input type=hidden name=csrf value='%s'>"
            "<button class=btn style='margin-top:12px'>ذخیره و بازتولیدِ همه لینک‌ها</button></form>") % (
        rows, html.escape("\n".join(ips)), csrf)
    return _page("پیکربندی", _top("<a href='/a/'>← داشبورد</a>", csrf) + "<div class=card>%s</div>" % body)

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
        if path in ("/a", "/a/"):      return _html(render_dashboard(csrf))
        if path == "/a/user":          return _html(render_user(query.get("token", [""])[0], csrf))
        if path == "/a/new":           return _html(render_new(csrf))
        if path == "/a/config":        return _html(render_config(csrf))
        if path == "/a/del":           return _html(render_delconfirm(query.get("token", [""])[0], csrf))
        return 404, {"Content-Type": "text/plain"}, b"not found"
    return route_admin_post(method, path, query, csrf, body, now, cookie_sid(cookie_header))

def _redirect(loc):
    return 302, {"Location": loc}, b""

def route_admin_post(method, path, query, csrf, body, now, sid):
    form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "ignore")).items()}
    if form.get("csrf") != csrf:
        return 403, {"Content-Type": "text/plain"}, b"forbidden"
    if path == "/a/logout":
        _sessions.pop(sid, None)
        ck = "mj_sess=; HttpOnly; Secure; SameSite=Strict; Path=/a; Max-Age=0"
        return 200, {"Content-Type": "text/html; charset=utf-8", "Set-Cookie": ck}, render_loggedout().encode("utf-8")
    token = form.get("token", "")
    def _num(x, cast):
        try: return cast(str(x).replace(",", "."))
        except Exception: return None
    if path == "/a/addvol":
        gb = _num(form.get("gb"), float)
        if gb: extend_volume(token, gb)
        refresh_usage(token); maybe_reenable(token); return _redirect("/a/user?token=" + token)
    if path == "/a/addtime":
        days = _num(form.get("days"), int)
        if days: extend_time(token, days)
        maybe_reenable(token); return _redirect("/a/user?token=" + token)
    if path == "/a/rename":
        name = (form.get("name") or "").strip()[:40]
        if name:
            c = db(); c.execute("UPDATE users SET label=? WHERE token=?", (name, token)); c.commit(); c.close()
        return _redirect("/a/user?token=" + token)
    if path == "/a/unlimit":
        field = form.get("field")
        if field in ("limit_bytes", "expiry_ts"): set_unlimited(token, field)
        maybe_reenable(token); return _redirect("/a/user?token=" + token)
    if path == "/a/freeze":
        if token:
            if form.get("on"): freeze_user(token)     # checkbox ticked -> cut now
            else:              unfreeze_user(token)    # ticked off -> bring back live
        return _redirect("/a/user?token=" + token)
    if path == "/a/delete":
        if form.get("confirm") == "yes" and token: delete_user(token)
        return _redirect("/a/")
    if path == "/a/new":
        gb = _num(form.get("gb"), float) or 0
        days = _num(form.get("days"), int) or 0
        name = (form.get("name") or "").strip()[:40] or None
        create_user(gb, days, label=name)
        return _redirect("/a/")
    if path == "/a/config":
        recipe = {ep["tag"]: {"enabled": form.get("en_" + ep["tag"]) is not None,
                              "count": max(0, _num(form.get("cnt_" + ep["tag"]), int) or 0)}
                  for ep in ENDPOINTS}
        set_recipe(recipe)
        ips = parse_ips(form.get("ips", ""))
        if ips: set_ips(ips)
        regenerate_all_subs()
        return _redirect("/a/config")
    return 404, {"Content-Type": "text/plain"}, b"not found"

class AdminHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _run(self, method):
        u = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(u.query)
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        cookie = self.headers.get("Cookie", "")
        try:
            status, headers, out = route_admin(method, u.path, query, cookie, body)
        except Exception as e:
            print("admin err", e, flush=True)
            status, headers, out = 500, {"Content-Type": "text/plain"}, b"error"
        self.send_response(status)
        for k, v in headers.items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        if out: self.wfile.write(out)
    def do_GET(self):  self._run("GET")
    def do_POST(self): self._run("POST")

def admin_server():
    ThreadingHTTPServer(("127.0.0.1", ADMIN_PORT), AdminHandler).serve_forever()

def main():
    if not TOKEN: print("no BOT_TOKEN", flush=True); sys.exit(1)
    init_db(); tg("deleteWebhook")
    threading.Thread(target=enforcer, daemon=True).start()
    threading.Thread(target=admin_server, daemon=True).start()
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
