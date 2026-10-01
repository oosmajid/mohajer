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
# Vendored QR encoder (qrcode-generator 2.0.4, MIT, Kazuhiko Arase), served at /sub-qr.js:
# the Cloudflare tunnel only forwards /sub-* here, and a CDN <script> may be blocked in Iran.
QR_JS_NAME = "sub-qr.js"
QR_JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qrcode.js")
# Same-origin font for both panels, using the existing /sub-* tunnel route.
FONT_NAME = "sub-font-vazirmatn-v33.003.woff2"
FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Vazirmatn-variable.woff2")
# Happ download links. GitHub's releases/latest/download/<asset> always resolves to the
# newest release; iOS ships only through the App Store.
HAPP_APPS = [
    ("اندروید", "https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk"),
    ("iOS", "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"),
    ("ویندوز", "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe"),
    ("مک", "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg"),
]
HAPP_ADD = "happ://add/"   # Happ deep link: happ://add/<subscription url>
# v2rayNG UrlSchemeActivity accepts an encoded subscription URL in `url`.
V2RAYNG_ADD = "v2rayng://install-config?url="
GB = 1024 ** 3

def user_info(name):
    # name like "sub-u-<token>" -> dict from dpbot.db, or None
    if not name.startswith("sub-u-"):
        return None
    token = name[len("sub-u-"):]
    try:
        # Not mode=ro: a read-only handle can't attach the WAL -shm file. We only
        # ever SELECT here, so a normal handle is read-only in practice.
        c = sqlite3.connect(DB_PATH, timeout=5)
        c.execute("PRAGMA busy_timeout=5000")
        c.row_factory = sqlite3.Row
        try:
            r = c.execute("SELECT used_bytes,usage_reset_bytes,limit_bytes,expiry_ts,created_ts,label,disabled_ts "
                          "FROM users WHERE token=?", (token,)).fetchone()
        except sqlite3.OperationalError:
            # Safe during rolling upgrades if the read-only subserver restarts just
            # before the bot has added the new baseline column.
            r = c.execute("SELECT used_bytes,0 AS usage_reset_bytes,limit_bytes,expiry_ts,created_ts,label,disabled_ts "
                          "FROM users WHERE token=?", (token,)).fetchone()
        c.close()
        if not r:
            return None
        info = dict(r)
        info["lifetime_used_bytes"] = int(info["used_bytes"] or 0)
        info["used_bytes"] = max(0, info["lifetime_used_bytes"] - int(info["usage_reset_bytes"] or 0))
        return info
    except Exception:
        return None

def fmt_bytes(b):
    b = float(b)
    if b <= 0: return "0"
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            fmt = "%.0f %s" if u in ("B", "KB") else ("%.3f %s" if u == "TB" else "%.2f %s")
            return fmt % (b, u)
        b /= 1024
    return "%.2f PB" % b

def human_left(ts):
    left = ts - int(time.time())
    if left <= 0: return "منقضی"
    d = left // 86400; h = (left % 86400) // 3600
    if d >= 1: return "%d روز و %d ساعت" % (d, h)
    return "%d ساعت" % h

def human_elapsed(created_ts):
    elapsed = max(0, int(time.time()) - int(created_ts or 0)) if created_ts else 0
    d = elapsed // 86400; h = (elapsed % 86400) // 3600
    if d >= 1: return "%d روز و %d ساعت" % (d, h)
    return "%d ساعت" % h

def bars_html(info):
    if not info: return ""
    out = ['<div class="panel stats">']
    # data
    used, lim = info["used_bytes"], info["limit_bytes"]
    if lim and lim > 0:
        pct = min(100.0, used / lim * 100.0)
        col = "var(--accent)" if pct < 70 else ("var(--warn)" if pct < 90 else "var(--dng)")
        val = "%s / %s" % (fmt_bytes(used), fmt_bytes(lim))
        out.append('<div class="stat"><div class="lbl"><span>حجم مصرفی</span><span class="v">%s</span></div>'
                   '<div class="track"><div class="fill" style="width:%.1f%%;background:%s"></div></div></div>' % (val, pct, col))
    else:
        out.append('<div class="stat"><div class="lbl"><span>حجم مصرفی (نامحدود)</span><span class="v">%s</span></div>'
                   '<div class="track"><div class="fill" style="width:100%%;background:var(--ink)"></div></div></div>'
                   % fmt_bytes(used))
    # time
    exp, cr = info["expiry_ts"], info["created_ts"] or 0
    if exp and exp > 0:
        total = max(1, exp - cr); elapsed = max(0, int(time.time()) - cr)
        pct = min(100.0, elapsed / total * 100.0)
        left = human_left(exp)
        col = "var(--accent)" if pct < 70 else ("var(--warn)" if pct < 90 else "var(--dng)")
        out.append('<div class="stat"><div class="lbl"><span>زمان باقی‌مانده</span><span>%s</span></div>'
                   '<div class="track"><div class="fill" style="width:%.1f%%;background:%s"></div></div></div>' % (html.escape(left), pct, col))
    else:
        out.append('<div class="stat"><div class="lbl"><span>زمان مصرف‌شده (نامحدود)</span><span>%s</span></div>'
                   '<div class="track"><div class="fill" style="width:100%%;background:var(--ink)"></div></div></div>'
                   % html.escape(human_elapsed(cr)))
    out.append("</div>")
    return "".join(out)

def parse_label(link):
    link = link.strip()
    try:
        if link.startswith("vmess://") and "@" not in link:
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
    if link.startswith("vmess://") and "@" not in link:
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
        return "اعتبار تمام شد — تمدید کنید"
    lim, used, exp = info["limit_bytes"], info["used_bytes"], info["expiry_ts"]
    voltxt = fmt_vol_fa(max(0, lim - used)) if (lim and lim > 0) else "نامحدود"
    timetxt = human_left_fa(exp) if (exp and exp > 0) else "نامحدود"
    return "باقیمانده %s / %s" % (voltxt, timetxt)

def update_name(info):
    return "بعد از تمدید، آپدیت کنید" if info.get("disabled_ts") else "هر روز یک‌بار آپدیت کنید"

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
<link rel="preload" href="/sub-font-vazirmatn-v33.003.woff2" as="font" type="font/woff2" crossorigin>
<title>%TITLE%</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect x='2' y='2' width='28' height='28' rx='8' fill='%23256BD1'/><path d='M10 22v-6m6 6V11m6 11V7' stroke='%23fff' stroke-width='3' stroke-linecap='round'/></svg>">
<script>(function(){try{var t=localStorage.getItem('mj-theme')||((window.matchMedia&&matchMedia('(prefers-color-scheme:dark)').matches)?'dark':'light');document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>
<style>
@font-face{font-family:"Vazirmatn";src:url("/sub-font-vazirmatn-v33.003.woff2") format("woff2");font-weight:100 900;font-style:normal;font-display:swap}
:root{--paper:#F4F7FB;--card:#FFFFFF;--ink:#17253D;--accent:#256BD1;--accent-text:#FFFFFF;--ok:#179773;--warn:#BD7A17;--dng:#D4545C;--mut:#68788F;--line:#DCE5F0;--soft:#EAF1FA;--hero:#EAF3FF;--shadow:0 12px 36px rgba(33,60,99,.06);--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;--sans:"Vazirmatn","Segoe UI",Tahoma,system-ui,sans-serif}
:root[data-theme=dark]{--paper:#101827;--card:#182438;--ink:#EDF3FC;--accent:#83B4FB;--accent-text:#10213A;--ok:#55D3A5;--warn:#F2BC69;--dng:#FF929C;--mut:#A7B5C8;--line:#31425A;--soft:#203149;--hero:#192F4B;--shadow:0 12px 36px rgba(0,0,0,.13)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;max-width:100%;overflow-x:hidden}
body{font-family:var(--sans);background:var(--paper);color:var(--ink);padding:22px 16px 48px;line-height:1.65;-webkit-font-smoothing:antialiased}
.wrap{max-width:680px;margin:0 auto}
.icon-sprite{position:absolute;width:0;height:0;overflow:hidden}
.icon{width:18px;height:18px;flex:0 0 18px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
:root .theme-sun{display:none}:root[data-theme=dark] .theme-moon{display:none}:root[data-theme=dark] .theme-sun{display:block}
.phead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 22px}
.brand{display:flex;align-items:center;gap:10px;font-size:18px;font-weight:800;letter-spacing:-.02em}
.mark{display:grid;place-items:center;width:36px;height:36px;border-radius:10px;background:var(--accent);color:var(--accent-text)}
.tbtn{display:grid;place-items:center;width:40px;height:40px;padding:0;flex:0 0 auto;border:1px solid var(--line);border-radius:11px;background:var(--card);color:var(--ink)}
.tbtn .icon{grid-area:1/1}
h1{font-size:clamp(25px,5vw,34px);letter-spacing:-.03em;line-height:1.3;margin:0 0 6px}
h2{font-size:16px;margin:0 0 6px}
p{margin:0}
.intro{color:var(--mut);font-size:13px;margin-bottom:20px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);padding:21px;margin-bottom:16px}
.stats{background:var(--hero);box-shadow:none;border-color:transparent}
.stat+.stat{margin-top:17px}
.lbl{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:12.5px;font-weight:700;margin-bottom:8px}
.lbl .v{direction:ltr;unicode-bidi:isolate;white-space:nowrap;font-variant-numeric:tabular-nums;font-size:12px}
.track{height:8px;background:var(--line);border-radius:99px;overflow:hidden}
.fill{height:100%;border-radius:99px}
.quickhead{display:flex;align-items:flex-start;gap:11px;margin-bottom:16px}
.quickhead .icon{color:var(--accent);margin-top:2px}
.hint2{color:var(--mut);font-size:12px;line-height:1.7}
.btn,button{font:inherit;font-size:13px;font-weight:750;cursor:pointer;transition:background .18s,transform .18s,border-color .18s}
.btn{display:flex;align-items:center;justify-content:center;gap:8px;border:1px solid transparent;border-radius:11px;padding:11px 16px;background:var(--accent);color:var(--accent-text);text-decoration:none;text-align:center}
.btn:hover,button:hover{transform:translateY(-1px)}
.btn:active,button:active{transform:translateY(1px)}
.quick-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
.quick-actions .btn{min-width:0}
.btn.secondary{background:var(--soft);border-color:var(--line);color:var(--ink)}
.apps{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:16px 0 8px}
.apps a{display:block;text-align:center;text-decoration:none;font-weight:700;font-size:12px;color:var(--ink);background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:10px 4px}
.apps a:hover{color:var(--accent);border-color:var(--accent)}
.qrbox{display:flex;gap:16px;align-items:center;background:var(--soft);border-radius:12px;padding:12px;margin-top:16px}
.qr{flex:0 0 auto}.qr svg{display:block;width:116px;height:116px;background:#fff;border-radius:8px;padding:5px}
.qrbox p{font-size:12px;color:var(--mut)}
.sectionhead{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin:24px 0 10px}
.sectionhead h2{font-size:18px;margin:0}.sub{font-size:12px;color:var(--mut)}
.bar{position:sticky;top:0;z-index:5;display:flex;gap:8px;padding:10px 0;background:var(--paper)}
.bar button{display:inline-flex;align-items:center;justify-content:center;gap:7px;flex:1;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink)}
.bar button:first-child{background:var(--accent);color:var(--accent-text);border-color:var(--accent)}
.bar button.ok,.copy.ok{border-color:var(--ok);color:var(--ok);background:var(--soft)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:8px;display:flex;align-items:center;gap:12px}
.meta{flex:1 1 auto;min-width:0}.name{font-weight:750;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:ltr;text-align:right}
.proto{font-size:11px;color:var(--mut);font-weight:650;margin-top:2px;text-transform:uppercase;letter-spacing:.03em}
.copy{display:inline-flex;align-items:center;gap:6px;flex:0 0 auto;background:var(--soft);color:var(--accent);border:1px solid var(--line);border-radius:9px;padding:7px 10px}
.foot{color:var(--mut);font-size:12px;text-align:center;margin-top:20px}
#buf{position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;border:0;padding:0}
@media(max-width:440px){body{padding:16px 12px 34px}.panel{padding:17px}.quick-actions{grid-template-columns:1fr}.apps{grid-template-columns:repeat(2,1fr)}.qrbox{gap:11px}.qr svg{width:98px;height:98px}}
 </style></head><body>
<svg xmlns="http://www.w3.org/2000/svg" class="icon-sprite" aria-hidden="true">
<symbol id="i-link" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.1 0l3-3A5 5 0 0 0 13 3l-2 2M14 11a5 5 0 0 0-7.1 0l-3 3A5 5 0 0 0 11 21l2-2"/></symbol>
<symbol id="i-copy" viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></symbol>
<symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
<symbol id="i-moon" viewBox="0 0 24 24"><path d="M20 15.5A8 8 0 0 1 8.5 4 8 8 0 1 0 20 15.5Z"/></symbol>
<symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></symbol>
<symbol id="i-route" viewBox="0 0 24 24"><circle cx="5" cy="6" r="2"/><circle cx="19" cy="18" r="2"/><path d="M7 6h7a4 4 0 0 1 0 8h-4a4 4 0 0 0 0 8h7"/></symbol>
</svg>
<div class="wrap">
<div class="phead"><div class="brand"><span class="mark"><svg class="icon"><use href="#i-route"/></svg></span>مهاجر</div><button id="themebtn" type="button" class="tbtn" onclick="toggleTheme()" aria-label="تغییر تم" title="تغییر تم"><svg class="icon theme-moon"><use href="#i-moon"/></svg><svg class="icon theme-sun"><use href="#i-sun"/></svg></button></div>
<h1>%TITLE%</h1><p class="intro">کانفیگ‌ها و وضعیت اشتراک را از این صفحه ببینید.</p>
%STATS%
<div class="panel quick">
<div class="quickhead"><svg class="icon"><use href="#i-link"/></svg><div><h2>افزودن اشتراک به برنامه</h2><p class="hint2">ابتدا برنامه را نصب کنید، سپس دکمهٔ همان برنامه را بزنید. v2rayNG برای اندروید است.</p></div></div>
<div class="quick-actions">
<a class="btn" id="happadd" href="#"><svg class="icon"><use href="#i-plus"/></svg>افزودن خودکار به Happ</a>
<a class="btn secondary" id="v2rayngadd" href="#"><svg class="icon"><use href="#i-plus"/></svg>افزودن خودکار به <span dir="ltr">v2rayNG</span></a>
</div>
<div class="apps">%APPS%</div>
<div class="qrbox"><div class="qr" id="qr"></div><p>برای افزودن اشتراک روی دستگاه دیگر، این کد را با Happ یا v2rayNG اسکن کنید.</p></div>
</div>
<div class="sectionhead"><h2>کانفیگ‌ها</h2><span class="sub">%COUNT% کانفیگ</span></div>
<div class="bar">
<button onclick="copyAll(this)"><svg class="icon"><use href="#i-copy"/></svg>کپی همه</button>
<button class="sec" onclick="copyLink(this)"><svg class="icon"><use href="#i-link"/></svg>لینک اشتراک</button>
</div>
<div id="list">%ROWS%</div>
<p class="foot">برای به‌روزرسانی خودکار، لینک اشتراک را در برنامهٔ خود اضافه کنید.</p>
</div>
<textarea id="buf" readonly></textarea>
<script src="/sub-qr.js"></script>
<script>
var CFG=%CONFIGS%;
var SUBURL=location.origin+location.pathname;
(function(){var a=document.getElementById('happadd');if(a)a.href='%HAPPADD%'+SUBURL;
var ng=document.getElementById('v2rayngadd');if(ng)ng.href='%V2RAYNGADD%'+encodeURIComponent(SUBURL)+'#'+encodeURIComponent(document.querySelector('h1').textContent);
try{var q=qrcode(0,'M');q.addData(SUBURL);q.make();document.getElementById('qr').innerHTML=q.createSvgTag({cellSize:5,margin:2,scalable:true});}catch(e){}})();
function flash(b){if(!b)return;var o=b.innerHTML;b.textContent='کپی شد';b.classList.add('ok');setTimeout(function(){b.innerHTML=o;b.classList.remove('ok')},1100);}
function fb(t,b){var x=document.getElementById('buf');x.value=t;x.focus();x.setSelectionRange(0,t.length);try{document.execCommand('copy')}catch(e){}window.getSelection&&window.getSelection().removeAllRanges();flash(b);}
function cp(t,b){if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(function(){flash(b)},function(){fb(t,b)});}else{fb(t,b);}}
function copyOne(i,b){cp(CFG[i],b)}
function copyAll(b){cp(CFG.join('\\n'),b)}
function copyLink(b){cp(location.origin+location.pathname,b)}
function toggleTheme(){var h=document.documentElement,d=h.getAttribute('data-theme')==='dark'?'light':'dark';h.setAttribute('data-theme',d);try{localStorage.setItem('mj-theme',d);}catch(e){}_syncTheme();}
function _syncTheme(){var b=document.getElementById('themebtn');if(b)b.setAttribute('aria-label',document.documentElement.getAttribute('data-theme')==='dark'?'تم روشن':'تم تیره');}_syncTheme();
</script></body></html>"""

ROW = ('<div class="card"><div class="meta"><div class="name">%s</div>'
       '<div class="proto">%s</div></div>'
       '<button class="copy" onclick="copyOne(%d,this)"><svg class="icon" aria-hidden="true"><use href="#i-copy"/></svg>کپی</button></div>')

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
    title = html.escape(str(info.get("label") or "کانفیگ‌ها")) if info else "کانفیگ‌ها"
    page = (PAGE.replace("%TITLE%", title)
                .replace("%STATS%", bars_html(info))
                .replace("%ROWS%", rows)
                .replace("%COUNT%", str(len(links)))
                .replace("%APPS%", "".join('<a href="%s" target="_blank" rel="noopener">%s</a>' % (html.escape(u), html.escape(n))
                                           for n, u in HAPP_APPS))
                .replace("%HAPPADD%", HAPP_ADD)
                .replace("%V2RAYNGADD%", V2RAYNG_ADD)
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
        if name == FONT_NAME:
            try:
                with open(FONT_PATH, "rb") as f: body = f.read()
            except OSError: self._send(404, "text/plain", b"not found"); return
            self._send(200, "font/woff2", body, {"Cache-Control": "public, max-age=31536000, immutable"}); return
        if name == QR_JS_NAME:
            try: body = open(QR_JS_PATH, "rb").read()
            except Exception: self._send(404, "text/plain", b"not found"); return
            self._send(200, "application/javascript; charset=utf-8", body, {"Cache-Control": "public, max-age=604800"}); return
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
