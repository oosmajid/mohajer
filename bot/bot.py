#!/usr/bin/env python3
# Mohajer - stdlib-only Telegram admin panel (no pip deps). Mints multi-protocol sub links:
# VLESS/VMess/Trojan over WebSocket (TLS + no-TLS, fronted by Cloudflare) + VLESS-XHTTP.
# Per-user quota + expiry are enforced live via `xray api adu/rmu/statsquery` (no xray restart).
# All config comes from bot.env (see config/bot.env.example). Single-file, runs under systemd.
import os, re, sys, json, time, html, base64, socket, sqlite3, secrets, threading, subprocess, math, tempfile, ipaddress
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
# Optional direct (non-Cloudflare) VLESS-REALITY endpoint, one per server. JSON with:
#   port  = inbound port xray listens on (0.0.0.0) on this box
#   addr / ext_port = the public IP:port clients dial (differs from port behind NAT)
#   pbk / priv / sid / sni / fp / flow = REALITY keys + camouflage target
# The inbound itself must already exist in xray (added once by the provisioning step).
# It shows up in /a/config like any endpoint; its default count is 0 = no REALITY links.
def _reality_endpoint(raw):
    try:
        r = json.loads(raw) if raw else None
    except Exception:
        return None
    if not isinstance(r, dict) or not all(r.get(k) for k in ("port", "addr", "pbk", "priv", "sni")):
        return None
    return {"tag": r.get("tag", "vless-reality"), "proto": "vless", "net": "tcp", "port": int(r["port"]),
            "label": "REALITY", "reality": {"addr": r["addr"], "port": int(r.get("ext_port") or r["port"]),
            "pbk": r["pbk"], "priv": r["priv"], "sni": r["sni"], "sid": r.get("sid", ""),
            "fp": r.get("fp", "chrome"), "flow": r.get("flow", "xtls-rprx-vision")}}
_REALITY_EP = _reality_endpoint(ENV.get("REALITY", ""))
if _REALITY_EP and not any(ep.get("tag") == _REALITY_EP["tag"] for ep in ENDPOINTS):
    ENDPOINTS.append(_REALITY_EP)
# Xray "finalmask" fragment added to TLS vless/trojan links as the `fm=` share-link
# param (read by v2rayNG/v2rayN). Splitting the first 1-3 writes hides the SNI from DPI
# that filters our domain; tlshello-only fragmenting does NOT get past it. vmess links
# can't carry it and noTLS doesn't benefit. Empty string disables it.
FRAGMENT_FM = ENV.get("FRAGMENT_FM", json.dumps(
    {"tcp": [{"type": "fragment", "settings": {"packets": "1-3", "length": "100-200", "delay": "10-20"}}]},
    separators=(",", ":")))
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
        usage_reset_bytes INTEGER DEFAULT 0,
        disabled_ts INTEGER DEFAULT 0, frozen INTEGER DEFAULT 0,
        config_override TEXT, credential_mode TEXT DEFAULT 'legacy', active_slots TEXT,
        rebase_floor INTEGER, usage_anchor INTEGER,
        auth_pending INTEGER DEFAULT 0, pending_delete INTEGER DEFAULT 0)""")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS usage_daily(
        token TEXT, day TEXT, start_used INTEGER, end_used INTEGER,
        PRIMARY KEY(token, day))""")
    cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "disabled_ts" not in cols:  # migrate existing DBs
        c.execute("ALTER TABLE users ADD COLUMN disabled_ts INTEGER DEFAULT 0")
    if "frozen" not in cols:       # manual admin freeze (independent of quota/expiry disable)
        c.execute("ALTER TABLE users ADD COLUMN frozen INTEGER DEFAULT 0")
    if "usage_reset_bytes" not in cols:  # lifetime usage at the last admin-visible reset
        c.execute("ALTER TABLE users ADD COLUMN usage_reset_bytes INTEGER DEFAULT 0")
    if "config_override" not in cols:  # NULL means this link follows the global settings
        c.execute("ALTER TABLE users ADD COLUMN config_override TEXT")
    if "credential_mode" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN credential_mode TEXT DEFAULT 'legacy'")
    if "active_slots" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN active_slots TEXT")
    if "rebase_floor" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN rebase_floor INTEGER")
    if "usage_anchor" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN usage_anchor INTEGER")
    if "auth_pending" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN auth_pending INTEGER DEFAULT 0")
    if "pending_delete" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN pending_delete INTEGER DEFAULT 0")
    c.execute("""CREATE TABLE IF NOT EXISTS slot_credentials(
        token TEXT NOT NULL, tag TEXT NOT NULL, security TEXT NOT NULL, external_port INTEGER NOT NULL,
        email TEXT NOT NULL UNIQUE, secret TEXT NOT NULL,
        PRIMARY KEY(token, tag, security, external_port))""")
    c.execute("""CREATE TABLE IF NOT EXISTS slot_mode_tags(
        token TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY(token,tag))""")
    c.execute("""CREATE TABLE IF NOT EXISTS legacy_emails(
        token TEXT NOT NULL, tag TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
        PRIMARY KEY(token,tag))""")
    c.execute("""CREATE TABLE IF NOT EXISTS usage_ledger(
        email TEXT PRIMARY KEY, token TEXT NOT NULL,
        last_raw INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER NOT NULL DEFAULT 0)""")
    # retired_bytes = lifetime traffic of already-deleted users, so deleting a user never
    # drops the dashboard's "total". Backfill ONCE from any daily rows left by past deletes
    # (their last recorded end_used ≈ their lifetime at deletion) so the count stays honest.
    if c.execute("SELECT v FROM meta WHERE k='retired_bytes'").fetchone() is None:
        orphan = c.execute(
            "SELECT COALESCE(SUM(mx),0) s FROM (SELECT MAX(end_used) mx FROM usage_daily "
            "WHERE token NOT IN (SELECT token FROM users) GROUP BY token)").fetchone()["s"]
        c.execute("INSERT INTO meta(k,v) VALUES('retired_bytes',?)", (str(int(orphan or 0)),))
    c.commit(); c.close()
    # Remember the pre-edit slots for legacy links. A later settings change can then
    # detect a removed port even though the old credential has no per-port identity.
    _snapshot_missing_active_slots()

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

def legacy_email(token, tag):
    c = db(); row = c.execute("SELECT email FROM legacy_emails WHERE token=? AND tag=?", (token, tag)).fetchone(); c.close()
    return row["email"] if row else ep_email(token, tag)

def _rotate_legacy_emails(token):
    """Use new Xray stats identities after a freeze or quota renewal."""
    c = db(); u = c.execute("SELECT credential_mode,active_slots FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if not u or u["credential_mode"] == "slots": return
    slots = _decode_slots(u["active_slots"]) if u["active_slots"] is not None else active_slot_keys(token)
    tags = {tag for tag, _, _ in slots} - slot_mode_tags(token)
    if not tags: return
    c = db()
    for tag in tags:
        email = "%s.g%s" % (ep_email(token, tag), secrets.token_hex(5))
        c.execute("INSERT INTO legacy_emails(token,tag,email) VALUES(?,?,?) "
                  "ON CONFLICT(token,tag) DO UPDATE SET email=excluded.email", (token, tag, email))
    c.commit(); c.close()

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
    fd, f = tempfile.mkstemp(prefix="dpbot_adu_", suffix=".json")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump({"inbounds": [ib]}, out)
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
    slots = active_slot_keys(token)
    converted = slot_mode_tags(token)
    mode = credential_mode(token)
    for ep in ENDPOINTS:
        ep_slots = sorted(s for s in slots if s[0] == ep["tag"])
        if not ep_slots: continue
        if mode == "slots" or ep["tag"] in converted:
            for tag, security, port in ep_slots:
                cred = ensure_slot_credential(token, ep, security, port)
                if not _adu(ep, cred["secret"], cred["email"]): ok = False
        elif not _adu(ep, secret, legacy_email(token, ep["tag"])):
            ok = False
    return ok

def _rmu_email(tag, email):
    try:
        r = subprocess.run([XRAY_BIN, "api", "rmu", "--server=%s" % XRAY_API, "-tag=%s" % tag, email],
                           capture_output=True, text=True, timeout=15)
        # Removing an already-absent user is idempotent. Xray versions differ in how
        # they report it; an API failure is still surfaced to the caller.
        out = (r.stdout + r.stderr).lower()
        return r.returncode == 0 or "not found" in out or "no such user" in out
    except Exception as e:
        print("rmu err", e, flush=True)
        return False

def _rmu(token, tag):
    return _rmu_email(tag, legacy_email(token, tag))

def xr_remove_user(token):
    # Keep credentials in SQLite for renewal; delete_user forgets them separately.
    c = db()
    creds = c.execute("SELECT tag,email FROM slot_credentials WHERE token=?", (token,)).fetchall()
    user = c.execute("SELECT credential_mode,active_slots FROM users WHERE token=?", (token,)).fetchone()
    c.close()
    converted = slot_mode_tags(token)
    old_slots = (_decode_slots(user["active_slots"]) if user and user["active_slots"] is not None
                 else active_slot_keys(token))
    legacy_tags = ({tag for tag, _, _ in old_slots} - converted
                   if user and user["credential_mode"] != "slots" else set())
    if legacy_tags: _enable_usage_ledger(token)
    refresh_usage(token)
    ok = True
    for cred in creds:
        if not _rmu_email(cred["tag"], cred["email"]): ok = False
    for ep in ENDPOINTS:
        if ep["tag"] in legacy_tags and not _rmu(token, ep["tag"]): ok = False
    refresh_usage(token)
    return ok

def _emergency_clear_dynamic_users():
    """Restart the existing Xray service when API revocation could not finish."""
    now = int(time.time())
    if now - int(meta_get("last_emergency_restart_ts", "0") or 0) < 120:
        return False
    meta_set("last_emergency_restart_ts", str(now))
    try:
        result = subprocess.run(["systemctl", "restart", XRAY_SERVICE],
                                capture_output=True, text=True, timeout=30)
    except Exception as exc:
        print("emergency xray restart failed:", exc, flush=True)
        return False
    if result.returncode != 0:
        print("emergency xray restart failed:", (result.stderr or result.stdout)[-400:], flush=True)
        return False
    # The restarted process has no dynamic users. Restore only eligible links;
    # pending delete, frozen, and disabled links are excluded by resync_all.
    try:
        resync_all(allow_missing_stats_restart=False)
    except Exception as exc:
        meta_set("membership_sync_pending", "1")
        print("emergency xray resync pending:", exc, flush=True)
    return True

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
            tok, rest = parts[1][2:].split(".", 1)
            # New per-slot emails append .tls443.<generation> (or .none80...).
            # Map them back to their provisioned inbound tag for socket cutoff.
            matched = False
            for ep in sorted(ENDPOINTS, key=lambda item: len(item["tag"]), reverse=True):
                tag = ep["tag"]
                if rest == tag or rest.startswith(tag + "."):
                    m.setdefault(tok, set()).add(tag)
                    matched = True
                    break
            if not matched and "." not in rest:
                m.setdefault(tok, set()).add(rest)
    return m

def online_tags_of(token):
    m = xr_online_map()
    return None if m is None else m.get(token, set())

def tags_to_cut_for_user(token):
    # Online presence is best effort: some Xray versions omit loopback-origin
    # cloudflared sockets. Use the subscribed inbound tags as the safe fallback.
    online = online_tags_of(token)
    if online: return online
    c = db(); u = c.execute("SELECT active_slots FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if u and u["active_slots"] is not None:
        return {tag for tag, _, _ in _decode_slots(u["active_slots"])}
    return {tag for tag, _, _ in active_slot_keys(token)}

def ports_to_kick(online_tags):
    # None -> unknown, reset every endpoint (safe fallback); empty -> offline, nothing;
    # non-empty -> only the ports the target is actually on (spares other protocols).
    if online_tags is None:
        return sorted({ep["port"] for ep in ENDPOINTS})
    return sorted({ep["port"] for ep in ENDPOINTS if ep["tag"] in online_tags})

def force_disconnect(online_tags):
    for p in ports_to_kick(online_tags):
        try:
            # Cloudflare transports arrive on loopback; direct REALITY connections
            # instead have the inbound port as their local source port.
            direct = any(ep["port"] == p and "reality" in ep for ep in ENDPOINTS)
            selector = (["sport", "=", ":%d" % p] if direct else
                        ["dst", "127.0.0.1", "dport", "=", ":%d" % p])
            subprocess.run(["ss", "-K"] + selector,
                           capture_output=True, text=True, timeout=10)
        except Exception as e:
            print("kick err", e, flush=True)

def xr_usage(token):
    # returns bytes, or None if the stats read FAILED. Never return 0 on failure:
    # a failed read must not be mistaken for "counter reset to 0" (see refresh_usage).
    result = _stable_statsquery("user>>>u_%s" % token, 15)
    if result is None: return None
    r, pid = result
    try:
        d = json.loads(r.stdout or "{}")
    except Exception:
        return None
    return UsageValue(sum(int(s.get("value", 0)) for s in (d.get("stat") or [])), pid)

class UsageValue(int):
    def __new__(cls, value, epoch_pid):
        item = int.__new__(cls, value)
        item.epoch_pid = epoch_pid
        return item

def xray_pid():
    try:
        return subprocess.run(["systemctl", "show", XRAY_SERVICE, "-p", "MainPID", "--value"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""

def begin_xray_counter_epoch(pid=None):
    """Bank old counters once when the running Xray process changes."""
    pid = xray_pid() if pid is None else pid
    if not pid or pid == "0": return False
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT v FROM meta WHERE k='xray_pid'").fetchone()
        prior = row["v"] if row else None
        changed = bool(prior and prior != pid)
        if changed:
            c.execute("UPDATE users SET base_bytes=COALESCE(base_bytes,0)+COALESCE(last_raw,0),last_raw=0 "
                      "WHERE usage_anchor IS NULL")
            c.execute("UPDATE usage_ledger SET last_raw=0")
        c.execute("INSERT INTO meta(k,v) VALUES('xray_pid',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (pid,))
        c.commit()
        return changed
    finally:
        c.close()

def _stable_statsquery(pattern, timeout):
    """Read stats from one Xray PID, banking a newly observed epoch first."""
    for _ in range(3):
        before = xray_pid()
        begin_xray_counter_epoch(before)
        try:
            r = subprocess.run([XRAY_BIN, "api", "statsquery", "--server=%s" % XRAY_API,
                                "-pattern", pattern], capture_output=True, text=True, timeout=timeout)
        except Exception:
            return None
        after = xray_pid()
        if before != after and after and after != "0":
            begin_xray_counter_epoch(after)
            continue
        if before and before != "0" and (not after or after == "0"):
            return None
        if r.returncode != 0: return None
        return r, after or before
    return None

def _stats_epoch_matches(c, pid):
    if not pid or pid == "0": return True
    row = c.execute("SELECT v FROM meta WHERE k='xray_pid'").fetchone()
    return row is not None and row["v"] == pid

def _stats_epoch_still_running(pid):
    if not pid or pid == "0": return True
    latest = xray_pid()
    if latest == pid: return True
    begin_xray_counter_epoch(latest)
    return False

# ---------------- sub links ----------------
def sub_path(token): return os.path.join(SUB_DIR, "sub-u-%s" % token)
def sub_url(token):  return "%s/sub-u-%s" % (SUB_BASE, token)

def _ws_link(ep, secret, address, port, sec):
    proto, net = ep["proto"], ep["net"]
    H = ep.get("host") or DOMAIN
    sni_host = ep.get("sni") or H
    fragment_fm = ep.get("fragment_fm", FRAGMENT_FM)
    qp = urllib.parse.quote(ep["path"], safe="")
    tls_on = (sec == "tls")
    base = ep["label"] if tls_on else (ep["label"].replace("-WS", "").replace("-XHTTP", "") + "-noTLS")
    nm = urllib.parse.quote("%s · %s" % (base, address))
    sni = ("&sni=%s" % urllib.parse.quote(sni_host, safe="")) if tls_on else ""
    if tls_on and fragment_fm:
        alpn = "http/1.1" if net == "ws" else "h2,http/1.1"  # CF only upgrades WebSocket over HTTP/1.1
        sni += "&fp=chrome&alpn=%s&fm=%s" % (urllib.parse.quote(alpn, safe=""), urllib.parse.quote(fragment_fm, safe=""))
    secp = "tls" if tls_on else "none"
    if proto == "vless":
        extra = "&mode=auto" if net == "xhttp" else ""
        return "vless://%s@%s:%s?encryption=none&security=%s&type=%s&host=%s%s&path=%s%s#%s" % (secret, address, port, secp, net, H, sni, qp, extra, nm)
    if proto == "trojan":
        return "trojan://%s@%s:%s?security=%s%s&type=%s&host=%s&path=%s#%s" % (secret, address, port, secp, sni, net, H, qp, nm)
    if proto == "vmess":
        j = {"v": "2", "ps": "%s · %s" % (base, address), "add": address, "port": str(port), "id": secret, "aid": "0", "scy": "auto",
             "net": net, "type": "none", "host": H, "path": ep["path"], "tls": ("tls" if tls_on else ""), "sni": (sni_host if tls_on else "")}
        return "vmess://" + base64.b64encode(json.dumps(j).encode()).decode()
    return ""

def _reality_link(ep, secret, n=0):
    r = ep["reality"]
    nm = urllib.parse.quote(ep.get("label", "REALITY") + " · مستقیم" + (" %d" % (n + 1) if n else ""))
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
    # Kept under its old name for stored settings and callers; CDN dial addresses
    # may now be IPv4 or DNS names. Host/SNI still come from each endpoint.
    addresses = []
    for token in re.split(r"[\s,]+", (text or "").strip()):
        if not token:
            continue
        if ":" in token:
            token, _, port = token.rpartition(":")
            if not port.isdigit() or not 1 <= int(port) <= 65535:
                continue
        try:
            ipaddress.IPv4Address(token)
            addresses.append(token)
            continue
        except ipaddress.AddressValueError:
            pass
        if (len(token) <= 253 and "." in token and not re.fullmatch(r"[0-9.]+", token)
                and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                        for label in token.split("."))):
            addresses.append(token.lower())
    return addresses

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

def approved_hosts():
    """Host/SNI pairs provisioned in Cloudflare for this server."""
    pairs = [{"host": DOMAIN, "sni": DOMAIN}]
    try:
        extra = json.loads(ENV.get("HOST_PROFILES", "[]"))
    except (TypeError, ValueError):
        extra = []
    for item in extra if isinstance(extra, list) else []:
        if not isinstance(item, dict): continue
        host, sni = str(item.get("host", "")).strip(), str(item.get("sni", "")).strip()
        if not all(re.fullmatch(r"[A-Za-z0-9.-]{1,253}", name) for name in (host, sni)): continue
        pair = {"host": host, "sni": sni}
        if pair not in pairs: pairs.append(pair)
    return pairs

def _endpoint_defaults(ep):
    return {"tls_ports": list(ep.get("tls_ports", [])),
            "notls_ports": list(ep.get("notls_ports", [])),
            "label": ep.get("label", ep["tag"]), "path": ep.get("path", ""),
            "host": DOMAIN, "sni": DOMAIN,
            "fragment_fm": "" if "reality" in ep or ep.get("proto") == "vmess" else FRAGMENT_FM}

def _normal_endpoint_settings(stored):
    result = {}
    stored = stored if isinstance(stored, dict) else {}
    hosts = approved_hosts()
    for ep in ENDPOINTS:
        tag = ep["tag"]; base = _endpoint_defaults(ep)
        raw = stored.get(tag, {})
        if not isinstance(raw, dict): raw = {}
        for key in ("tls_ports", "notls_ports"):
            allowed = base[key]
            if isinstance(raw.get(key), list):
                base[key] = [p for p in allowed if p in raw[key]]
        label = str(raw.get("label", "")).strip()
        if label: base["label"] = label[:64]
        pair = {"host": str(raw.get("host", "")), "sni": str(raw.get("sni", ""))}
        if pair in hosts: base.update(pair)
        fm = raw.get("fragment_fm")
        if isinstance(fm, str):
            try:
                if fm and not isinstance(json.loads(fm), dict): raise ValueError("fragment must be an object")
                base["fragment_fm"] = fm
            except ValueError:
                pass
        # Path is pinned to its provisioned inbound and Cloudflare tunnel route.
        result[tag] = base
    return result

def get_endpoint_settings():
    try: stored = json.loads(meta_get("endpoint_settings") or "{}")
    except (TypeError, ValueError): stored = {}
    return _normal_endpoint_settings(stored)

def set_endpoint_settings(settings):
    meta_set("endpoint_settings", json.dumps(_normal_endpoint_settings(settings), ensure_ascii=False))

def get_link_override(token):
    c = db(); row = c.execute("SELECT config_override FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if not row or row["config_override"] is None: return None
    try: value = json.loads(row["config_override"])
    except (TypeError, ValueError): return {}
    return value if isinstance(value, dict) else {}

def set_link_override(token, settings, queue_sync=False, queue_outbound=False):
    c = db(); cur = c.execute("UPDATE users SET config_override=? WHERE token=?",
                            (json.dumps(settings, ensure_ascii=False) if settings is not None else None, token))
    if cur.rowcount and queue_sync:
        c.execute("INSERT INTO meta(k,v) VALUES('membership_sync_pending','1') "
                  "ON CONFLICT(k) DO UPDATE SET v='1'")
    if cur.rowcount and queue_outbound:
        c.execute("INSERT INTO meta(k,v) VALUES('outbound_sync_pending','1') "
                  "ON CONFLICT(k) DO UPDATE SET v='1'")
    c.commit(); c.close()
    return cur.rowcount > 0

def global_settings_snapshot():
    return {"recipe": get_recipe(), "ips": get_ips(),
            "endpoint_settings": get_endpoint_settings(), "outbounds": get_outbounds()}

def store_global_config(settings):
    """Commit all public endpoint settings with a durable reconciliation marker."""
    c = db()
    for key, value in (
        ("config_recipe", json.dumps(settings["recipe"])),
        ("clean_ips", ",".join(settings["ips"])),
        ("endpoint_settings", json.dumps(_normal_endpoint_settings(settings["endpoint_settings"]), ensure_ascii=False)),
        ("membership_sync_pending", "1"),
    ):
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
    c.commit(); c.close()

def effective_recipe(token):
    custom = get_link_override(token)
    if custom is None: return get_recipe()
    recipe = custom.get("recipe", {})
    recipe = recipe if isinstance(recipe, dict) else {}
    # A newly provisioned endpoint must stay off for a link with a frozen snapshot.
    return {ep["tag"]: recipe.get(ep["tag"], {"enabled": False, "count": 0}) for ep in ENDPOINTS}

def effective_endpoint_settings(token):
    custom = get_link_override(token)
    if custom is None: return get_endpoint_settings()
    return _normal_endpoint_settings(custom.get("endpoint_settings"))

def effective_ips(token):
    custom = get_link_override(token)
    if custom is None: return get_ips()
    ips = custom.get("ips", [])
    return ips if isinstance(ips, list) and ips else get_ips()

def effective_outbounds(token):
    custom = get_link_override(token)
    if custom is None: return get_outbounds()
    obs = custom.get("outbounds", [])
    return obs if isinstance(obs, list) else []

def _configured_ep(ep, settings):
    merged = dict(ep)
    merged.update((settings or {}).get(ep["tag"], {}))
    return merged

def active_slot_keys(token, recipe=None, settings=None):
    """The distinct external-port identities emitted by this link's recipe."""
    recipe = effective_recipe(token) if recipe is None else recipe
    settings = effective_endpoint_settings(token) if settings is None else settings
    active = set()
    for source in ENDPOINTS:
        ep = _configured_ep(source, settings)
        tag = ep["tag"]
        r = recipe.get(tag, {"enabled": True, "count": len(_ep_slots(ep))})
        try: count = max(0, int(r.get("count", 0)))
        except (TypeError, ValueError): count = 0
        if not r.get("enabled") or not count: continue
        slots = [(int(ep["reality"]["port"]), "reality")] if "reality" in ep else _ep_slots(ep)
        for port, security in slots[:min(count, len(slots))]:
            active.add((tag, security, int(port)))
    return active

def _encode_slots(slots):
    return json.dumps([list(s) for s in sorted(slots)], separators=(",", ":"))

def _decode_slots(raw):
    try: return {(str(t), str(s), int(p)) for t, s, p in json.loads(raw or "[]")}
    except (TypeError, ValueError): return set()

def _snapshot_missing_active_slots():
    c = db(); rows = c.execute("SELECT token,credential_mode FROM users WHERE active_slots IS NULL").fetchall(); c.close()
    if not rows: return
    # The previous bot added every legacy credential to its backend inbound,
    # independent of the subscription recipe. Even an unlisted external port
    # could still authenticate with that shared credential. Model every
    # provisioned port as previously active so the first reconciliation revokes
    # disabled protocols and rotates a tag whose port set has shrunk.
    provisioned = set()
    for ep in ENDPOINTS:
        slots = [(int(ep["reality"]["port"]), "reality")] if "reality" in ep else _ep_slots(ep)
        provisioned.update((ep["tag"], security, int(port)) for port, security in slots)
    snapshots = [(_encode_slots(provisioned if (u["credential_mode"] or "legacy") == "legacy"
                                else active_slot_keys(u["token"])), u["token"]) for u in rows]
    c = db()
    c.executemany("UPDATE users SET active_slots=? WHERE token=? AND active_slots IS NULL", snapshots)
    if any((u["credential_mode"] or "legacy") == "legacy" for u in rows):
        c.execute("INSERT INTO meta(k,v) VALUES('membership_sync_pending','1') "
                  "ON CONFLICT(k) DO UPDATE SET v='1'")
    c.commit(); c.close()

def active_endpoint_tags(recipe, settings=None):
    """Endpoint tags with at least one emitted configuration."""
    return {tag for tag, _, _ in active_slot_keys(None, recipe, settings or get_endpoint_settings())}

def credential_mode(token):
    c = db(); u = c.execute("SELECT credential_mode FROM users WHERE token=?", (token,)).fetchone(); c.close()
    return u["credential_mode"] if u else "slots"  # create_user provisions before INSERT

def slot_mode_tags(token):
    c = db(); rows = c.execute("SELECT tag FROM slot_mode_tags WHERE token=?", (token,)).fetchall(); c.close()
    return {r["tag"] for r in rows}

def _mark_slot_mode_tag(token, tag):
    c = db(); c.execute("INSERT OR IGNORE INTO slot_mode_tags(token,tag) VALUES(?,?)", (token, tag))
    c.commit(); c.close()

def _slot_credential_map(token):
    c = db(); rows = c.execute("SELECT * FROM slot_credentials WHERE token=?", (token,)).fetchall(); c.close()
    return {(r["tag"], r["security"], int(r["external_port"])): r for r in rows}

def ensure_slot_credential(token, ep, security, port):
    """Persist a fresh identity when a slot is first enabled or re-enabled."""
    c = db()
    row = c.execute("SELECT * FROM slot_credentials WHERE token=? AND tag=? AND security=? AND external_port=?",
                    (token, ep["tag"], security, port)).fetchone()
    if row: c.close(); return row
    secret = (secrets.token_urlsafe(32) if ep["proto"] == "trojan" else str(uuidlib.uuid4()))
    suffix = {"tls": "tls", "none": "none", "reality": "reality"}[security]
    email = "%s.%s%d.%s" % (ep_email(token, ep["tag"]), suffix, port, secrets.token_hex(5))
    c.execute("INSERT OR IGNORE INTO slot_credentials(token,tag,security,external_port,email,secret) VALUES(?,?,?,?,?,?)",
              (token, ep["tag"], security, port, email, secret))
    c.commit()
    row = c.execute("SELECT * FROM slot_credentials WHERE token=? AND tag=? AND security=? AND external_port=?",
                    (token, ep["tag"], security, port)).fetchone()
    c.close(); return row

def _set_auth_state(token, mode, slots):
    c = db(); c.execute("UPDATE users SET credential_mode=?,active_slots=? WHERE token=?",
                        (mode, _encode_slots(slots), token)); c.commit(); c.close()

def _reconcile_user_membership(u, before_recipe=None, before_settings=None):
    """Reconcile each endpoint without rotating credentials on other endpoints."""
    token, secret = u["token"], u["uuid"]
    desired = active_slot_keys(token)
    previous = (_decode_slots(u["active_slots"]) if u["active_slots"] is not None else
                active_slot_keys(token, before_recipe, before_settings))
    removed = previous - desired
    desired_tags = {slot[0] for slot in desired}
    previous_tags = {slot[0] for slot in previous}
    eligible = _eligible_for_membership(u)
    mode = u["credential_mode"] or "legacy"
    converted = slot_mode_tags(token)
    transition_tags = ({slot[0] for slot in removed} - converted) if mode == "legacy" else set()
    ep_by_tag = {e["tag"]: e for e in ENDPOINTS}
    old_creds = _slot_credential_map(token)
    to_remove = set(old_creds) - desired
    # An endpoint that was never emitted has no legacy credential to revoke.
    # Leaving it alone also avoids making an unrelated first-time edit depend
    # on a historical stats snapshot.
    revoke_legacy = ((transition_tags | (previous_tags - desired_tags)) - converted
                     if mode == "legacy" else set())
    diagnosis = {}
    if (revoke_legacy or to_remove) and not _enable_usage_ledger(token, diagnosis):
        return False, set(), diagnosis
    ok = True
    for ep in ENDPOINTS:
        tag = ep["tag"]
        keys = sorted(s for s in desired if s[0] == tag)
        uses_slots = mode == "slots" or tag in converted or tag in transition_tags
        if uses_slots:
            for key in keys:
                cred = old_creds.get(key) or ensure_slot_credential(token, ep, key[1], key[2])
                if eligible and (tag in transition_tags or key not in old_creds):
                    if not _adu(ep, cred["secret"], cred["email"]): ok = False
        elif tag in desired_tags and eligible and tag not in previous_tags:
            if not _adu(ep, secret, legacy_email(token, tag)): ok = False
    if not ok: return False, set(), diagnosis
    cut = set()
    for ep in ENDPOINTS:
        tag = ep["tag"]
        if tag in revoke_legacy:
            removed_ok = _rmu(token, tag)
            if not removed_ok: ok = False
            else:
                _mark_slot_mode_tag(token, tag)
                cut.add(tag)
    for key in sorted(to_remove):
        cred = old_creds[key]
        if not _rmu_email(key[0], cred["email"]): ok = False; continue
        c = db(); c.execute("DELETE FROM slot_credentials WHERE email=?", (cred["email"],)); c.commit(); c.close()
        cut.add(key[0])
    if ok: _set_auth_state(token, mode, desired)
    write_sub(token, secret, u["label"])
    return ok, cut, diagnosis

def _eligible_for_membership(u):
    return not (u["disabled_ts"] or u["frozen"] or u["pending_delete"] or exhaust_reason(u))

def xr_reconcile_user_endpoints(token, secret, before_recipe=None, before_settings=None):
    """Update a link after settings change, including live sessions on removed tags."""
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if not u: return False
    ok, cut, _ = _reconcile_user_membership(u, before_recipe, before_settings)
    if cut: force_disconnect(cut)
    if not ok:
        meta_set("membership_sync_pending", "1")
        # A successful restart clears any legacy credential the API could not
        # revoke. resync_all restores only the desired, eligible identities.
        if _emergency_clear_dynamic_users():
            ok = meta_get("membership_sync_pending") != "1"
    return ok

def xr_reconcile_all_users(before_recipe=None, before_settings=None, tokens=None):
    """Reconcile eligible links after a global change; reset each affected port once."""
    c = db(); rows = c.execute("SELECT * FROM users").fetchall(); c.close()
    selected = set(tokens) if tokens is not None else None
    rows = [u for u in rows if selected is None or u["token"] in selected]
    if not rows: return True
    ok = True; cut = set()
    for u in rows:
        user_ok, user_cut, _ = _reconcile_user_membership(u, before_recipe, before_settings)
        ok = user_ok and ok; cut.update(user_cut)
    if cut: force_disconnect(cut)
    if not ok:
        meta_set("membership_sync_pending", "1")
        if _emergency_clear_dynamic_users():
            ok = meta_get("membership_sync_pending") != "1"
    return ok

# ---------------- outbounds (clean-egress routing) ----------------
# Route chosen domains out through a CLEAN upstream instead of this VPS's (often flagged)
# datacenter IP, so AI sites (Gemini/NotebookLM/Claude) work. Everything else stays direct.
# Each outbound also gets a loopback-only SOCKS inbound so the panel can TEST it live
# (never spawn a second xray on the box — see AGENTS.md).
XRAY_CONF         = ENV.get("XRAY_CONF", "/usr/local/etc/xray/config.json")
OB_TEST_PORT_BASE = int(ENV.get("OB_TEST_PORT_BASE", "10810"))
OB_TEST_SITES     = ["gemini.google.com", "notebooklm.google.com", "claude.ai"]
OB_BIND_WAIT      = 8      # seconds to wait for xray to bind the test ports after a restart

def get_outbounds():
    # [{"tag","link","domains":[...]}]; tag = xray outboundTag, domains route to it
    try:
        v = json.loads(meta_get("outbounds") or "[]")
    except Exception:
        return []
    out = []
    for o in v if isinstance(v, list) else []:
        if isinstance(o, dict) and o.get("tag") and o.get("link"):
            out.append({"tag": str(o["tag"]), "link": str(o["link"]),
                        "domains": [str(d) for d in (o.get("domains") or [])]})
    return out

def set_outbounds(obs, queue_apply=False):
    c = db()
    c.execute("INSERT INTO meta(k,v) VALUES('outbounds',?) "
              "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (json.dumps(obs),))
    if queue_apply:
        c.execute("INSERT INTO meta(k,v) VALUES('outbound_sync_pending','1') "
                  "ON CONFLICT(k) DO UPDATE SET v='1'")
    c.commit(); c.close()

def ob_test_port(i):
    return OB_TEST_PORT_BASE + i

def parse_domains(text):
    # one per line / comma separated; keeps xray prefixes (domain: / geosite: / regexp:)
    toks = [t.strip() for t in re.split(r"[\s,]+", (text or "").strip()) if t.strip()]
    return [t for t in toks if not t.startswith("#")]

def _q1(qs, *names, **kw):
    for n in names:
        if qs.get(n):
            return qs[n][0]
    return kw.get("default", "")

def parse_outbound_link(link, tag):
    """vless:// trojan:// ss:// socks:// http:// -> an xray outbound dict. Raises ValueError."""
    link = (link or "").strip()
    u = urllib.parse.urlparse(link)
    scheme = (u.scheme or "").lower()
    host, port = u.hostname, u.port
    qs = urllib.parse.parse_qs(u.query or "")

    if scheme in ("socks", "socks5", "http", "https"):
        if not host or not port: raise ValueError("آدرس یا پورت ناقص است")
        srv = {"address": host, "port": int(port)}
        if u.username:
            srv["users"] = [{"user": urllib.parse.unquote(u.username),
                             "pass": urllib.parse.unquote(u.password or "")}]
        return {"tag": tag, "protocol": ("socks" if scheme.startswith("socks") else "http"),
                "settings": {"servers": [srv]}}

    if scheme == "ss":
        method = password = None
        if host and port and u.username:
            raw = u.username
            try:
                dec = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
                method, password = dec.split(":", 1)
            except Exception:
                raise ValueError("ss:// نامعتبر (بخش رمز)")
        else:
            raw = link[len("ss://"):].split("#", 1)[0]
            try:
                dec = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
                head, hp = dec.rsplit("@", 1)
                method, password = head.split(":", 1)
                host, port = hp.rsplit(":", 1); port = int(port)
            except Exception:
                raise ValueError("ss:// نامعتبر")
        if not host or not port: raise ValueError("آدرس یا پورت ناقص است")
        return {"tag": tag, "protocol": "shadowsocks",
                "settings": {"servers": [{"address": host, "port": int(port),
                                          "method": method, "password": password}]}}

    if scheme in ("vless", "trojan"):
        if not host or not port: raise ValueError("آدرس یا پورت ناقص است")
        cred = urllib.parse.unquote(u.username or "")
        if not cred: raise ValueError("uuid/رمز در لینک نیست")
        net = _q1(qs, "type", default="tcp") or "tcp"
        sec = _q1(qs, "security", default="none")
        sni = _q1(qs, "sni", "peer") or _q1(qs, "host") or host
        stream = {"network": net, "security": ("tls" if sec in ("tls", "reality") else "none")}
        if stream["security"] == "tls":
            t = {"serverName": sni}
            if _q1(qs, "allowInsecure") in ("1", "true"): t["allowInsecure"] = True
            fp = _q1(qs, "fp")
            if fp: t["fingerprint"] = fp
            stream["tlsSettings"] = t
        if net == "ws":
            stream["wsSettings"] = {"path": _q1(qs, "path", default="/") or "/",
                                    "headers": {"Host": _q1(qs, "host") or sni}}
        elif net == "grpc":
            stream["grpcSettings"] = {"serviceName": _q1(qs, "serviceName")}
        if scheme == "vless":
            usr = {"id": cred, "encryption": _q1(qs, "encryption", default="none") or "none"}
            flow = _q1(qs, "flow")
            if flow: usr["flow"] = flow
            ob = {"tag": tag, "protocol": "vless",
                  "settings": {"vnext": [{"address": host, "port": int(port), "users": [usr]}]}}
        else:
            ob = {"tag": tag, "protocol": "trojan",
                  "settings": {"servers": [{"address": host, "port": int(port), "password": cred}]}}
        ob["streamSettings"] = stream
        return ob

    raise ValueError("پروتکل پشتیبانی نمی‌شود: %s" % (scheme or "؟"))

def ob_xtag(tag):
    # every outbound/rule we own carries this prefix inside xray's config, so on the next
    # apply we can tell ours from anything the operator added by hand and clean up exactly
    # what we created (a deleted outbound must not leave a dangling rule behind).
    return "mj-" + tag

def ob_user_xtag(token, tag):
    # ':' is excluded by the panel's outbound-name parser, so this namespace cannot
    # collide with a global `mj-<name>` outbound even when the names match.
    return "mj-u:%s:%s" % (token, tag)

def ob_user_matcher(token):
    # Xray matches `user` against the inbound client's email. The suffix includes
    # its endpoint tag, and may also include a per-config ID in future.
    return "regexp:^u_%s\\." % re.escape(token)

def ob_owned(tag):
    return tag.startswith("mj-") or tag in ("direct", "block")

def _routing_rule_owned(rule):
    """Recognize our generated rules without dropping operator block rules."""
    outbound = str(rule.get("outboundTag", ""))
    if outbound.startswith("mj-") or any(str(t).startswith("mjtest-") for t in (rule.get("inboundTag") or [])):
        return True
    if outbound != "direct": return False
    if (rule.get("user") and rule.get("ip") == ["geoip:private"] and
            set(rule) <= {"type", "ip", "outboundTag", "user"}):
        return True
    users = rule.get("user") or []
    return (rule.get("network") == "tcp,udp" and set(rule) <= {"type", "user", "network", "outboundTag"}
            and len(users) == 1 and str(users[0]).startswith("regexp:^u_"))

def ob_catchall_index(obs):
    """Index of the outbound that takes ALL traffic (the first one with no domains), or None.
       Empty domain list = "send everything here"; later empty ones are unreachable."""
    for i, o in enumerate(obs):
        if not [d for d in (o.get("domains") or []) if d]:
            return i
    return None

def build_xray_sections(obs, custom=None):
    """-> (outbounds, routing rules, loopback test inbounds).

    xray sends anything no rule matched to the FIRST outbound, so:
      * no catch-all  -> `direct` is first: only the listed domains leave via an outbound.
      * a catch-all   -> that outbound is first: EVERYTHING leaves through it, and the
                         other outbounds still win for their own domains (rules beat default).
    Custom links get user-scoped rules plus a final user-scoped fallback. That fallback
    prevents the global rules (and first outbound) from handling their traffic.
    """
    custom = custom or {}
    direct = {"tag": "direct", "protocol": "freedom", "settings": {}}
    parsed = [parse_outbound_link(o["link"], ob_xtag(o["tag"])) for o in obs]
    catch = ob_catchall_index(obs)
    outs = ([parsed[catch]] if catch is not None else []) + [direct] + \
           [ob for i, ob in enumerate(parsed) if i != catch]
    rules, tests = [], []
    for i, o in enumerate(obs):
        itag = "mjtest-%s" % o["tag"]
        tests.append({"tag": itag, "listen": "127.0.0.1", "port": ob_test_port(i),
                      "protocol": "socks", "settings": {"auth": "noauth", "udp": False}})
        # test-inbound rules FIRST so a test always exits via its own outbound
        rules.append({"type": "field", "inboundTag": [itag], "outboundTag": ob_xtag(o["tag"])})
    for token, user_obs in custom.items():
        user_obs = user_obs or []
        user = [ob_user_matcher(token)]
        for o in user_obs:
            outs.append(parse_outbound_link(o["link"], ob_user_xtag(token, o["tag"])))
        user_catch = ob_catchall_index(user_obs)
        if user_catch is not None:
            # Match the global catch-all's safety rule, but only for this link.
            rules.append({"type": "field", "user": user, "ip": ["geoip:private"], "outboundTag": "direct"})
        for o in user_obs:
            doms = [d for d in (o.get("domains") or []) if d]
            if doms:
                rules.append({"type": "field", "user": user, "domain": doms,
                              "outboundTag": ob_user_xtag(token, o["tag"])})
        # A link with no custom catch-all must go direct when no domain rule matched.
        # This rule also prevents a global domain rule from leaking into that link.
        fallback = ob_user_xtag(token, user_obs[user_catch]["tag"]) if user_catch is not None else "direct"
        rules.append({"type": "field", "user": user, "network": "tcp,udp", "outboundTag": fallback})
    if catch is not None:
        # never push LAN/loopback (incl. our own tunnel plumbing) through the upstream
        rules.append({"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"})
    for o in obs:
        doms = [d for d in (o.get("domains") or []) if d]
        if doms:
            rules.append({"type": "field", "domain": doms, "outboundTag": ob_xtag(o["tag"])})
    outs.append({"tag": "block", "protocol": "blackhole", "settings": {}})
    return outs, rules, tests

def _custom_outbound_sets():
    # A missing DB is possible in isolated config-builder tests; on a running bot
    # init_db has already created it, and read errors must stop the apply.
    if not os.path.exists(DB_PATH): return {}
    c = db()
    try:
        rows = c.execute("SELECT token FROM users WHERE config_override IS NOT NULL").fetchall()
    finally:
        c.close()
    global_outbounds = get_outbounds()
    # An unchanged custom snapshot has the same route as the global rules. Keep
    # it in SQLite, but only add user-scoped Xray rules once the two diverge.
    routed = {}
    for u in rows:
        own = effective_outbounds(u["token"])
        if own != global_outbounds:
            routed[u["token"]] = own
    return routed

def _xray_api_ready():
    try:
        r = subprocess.run([XRAY_BIN, "api", "statsquery", "--server=%s" % XRAY_API,
                            "-pattern", "user>>>u_"], capture_output=True, text=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False

def apply_xray_outbounds(obs=None, custom=None):
    """Rewrite ONLY outbounds/routing (+ our mjtest-* loopback inbounds) in xray's config.
       Validates with `xray -test` and refuses to write a broken config. -> (ok, message)."""
    obs = get_outbounds() if obs is None else obs
    try:
        with open(XRAY_CONF, encoding="utf-8") as current_file:
            original_text = current_file.read()
        cfg = json.loads(original_text)
    except Exception as e:
        return False, "خواندن کانفیگ xray ناموفق بود: %s" % e
    original_cfg = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    try:
        custom = _custom_outbound_sets() if custom is None else custom
        outs, rules, tests = build_xray_sections(obs, custom)
    except (ValueError, sqlite3.Error) as e:
        return False, str(e)
    # keep every real inbound (endpoints + api); replace only our own test inbounds
    cfg["inbounds"] = [ib for ib in (cfg.get("inbounds") or [])
                       if not str(ib.get("tag", "")).startswith("mjtest-")] + tests
    # keep outbounds we don't manage (e.g. a hand-added "blocked"); drop every previous mj-* one
    foreign = [ob for ob in (cfg.get("outbounds") or []) if not ob_owned(str(ob.get("tag", "")))]
    cfg["outbounds"] = outs[:-1] + foreign + outs[-1:]        # ...keep `block` last
    # keep foreign routing rules — above all, `inboundTag:[api] -> outboundTag:api`,
    # without which the gRPC API stops working and the bot can no longer manage users.
    api_tag = str((cfg.get("api") or {}).get("tag") or "")
    alive = {str(ob.get("tag", "")) for ob in cfg["outbounds"]} | ({api_tag} if api_tag else set())
    keep = [r for r in ((cfg.get("routing") or {}).get("rules") or [])
            if not _routing_rule_owned(r) and str(r.get("outboundTag", "")) in alive]
    # API and test inbounds stay first. Operator routing policies then apply to
    # every link, including custom links; per-link rules override only the panel's
    # generated global rules.
    api_keep = [r for r in keep if api_tag and api_tag in (r.get("inboundTag") or [])]
    foreign_keep = [r for r in keep if r not in api_keep]
    tests_scoped = [r for r in rules if any(str(t).startswith("mjtest-") for t in (r.get("inboundTag") or []))]
    user_scoped = [r for r in rules if r.get("user")]
    global_rules = [r for r in rules if r not in tests_scoped and r not in user_scoped and r not in keep]
    rules = api_keep + tests_scoped + foreign_keep + user_scoped + global_rules
    if rules:
        # AsIs, NOT IPIfNonMatch: our rules match on the requested domain (and geoip:private
        # sees a literal IP), so resolution buys nothing — but IPIfNonMatch makes xray do a
        # DNS lookup for EVERY connection that matches no rule, adding latency to all traffic.
        cfg["routing"] = {"domainStrategy": "AsIs", "rules": rules}
    else:
        cfg.pop("routing", None)
    if json.dumps(cfg, sort_keys=True, ensure_ascii=False) == original_cfg:
        return ((True, "تنظیمات خروجی از قبل اعمال شده است.") if _xray_api_ready() else
                (False, "تنظیمات روی دیسک یکسان است اما API سرویس Xray آماده نیست"))
    # the suffix MUST stay .json: xray picks the config format from the file extension and
    # rejects anything else with "failed to get format of <file>", which made every apply fail.
    tmp = XRAY_CONF + ".mjnew.json"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(cfg, indent=2, ensure_ascii=False))
        r = subprocess.run([XRAY_BIN, "-test", "-c", tmp], capture_output=True, text=True, timeout=25)
        if r.returncode != 0:
            os.unlink(tmp)
            return False, "کانفیگ نامعتبر است، چیزی تغییر نکرد: %s" % ((r.stderr or r.stdout or "").strip()[-250:])
        with open(XRAY_CONF + ".bak." + time.strftime("%Y%m%d%H%M%S"), "w", encoding="utf-8") as b:
            b.write(open(XRAY_CONF, encoding="utf-8").read())
        os.replace(tmp, XRAY_CONF)
    except Exception as e:
        try: os.unlink(tmp)
        except Exception: pass
        return False, "نوشتن کانفیگ ناموفق بود: %s" % e
    try:
        restarted = subprocess.run(["systemctl", "restart", XRAY_SERVICE], capture_output=True, text=True, timeout=30)
        restart_error = "" if restarted.returncode == 0 else (restarted.stderr or restarted.stdout or "restart failed").strip()
    except Exception as e:
        restart_error = str(e)
    if restart_error:
        # Keep the file and running service aligned if systemd rejects the new
        # config after the file was replaced. The validated original stays in
        # memory, and the timestamped on-disk backup is kept for an operator.
        rollback_tmp = XRAY_CONF + ".mjrollback.json"
        try:
            with open(rollback_tmp, "w", encoding="utf-8") as old_file:
                old_file.write(original_text)
            os.replace(rollback_tmp, XRAY_CONF)
            recovered = subprocess.run(["systemctl", "restart", XRAY_SERVICE],
                                       capture_output=True, text=True, timeout=30)
            detail = "کانفیگ قبلی بازیابی شد" if recovered.returncode == 0 else "بازیابی سرویس هم ناموفق بود"
        except Exception as e:
            detail = "بازیابی کانفیگ قبلی ناموفق بود: %s" % e
            try: os.unlink(rollback_tmp)
            except OSError: pass
        return False, "ری‌استارت xray ناموفق بود: %s؛ %s" % (restart_error[-250:], detail)
    deadline = time.time() + OB_BIND_WAIT
    first_attempt = True
    while first_attempt or time.time() < deadline:
        first_attempt = False
        if _xray_api_ready(): break
        time.sleep(0.3)
    else:
        return False, "Xray راه‌اندازی شد اما API آن هنوز آماده نیست؛ همگام‌سازی دوباره تلاش می‌شود"
    # Xray binds its test ports a moment AFTER systemd reports the restart done.
    while obs and time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", ob_test_port(0)), timeout=0.5).close(); break
        except OSError:
            time.sleep(0.3)
    return True, "اعمال شد (%d خروجی). کاربران خودکار resync می‌شوند." % \
           (len(obs) + sum(len(v or []) for v in custom.values()))

def _socks5_get(port, host, path="/", timeout=12):
    """SOCKS5 -> TLS -> minimal HTTPS GET through a loopback test inbound. -> (status, body_head)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")
        if s.recv(2)[:2] != b"\x05\x00": raise OSError("دست‌دادن SOCKS ناموفق")
        hb = host.encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + (443).to_bytes(2, "big"))
        rep = s.recv(4)
        if len(rep) < 2 or rep[1] != 0: raise OSError("خروجی وصل نشد")
        atyp = rep[3] if len(rep) > 3 else 1
        if   atyp == 1: s.recv(6)
        elif atyp == 3: s.recv(s.recv(1)[0] + 2)
        elif atyp == 4: s.recv(18)
        w = ssl.create_default_context().wrap_socket(s, server_hostname=host)
        # look like a browser: a bare GET gets 403'd by bot filters even from a clean IP,
        # which would make a perfectly good outbound look blocked
        w.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\n"
                   "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36\r\n"
                   "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8\r\n"
                   "Accept-Language: en-US,en;q=0.9\r\n"
                   "Connection: close\r\n\r\n" % (path, host)).encode())
        buf = b""
        while len(buf) < 4096:
            try: chunk = w.recv(4096)
            except Exception: break
            if not chunk: break
            buf += chunk
        parts = buf.split(b"\r\n", 1)[0].decode("latin-1", "ignore").split()
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        body = buf.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in buf else b""
        return code, body[:300].decode("utf-8", "ignore")
    finally:
        try: s.close()
        except Exception: pass

def test_outbound(tag):
    obs = get_outbounds()
    idx = next((i for i, o in enumerate(obs) if o["tag"] == tag), None)
    if idx is None: return "خروجی پیدا نشد"
    port = ob_test_port(idx)
    try:
        code, body = _socks5_get(port, "api.ipify.org", "/?format=json")
        ip = body.strip()[:60] if code == 200 else "نامشخص (HTTP %s)" % code
    except Exception as e:
        return "❌ به خروجی وصل نشد: %s — اول «ذخیره و اعمال» را بزنید." % e
    marks = []
    for hostname in OB_TEST_SITES:
        try:
            code, _ = _socks5_get(port, hostname, "/")
            marks.append("%s %s" % (hostname, "✅" if code in (200, 301, 302) else "⛔️%d" % code))
        except Exception:
            marks.append("%s ❌" % hostname)
    return "IP خروجی: %s · %s" % (ip, " · ".join(marks))

def write_sub(token, secret, label):
    ips = effective_ips(token) or DEFAULT_IPS
    recipe = effective_recipe(token); settings = effective_endpoint_settings(token)
    mode = credential_mode(token); converted = slot_mode_tags(token); links = []; gi = 0
    for source in ENDPOINTS:
        ep = _configured_ep(source, settings)
        r = recipe.get(ep["tag"], {"enabled": True, "count": len(_ep_slots(ep))})
        count = max(0, int(r.get("count", 0))) if r.get("enabled") else 0
        if "reality" in ep:      # direct link: no ports/clean IPs to cycle, just `count` copies
            if count:
                identity = (ensure_slot_credential(token, ep, "reality", int(ep["reality"]["port"]))["secret"]
                            if mode == "slots" or ep["tag"] in converted else secret)
                links += [_reality_link(ep, identity, k) for k in range(count)]
            continue
        slots = _ep_slots(ep)
        if not count or not slots:
            continue
        for k in range(count):
            port, sec = slots[k % len(slots)]
            identity = (ensure_slot_credential(token, ep, sec, int(port))["secret"]
                        if mode == "slots" or ep["tag"] in converted else secret)
            links.append(_ws_link(ep, identity, ips[gi % len(ips)], port, sec)); gi += 1
    path = sub_path(token); tmp = path + ".tmp-" + secrets.token_hex(4)
    try:
        with open(tmp, "w") as out:
            out.write(base64.b64encode("\n".join(l for l in links if l).encode()).decode())
        os.replace(tmp, path)
    finally:
        try: os.remove(tmp)
        except FileNotFoundError: pass

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
        if b < 1024: return (("%.3f" if u == "TB" else "%.1f") + " %s") % (b, u)
        b /= 1024
    return "%.1f PB" % b

def human_limit(lb): return "نامحدود" if lb <= 0 else fmt_bytes(lb)

def current_usage(u):
    """Usage since the latest manual reset; used_bytes itself remains lifetime traffic."""
    reset = u["usage_reset_bytes"] if "usage_reset_bytes" in u.keys() else 0
    return max(0, int(u["used_bytes"] or 0) - int(reset or 0))

def human_expiry(ts):
    if ts <= 0: return "نامحدود"
    left = ts - int(time.time())
    if left <= 0: return "منقضی"
    d = left // 86400; h = (left % 86400) // 3600
    if d: return "%d روز و %d ساعت" % (d, h)
    if h: return "%d ساعت" % h
    return "%d دقیقه" % max(1, (left + 59) // 60)

def is_admin(uid):
    if ADMIN_IDS: return uid in ADMIN_IDS
    a = meta_get("admin_id"); return a is not None and int(a) == uid

# ---------------- core ops ----------------
def create_user(vol_gb, dur_days, label=None):
    token = secrets.token_hex(8)
    secret = str(uuidlib.uuid4())
    label = label or ("link-%s" % token[:6])
    limit_bytes = int(vol_gb * GB) if vol_gb and vol_gb > 0 else 0
    expiry_ts = int(time.time() + float(dur_days) * 86400) if dur_days and dur_days > 0 else 0
    if not xr_add_user(token, secret):
        xr_remove_user(token)
        c = db(); c.execute("DELETE FROM slot_credentials WHERE token=?", (token,)); c.commit(); c.close()
        return None
    try: write_sub(token, secret, label)
    except Exception:
        xr_remove_user(token)
        c = db(); c.execute("DELETE FROM slot_credentials WHERE token=?", (token,)); c.commit(); c.close()
        raise
    c = db()
    c.execute("""INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,base_bytes,last_raw,used_bytes,
                 credential_mode,active_slots,usage_anchor) VALUES(?,?,?,?,?,?,?,0,0,0,'slots',?,0)""",
              (token, secret, "u_" + token, label, limit_bytes, expiry_ts, int(time.time()),
               _encode_slots(active_slot_keys(token))))
    c.commit(); c.close()
    return token

def delete_user(token):
    refresh_usage(token)
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    if not u: c.close(); return False
    c.close()
    tags = tags_to_cut_for_user(token)
    c = db(); c.execute("UPDATE users SET pending_delete=1,auth_pending=1 WHERE token=?", (token,))
    c.commit(); c.close()
    if not xr_remove_user(token) and not _emergency_clear_dynamic_users():
        force_disconnect(tags)
        return False
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    if not u: c.close(); return False
    custom = u["config_override"] is not None
    # Keep lifetime totals and daily rows when deleting a subscription.
    retired = int(meta_get("retired_bytes", "0") or 0) + int(u["used_bytes"] or 0)
    c.execute("INSERT INTO meta(k,v) VALUES('retired_bytes',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (str(retired),))
    c.execute("DELETE FROM slot_credentials WHERE token=?", (token,))
    c.execute("DELETE FROM slot_mode_tags WHERE token=?", (token,))
    c.execute("DELETE FROM legacy_emails WHERE token=?", (token,))
    c.execute("DELETE FROM usage_ledger WHERE token=?", (token,))
    c.execute("DELETE FROM users WHERE token=?", (token,)); c.commit(); c.close()
    del_sub(token)
    force_disconnect(tags)
    if custom:
        try:
            applied, reason = apply_xray_outbounds()
        except Exception as exc:
            applied, reason = False, str(exc)
        if applied:
            meta_set("outbound_sync_pending", "")
            resync_all()
        else:
            meta_set("outbound_sync_pending", "1")
            print("outbound cleanup pending:", reason, flush=True)
    return True

def exhaust_reason(u, now=None):
    now = now or int(time.time())
    if (u["limit_bytes"] or 0) > 0 and current_usage(u) >= u["limit_bytes"]: return "حجم تمام شد"
    if (u["expiry_ts"] or 0) > 0 and now >= u["expiry_ts"]: return "زمان تمام شد"
    return None

def disable_user(token):
    # exhausted: stop service (remove from xray) but KEEP the row + sub file so it can be renewed within the grace window
    tags = tags_to_cut_for_user(token)  # capture BEFORE rmu
    c = db(); c.execute("UPDATE users SET disabled_ts=?,auth_pending=1 WHERE token=?",
                        (int(time.time()), token)); c.commit(); c.close()
    removed = xr_remove_user(token)
    if not removed: removed = _emergency_clear_dynamic_users()
    if removed:
        _rotate_legacy_emails(token)
        c = db(); c.execute("UPDATE users SET auth_pending=0 WHERE token=?", (token,)); c.commit(); c.close()
    force_disconnect(tags)           # cut the live session so quota/expiry actually takes effect now
    return removed

def reenable_user(token):
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if not u or u["pending_delete"]: return False
    if not xr_add_user(token, u["uuid"]): return False
    write_sub(token, u["uuid"], u["label"])
    c = db(); c.execute("UPDATE users SET disabled_ts=0,auth_pending=0 WHERE token=?", (token,)); c.commit(); c.close()
    return True

def maybe_reenable(token):
    # after an extend: if it was disabled but now has quota/time again, bring it back live immediately
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if u and u["disabled_ts"] and not u["frozen"] and not exhaust_reason(u):
        return reenable_user(token)
    return False

def freeze_user(token):
    # manual admin freeze: cut the link NOW and keep it out of xray until unfrozen. Fully
    # independent of the quota/expiry disable — no 48h grace, and the enforcer never touches it.
    tags = tags_to_cut_for_user(token)  # capture BEFORE rmu
    c = db(); c.execute("UPDATE users SET frozen=1,auth_pending=1 WHERE token=?", (token,)); c.commit(); c.close()
    removed = xr_remove_user(token)
    if not removed: removed = _emergency_clear_dynamic_users()
    if removed:
        _rotate_legacy_emails(token)
        c = db(); c.execute("UPDATE users SET auth_pending=0 WHERE token=?", (token,)); c.commit(); c.close()
    force_disconnect(tags)           # drop the live session immediately
    return removed

def unfreeze_user(token):
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if not u or u["pending_delete"]: return False
    # bring it back live unless it's also quota/expiry-disabled or now exhausted
    if not u["disabled_ts"] and not exhaust_reason(u):
        if not xr_add_user(token, u["uuid"]): return False
        write_sub(token, u["uuid"], u["label"])
    c = db(); c.execute("UPDATE users SET frozen=0,auth_pending=0 WHERE token=?", (token,)); c.commit(); c.close()
    return True

def refresh_usage(token):
    for _ in range(3):
        begin_xray_counter_epoch()
        c = db(); selected = c.execute("SELECT usage_anchor FROM users WHERE token=?", (token,)).fetchone(); c.close()
        if not selected: return
        ledger = selected["usage_anchor"] is not None
        raw = xr_usage_all() if ledger else xr_usage(token)
        if raw is None: return  # failed read is never a counter reset
        pid = getattr(raw, "epoch_pid", None)
        if not _stats_epoch_still_running(pid): continue
        c = db()
        try:
            c.execute("BEGIN IMMEDIATE")
            u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
            if not u or not _stats_epoch_matches(c, pid) or (u["usage_anchor"] is not None) != ledger:
                c.rollback(); continue
            if ledger:
                if getattr(raw, "emails", None) is not None:
                    _ledger_apply_snapshot(c, u, raw.emails)
            else:
                base, used = _usage_from_raw(u, raw)
                c.execute("UPDATE users SET base_bytes=?,last_raw=?,used_bytes=?,rebase_floor=NULL WHERE token=?",
                          (base, int(raw), used, token))
            c.commit(); return
        finally:
            c.close()

def _usage_from_raw(u, raw):
    base, last = int(u["base_bytes"] or 0), int(u["last_raw"] or 0)
    floor = u["rebase_floor"] if "rebase_floor" in u.keys() else None
    if floor is not None and raw < last:
        # A removed slot counter disappeared, but surviving slots did not reset.
        # Preserve the lifetime total instead of counting survivor bytes twice.
        return int(floor) - raw, int(floor)
    if raw < last: base += last  # a genuine Xray counter reset
    return base, base + raw

def reset_usage(token):
    # Capture the freshest available lifetime counter, then atomically move only the
    # display/quota baseline. Historical totals and daily rows remain untouched.
    # The bulk reader distinguishes a missing user stat from a real zero, avoiding
    # the transient-empty-response double-counting trap handled by refresh_all_usage.
    refresh_all_usage()
    c = db()
    cur = c.execute("UPDATE users SET usage_reset_bytes=used_bytes WHERE token=?", (token,))
    changed = cur.rowcount > 0
    c.commit(); c.close()
    if changed:
        maybe_reenable(token)
    return changed

def rename_user(token, name):
    name = (name or "").strip()[:40]
    if not name:
        return False
    c = db(); cur = c.execute("UPDATE users SET label=? WHERE token=?", (name, token))
    changed = cur.rowcount > 0
    c.commit(); c.close()
    return changed

def xr_usage_all():
    # ONE statsquery for all users -> {token: bytes}, or None if the read FAILED.
    # None is CRITICAL: a failed/empty read must NOT be treated as "everyone reset to 0",
    # because that false reset double-counts every user's traffic (the 2026-07 usage-spike bug).
    result = _stable_statsquery("user>>>u_", 20)
    if result is None: return None
    r, pid = result
    try:
        d = json.loads(r.stdout or "{}")
    except Exception:
        return None
    tot, emails = {}, {}
    for s in (d.get("stat") or []):
        nm = s.get("name", "")
        parts = nm.split(">>>")
        if len(parts) < 4 or parts[0] != "user" or not parts[1].startswith("u_") or parts[2] != "traffic":
            continue
        email = parts[1]
        if "." not in email: continue
        tk = email[2:].split(".", 1)[0]
        value = int(s.get("value", 0))
        tot[tk] = tot.get(tk, 0) + value
        emails[email] = emails.get(email, 0) + value
    return UsageSnapshot(tot, emails, pid)

class UsageSnapshot(dict):
    def __init__(self, totals, emails, epoch_pid=None):
        super().__init__(totals)
        self.emails = emails
        self.epoch_pid = epoch_pid

def _ledger_apply_snapshot(c, u, emails):
    token = u["token"]
    prefix = "u_%s." % token
    old = {r["email"]: r for r in c.execute(
        "SELECT email,last_raw,total_bytes FROM usage_ledger WHERE token=?", (token,)).fetchall()}
    for email, raw in emails.items():
        if not email.startswith(prefix): continue
        raw = int(raw)
        prior = old.get(email)
        if prior:
            last = int(prior["last_raw"])
            delta = raw - last if raw >= last else raw  # only this identity reset
            c.execute("UPDATE usage_ledger SET last_raw=?,total_bytes=total_bytes+? WHERE email=?",
                      (raw, max(0, delta), email))
        else:
            # Every email first seen after the anchor is a new identity. Slot
            # credentials include a random generation, so revoked IDs never revive.
            c.execute("INSERT INTO usage_ledger(email,token,last_raw,total_bytes) VALUES(?,?,?,?)",
                      (email, token, raw, raw))
    ledger_total = c.execute("SELECT COALESCE(SUM(total_bytes),0) n FROM usage_ledger WHERE token=?",
                             (token,)).fetchone()["n"]
    used = int(u["usage_anchor"]) + int(ledger_total)
    c.execute("UPDATE users SET used_bytes=? WHERE token=?", (used, token))
    return used

def _enable_usage_ledger(token, diagnosis=None):
    """Anchor existing lifetime usage before rotating any legacy identity."""
    for _ in range(3):
        begin_xray_counter_epoch()
        c = db(); prior = c.execute("SELECT usage_anchor FROM users WHERE token=?", (token,)).fetchone(); c.close()
        if not prior: return False
        if prior["usage_anchor"] is not None: return True
        snapshot = xr_usage_all()
        if snapshot is None or getattr(snapshot, "emails", None) is None: return False
        pid = getattr(snapshot, "epoch_pid", None)
        if not _stats_epoch_still_running(pid): continue
        c = db()
        try:
            c.execute("BEGIN IMMEDIATE")
            if not _stats_epoch_matches(c, pid): c.rollback(); continue
            u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
            if not u: c.rollback(); return False
            if u["usage_anchor"] is not None: c.rollback(); return True
            if token not in snapshot and int(u["last_raw"] or 0) > 0:
                # A transiently missing counter cannot serve as a baseline.
                if diagnosis is not None:
                    diagnosis["missing_stats_pid"] = pid
                c.rollback(); return False
            if token in snapshot:
                raw = int(snapshot[token])
                base, used = _usage_from_raw(u, raw)
            else:
                raw = int(u["last_raw"] or 0)
                base, used = int(u["base_bytes"] or 0), int(u["used_bytes"] or 0)
            c.execute("UPDATE users SET base_bytes=?,last_raw=?,used_bytes=?,usage_anchor=?,rebase_floor=NULL WHERE token=?",
                      (base, raw, used, used, token))
            prefix = "u_%s." % token
            for email, value in snapshot.emails.items():
                if email.startswith(prefix):
                    c.execute("INSERT OR IGNORE INTO usage_ledger(email,token,last_raw,total_bytes) VALUES(?,?,?,0)",
                              (email, token, int(value)))
            c.commit(); return True
        finally:
            c.close()
    return False

def refresh_all_usage():
    for _ in range(3):
        begin_xray_counter_epoch()
        raws = xr_usage_all()
        if raws is None: return   # failed read must never be mistaken for a counter reset
        pid = getattr(raws, "epoch_pid", None)
        if not _stats_epoch_still_running(pid): continue
        c = db()
        try:
            c.execute("BEGIN IMMEDIATE")
            if not _stats_epoch_matches(c, pid): c.rollback(); continue
            today = day_key()
            for u in c.execute("SELECT token,base_bytes,last_raw,used_bytes,rebase_floor,usage_anchor FROM users").fetchall():
                if u["usage_anchor"] is not None:
                    emails = getattr(raws, "emails", None)
                    used = _ledger_apply_snapshot(c, u, emails) if emails is not None else u["used_bytes"]
                elif u["token"] in raws:
                    raw = raws[u["token"]]
                    base, used = _usage_from_raw(u, raw)
                    c.execute("UPDATE users SET base_bytes=?,last_raw=?,used_bytes=?,rebase_floor=NULL WHERE token=?",
                              (base, raw, used, u["token"]))
                else:
                    used = u["used_bytes"]
                record_daily(c, u["token"], used, today)
            prune_daily(c)
            c.commit(); return
        finally:
            c.close()

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
    # "total" = live users' lifetime + retired (deleted users') lifetime, so a delete never
    # lowers it. today/last30 count EVERY daily row (incl. deleted users, until they age out),
    # so a delete doesn't lower those either. retired_bytes >= each deleted user's 30-day
    # delta, so the "30d <= total" invariant still holds.
    c = db(); day = day_key(); cutoff30 = day_key(time.time() - 30 * 86400)
    live = c.execute("SELECT COALESCE(SUM(used_bytes),0) v FROM users").fetchone()["v"]
    r = c.execute("SELECT v FROM meta WHERE k='retired_bytes'").fetchone()
    total = int(live) + (int(r["v"]) if r and str(r["v"]).lstrip("-").isdigit() else 0)
    today = c.execute("SELECT COALESCE(SUM(max(end_used-start_used,0)),0) v FROM usage_daily WHERE day=?", (day,)).fetchone()["v"]
    last30 = c.execute("SELECT COALESCE(SUM(max(end_used-start_used,0)),0) v FROM usage_daily WHERE day>=?", (cutoff30,)).fetchone()["v"]
    c.close(); return int(total), int(today), int(last30)

def resync_all(allow_missing_stats_restart=True):
    # A restart clears Xray's dynamic users. Reconcile stored identities first,
    # then register every eligible identity again, including unchanged slots.
    # A failed per-link revocation must not restore its old shared credential.
    begin_xray_counter_epoch()
    c = db(); rows = c.execute("SELECT * FROM users").fetchall(); c.close()
    ok = True; cut = set(); missing_stats = {}
    for u in rows:
        user_ok, user_cut, diagnosis = _reconcile_user_membership(u)
        cut.update(user_cut)
        if not user_ok:
            ok = False
            if "missing_stats_pid" in diagnosis:
                missing_stats[u["token"]] = diagnosis["missing_stats_pid"]
            continue
        if not _eligible_for_membership(u): continue  # grace, freeze, or exhausted quota -> keep out of xray
        if not xr_add_user(u["token"], u["uuid"]):
            ok = False
            continue
        write_sub(u["token"], u["uuid"], u["label"])
    if cut: force_disconnect(cut)
    meta_set("membership_sync_pending", "" if ok else "1")
    if not ok and missing_stats and allow_missing_stats_restart:
        # These historical counters cannot be reconstructed from a successful
        # but empty stats response. Refresh every available counter before a
        # controlled restart, then require a second absent read on the same PID.
        # The new PID banks last_raw and can safely anchor known lifetime usage.
        refresh_all_usage()
        pid = xray_pid()
        if pid and pid != "0" and all(seen == pid for seen in missing_stats.values()):
            snapshot = xr_usage_all()
            if (snapshot is not None and getattr(snapshot, "epoch_pid", None) == pid
                    and all(token not in snapshot for token in missing_stats)):
                if _emergency_clear_dynamic_users():
                    return meta_get("membership_sync_pending") != "1"
    return ok

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
            [{"text": "🌐 آدرس‌های CDN", "callback_data": "ips"}]]

def _grid(items, cb):
    rows, r = [], []
    for label, val in items:
        r.append({"text": label, "callback_data": "%s:%s" % (cb, val)})
        if len(r) == 3: rows.append(r); r = []
    if r: rows.append(r)
    return rows

TEST_LINK = (500 / 1024, 1, "Test")   # one-tap trial link: 500MB, 1 day

def vol_kb():
    rows = [[{"text": "🧪 کانفیگ تست (۵۰۰ مگ · ۱ روز)", "callback_data": "testlink"}]]
    rows += _grid(VOLS, "vol")
    rows.append([{"text": "♾ نامحدود", "callback_data": "vol:0"}, {"text": "✏️ دلخواه", "callback_data": "vol:custom"}])
    rows.append([{"text": "بازگشت", "callback_data": "menu"}]); return rows

def dur_kb():
    rows = _grid(DURS, "dur")
    rows.append([{"text": "♾ نامحدود", "callback_data": "dur:0"}, {"text": "✏️ دلخواه", "callback_data": "dur:custom"}])
    rows.append([{"text": "بازگشت", "callback_data": "new"}]); return rows

def list_kb():
    c = db(); rows = c.execute("SELECT * FROM users ORDER BY created_ts DESC").fetchall(); c.close()
    kb = [[{"text": "%s %s · %s/%s · %s" % (("⏸" if u["disabled_ts"] else "🔗"), u["label"], fmt_bytes(current_usage(u)), human_limit(u["limit_bytes"]), human_expiry(u["expiry_ts"])),
            "callback_data": "u:%s" % u["token"]}] for u in rows]
    kb.append([{"text": "بازگشت", "callback_data": "menu"}]); return kb

def result_text(token):
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    return ("✅ <b>لینک «%s» ساخته شد</b>\n📦 %s · ⏳ %s\n\n🔗 <code>%s</code>"
            % (html.escape(u["label"]), human_limit(u["limit_bytes"]), human_expiry(u["expiry_ts"]), sub_url(token)))

def detail_text(u):
    status = ""
    dts = u["disabled_ts"] if "disabled_ts" in u.keys() else 0
    if dts:
        left_h = max(0, (dts + GRACE_SECONDS - int(time.time())) // 3600)
        status = "⏸ <b>غیرفعال شد</b> (%s) — تا ~%d ساعت دیگر قابل تمدید است، وگرنه خودکار حذف می‌شود.\n\n" % (exhaust_reason(u) or "اتمام", left_h)
    return ("%s🔗 <b>%s</b>\n\n📦 مصرف: %s از %s\n⏳ %s\n🆔 <code>u_%s</code>\n\n🔗 <code>%s</code>"
            % (status, html.escape(u["label"]), fmt_bytes(current_usage(u)), human_limit(u["limit_bytes"]),
               human_expiry(u["expiry_ts"]), u["token"], sub_url(u["token"])))

def detail_kb(token):
    return [[{"text": "➕ حجم", "callback_data": "av:%s" % token}, {"text": "➕ زمان", "callback_data": "at:%s" % token}],
            [{"text": "✏️ ویرایش نام", "callback_data": "rn:%s" % token}, {"text": "♻️ ریست مصرف", "callback_data": "rstq:%s" % token}],
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
        c.execute("UPDATE users SET expiry_ts=? WHERE token=?", (base + int(float(days) * 86400), token)); c.commit()
    c.close()

def set_unlimited(token, field):
    c = db(); c.execute("UPDATE users SET %s=0 WHERE token=?" % field, (token,)); c.commit(); c.close()

WELCOME = "🔐 <b>پنل Mohajer</b>\nیکی را انتخاب کن:"

def route_cb(chat, mid, data, cbid):
    if data == "menu":
        pending.pop(chat, None); answer(cbid); edit(chat, mid, WELCOME, main_menu_kb()); return
    if data == "ips":
        answer(cbid); ips = get_ips()
        txt = "🌐 <b>آدرس‌های اتصال CDN</b>\nدامنه یا IP؛ در کانفیگ همه‌ی لینک‌ها استفاده می‌شوند:\n\n" + "\n".join("• <code>%s</code>" % html.escape(i) for i in ips)
        edit(chat, mid, txt, [[{"text": "✏️ ویرایش لیست", "callback_data": "ips_edit"}], [{"text": "بازگشت", "callback_data": "menu"}]]); return
    if data == "ips_edit":
        pending[chat] = {"stage": "ips_edit"}; answer(cbid)
        edit(chat, mid, "دامنهٔ فعال Cloudflare یا IP تمیز را بفرست (با کاما یا هر خط یکی):\n<code>cdn.example.com, 104.16.96.1</code>"); return
    if data == "new":
        pending[chat] = {"stage": "vol"}; answer(cbid); edit(chat, mid, "📦 حجم لینک را انتخاب کن:", vol_kb()); return
    if data.startswith("vol:"):
        v = data.split(":", 1)[1]
        if v == "custom":
            pending[chat] = {"stage": "vol_custom"}; answer(cbid); edit(chat, mid, "عدد حجم را به <b>گیگابایت</b> بفرست (مثلاً 0.5 یا 25):"); return
        pending[chat] = {"stage": "dur", "vol_gb": float(v)}; answer(cbid)
        edit(chat, mid, "حجم: %s ✅\n⏳ مدت زمان را انتخاب کن:" % ("نامحدود" if float(v) == 0 else "%sGB" % v), dur_kb()); return
    if data.startswith("dur:"):
        d = data.split(":", 1)[1]; st = pending.get(chat, {})
        if d == "custom":
            st["stage"] = "dur_custom"; pending[chat] = st; answer(cbid); edit(chat, mid, "تعداد <b>روز</b> را بفرست (مثلاً 0.5 یا 45):"); return
        st["dur_days"] = int(d); st["stage"] = "name"; pending[chat] = st; answer(cbid)
        edit(chat, mid, "🏷 یک نام برای این لینک بفرست (مثلاً اسم مشتری):", [[{"text": "⏭ بدون نام", "callback_data": "noname"}]])
        return
    if data == "testlink":
        pending.pop(chat, None); answer(cbid, "در حال ساخت…")
        token = create_user(*TEST_LINK)
        if token: edit(chat, mid, result_text(token), [[{"text": "بازگشت به منو", "callback_data": "menu"}]])
        else:     edit(chat, mid, "❌ خطا در ساخت کاربر.", main_menu_kb())
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
    if data.startswith("rn:"):
        token = data[3:]; pending[chat] = {"stage": "rename", "token": token}; answer(cbid)
        edit(chat, mid, "🏷 نام جدید لینک را بفرست:", [[{"text": "بازگشت", "callback_data": "u:%s" % token}]])
        return
    if data.startswith("rstq:"):
        token = data[5:]; answer(cbid)
        edit(chat, mid, "♻️ مصرف نمایشی این لینک صفر شود؟\nآمار مصرف کل حذف نمی‌شود.",
             [[{"text": "بله، ریست کن", "callback_data": "rst:%s" % token}],
              [{"text": "انصراف", "callback_data": "u:%s" % token}]])
        return
    if data.startswith("rst:"):
        token = data[4:]
        answer(cbid, "در حال ریست…")
        if not reset_usage(token):
            edit(chat, mid, "📋 لینک پیدا نشد.", list_kb()); return
        c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
        edit(chat, mid, detail_text(u), detail_kb(token)); return
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
        token = data[2:]; pending.pop(chat, None); refresh_usage(token)
        c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
        if not u: answer(cbid, "یافت نشد"); edit(chat, mid, "📋 لینک‌ها:", list_kb()); return
        answer(cbid); edit(chat, mid, detail_text(u), detail_kb(token)); return
    if data.startswith("del:"):
        token = data[4:]
        if delete_user(token):
            answer(cbid, "حذف شد 🗑"); edit(chat, mid, "📋 لینک‌ها:", list_kb())
        else:
            answer(cbid, "حذف در Xray کامل نشد؛ دوباره تلاش می‌شود")
            edit(chat, mid, "⚠️ حذف در Xray کامل نشد. پنل دوباره تلاش می‌کند.",
                 [[{"text": "بازگشت به لینک", "callback_data": "u:%s" % token}]])
        return
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
            send(chat, "❌ هیچ دامنه یا IPv4 معتبری پیدا نشد. مثل <code>cdn.example.com, 104.16.96.1</code> بفرست.", main_menu_kb()); return
        set_ips(valid); regenerate_all_subs()
        send(chat, "✅ <b>%d آدرس</b> ذخیره و همه‌ی لینک‌ها بروز شدند:\n%s\n\nمشتری‌ها فقط کافیست Update بزنند." % (len(valid), "\n".join("• <code>%s</code>" % html.escape(i) for i in valid)), main_menu_kb()); return
    if st and st.get("stage") in ("addvol_custom", "addtime_custom"):
        is_vol = st["stage"] == "addvol_custom"; token = st["token"]
        try: n = float(text.replace(",", "."))
        except Exception: send(chat, "یک عدد مثبت بفرست:"); return
        if not math.isfinite(n) or n <= 0:
            send(chat, "یک عدد مثبت بفرست:"); return
        (extend_volume if is_vol else extend_time)(token, n); pending.pop(chat, None); refresh_usage(token); maybe_reenable(token)
        c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
        if u: send(chat, "✅ بروز شد.\n\n" + detail_text(u), detail_kb(token))
        return
    if st and st.get("stage") == "rename":
        token = st["token"]
        if not text.strip():
            send(chat, "نام نمی‌تواند خالی باشد؛ یک نام بفرست:"); return
        changed = rename_user(token, text); pending.pop(chat, None)
        c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
        if changed and u: send(chat, "✅ نام لینک تغییر کرد.\n\n" + detail_text(u), detail_kb(token))
        else: send(chat, "❌ لینک پیدا نشد.", main_menu_kb())
        return
    if st and st.get("stage") == "vol_custom":
        try: gb = float(text.replace(",", "."))
        except Exception: send(chat, "یک عدد مثبت بفرست (GB):"); return
        if not math.isfinite(gb) or gb <= 0:
            send(chat, "یک عدد مثبت بفرست (GB):"); return
        st["vol_gb"] = gb; st["stage"] = "dur"; pending[chat] = st
        send(chat, "حجم: %sGB ✅\nحالا مدت را انتخاب کن:" % gb, dur_kb()); return
    if st and st.get("stage") == "dur_custom":
        try: d = float(text.replace(",", "."))
        except Exception: send(chat, "یک عدد مثبت بفرست (روز):"); return
        if not math.isfinite(d) or d <= 0:
            send(chat, "یک عدد مثبت بفرست (روز):"); return
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
    next_outbound_retry = 0
    next_membership_retry = 0
    while True:
        try:
            pid = xray_pid()
            if pid and pid != "0" and pid != last_pid:
                resync_all(); last_pid = pid; meta_set("xray_pid", pid)
            # Credential changes must settle before a pending outbound rewrite
            # restarts Xray and clears the counters needed for legacy migration.
            if meta_get("membership_sync_pending") == "1" and time.time() >= next_membership_retry:
                next_membership_retry = time.time() + 60
                resync_all()
            if (meta_get("outbound_sync_pending") == "1" and
                    meta_get("membership_sync_pending") != "1" and time.time() >= next_outbound_retry):
                next_outbound_retry = time.time() + 300
                refresh_all_usage()  # Xray restart would otherwise discard unbanked counters
                applied, reason = apply_xray_outbounds()
                if applied:
                    meta_set("outbound_sync_pending", "")
                    resync_all()
                else:
                    print("outbound cleanup retry failed:", reason, flush=True)
            refresh_all_usage()
            c = db(); rows = c.execute("SELECT token,uuid,used_bytes,usage_reset_bytes,limit_bytes,expiry_ts,label,disabled_ts,frozen,auth_pending,pending_delete FROM users").fetchall(); c.close()
            now = int(time.time())
            for cur in rows:
                if cur["pending_delete"]:
                    if delete_user(cur["token"]):
                        notify_admin("🗑 لینک «%s» پس از تلاش دوباره حذف شد." % cur["label"])
                    continue
                if cur["auth_pending"]:
                    tags = tags_to_cut_for_user(cur["token"])
                    removed = xr_remove_user(cur["token"])
                    force_disconnect(tags)
                    if not removed: continue
                    _rotate_legacy_emails(cur["token"])
                    c = db(); c.execute("UPDATE users SET auth_pending=0 WHERE token=?", (cur["token"],)); c.commit(); c.close()
                if cur["frozen"]: continue   # manually frozen -> ignore all auto disable/grace/reenable
                reason = exhaust_reason(cur, now)
                if reason:
                    if not cur["disabled_ts"]:                       # just ran out -> disable + notify, start 48h grace
                        if disable_user(cur["token"]):
                            notify_admin("⏸ لینک «%s» غیرفعال شد (%s).\nتا ۴۸ ساعت قابل تمدید است؛ بعد از آن خودکار حذف می‌شود." % (cur["label"], reason))
                        else:
                            notify_admin("⚠️ غیرفعال‌سازی لینک «%s» در Xray کامل نشد؛ پنل دوباره تلاش می‌کند." % cur["label"])
                    elif now - cur["disabled_ts"] >= GRACE_SECONDS:  # grace over -> delete + notify
                        if delete_user(cur["token"]):
                            notify_admin("🗑 لینک «%s» پس از ۴۸ ساعت مهلتِ تمدید، خودکار حذف شد." % cur["label"])
                elif cur["disabled_ts"]:                             # got renewed -> bring back live + notify
                    if reenable_user(cur["token"]):
                        notify_admin("▶️ لینک «%s» تمدید شد و دوباره فعال شد." % cur["label"])
        except Exception as e:
            print("enforcer err", e, flush=True)
        time.sleep(POLL)

# ================= ADMIN PANEL (web) =================
LOGIN_TTL = 600      # one-time login link lifetime (s)
SESS_TTL  = int(ENV.get("SESS_DAYS", "30")) * 86400   # stay logged in this long
_login_tokens = {}   # token -> expires_ts
_sessions = {}       # sid -> {"exp": ts, "csrf": str}; mirror of the meta rows below

# Sessions are ALSO stored in `meta` as sess_<sid>, because the panel used to log the
# admin out on every bot restart (deploy, reboot, crash) — an in-RAM dict can't outlive
# the process, so a "30 day" cookie meant nothing.
def _sess_key(sid): return "sess_" + sid

def _sess_put(sid, rec):
    _sessions[sid] = rec
    try:                       # best effort: a DB hiccup must not break logging in
        meta_set(_sess_key(sid), json.dumps(rec))
    except Exception:
        pass

def _sess_get(sid):
    s = _sessions.get(sid)
    if s: return s
    try:
        raw = meta_get(_sess_key(sid))          # survives a bot restart
        if not raw: return None
        s = json.loads(raw)
        if not isinstance(s, dict) or "csrf" not in s: return None
    except Exception:
        return None
    _sessions[sid] = s
    return s

def _sess_drop(sid):
    _sessions.pop(sid, None)
    try:
        c = db(); c.execute("DELETE FROM meta WHERE k=?", (_sess_key(sid),)); c.commit(); c.close()
    except Exception:
        pass

def _prune_auth(now):
    for k in [k for k, v in _login_tokens.items() if v <= now]: _login_tokens.pop(k, None)
    for k in [k for k, s in _sessions.items() if s["exp"] <= now]: _sessions.pop(k, None)
    try:
        c = db(); c.execute("DELETE FROM meta WHERE k LIKE 'sess_%' AND CAST(json_extract(v,'$.exp') AS INTEGER) <= ?",
                            (now,)); c.commit(); c.close()
    except Exception:
        pass          # json_extract needs sqlite >= 3.9; expiry is enforced below regardless

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
    _sess_put(sid, {"exp": now + SESS_TTL, "csrf": csrf})
    return sid, csrf

def session_csrf(sid, now=None):
    now = now or int(time.time())
    s = _sess_get(sid) if sid else None
    if not s or s.get("exp", 0) <= now:
        if sid: _sess_drop(sid)
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
    rows = c.execute("SELECT token,label,used_bytes,usage_reset_bytes,limit_bytes,expiry_ts,disabled_ts,frozen,created_ts,"
                     "config_override IS NOT NULL AS custom_config FROM users ORDER BY created_ts DESC").fetchall()
    daily = {r["token"]: int(r["v"] or 0) for r in
             c.execute("SELECT token, max(end_used-start_used,0) v FROM usage_daily WHERE day=?", (today,)).fetchall()}
    c.close()
    out = []
    for r in rows:
        d = dict(r); d["current_used_bytes"] = current_usage(d)
        d["today"] = daily.get(r["token"], 0); out.append(d)
    return out

ADMIN_ICONS = """<svg xmlns="http://www.w3.org/2000/svg" class="icon-sprite" aria-hidden="true">
<symbol id="ico-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
<symbol id="ico-sliders" viewBox="0 0 24 24"><path d="M4 7h9m4 0h3M4 17h3m4 0h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/></symbol>
<symbol id="ico-route" viewBox="0 0 24 24"><circle cx="5" cy="6" r="2"/><circle cx="19" cy="18" r="2"/><path d="M7 6h7a4 4 0 0 1 0 8h-4a4 4 0 0 0 0 8h7"/></symbol>
<symbol id="ico-search" viewBox="0 0 24 24"><circle cx="10.8" cy="10.8" r="6.3"/><path d="m16 16 4.5 4.5"/></symbol>
<symbol id="ico-check" viewBox="0 0 24 24"><path d="m5 12 4.5 4.5L19 7"/></symbol>
<symbol id="ico-trash" viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v6m4-6v6"/></symbol>
<symbol id="ico-refresh" viewBox="0 0 24 24"><path d="M20 7v5h-5M4 17v-5h5M6 9a7 7 0 0 1 12-2l2 5M4 12l2 5a7 7 0 0 0 12-2"/></symbol>
<symbol id="ico-back" viewBox="0 0 24 24"><path d="m9 5 7 7-7 7M16 12H4"/></symbol>
<symbol id="ico-logout" viewBox="0 0 24 24"><path d="M10 4H5v16h5m5-4 4-4-4-4m4 4H9"/></symbol>
<symbol id="ico-moon" viewBox="0 0 24 24"><path d="M20 15.5A8 8 0 0 1 8.5 4 8 8 0 1 0 20 15.5Z"/></symbol>
<symbol id="ico-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></symbol>
</svg>"""

def _icon(name):
    return '<svg class="icon" aria-hidden="true"><use href="#ico-%s"></use></svg>' % name

ADMIN_CSS = """
:root{--paper:#F4F7FB;--card:#FFFFFF;--ink:#17253D;--accent:#256BD1;--accent-text:#FFFFFF;--ok:#179773;--warn:#BD7A17;--dng:#D4545C;--frz:#568BBF;--mut:#68788F;--line:#DCE5F0;--soft:#EAF1FA;--hero:#EAF3FF;--shadow:0 12px 36px rgba(33,60,99,.06);--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;--sans:"Vazirmatn","Segoe UI",Tahoma,system-ui,sans-serif;--display:"Estedad","Vazirmatn","Segoe UI",Tahoma,system-ui,sans-serif}
:root[data-theme=dark]{--paper:#101827;--card:#182438;--ink:#EDF3FC;--accent:#83B4FB;--accent-text:#10213A;--ok:#55D3A5;--warn:#F2BC69;--dng:#FF929C;--frz:#8BBCEB;--mut:#A7B5C8;--line:#31425A;--soft:#203149;--hero:#192F4B;--shadow:0 12px 36px rgba(0,0,0,.13)}
*{box-sizing:border-box}
.icon-sprite{position:absolute;width:0;height:0;overflow:hidden}
.icon{width:17px;height:17px;flex:0 0 17px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
:root .theme-sun{display:none}
:root[data-theme=dark] .theme-moon{display:none}
:root[data-theme=dark] .theme-sun{display:block}
html,body{margin:0;max-width:100%}
body{min-height:100vh;background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.65;padding:0 24px 64px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto}
a{color:inherit;text-decoration:none}
a:hover{color:var(--accent)}
.mono,.n{font-variant-numeric:tabular-nums}
.mono{font-family:var(--mono)}
.n{unicode-bidi:isolate;direction:ltr}
.top{display:flex;align-items:center;gap:24px;min-height:76px;margin-bottom:34px;border-bottom:1px solid var(--line)}
.brand{display:inline-flex;align-items:center;gap:11px;font-family:var(--display);font-size:19px;font-weight:800;letter-spacing:-.02em;white-space:nowrap}
.brand:hover{color:var(--ink)}
.dot-sig{position:relative;width:34px;height:34px;flex:0 0 34px;border-radius:10px;background:var(--accent)}
.dot-sig:before{content:"";position:absolute;inset:9px 9px 9px 10px;background:linear-gradient(to top,var(--accent-text) 0 35%,transparent 35% 100%) left bottom/3px 100% no-repeat,linear-gradient(to top,var(--accent-text) 0 65%,transparent 65% 100%) center bottom/3px 100% no-repeat,linear-gradient(to top,var(--accent-text) 0 100%,transparent 100% 100%) right bottom/3px 100% no-repeat}
.crumb{font-size:13px;font-weight:650;color:var(--mut)}
.crumb a{display:inline-flex;align-items:center;gap:5px}
.crumb .icon{width:15px;height:15px;flex-basis:15px}
.crumb a:hover{color:var(--accent)}
.rightnav{margin-inline-start:auto;display:flex;align-items:center;gap:10px}
.pagehead{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin:0 0 22px}
.pagehead h1{font-family:var(--display);font-size:clamp(27px,3.2vw,38px);line-height:1.3;letter-spacing:-.025em;margin:2px 0 4px;font-weight:800}
.pagehead p{font-size:13px;color:var(--mut);margin:0}
.eyebrow{display:inline-block;font-size:11px;font-weight:800;letter-spacing:.02em;color:var(--accent);margin-bottom:3px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);padding:22px;margin:0 0 18px;min-width:0}
.card h2{font-family:var(--display);font-size:16px;line-height:1.4;margin:0 0 15px;font-weight:750}
.hero{background:var(--hero);border-color:transparent;box-shadow:none}
.dashboard-hero{display:grid;grid-template-columns:minmax(250px,.8fr) minmax(0,1.2fr);grid-template-rows:auto auto auto 1fr;column-gap:32px;row-gap:6px;padding:27px 30px}
.dashboard-hero>.eyebrow,.dashboard-hero>.big,.dashboard-hero>.metrics,.dashboard-hero>.pills{grid-column:1}
.dashboard-hero>.eyebrow{grid-row:1}
.dashboard-hero>.big{grid-row:2}
.dashboard-hero>.metrics{grid-row:3}
.dashboard-hero>.pills{grid-row:4;align-self:end}
.dashboard-hero>.chart{grid-column:2;grid-row:1/5;display:flex;flex-direction:column;justify-content:flex-end;margin:0;border-inline-start:1px solid var(--line);padding-inline-start:28px}
.dashboard-hero .chart svg{height:130px}
.big{font-family:var(--display);font-size:clamp(38px,5vw,58px);font-weight:800;line-height:1.15;letter-spacing:-.035em}
.big .n{font-family:var(--mono);letter-spacing:-.07em}
.big small{font-family:var(--sans);font-size:16px;font-weight:650;margin-inline-start:6px;letter-spacing:0}
.title{font-family:var(--display);font-size:clamp(24px,3vw,34px);font-weight:800;line-height:1.3;margin:5px 0}
.metrics{display:flex;gap:26px;flex-wrap:wrap;margin-top:10px}
.metric .k{font-size:11px;color:var(--mut);font-weight:700}
.metric .v{font-size:18px;font-weight:750;margin-top:2px}
.pills{display:flex;gap:8px;flex-wrap:wrap;margin-top:20px}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:650;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:5px 11px}
.linkbadge{align-self:flex-start;display:inline-flex;width:max-content;max-width:100%;white-space:nowrap;font-size:10px;font-weight:700;line-height:1.3;border-radius:999px;background:var(--soft);color:var(--accent);padding:3px 8px;margin-top:3px}
.d,.st{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--mut);flex:0 0 auto}
.d.ok,.st.ok{background:var(--ok)}
.d.off,.st.off{background:var(--mut)}
.st{width:10px;height:10px}
.st.warn{background:var(--warn)}
.st.dng{background:var(--dng)}
.st.frz{background:var(--frz)}
.st.on{box-shadow:0 0 0 4px color-mix(in srgb,var(--ok) 18%,transparent)}
.chart{margin-top:20px}
.chart>.eyebrow{color:var(--mut);margin-bottom:12px}
svg{display:block;width:100%;color:var(--accent)}
.btn .icon{color:inherit}
.bar{cursor:pointer}
.tt{position:fixed;display:none;background:var(--ink);color:var(--card);border-radius:9px;padding:6px 10px;font-family:var(--mono);font-size:12px;pointer-events:none;z-index:60;box-shadow:var(--shadow)}
.u{display:grid;grid-template-columns:10px minmax(0,1fr) minmax(100px,160px) minmax(105px,142px);align-items:center;gap:16px;padding:15px 6px;border-top:1px solid var(--line);color:var(--ink);min-width:0}
.u[hidden]{display:none}
.u:hover{background:var(--soft);border-radius:9px}
.u:first-of-type{border-top:0}
.nm{flex:1 1 auto;min-width:0;display:flex;flex-direction:column}
.nm b{font-size:14px;font-weight:750;line-height:1.4}
.nm .sub{font-size:11px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono)}
.meter{min-width:0}
.trk{display:block;height:7px;background:var(--line);border-radius:99px;overflow:hidden}
.fil{display:block;height:100%;background:var(--accent);border-radius:99px}
.rt{font-size:12px;color:var(--mut);text-align:end;min-width:0;line-height:1.5;font-weight:650}
.btn,.tbtn{display:inline-flex;align-items:center;justify-content:center;gap:7px;font-family:inherit;font-size:13px;font-weight:750;border:1px solid transparent;border-radius:10px;padding:10px 15px;cursor:pointer;text-decoration:none;transition:background .18s,transform .18s,box-shadow .18s}
.btn{background:var(--accent);color:var(--accent-text);box-shadow:0 4px 10px color-mix(in srgb,var(--accent) 20%,transparent)}
.btn:hover{color:var(--accent-text);transform:translateY(-1px);box-shadow:0 7px 16px color-mix(in srgb,var(--accent) 22%,transparent)}
.btn:active,.tbtn:active{transform:translateY(1px)}
.btn.ghost{background:var(--card);color:var(--ink);border-color:var(--line);box-shadow:none}
.btn.ghost:hover,.tbtn:hover{color:var(--accent);border-color:var(--accent);background:var(--soft);box-shadow:none}
.btn.danger{background:var(--dng);color:#FFFFFF;box-shadow:none}
.tbtn{width:38px;height:38px;padding:0;font-size:17px;line-height:1;background:var(--card);color:var(--ink);border-color:var(--line)}
input[type=text],input[type=number],input[type=search],input:not([type]),textarea,select{background:var(--card);border:1px solid var(--line);border-radius:10px;color:var(--ink);padding:10px 12px;font:inherit;font-size:13px;min-height:42px;min-width:0;outline:none}
input[type=text],input[type=number],input[type=search],input:not([type]){flex:1 1 150px;max-width:360px}
input[type=number]{max-width:115px}
textarea{width:100%;max-width:100%;font-family:var(--mono);line-height:1.5;resize:vertical}
select{width:100%;max-width:100%}
input::placeholder,textarea::placeholder{color:var(--mut);opacity:.82}
input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 18%,transparent)}
input[type=checkbox]{width:17px;height:17px;accent-color:var(--accent);flex:0 0 auto}
.row{display:flex;gap:9px;flex-wrap:wrap;align-items:center}
form.row{margin:0 0 8px}
.grid{display:grid;gap:13px}
.create-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.field{display:grid;gap:6px;min-width:0}
.field>span{font-size:12px;font-weight:700;color:var(--mut)}
.field input{width:100%;max-width:none}
.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.toolbar .row{margin-inline-start:auto}
#linksearch{width:220px;max-width:100%}
.eprow{display:flex;align-items:center;gap:12px;justify-content:space-between;border:1px solid var(--line);border-radius:13px;padding:16px;background:var(--card)}
.eplabel{display:flex;align-items:center;gap:10px;flex:1;min-width:0;cursor:pointer}
.eplabel b{font-weight:750}
.eptag{display:block;font-size:11px;color:var(--mut);font-family:var(--mono);margin-top:2px}
.hint{font-size:12px;color:var(--mut);font-weight:550;margin:0 0 7px}
code{display:block;font-family:var(--mono);background:var(--soft);border:1px solid var(--line);border-radius:9px;padding:8px 10px;word-break:break-all;font-size:12px;color:var(--ink);unicode-bidi:plaintext}
.hint code,p code{display:inline;border-radius:5px;padding:1px 5px}
.obmsg{padding:12px 14px;font-size:13px}
.obmsg.good{border-color:var(--ok);background:color-mix(in srgb,var(--ok) 9%,var(--card))}
.obmsg.bad{border-color:var(--dng);background:color-mix(in srgb,var(--dng) 9%,var(--card))}
.pill.oball{background:color-mix(in srgb,var(--ok) 12%,var(--card));color:var(--ink)}
.pill.obwarn{background:color-mix(in srgb,var(--warn) 14%,var(--card));color:var(--ink)}
.obres{margin-top:10px;padding:10px 12px;border:1px dashed var(--line);border-radius:10px;background:var(--soft);font-family:var(--mono);font-size:12px;word-break:break-word;unicode-bidi:plaintext}
.switch{display:flex;align-items:center;gap:12px;cursor:pointer;user-select:none}
.switch input{position:absolute;opacity:0;width:0;height:0}
.switch .knob{position:relative;flex:0 0 auto;width:46px;height:26px;background:var(--line);border-radius:999px;transition:background .18s}
.switch .knob::after{content:"";position:absolute;top:3px;inset-inline-start:3px;width:20px;height:20px;border-radius:50%;background:var(--card);box-shadow:0 1px 4px rgba(0,0,0,.15);transition:inset-inline-start .18s}
.switch input:checked+.knob{background:var(--accent)}
.switch input:checked+.knob::after{inset-inline-start:23px}
.switch input:focus-visible+.knob{outline:2px solid var(--accent);outline-offset:3px}
.switch .swtxt{display:flex;flex-direction:column;line-height:1.35}
.switch .swsub{font-size:12px;color:var(--mut)}
.switch.on .swtxt b{color:var(--accent)}
.spin{display:inline-block;width:11px;height:11px;margin-inline-end:7px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:sp .7s linear infinite;vertical-align:-1px}
@keyframes sp{to{transform:rotate(360deg)}}
button[disabled]{opacity:.65;cursor:progress}
.warnpulse{background:var(--warn)!important;color:#17253D!important}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media (max-width:820px){.dashboard-hero{grid-template-columns:1fr;grid-template-rows:auto;gap:6px}.dashboard-hero>.eyebrow,.dashboard-hero>.big,.dashboard-hero>.metrics,.dashboard-hero>.pills,.dashboard-hero>.chart{grid-column:1;grid-row:auto}.dashboard-hero>.chart{border-inline-start:0;border-top:1px solid var(--line);padding:17px 0 0;margin-top:12px}.dashboard-hero .chart svg{height:100px}}
@media (max-width:620px){body{padding:0 14px 40px}.top{min-height:66px;margin-bottom:25px;gap:12px}.brand{font-size:17px}.crumb{display:none}.rightnav{gap:6px}.rightnav .btn{padding:8px 10px}.pagehead{align-items:stretch;flex-direction:column}.pagehead .btn{align-self:flex-start}.card{padding:17px;border-radius:15px}.dashboard-hero{padding:21px}.metrics{gap:16px}.create-grid{grid-template-columns:1fr}.toolbar .row{margin-inline-start:0}.toolbar #linksearch{width:100%}.u{grid-template-columns:10px minmax(0,1fr) auto;gap:8px 12px;padding:13px 4px}.u .st{grid-column:1;grid-row:1}.u .nm{grid-column:2;grid-row:1}.u .rt{grid-column:3;grid-row:1}.u .meter{grid-column:2/4;grid-row:2}.rt{font-size:11px}.row>.btn{flex:1 1 auto}.btn{min-height:42px}.eprow{padding:12px}.eprow .row{align-items:flex-start}}
@media (prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
"""

def _page(title, inner):
    return ("<!doctype html><html lang=fa dir=rtl><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta name=color-scheme content='light dark'>"
            "<link rel=icon href=\"data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect x='2' y='2' width='28' height='28' rx='8' fill='%%23256BD1'/><path d='M10 22v-6m6 6V11m6 11V7' stroke='%%23fff' stroke-width='3' stroke-linecap='round'/></svg>\">"
            "<title>%s</title>"
            "<script>(function(){try{var t=localStorage.getItem('mj-theme')||((window.matchMedia&&matchMedia('(prefers-color-scheme:dark)').matches)?'dark':'light');document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>"
            "<style>%s</style></head><body>%s<div class=wrap>%s</div><div id=tt class=tt></div>"
            "<script>function toggleTheme(){var h=document.documentElement,d=h.getAttribute('data-theme')==='dark'?'light':'dark';h.setAttribute('data-theme',d);try{localStorage.setItem('mj-theme',d);}catch(e){}}"
            "(function(){var t=document.getElementById('tt');document.addEventListener('click',function(e){"
            "var b=e.target.closest&&e.target.closest('.bar');if(b){t.textContent=b.getAttribute('data-t')+' — '+b.getAttribute('data-v');"
            "t.style.display='block';var w=t.offsetWidth;t.style.left=Math.max(6,Math.min(e.clientX-w/2,window.innerWidth-w-6))+'px';"
            "t.style.top=Math.max(6,e.clientY-40)+'px';}else{t.style.display='none';}});})();</script>"
            "</body></html>" % (html.escape(title), ADMIN_CSS, ADMIN_ICONS, inner))

def _html(page):
    return 200, {"Content-Type": "text/html; charset=utf-8"}, page.encode("utf-8")

def _top(crumb="", csrf=None):
    right = ("<span class=crumb>%s</span>" % crumb) if crumb else ""
    if csrf:
        right += ("<form method=post action='/a/logout' style='margin:0'>"
                  "<input type=hidden name=csrf value='%s'>"
                  "<button class='btn ghost'>%s خروج</button></form>") % (csrf, _icon("logout"))
    right += ("<button id=themebtn type=button class=tbtn onclick=\"toggleTheme()\" "
              "aria-label='تغییر تم' title='تغییر تم'>"
              "<span class=theme-moon>%s</span><span class=theme-sun>%s</span></button>") % (
                  _icon("moon"), _icon("sun"))
    return ("<header class=top><a class=brand href='/a/'><span class=dot-sig aria-hidden=true></span>Mohajer</a>"
            "<span class=rightnav>%s</span></header>" % right)

def _page_heading(title, detail="", action=""):
    return ("<div class=pagehead><div><span class=eyebrow>پنل مدیریت مهاجر</span>"
            "<h1>%s</h1><p>%s</p></div>%s</div>" %
            (html.escape(title), html.escape(detail), action))

def _back(url, label):
    return "<a href='%s'>%s %s</a>" % (html.escape(url, quote=True), _icon("back"), html.escape(label))

def _metric_big(b):
    s = fmt_bytes(b); p = s.rsplit(" ", 1)
    return ("%s<small>%s</small>" % (p[0], p[1])) if len(p) == 2 else s

def svg_bars(series, w=760, h=96):
    vals = [v for _, v in series]; mx = max(vals + [1]); n = len(series) or 1; bw = w / n; bars = ""
    for i, (lab, v) in enumerate(series):
        bh = max(3.0, (v / mx) * (h - 6)); val = fmt_bytes(v)
        bars += ('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" fill="currentColor" opacity="%s"></rect>'
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
    lim = u["limit_bytes"] or 0; used = u["current_used_bytes"]; dis = u["disabled_ts"]
    if lim > 0:
        pct = min(100, int(used * 100 / lim))
        st = "dng" if dis else ("warn" if pct >= 90 else "ok")
        fill = "<span class=fil style='width:%d%%'></span>" % pct
    else:
        st = "off" if dis else "ok"
        fill = "<span class=fil style='width:100%;opacity:.3'></span>"
    if u["frozen"]: st = "frz"          # manual freeze -> icy square, overrides quota colour
    if online and not u["frozen"]: st += " on"   # live now -> pulsing halo in the square's own colour
    badge = "<span class=linkbadge>اختصاصی</span>" if u["custom_config"] else ""
    return ("<a class=u href='/a/user?token=%s'><span class='st %s'></span>"
            "<span class=nm><b>%s</b>%s<span class=sub><span class=n>%s / %s</span></span></span>"
            "<span class=meter><span class=trk>%s</span></span>"
            "<span class=rt><span class=n>%s</span><br>%s</span></a>") % (
        u["token"], st, html.escape(u["label"]),
        badge, fmt_bytes(used), human_limit(lim), fill,
        fmt_bytes(u["today"]), human_expiry(u["expiry_ts"]))

def render_dashboard(csrf):
    total, today, last30 = panel_usage_summary()
    ov = users_overview()
    active = sum(1 for u in ov if not u["disabled_ts"]); disabled = len(ov) - active
    chart = svg_bars(daily_series(7))
    hero = ("<div class='card hero dashboard-hero'><div class=eyebrow>مصرف امروز</div><div class=big><span class=n>%s</span></div>"
            "<div class=metrics><div class=metric><div class=k>کل</div><div class='v mono'><span class=n>%s</span></div></div>"
            "<div class=metric><div class=k>۳۰ روز اخیر</div><div class='v mono'><span class=n>%s</span></div></div></div>"
            "<div class=pills><span class=pill><span class='d ok'></span>%d فعال</span>"
            "<span class=pill><span class='d off'></span>%d غیرفعال</span></div>"
            "<div class=chart><div class=eyebrow>۷ روز اخیر</div>%s</div></div>") % (
        _metric_big(today), fmt_bytes(total), fmt_bytes(last30), active, disabled, chart)
    onmap = xr_online_map() or {}
    rows = "".join(_user_row(u, u["token"] in onmap) for u in ov) or "<div class=u><span class=nm style='color:var(--mut)'>هنوز لینکی نساخته‌ای</span></div>"
    users = ("<div class=card><div class=toolbar>"
             "<h2 style='margin:0'>لینک‌ها</h2>"
             "<input id=linksearch type=search placeholder='جست‌وجوی لینک' aria-label='جست‌وجوی لینک'>"
             "<span class=row>"
             "<a class='btn ghost' href='/a/config'>%s پیکربندی</a>"
             "</span></div>%s<p id=search-empty class=hint hidden>لینکی با این نام پیدا نشد.</p></div>"
             "<script>(function(){var q=document.getElementById('linksearch'),e=document.getElementById('search-empty');"
             "if(!q)return;q.addEventListener('input',function(){var term=q.value.trim().toLocaleLowerCase(),seen=0;"
             "document.querySelectorAll('a.u').forEach(function(row){var hit=row.textContent.toLocaleLowerCase().includes(term);"
             "row.hidden=!hit;if(hit)seen++;});e.hidden=seen!==0;});})();</script>") % (_icon("sliders"), rows)
    heading = _page_heading("نمای کلی", "مصرف و وضعیت لینک‌ها در یک نگاه",
                            "<a class=btn href='/a/new'>%s لینک جدید</a>" % _icon("plus"))
    return _page("پنل", _top("", csrf) + heading + hero + users)

def _form(action, fields, csrf, btn, cls="btn"):
    inner = "".join(fields) + "<input type=hidden name=csrf value='%s'>" % csrf
    return "<form method=post action='%s' class=row>%s<button class='%s'>%s</button></form>" % (action, inner, cls, btn)

def render_user(token, csrf, msg=""):
    c = db(); u = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if not u:
        return _page("یافت نشد", _top(_back("/a/", "داشبورد"), csrf) + "<div class=card>لینکی با این شناسه پیدا نشد.</div>")
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
        _form("/a/addvol", [tk, "<input type=number name=gb step=any min=0 placeholder='حجم (گیگ)'>"], csrf, "افزودن حجم") +
        _form("/a/addtime", [tk, "<input type=number name=days step=any min=0 placeholder='مدت (روز)'>"], csrf, "افزودن زمان") +
        _form("/a/rename", [tk, "<input type=text name=name placeholder='نام تازه'>"], csrf, "تغییر نام") +
        _form("/a/unlimit", [tk, "<input type=hidden name=field value=limit_bytes>"], csrf, "حجم نامحدود", "btn ghost") +
        _form("/a/unlimit", [tk, "<input type=hidden name=field value=expiry_ts>"], csrf, "زمان نامحدود", "btn ghost"))
    reset = "<a class='btn ghost' href='/a/reset?token=%s'>%s ریست مصرف</a>" % (token, _icon("refresh"))
    dele = "<a class='btn danger' href='/a/del?token=%s'>%s حذف لینک</a>" % (token, _icon("trash"))
    hero = ("<div class='card hero'><div class=eyebrow><span class='st %s' style='display:inline-block;margin-inline-start:6px;vertical-align:middle'></span>%s</div>"
            "<h1 class=title>%s</h1>"
            "<div class=metrics><div class=metric><div class=k>مصرف</div><div class=v><span class=n>%s / %s</span></div></div>"
            "<div class=metric><div class=k>امروز</div><div class='v mono'><span class=n>%s</span></div></div>"
            "<div class=metric><div class=k>انقضا</div><div class=v>%s</div></div></div>"
            "<div class=chart><div class=eyebrow>۳۰ روز اخیر</div>%s</div></div>") % (
        st, stlabel, html.escape(u["label"]),
        fmt_bytes(current_usage(u)), human_limit(u["limit_bytes"]), fmt_bytes(today_u),
        human_expiry(u["expiry_ts"]), chart)
    link = "<div class=card><h2>لینک اشتراک</h2><code>%s</code></div>" % sub_url(token)
    config_link = "<a class='btn ghost' href='/a/user-config?token=%s'>%s تنظیمات لینک</a>" % (token, _icon("sliders"))
    actions = "<div class=card><h2>مدیریت</h2><div class=grid>%s</div><div class=row style='margin-top:10px'>%s%s%s</div></div>" % (forms, config_link, reset, dele)
    notice = ("<div class=card role=alert>%s</div>" % html.escape(msg)) if msg else ""
    return _page("کاربر", _top(_back("/a/", "داشبورد"), csrf) + notice + hero + frz_card + link + actions)

def render_new(csrf):
    f = ("<form method=post action='/a/new' class=grid>"
         "<div class=create-grid>"
         "<label class=field><span>نام مشتری</span><input type=text name=name placeholder='مثلاً علی' required></label>"
         "<label class=field><span>حجم (گیگابایت)</span><input type=number name=gb step=any min=0 "
         "placeholder='۰ = نامحدود' required></label>"
         "<label class=field><span>مدت (روز)</span><input type=number name=days step=any min=0 "
         "placeholder='۰ = نامحدود' required></label></div>"
         "<input type=hidden name=csrf value='%s'>"
         "<div class=row><button class=btn>%s ساخت لینک</button></div></form>") % (_config_html(csrf), _icon("plus"))
    return _page("لینک جدید", _top(_back("/a/", "داشبورد"), csrf) +
                 _page_heading("لینک جدید", "حجم، مدت و نام مشتری را تعیین کنید.") +
                 "<div class=card>%s</div>" % f)

def render_delconfirm(token, csrf):
    f = _form("/a/delete", ["<input type=hidden name=token value='%s'>" % token,
                            "<input type=hidden name=confirm value=yes>"], csrf, "بله، حذف کن", "btn danger")
    return _page("حذف", _top(_back("/a/user?token=%s" % token, "بازگشت"), csrf) +
                 "<div class=card><h2>حذف لینک</h2><p style='color:var(--mut);margin:0 0 12px'>"
                 "این کار برگشت‌ناپذیر است؛ لینک و کانفیگ‌های این مشتری حذف می‌شوند.</p>"
                 "<div class=row>%s<a class='btn ghost' href='/a/user?token=%s'>انصراف</a></div></div>" % (f, token))

def render_resetconfirm(token, csrf):
    c = db(); u = c.execute("SELECT label FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if not u:
        return _page("یافت نشد", _top(_back("/a/", "داشبورد"), csrf) + "<div class=card>لینک پیدا نشد.</div>")
    f = _form("/a/reset", ["<input type=hidden name=token value='%s'>" % token,
                           "<input type=hidden name=confirm value=yes>"], csrf, "بله، ریست کن")
    return _page("ریست مصرف", _top(_back("/a/user?token=%s" % token, "بازگشت"), csrf) +
                 "<div class=card><h2>ریست مصرف «%s»</h2><p style='color:var(--mut);margin:0 0 12px'>"
                 "مصرف دورهٔ جاری صفر می‌شود؛ آمار مصرف کل حذف نخواهد شد.</p>"
                 "<div class=row>%s<a class='btn ghost' href='/a/user?token=%s'>انصراف</a></div></div>" %
                 (html.escape(u["label"]), f, token))

def _config_html(value):
    return html.escape(str(value), quote=True)

def _config_message(msg):
    if not msg: return ""
    bad = str(msg).startswith(("خطا", "error:", "Error:"))
    return "<div class='card obmsg %s' role='%s'>%s</div>" % (
        "bad" if bad else "good", "alert" if bad else "status", _config_html(msg))

def _sync_notice():
    notes = []
    if meta_get("membership_sync_pending") == "1":
        notes.append("اتصال‌ها هنوز کامل همگام نشده‌اند؛ پنل دوباره تلاش می‌کند.")
    if meta_get("outbound_sync_pending") == "1":
        notes.append("تغییر خروجی‌ها هنوز در Xray اعمال نشده است؛ پنل دوباره تلاش می‌کند.")
    return _config_message("خطا: " + " ".join(notes)) if notes else ""

def _render_config_fields(settings, editable=True):
    """The same provisioned endpoint controls for the global and per-link pages."""
    recipe = settings.get("recipe") or {}
    endpoint_settings = settings.get("endpoint_settings") or {}
    ips = settings.get("ips") or []
    hosts = approved_hosts()
    disabled = "" if editable else " disabled"
    rows = []
    for ep in ENDPOINTS:
        tag = ep["tag"]; key = _config_html(tag)
        r = recipe.get(tag) or {}
        cfg = endpoint_settings.get(tag) or _endpoint_defaults(ep)
        name = _config_html(ep.get("label", tag))
        is_reality = "reality" in ep
        try: count = max(0, int(r.get("count", 0)))
        except (TypeError, ValueError): count = 0
        head = ("<div class=row style='justify-content:space-between'>"
                "<label class=eplabel><input type=checkbox name='en_%s'%s%s>"
                "<span><b>%s</b><span class=eptag>%s · %s</span></span></label>"
                "<label class=hint>تعداد <input type=number name='cnt_%s' value='%d' min=0%s "
                "aria-label='تعداد %s'></label></div>") % (
                    key, " checked" if r.get("enabled", True) else "", disabled,
                    name, key, "REALITY مستقیم" if is_reality else _config_html(
                        "%s / %s" % (ep.get("proto", ""), ep.get("net", ""))),
                    key, count, disabled, key)
        label = ("<label class=hint for='label_%s'>نام کانفیگ</label>"
                 "<input type=text id='label_%s' name='label_%s' maxlength=64 value='%s'%s "
                 "style='width:100%%;max-width:100%%'>") % (
                     key, key, key, _config_html(cfg.get("label", ep.get("label", tag))), disabled)
        if is_reality:
            ports = "<p class=hint>پورت مستقیمِ آماده‌شده: <span class=n>%s</span></p>" % _config_html(ep.get("port", ""))
            host = "<p class=hint>Host و SNI کلادفلر برای REALITY کاربرد ندارد.</p>"
            fragment = "<p class=hint>Fragment برای این کانفیگ کاربرد ندارد.</p>"
        else:
            port_items = []
            for field, title in (("tls", "TLS"), ("notls", "بدون TLS")):
                allowed = ep.get("tls_ports" if field == "tls" else "notls_ports", [])
                selected = cfg.get("tls_ports" if field == "tls" else "notls_ports", [])
                if not allowed: continue
                checks = "".join(
                    "<label class=pill><input type=checkbox name='%s_%s_%s'%s%s>"
                    "<span class=n>%s</span></label>" % (
                        field, key, _config_html(port), " checked" if port in selected else "",
                        disabled, _config_html(port)) for port in allowed)
                port_items.append("<div><span class=hint>%s</span><div class=row>%s</div></div>" % (title, checks))
            ports = "<div class=grid>%s</div>" % "".join(port_items)
            selected_host = next((i for i, pair in enumerate(hosts)
                                  if pair["host"] == cfg.get("host") and pair["sni"] == cfg.get("sni")), 0)
            options = "".join("<option value='%d'%s>%s</option>" % (
                i, " selected" if i == selected_host else "",
                _config_html("Host: %s · SNI: %s" % (pair["host"], pair["sni"])))
                for i, pair in enumerate(hosts))
            host = ("<label class=hint for='hostidx_%s'>جفت Host / SNI آماده‌شده</label>"
                    "<select id='hostidx_%s' name='hostidx_%s'%s>%s</select>") % (
                        key, key, key, disabled, options)
            if ep.get("proto") == "vmess":
                fragment = "<p class=hint>Fragment در لینک VMess پشتیبانی نمی‌شود.</p>"
            else:
                fragment = ("<label class=hint for='fm_%s'>Fragment JSON (خالی = غیرفعال)</label>"
                            "<textarea id='fm_%s' name='fm_%s' rows=3 dir=ltr%s>%s</textarea>") % (
                                key, key, key, disabled, _config_html(cfg.get("fragment_fm", "")))
        rows.append("<div class=eprow style='display:block'><div class=grid>%s%s%s%s%s</div></div>" % (
            head, label, ports, host, fragment))
    return ("<h2>نوع و تعداد کانفیگ‌ها</h2>"
            "<p class=hint>فقط پورت‌ها، مسیرها و جفت‌های Host / SNI آماده‌شده روی سرور قابل انتخاب‌اند. "
            "تعداد سقفی ندارد؛ تعداد بیشتر روی پورت‌ها و آدرس‌های اتصال پخش می‌شود. "
            "با حذف پورت یا غیرفعال‌کردن پروتکل، کانفیگ قدیمی آن قطع می‌شود و برنامهٔ کاربر باید اشتراک را تازه کند.</p>%s"
            "<h2 style='margin-top:16px'>آدرس‌های اتصال CDN (دامنه یا IPv4)</h2>"
            "<p class=hint>دامنه باید در Cloudflare فعال و به همین مسیر متصل باشد. Host و SNI را از بالا انتخاب کنید.</p>"
            "<textarea name=ips rows=4 dir=ltr placeholder='cdn.example.com, 104.16.96.1'%s>%s</textarea>") % (
                "".join(rows), disabled, _config_html("\n".join(ips)))

def _render_link_outbounds(obs, editable):
    rows = []
    for i, ob in enumerate(obs):
        if not isinstance(ob, dict): continue
        tag = _config_html(ob.get("tag", ""))
        link = _config_html(ob.get("link", ""))
        domains = _config_html("\n".join(ob.get("domains") or []))
        if editable:
            rows.append("<div class=eprow style='display:block'><div class=grid>"
                        "<label class=hint for='ob_tag_%d'>نام خروجی</label>"
                        "<input type=text id='ob_tag_%d' name='ob_tag_%d' maxlength=24 value='%s' "
                        "style='width:100%%;max-width:100%%'>"
                        "<label class=hint for='ob_link_%d'>لینک خروجی</label>"
                        "<textarea id='ob_link_%d' name='ob_link_%d' rows=3 dir=ltr>%s</textarea>"
                        "<label class=hint for='ob_dom_%d'>دامنه‌ها؛ هر خط یکی. خالی = خروجی همهٔ ترافیک</label>"
                        "<textarea id='ob_dom_%d' name='ob_dom_%d' rows=3 dir=ltr>%s</textarea>"
                        "<label class=row><input type=checkbox name='ob_delete_%d'>حذف این خروجی</label>"
                        "</div></div>" % (i, i, i, tag, i, i, i, link, i, i, i, domains, i))
        else:
            summary = "همهٔ ترافیک" if not ob.get("domains") else "، ".join(ob["domains"])
            rows.append("<div class=eprow style='display:block'><b>%s</b>"
                        "<p class=hint>%s</p><details><summary class=hint>نمایش لینک خروجی</summary>"
                        "<code dir=ltr>%s</code></details></div>" % (
                            tag, _config_html(summary), link))
    if editable:
        rows.append("<div class=eprow style='display:block'><div class=grid>"
                    "<b>افزودن خروجی</b>"
                    "<input type=text name=new_ob_tag maxlength=24 placeholder='نام کوتاه، مثلاً clean-ai' "
                    "style='width:100%;max-width:100%'>"
                    "<textarea name=new_ob_link rows=3 dir=ltr placeholder='vless://… یا socks://…'></textarea>"
                    "<label class=hint for=new_ob_dom>دامنه‌ها؛ هر خط یکی. خالی = همهٔ ترافیک</label>"
                    "<textarea id=new_ob_dom name=new_ob_dom rows=3 dir=ltr></textarea>"
                    "</div></div>")
    elif not rows:
        rows.append("<p class=hint>خروجی‌ای تعریف نشده؛ ترافیک مستقیم می‌رود.</p>")
    return "<h2 style='margin-top:16px'>خروجی‌ها و دامنه‌ها</h2>%s" % "".join(rows)

def render_config(csrf, msg=""):
    fields = _render_config_fields(global_settings_snapshot())
    body = ("<form method=post action='/a/config' class=grid>%s"
            "<input type=hidden name=endpoint_fields value=1>"
            "<input type=hidden name=csrf value='%s'>"
            "<button class=btn style='margin-top:12px'>%s ذخیره و بازتولید همهٔ لینک‌های پیش‌فرض</button>"
            "</form>") % (fields, _config_html(csrf), _icon("check"))
    return _page("پیکربندی", _top(_back("/a/", "داشبورد"), csrf) +
                 _page_heading("پیکربندی عمومی", "تغییرات این صفحه برای لینک‌های پیش‌فرض اعمال می‌شود.") +
                 _config_message(msg) + _sync_notice() + "<div class=card>%s</div>" % body +
                 _render_outbounds_section(csrf))

def render_user_config(token, csrf, msg=""):
    c = db(); user = c.execute("SELECT label FROM users WHERE token=?", (token,)).fetchone(); c.close()
    if not user:
        return _page("یافت نشد", _top(_back("/a/", "داشبورد"), csrf) +
                     "<div class=card>لینکی با این شناسه پیدا نشد.</div>")
    token_attr = _config_html(token)
    back = _back("/a/user?token=%s" % _config_html(urllib.parse.quote(token, safe="")),
                 user["label"])
    custom = get_link_override(token)
    if custom is None:
        snapshot = global_settings_snapshot()
        mode = "<div class='card hero'><b>حالت پیش‌فرض عمومی</b><p>این لینک تغییرات بعدی تنظیمات عمومی را دنبال می‌کند.</p></div>"
        fields = _render_config_fields(snapshot, editable=False)
        outbounds = _render_link_outbounds(snapshot.get("outbounds") or [], editable=False)
        buttons = ("<form method=post action='/a/user-config'>"
                   "<input type=hidden name=token value='%s'>"
                   "<input type=hidden name=csrf value='%s'>"
                   "<button class=btn name=action value=customize>فعال‌سازی تنظیمات اختصاصی</button>"
                   "</form>") % (token_attr, _config_html(csrf))
        body = "<div class=card><div class=grid>%s%s</div></div>%s" % (fields, outbounds, buttons)
    else:
        mode = "<div class='card hero'><b>حالت اختصاصی</b><p>تغییرات این صفحه فقط روی همین لینک اعمال می‌شود.</p></div>"
        fields = _render_config_fields(custom, editable=True)
        outbounds = _render_link_outbounds(custom.get("outbounds") or [], editable=True)
        save = ("<form method=post action='/a/user-config' class=grid>"
                "<input type=hidden name=token value='%s'>"
                "<input type=hidden name=csrf value='%s'>"
                "<input type=hidden name=endpoint_fields value=1>"
                "<input type=hidden name=outbound_fields value=1>%s%s"
                "<button class=btn name=action value=save>ذخیره و بازتولید این لینک</button>"
                "</form>") % (token_attr, _config_html(csrf), fields, outbounds)
        reset = ("<form method=post action='/a/user-config' style='margin-top:14px'>"
                 "<input type=hidden name=token value='%s'>"
                 "<input type=hidden name=csrf value='%s'>"
                 "<button class='btn ghost' name=action value=default>بازگشت به پیش‌فرض عمومی</button>"
                 "</form>") % (token_attr, _config_html(csrf))
        body = "<div class=card>%s%s</div>" % (save, reset)
    return _page("تنظیمات لینک", _top(back, csrf) +
                 _page_heading("تنظیمات لینک", user["label"]) +
                 _config_message(msg) + _sync_notice() + mode + body)

OB_JS = """
var OB={csrf:''};
function obToast(m,ok){var t=document.getElementById('obmsg');if(!t)return;
 t.innerHTML='<b>'+m.replace(/[<>&]/g,'')+'</b>';t.className='card obmsg '+(ok?'good':'bad');t.style.display='block';
 clearTimeout(OB.tm);OB.tm=setTimeout(function(){t.style.display='none';},9000);}
function obBusy(b,on,txt){if(!b)return;if(on){b.dataset.o=b.innerHTML;b.innerHTML='<span class=spin></span>'+(txt||'صبر کنید…');b.disabled=true;}
 else{if(b.dataset.o)b.innerHTML=b.dataset.o;b.disabled=false;}}
function obForm(){return document.getElementById('obform');}
function obData(f){var u=new URLSearchParams(new FormData(f)),o=obForm();
 if(o&&o!==f)new URLSearchParams(new FormData(o)).forEach(function(v,k){if(k!=='csrf')u.append(k,v);});
 u.set('csrf',OB.csrf);u.set('ajax','1');return u;}
function obPost(url,body,btn,txt,done){obBusy(btn,true,txt);
 fetch(url,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'},body:body})
 .then(function(r){return r.json();})
 .then(function(j){obBusy(btn,false);
  if(j.list!=null){document.getElementById('oblist').innerHTML=j.list;obRecalc();obDirty(false);}
  if(j.msg)obToast(j.msg,j.ok);if(done)done(j);})
 .catch(function(e){obBusy(btn,false);obToast('ارتباط با پنل قطع شد: '+e,false);});}
function obRecalc(){var first=null,n=document.querySelectorAll('#oblist textarea[data-tag]').length;
 var w=document.getElementById('obsavewrap');if(w)w.style.display=n?'block':'none';
 document.querySelectorAll('#oblist textarea[data-tag]').forEach(function(t){
  var b=document.getElementById('all-'+t.dataset.tag);if(!b)return;
  if(t.value.trim()){b.style.display='none';return;}
  b.style.display='inline-flex';
  if(first===null){first=t.dataset.tag;b.className='pill oball';b.textContent='همهٔ ترافیک از این خروجی رد می‌شود';}
  else{b.className='pill obwarn';b.textContent='بی‌اثر: خروجیِ بالاتر همهٔ ترافیک را گرفته';}});}
function obDirty(on){var s=document.getElementById('obsavebtn');if(!s)return;
 s.classList.toggle('warnpulse',!!on);
 s.textContent=on?'ذخیره و اعمال (تغییرِ ذخیره‌نشده)':'ذخیره و اعمال روی Xray';}
document.addEventListener('input',function(e){var t=e.target;
 if(t&&t.matches&&t.matches('textarea[data-tag]')){obRecalc();obDirty(true);}});
document.addEventListener('submit',function(e){var f=e.target;if(!f||!f.dataset||!f.dataset.act)return;
 e.preventDefault();var a=f.dataset.act,btn=f.querySelector('button'),tag=(f.elements.tag||{}).value||'';
 if(a==='del'&&!confirm('خروجی «'+tag+'» حذف و از xray برداشته شود؟'))return;
 if(a==='test'){var box=document.getElementById('res-'+tag);if(box){box.style.display='block';box.textContent='در حال تست… (تا ۳۰ ثانیه)';}
  obPost('/a/obtest',obData(f),btn,'تست…',function(j){if(box)box.textContent=j.result||j.msg||'';});return;}
 obPost({add:'/a/obadd',del:'/a/obdel',save:'/a/obsave'}[a],obData(f),btn,
        a==='save'?'اعمال روی xray…':'…',function(j){if(a==='add'&&j.ok)f.reset();
                                                     if(a==='save'&&j.ok)obDirty(false);});});
"""

def _ob_card(i, o, csrf):
    try:
        kind = parse_outbound_link(o["link"], o["tag"])["protocol"]
    except ValueError as e:
        kind = str(e)
    res = meta_get("ob_test_" + o["tag"], "")
    tag = html.escape(o["tag"])
    return (
        "<div class=card>"
        "<div class=row style='justify-content:space-between;align-items:center'>"
        "<b>%s</b><span class=eptag>%s · تست: 127.0.0.1:%d</span></div>"
        "<p class=hint dir=ltr style='word-break:break-all;text-align:left'>%s</p>"
        "<span class='pill oball' id='all-%s' style='display:none'></span>"
        "<label class=hint style='margin-top:8px'>دامنه‌هایی که از این خروجی بروند "
        "(هر خط یکی) — <b>خالی بگذارید تا همهٔ ترافیک از اینجا برود</b>:</label>"
        "<textarea name='dom_%s' data-tag='%s' rows=4 form=obform "
        "placeholder='خالی = همهٔ ترافیک&#10;یا مثلاً:&#10;geosite:google&#10;claude.ai'>%s</textarea>"
        "<div class=obres id='res-%s'%s>%s</div>"
        "<div class=row style='margin-top:10px'>"
        "<form data-act=test method=post action='/a/obtest' style='margin:0'>"
        "<input type=hidden name=csrf value='%s'><input type=hidden name=tag value='%s'>"
        "<button type=submit class='btn ghost'>%s تست این خروجی</button></form>"
        "<form data-act=del method=post action='/a/obdel' style='margin:0'>"
        "<input type=hidden name=csrf value='%s'><input type=hidden name=tag value='%s'>"
        "<button type=submit class='btn ghost'>حذف</button></form></div></div>"
    ) % (tag, html.escape(kind), ob_test_port(i), html.escape(o["link"]), tag,
         tag, tag, html.escape("\n".join(o.get("domains") or [])),
         tag, ("" if res else " style='display:none'"), html.escape(res),
         csrf, tag, _icon("search"), csrf, tag)

def render_ob_list(csrf):
    """Just the cards — re-rendered on its own and swapped in without a page reload."""
    obs = get_outbounds()
    if not obs:
        return ("<div class=card><p class=hint style='margin:0'>هنوز خروجی‌ای تعریف نشده — "
                "همهٔ ترافیک مستقیم از IP همین سرور می‌رود.</p></div>")
    return "".join(_ob_card(i, o, csrf) for i, o in enumerate(obs))

def _render_outbounds_section(csrf):
    obs = get_outbounds()
    add = ("<div class=card><h2>افزودن خروجی</h2>"
           "<p class=hint>لینک را همان‌طور که هست بچسبانید: "
           "<code>vless://</code> · <code>trojan://</code> · <code>ss://</code> · "
           "<code>socks://user:pass@host:port</code> · <code>http://…</code></p>"
           "<form data-act=add method=post action='/a/obadd' class=grid>"
           "<input name=tag placeholder='یک نام کوتاه، مثلاً clean-ai' maxlength=24 required>"
           "<textarea name=link rows=3 placeholder='vless://…  یا  socks://user:pass@1.2.3.4:1080' required></textarea>"
           "<input type=hidden name=csrf value='%s'>"
           "<button type=submit class=btn style='margin-top:10px'>%s افزودن</button></form></div>") % (csrf, _icon("plus"))

    save = ("<div id=obsavewrap%s>"
            "<form data-act=save method=post action='/a/obsave' id=obform>"
            "<input type=hidden name=csrf value='%s'>"
            "<button type=submit id=obsavebtn class=btn style='width:100%%'>ذخیره و اعمال روی Xray</button></form>"
            "<p class=hint style='margin-top:8px'>اعمال، کانفیگ را با <code>xray -test</code> اعتبارسنجی می‌کند؛ "
            "اگر خراب باشد چیزی تغییر نمی‌کند. بعد xray ری‌استارت می‌شود و کاربران خودکار resync می‌شوند "
            "(لینک کسی عوض نمی‌شود).</p></div>") % (("" if obs else " style='display:none'"), csrf)

    body = ("<section id=outbounds><div id=obmsg class='card obmsg' style='display:none'></div>" +
            _page_heading("خروجی‌ها", "مسیر خروج ترافیک از سرور را مدیریت کنید.") + add +
            "<h2 style='margin:18px 0 8px'>خروجی‌های تعریف‌شده</h2>" +
            "<div id=oblist>%s</div>" % render_ob_list(csrf) + save +
            "<script>%s\nOB.csrf=%s;obRecalc();</script></section>" % (OB_JS, json.dumps(csrf)))
    return body

def render_outbounds(csrf, msg=""):
    return render_config(csrf, msg)

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
        if path == "/a/user":          return _html(render_user(query.get("token", [""])[0], csrf, query.get("msg", [""])[0]))
        if path == "/a/new":           return _html(render_new(csrf))
        if path == "/a/config":        return _html(render_config(csrf, query.get("msg", [""])[0]))
        if path == "/a/user-config":
            return _html(render_user_config(query.get("token", [""])[0], csrf, query.get("msg", [""])[0]))
        if path == "/a/outbounds":     return _html(render_outbounds(csrf, query.get("msg", [""])[0]))
        if path == "/a/reset":         return _html(render_resetconfirm(query.get("token", [""])[0], csrf))
        if path == "/a/del":           return _html(render_delconfirm(query.get("token", [""])[0], csrf))
        return 404, {"Content-Type": "text/plain"}, b"not found"
    return route_admin_post(method, path, query, csrf, body, now, cookie_sid(cookie_header))

def _redirect(loc):
    return 302, {"Location": loc}, b""

def _parse_config_fields(form, current):
    """Parse settings common to the global and one-link forms before any writes."""
    recipe = {}
    for ep in ENDPOINTS:
        tag = ep["tag"]
        try: count = int(form.get("cnt_" + tag, "0"))
        except (TypeError, ValueError): raise ValueError("تعداد کانفیگ «%s» نامعتبر است" % tag)
        if count < 0: raise ValueError("تعداد کانفیگ نمی‌تواند منفی باشد")
        recipe[tag] = {"enabled": ("en_" + tag) in form, "count": count}
    ips = parse_ips(form.get("ips", ""))
    if not ips: raise ValueError("حداقل یک دامنه یا IPv4 معتبر وارد کنید")
    options = current["endpoint_settings"]
    if form.get("endpoint_fields") == "1":
        options = {}
        hosts = approved_hosts()
        for ep in ENDPOINTS:
            tag = ep["tag"]; base = _endpoint_defaults(ep)
            if "reality" in ep:
                label = (form.get("label_" + tag) or "").strip()
                if not label or len(label) > 64:
                    raise ValueError("نام کانفیگ باید بین ۱ تا ۶۴ نویسه باشد")
                options[tag] = {**base, "label": label}
                continue
            try: host_index = int(form.get("hostidx_" + tag, "0"))
            except (TypeError, ValueError): raise ValueError("گزینهٔ دامنه/SNI نامعتبر است")
            if not 0 <= host_index < len(hosts): raise ValueError("گزینهٔ دامنه/SNI آماده نیست")
            label = (form.get("label_" + tag) or "").strip()
            if not label or len(label) > 64: raise ValueError("نام کانفیگ باید بین ۱ تا ۶۴ نویسه باشد")
            fragment = "" if ep.get("proto") == "vmess" else (form.get("fm_" + tag) or "").strip()
            if fragment:
                try:
                    if not isinstance(json.loads(fragment), dict): raise ValueError()
                except ValueError: raise ValueError("Fragment برای «%s» باید JSON معتبر باشد" % tag)
            options[tag] = {**base,
                "tls_ports": [p for p in ep.get("tls_ports", []) if ("tls_%s_%s" % (tag, p)) in form],
                "notls_ports": [p for p in ep.get("notls_ports", []) if ("notls_%s_%s" % (tag, p)) in form],
                "label": label, "host": hosts[host_index]["host"], "sni": hosts[host_index]["sni"],
                "fragment_fm": fragment}
    return {"recipe": recipe, "ips": ips, "endpoint_settings": _normal_endpoint_settings(options)}

def _parse_custom_outbounds(form, previous):
    if form.get("outbound_fields") != "1": return previous
    obs = []
    indexes = sorted({int(m.group(1)) for key in form for m in [re.fullmatch(r"ob_tag_(\d+)", key)] if m})
    for i in indexes:
        if form.get("ob_delete_%d" % i): continue
        tag = (form.get("ob_tag_%d" % i) or "").strip()
        link = (form.get("ob_link_%d" % i) or "").strip()
        domains = parse_domains(form.get("ob_dom_%d" % i, ""))
        if tag or link: obs.append({"tag": tag, "link": link, "domains": domains})
    new_tag = (form.get("new_ob_tag") or "").strip()
    new_link = (form.get("new_ob_link") or "").strip()
    if new_tag or new_link:
        obs.append({"tag": new_tag, "link": new_link,
                    "domains": parse_domains(form.get("new_ob_dom", ""))})
    seen = set()
    for o in obs:
        tag = o["tag"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", tag) or tag in ("direct", "block"):
            raise ValueError("نام خروجی باید ۱ تا ۲۴ حرف/عدد یا _ و - باشد")
        if tag in seen: raise ValueError("نام خروجی تکراری است: %s" % tag)
        seen.add(tag)
        try: parse_outbound_link(o["link"], ob_user_xtag("check", tag))
        except ValueError as e: raise ValueError("لینک خروجی «%s» نامعتبر است: %s" % (tag, e))
    return obs

def route_admin_post(method, path, query, csrf, body, now, sid):
    form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "ignore")).items()}
    if form.get("csrf") != csrf:
        return 403, {"Content-Type": "text/plain"}, b"forbidden"
    if path == "/a/logout":
        _sess_drop(sid)          # manual logout is the only thing that ends a session
        ck = "mj_sess=; HttpOnly; Secure; SameSite=Strict; Path=/a; Max-Age=0"
        return 200, {"Content-Type": "text/html; charset=utf-8", "Set-Cookie": ck}, render_loggedout().encode("utf-8")
    token = form.get("token", "")
    def _num(x, cast):
        try:
            n = cast(str(x).replace(",", "."))
            return n if math.isfinite(n) and n >= 0 else None
        except Exception: return None
    if path == "/a/addvol":
        gb = _num(form.get("gb"), float)
        if gb: extend_volume(token, gb)
        refresh_usage(token); maybe_reenable(token); return _redirect("/a/user?token=" + token)
    if path == "/a/addtime":
        days = _num(form.get("days"), float)
        if days: extend_time(token, days)
        maybe_reenable(token); return _redirect("/a/user?token=" + token)
    if path == "/a/rename":
        rename_user(token, form.get("name"))
        return _redirect("/a/user?token=" + token)
    if path == "/a/reset":
        if form.get("confirm") == "yes" and token: reset_usage(token)
        return _redirect("/a/user?token=" + token)
    if path == "/a/unlimit":
        field = form.get("field")
        if field in ("limit_bytes", "expiry_ts"): set_unlimited(token, field)
        maybe_reenable(token); return _redirect("/a/user?token=" + token)
    if path == "/a/freeze":
        if token:
            ok = freeze_user(token) if form.get("on") else unfreeze_user(token)
            if not ok:
                return _redirect("/a/user?token=%s&msg=%s" % (
                    urllib.parse.quote(token), urllib.parse.quote("همگام‌سازی Xray کامل نشد؛ پنل دوباره تلاش می‌کند")))
        return _redirect("/a/user?token=" + token)
    if path == "/a/delete":
        if form.get("confirm") == "yes" and token and not delete_user(token):
            return _redirect("/a/user?token=%s&msg=%s" % (
                urllib.parse.quote(token), urllib.parse.quote("حذف در Xray کامل نشد؛ پنل دوباره تلاش می‌کند")))
        return _redirect("/a/")
    if path == "/a/new":
        gb = _num(form.get("gb"), float) or 0
        days = _num(form.get("days"), float) or 0
        name = (form.get("name") or "").strip()[:40] or None
        create_user(gb, days, label=name)
        return _redirect("/a/")
    if path == "/a/config":
        before = global_settings_snapshot()
        already_pending = meta_get("membership_sync_pending") == "1"
        try: updated = _parse_config_fields(form, before)
        except ValueError as e:
            return _redirect("/a/config?msg=" + urllib.parse.quote("خطا: " + str(e)))
        store_global_config(updated)
        synced = xr_reconcile_all_users(before_recipe=before["recipe"],
                                        before_settings=before["endpoint_settings"])
        if not synced:
            store_global_config(before)
            restored = xr_reconcile_all_users(before_recipe=updated["recipe"],
                                              before_settings=updated["endpoint_settings"])
            regenerate_all_subs()
            if restored and not already_pending: meta_set("membership_sync_pending", "")
            msg = ("خطا: تنظیمات عمومی قبلی بازگردانده شد؛ کانفیگ‌هایی که شناسه‌شان در میانهٔ تغییر "
                   "لغو شده، نیاز به به‌روزرسانی اشتراک دارند" if restored else
                   "خطا: تنظیمات عمومی قبلی بازگردانده شد اما همگام‌سازی Xray هنوز کامل نیست")
        else:
            regenerate_all_subs()
            if not already_pending: meta_set("membership_sync_pending", "")
            msg = "ذخیره شد"
        return _redirect("/a/config?msg=" + urllib.parse.quote(msg))
    if path == "/a/user-config":
        c = db(); user = c.execute("SELECT uuid,label FROM users WHERE token=?", (token,)).fetchone(); c.close()
        if not user: return _redirect("/a/")
        action = form.get("action", "")
        before_override = get_link_override(token)
        before_recipe = effective_recipe(token)
        before_settings = effective_endpoint_settings(token)
        before_outbounds = effective_outbounds(token)
        if action == "customize":
            if before_override is not None:
                return _redirect("/a/user-config?token=" + urllib.parse.quote(token))
            updated = global_settings_snapshot()
        elif action == "default":
            updated = None
        elif action == "save" and before_override is not None:
            try:
                fields = _parse_config_fields(form, before_override)
                obs = _parse_custom_outbounds(form, before_override.get("outbounds", []))
            except ValueError as e:
                return _redirect("/a/user-config?token=%s&msg=%s" % (
                    urllib.parse.quote(token), urllib.parse.quote("خطا: " + str(e))))
            updated = {**fields, "outbounds": obs}
        else:
            return _redirect("/a/user-config?token=" + urllib.parse.quote(token))
        # A copied custom snapshot routes exactly as the current global rules.
        # Xray only needs new user-scoped rules once either recipe diverges.
        global_outbounds = get_outbounds()
        before_route = before_outbounds if before_override is not None and before_outbounds != global_outbounds else None
        after_outbounds = global_outbounds if updated is None else updated.get("outbounds", [])
        after_route = after_outbounds if updated is not None and after_outbounds != global_outbounds else None
        routing_changed = before_route != after_route
        already_pending = meta_get("membership_sync_pending") == "1"
        outbound_was_pending = meta_get("outbound_sync_pending") == "1"
        if not set_link_override(token, updated, queue_sync=True,
                                 queue_outbound=routing_changed): return _redirect("/a/")
        after_recipe = effective_recipe(token)
        after_settings = effective_endpoint_settings(token)
        # Revoke/migrate credentials while their Xray usage counters are still
        # available. Applying outbound routing restarts Xray and clears them.
        synced = xr_reconcile_user_endpoints(token, user["uuid"],
                                             before_recipe=before_recipe,
                                             before_settings=before_settings)
        if not synced:
            set_link_override(token, before_override)
            restored = xr_reconcile_user_endpoints(token, user["uuid"],
                                                   before_recipe=after_recipe,
                                                   before_settings=after_settings)
            write_sub(token, user["uuid"], user["label"])
            if restored and not already_pending: meta_set("membership_sync_pending", "")
            if routing_changed and not outbound_was_pending: meta_set("outbound_sync_pending", "")
            detail = ("تنظیمات قبلی بازگردانده شد؛ اگر شناسه‌ای در میانهٔ تغییر لغو شده باشد، "
                      "کاربر باید اشتراک را تازه کند" if restored else
                      "تنظیمات قبلی بازگردانده شد اما همگام‌سازی اتصال‌ها هنوز کامل نیست")
            return _redirect("/a/user-config?token=%s&msg=%s" % (
                urllib.parse.quote(token), urllib.parse.quote("خطا: " + detail)))
        if routing_changed:
            ok, reason = apply_xray_outbounds()
            if not ok:
                # Credential revocation cannot be undone by restoring an old
                # snapshot: the old imported URI must stay revoked. Keep the
                # saved endpoint settings and retry only the outbound apply.
                meta_set("outbound_sync_pending", "1")
                write_sub(token, user["uuid"], user["label"])
                resync_all()
                return _redirect("/a/user-config?token=%s&msg=%s" % (
                    urllib.parse.quote(token), urllib.parse.quote(
                        "خطا: تنظیمات لینک ذخیره شد، اما خروجی هنوز اعمال نشده و دوباره تلاش می‌شود: " + reason)))
            meta_set("outbound_sync_pending", "")
        write_sub(token, user["uuid"], user["label"])
        if routing_changed: resync_all()  # Xray restart dropped the in-memory clients
        elif not already_pending: meta_set("membership_sync_pending", "")
        msg = "تنظیمات این لینک ذخیره شد" if synced else "خطا: ذخیره شد اما همگام‌سازی Xray کامل نشد"
        return _redirect("/a/user-config?token=%s&msg=%s" % (
            urllib.parse.quote(token), urllib.parse.quote(msg)))
    # --- outbounds: answer JSON to the panel's fetch(), plain redirects without JS ---
    def _ob_reply(ok, msg, relist=True, **extra):
        if form.get("ajax") != "1":
            return _redirect("/a/outbounds?msg=" + urllib.parse.quote(msg))
        d = {"ok": bool(ok), "msg": msg}
        d.update(extra)
        if relist: d["list"] = render_ob_list(csrf)
        return 200, {"Content-Type": "application/json; charset=utf-8"}, \
               json.dumps(d, ensure_ascii=False).encode("utf-8")

    def _ob_keep_domains(obs):
        # the domain boxes ride along with every action, so nothing typed is ever lost
        for o in obs:
            if ("dom_" + o["tag"]) in form:
                o["domains"] = parse_domains(form.get("dom_" + o["tag"], ""))
        return obs

    if path == "/a/obadd":
        tag = re.sub(r"[^A-Za-z0-9_-]", "", (form.get("tag") or "").strip())[:24]
        link = (form.get("link") or "").strip()
        obs = _ob_keep_domains(get_outbounds())
        if not tag or not link:
            return _ob_reply(False, "نام و لینک لازم است")
        if tag in ("direct", "block") or any(o["tag"] == tag for o in obs):
            return _ob_reply(False, "این نام قبلاً استفاده شده")
        try:
            parse_outbound_link(link, tag)          # validate before storing
        except ValueError as e:
            return _ob_reply(False, "لینک نامعتبر: %s" % e)
        obs.append({"tag": tag, "link": link, "domains": []})
        set_outbounds(obs)
        return _ob_reply(True, "«%s» اضافه شد. خالی بماند = همهٔ ترافیک از آن می‌رود. "
                               "برای فعال شدن «ذخیره و اعمال» را بزنید." % tag)
    if path == "/a/obdel":
        tag = (form.get("tag") or "").strip()
        set_outbounds([o for o in _ob_keep_domains(get_outbounds()) if o["tag"] != tag], queue_apply=True)
        meta_set("ob_test_" + tag, "")
        refresh_all_usage()  # the Xray restart clears live traffic counters
        ok, msg = apply_xray_outbounds()
        if ok:
            meta_set("outbound_sync_pending", "")
            resync_all()
        return _ob_reply(ok, ("حذف شد. " + msg) if ok else
                         "حذف ذخیره شد؛ اعمال روی Xray دوباره تلاش می‌شود: " + msg)
    if path == "/a/obsave":
        obs = _ob_keep_domains(get_outbounds())
        set_outbounds(obs, queue_apply=True)
        refresh_all_usage()
        ok, msg = apply_xray_outbounds(obs)
        if ok:
            meta_set("outbound_sync_pending", "")
            resync_all()
        return _ob_reply(ok, msg if ok else
                         "تنظیمات ذخیره شد؛ اعمال روی Xray دوباره تلاش می‌شود: " + msg,
                         relist=False)
    if path == "/a/obtest":
        tag = (form.get("tag") or "").strip()
        res = test_outbound(tag)
        meta_set("ob_test_" + tag, res)
        return _ob_reply(True, "تست «%s» انجام شد" % tag, relist=False, result=res)
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
