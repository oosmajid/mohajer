#!/usr/bin/env python3
# Mohajer sub-server. Serves the sub-* files written by bot.py:
#   - proxy clients (v2rayNG, etc.)  -> raw base64 config list + Subscription-Userinfo header
#   - browsers (UA contains Mozilla) -> mobile copy-page with data/time progress bars
# Read-only against dpbot.db; runs on 127.0.0.1:8090 behind the Cloudflare tunnel.
import os, re, json, html, time, base64, sqlite3, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Paths come from env (set by the systemd unit / wizard); defaults match the
# legacy live server so nothing breaks if env is absent.
ROOT = os.environ.get("SUB_DIR", "/opt/dpsub")
DB_PATH = os.environ.get("DB", "/opt/dpbot/dpbot.db")
HOST = os.environ.get("SUB_HOST", "127.0.0.1")
PORT = int(os.environ.get("SUB_PORT", "8090"))
SAFE = re.compile(r"^sub-[A-Za-z0-9_.-]+$")
GB = 1024 ** 3

def user_info(name):
    # name like "sub-u-<token>" -> dict from dpbot.db, or None
    if not name.startswith("sub-u-"):
        return None
    token = name[len("sub-u-"):]
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT used_bytes,limit_bytes,expiry_ts,created_ts,label,disabled_ts FROM users WHERE token=?", (token,)).fetchone()
        c.close()
        return dict(r) if r else None
    except Exception:
        return None

def fmt_bytes(b):
    b = float(b)
    if b <= 0: return "0"
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024: return ("%.0f %s" if u in ("B", "KB") else "%.2f %s") % (b, u)
        b /= 1024
    return "%.2f PB" % b

def human_left(ts):
    left = ts - int(time.time())
    if left <= 0: return "منقضی"
    d = left // 86400; h = (left % 86400) // 3600
    if d >= 1: return "%d روز و %d ساعت" % (d, h)
    return "%d ساعت" % h

def bars_html(info):
    if not info: return ""
    out = ['<div class="stats">']
    # data
    used, lim = info["used_bytes"], info["limit_bytes"]
    if lim and lim > 0:
        pct = min(100.0, used / lim * 100.0)
        col = "#2FCB74" if pct < 70 else ("#FFB020" if pct < 90 else "#FF5A47")
        val = "%s / %s" % (fmt_bytes(used), fmt_bytes(lim))
        out.append('<div class="stat"><div class="lbl"><span>📦 حجم مصرفی</span><span class="v">%s</span></div>'
                   '<div class="track"><div class="fill" style="width:%.1f%%;background:%s"></div></div></div>' % (val, pct, col))
    else:
        out.append('<div class="stat"><div class="lbl"><span>📦 حجم</span><span>نامحدود</span></div>'
                   '<div class="track"><div class="fill" style="width:100%;background:var(--ink)"></div></div></div>')
    # time
    exp, cr = info["expiry_ts"], info["created_ts"] or 0
    if exp and exp > 0:
        total = max(1, exp - cr); elapsed = max(0, int(time.time()) - cr)
        pct = min(100.0, elapsed / total * 100.0)
        left = human_left(exp)
        col = "#2FCB74" if pct < 70 else ("#FFB020" if pct < 90 else "#FF5A47")
        out.append('<div class="stat"><div class="lbl"><span>⏳ زمان باقی‌مانده</span><span>%s</span></div>'
                   '<div class="track"><div class="fill" style="width:%.1f%%;background:%s"></div></div></div>' % (html.escape(left), pct, col))
    else:
        out.append('<div class="stat"><div class="lbl"><span>⏳ زمان</span><span>نامحدود</span></div>'
                   '<div class="track"><div class="fill" style="width:100%;background:var(--ink)"></div></div></div>')
    out.append("</div>")
    return "".join(out)

def parse_label(link):
    link = link.strip()
    try:
        if link.startswith("vmess://"):
            raw = link[8:]
            j = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore"))
            return (j.get("ps") or "VMess"), "vmess"
        proto = link.split("://", 1)[0]
        name = urllib.parse.unquote(link.split("#", 1)[1]) if "#" in link else ""
        return (name or proto), proto
    except Exception:
        return "config", "?"

def relabel(link, name):
    link = link.strip()
    if link.startswith("vmess://"):
        raw = link[8:]
        j = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore"))
        j["ps"] = name
        return "vmess://" + base64.b64encode(json.dumps(j).encode()).decode()
    base = link.rsplit("#", 1)[0] if "#" in link else link
    return base + "#" + urllib.parse.quote(name)

def _fa_digits(s):
    return s.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))

def fmt_vol_fa(b):
    # RTL-safe volume: Persian unit + Persian digits, no Latin letters (Latin "GB" breaks bidi in client lists)
    b = float(b)
    if b <= 0:
        return "۰"
    for u in ["بایت", "کیلوبایت", "مگ", "گیگ"]:
        if b < 1024:
            s = ("%.0f" % b) if u in ("بایت", "کیلوبایت") else ("%.1f" % b)
            return _fa_digits(s) + " " + u
        b /= 1024
    return _fa_digits("%.1f" % b) + " ترابایت"

def human_left_fa(ts):
    left = ts - int(time.time())
    if left <= 0:
        return "منقضی"
    d = left // 86400; h = (left % 86400) // 3600
    return _fa_digits("%d روز و %d ساعت" % (d, h)) if d >= 1 else _fa_digits("%d ساعت" % h)

def status_name(info):
    if info.get("disabled_ts"):
        return "⛔ اعتبار تمام شد — تمدید کنید"
    lim, used, exp = info["limit_bytes"], info["used_bytes"], info["expiry_ts"]
    voltxt = fmt_vol_fa(max(0, lim - used)) if (lim and lim > 0) else "نامحدود"
    timetxt = human_left_fa(exp) if (exp and exp > 0) else "نامحدود"
    return "باقیمانده %s / %s" % (voltxt, timetxt)

def update_name(info):
    return "🔄 بعد از تمدید، آپدیت کنید" if info.get("disabled_ts") else "🔄 هر روز یک‌بار آپدیت کنید"

def decorate(links, info):
    if not links or not info:
        return links
    tmpl = links[0]
    out = [relabel(tmpl, status_name(info)), relabel(tmpl, update_name(info))]
    if info.get("disabled_ts"):
        return out
    return out + links

PAGE = """<!doctype html><html lang="fa" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="color-scheme" content="light dark">
<title>کانفیگ‌ها</title>
<script>(function(){try{var t=localStorage.getItem('mj-theme')||((window.matchMedia&&matchMedia('(prefers-color-scheme:dark)').matches)?'dark':'light');document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>
<style>
:root{--paper:#F4F1E8;--card:#FFFFFF;--ink:#111111;--accent:#FFDD2D;--ok:#2FCB74;--warn:#FFB020;--dng:#FF5A47;--mut:#6B675C;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
:root[data-theme=dark]{--paper:#16150F;--card:#211F17;--ink:#F1EEE3;--mut:#9C978B}
:root[data-theme=dark] button:not(.sec):not(.copy):not(.tbtn){color:#111111}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;max-width:100%;overflow-x:hidden}
body{font-family:Tahoma,"Segoe UI",-apple-system,system-ui,Vazirmatn,sans-serif;background:var(--paper);color:var(--ink);padding:16px 12px 30px;line-height:1.5}
.wrap{max-width:560px;margin:0 auto}
h1{font-size:20px;font-weight:800;margin:2px 2px 14px}
.stats{background:var(--card);border:3px solid var(--ink);box-shadow:5px 5px 0 var(--ink);padding:14px;margin:0 0 16px}
.stat{margin-bottom:14px}.stat:last-child{margin-bottom:0}
.lbl{display:flex;justify-content:space-between;font-size:12.5px;font-weight:700;margin-bottom:7px}
.lbl .v{direction:ltr;unicode-bidi:isolate;white-space:nowrap;font-family:var(--mono)}
.track{height:14px;background:var(--card);border:2px solid var(--ink);overflow:hidden}
.fill{height:100%}
.sub{font-size:12px;color:var(--mut);font-weight:700;margin:0 2px 12px}
.bar{position:sticky;top:0;background:var(--paper);padding:8px 0;display:flex;gap:8px;z-index:5}
.bar button{flex:1}
button{font-family:inherit;font-size:14px;font-weight:800;border:3px solid var(--ink);padding:12px 10px;cursor:pointer;background:var(--accent);color:var(--ink);box-shadow:3px 3px 0 var(--ink);transition:transform .06s,box-shadow .06s}
button:hover{transform:translate(-1px,-1px);box-shadow:4px 4px 0 var(--ink)}
button:active{transform:translate(3px,3px);box-shadow:0 0 0 var(--ink)}
button.sec{background:var(--card)}
.phead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:2px 2px 14px}
.phead h1{margin:0}
.tbtn{display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;padding:0;font-size:17px;line-height:1;border:3px solid var(--ink);background:var(--card);color:var(--ink);cursor:pointer;box-shadow:3px 3px 0 var(--ink);transition:transform .06s,box-shadow .06s;flex:0 0 auto}
.tbtn:hover{transform:translate(-1px,-1px);box-shadow:4px 4px 0 var(--ink)}
.tbtn:active{transform:translate(3px,3px);box-shadow:0 0 0 var(--ink)}
.card{background:var(--card);border:2px solid var(--ink);padding:11px 13px;margin-bottom:10px;display:flex;align-items:center;gap:12px}
.meta{flex:1 1 auto;min-width:0}
.name{font-weight:800;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:ltr;text-align:right;font-family:var(--mono)}
.proto{font-size:11px;color:var(--mut);font-weight:700;margin-top:3px;text-transform:uppercase;letter-spacing:.04em}
.copy{flex:0 0 auto;background:var(--card);color:var(--ink);border:2px solid var(--ink);box-shadow:2px 2px 0 var(--ink);padding:9px 15px;font-size:13px;font-weight:800}
.copy.ok{background:var(--ok);color:#04231e}
.foot{color:var(--mut);font-size:12px;font-weight:700;text-align:center;margin-top:18px}
#buf{position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;border:0;padding:0}
</style></head><body><div class="wrap">
<div class="phead"><h1>📋 کانفیگ‌ها</h1><button id="themebtn" type="button" class="tbtn" onclick="toggleTheme()" aria-label="تغییر تم" title="تغییر تم">🌙</button></div>
%STATS%
<p class="sub">%COUNT% کانفیگ — تکی کپی کن یا «کپی همه».</p>
<div class="bar">
<button onclick="copyAll(this)">📑 کپی همه</button>
<button class="sec" onclick="copyLink(this)">🔗 لینک ساب</button>
</div>
<div id="list">%ROWS%</div>
<p class="foot">برای اتصال خودکار، «لینک ساب» را در اپ به‌عنوان Subscription اضافه کن.</p>
</div>
<textarea id="buf" readonly></textarea>
<script>
var CFG=%CONFIGS%;
function flash(b){if(!b)return;var o=b.textContent;b.textContent='✓ شد';b.classList.add('ok');setTimeout(function(){b.textContent=o;b.classList.remove('ok')},1100);}
function fb(t,b){var x=document.getElementById('buf');x.value=t;x.focus();x.setSelectionRange(0,t.length);try{document.execCommand('copy')}catch(e){}window.getSelection&&window.getSelection().removeAllRanges();flash(b);}
function cp(t,b){if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(function(){flash(b)},function(){fb(t,b)});}else{fb(t,b);}}
function copyOne(i,b){cp(CFG[i],b)}
function copyAll(b){cp(CFG.join('\\n'),b)}
function copyLink(b){cp(location.origin+location.pathname,b)}
function toggleTheme(){var h=document.documentElement,d=h.getAttribute('data-theme')==='dark'?'light':'dark';h.setAttribute('data-theme',d);try{localStorage.setItem('mj-theme',d);}catch(e){}_syncTheme();}
function _syncTheme(){var b=document.getElementById('themebtn');if(b)b.textContent=document.documentElement.getAttribute('data-theme')==='dark'?'☀️':'🌙';}_syncTheme();
</script></body></html>"""

ROW = ('<div class="card"><div class="meta"><div class="name">%s</div>'
       '<div class="proto">%s</div></div>'
       '<button class="copy" onclick="copyOne(%d,this)">کپی</button></div>')

def decode_links(b64):
    try:
        return [l for l in base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", "ignore").splitlines() if l.strip()]
    except Exception:
        return []

def build_response(name, b64, info, ua, wants_raw):
    links = decorate(decode_links(b64), info)
    if wants_raw or "Mozilla" not in ua:
        body = base64.b64encode("\n".join(links).encode()).decode().encode()
        extra = {"Profile-Update-Interval": "12"}
        if info:
            parts = ["upload=0", "download=%d" % int(info["used_bytes"])]
            if info["limit_bytes"] and info["limit_bytes"] > 0: parts.append("total=%d" % int(info["limit_bytes"]))
            if info["expiry_ts"] and info["expiry_ts"] > 0: parts.append("expire=%d" % int(info["expiry_ts"]))
            extra["Subscription-Userinfo"] = "; ".join(parts)
        return 200, "text/plain; charset=utf-8", body, extra
    rows = "".join(ROW % (html.escape(parse_label(l)[0]), html.escape(parse_label(l)[1]), i) for i, l in enumerate(links)) or "<p>خالی</p>"
    page = (PAGE.replace("%STATS%", bars_html(info))
                .replace("%ROWS%", rows)
                .replace("%COUNT%", str(len(links)))
                .replace("%CONFIGS%", json.dumps(links).replace("</", "<\\/")))
    return 200, "text/html; charset=utf-8", page.encode("utf-8"), {}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); name = u.path.lstrip("/")
        if not SAFE.match(name): self._send(404, "text/plain", b"not found"); return
        fp = os.path.join(ROOT, name)
        if not os.path.isfile(fp): self._send(404, "text/plain", b"not found"); return
        b64 = open(fp).read().strip()
        info = user_info(name)
        ua = self.headers.get("User-Agent", "")
        wants_raw = "raw" in urllib.parse.parse_qs(u.query)
        code, ctype, body, extra = build_response(name, b64, info, ua, wants_raw)
        self._send(code, ctype, body, extra)

if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
