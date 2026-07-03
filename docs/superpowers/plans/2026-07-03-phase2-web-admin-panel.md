# Phase 2 — Web admin panel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a web admin panel to `bot/bot.py`, reachable via a one-time expiring link minted by `/admin`, that shows per-user stats + panel charts and can create / rename / extend / delete links — all behind an HttpOnly session cookie with CSRF-protected mutations.

**Architecture:** A `ThreadingHTTPServer` runs inside the bot process on `127.0.0.1:ADMIN_PORT`, so it reuses the bot's existing writer functions and preserves the single-writer invariant. All routing/auth is a **pure function** `route_admin(method, path, query, cookie_header, body, now) -> (status, headers, body_bytes)`; the HTTP handler is a thin shell that parses the request, calls `route_admin`, and writes the response. This makes the entire panel unit-testable with a temp sqlite DB and stubbed xray calls — no sockets.

**Tech Stack:** Python 3 stdlib only (`http.server`, `http.cookies`, `secrets`, `urllib.parse`, `sqlite3`, `unittest`). Server-rendered HTML, RTL, dark, inline CSS/SVG, no JS frameworks.

## Global Constraints

- Stdlib only — no pip packages.
- `bot/bot.py` remains the only writer of DB and xray. The admin server runs inside it.
- Admin server binds `127.0.0.1` only; it is reachable solely through the cloudflared `/a/*` ingress.
- Auth: one-time login token (TTL 600s) → HttpOnly+Secure+SameSite=Strict session cookie (TTL 86400s). All POST mutations require a CSRF token bound to the session. Delete requires a second confirmation page. Unknown token / missing session → a neutral "expired" page (no enumeration).
- In-memory session/token state (dicts). A bot restart logs everyone out (acceptable).
- Depends on Phase 1 (usage_daily, day_key, panel_usage_summary, record_daily) already merged.
- Reuse existing bot functions: `create_user`, `delete_user`, `extend_volume`, `extend_time`, `set_unlimited`, `refresh_usage`, `maybe_reenable`, `fmt_bytes`, `human_limit`, `human_expiry`, `sub_url`, `db`, `day_key`, `panel_usage_summary`.

## File Structure

- `bot/bot.py` — add near the ENV block: `ADMIN_PORT`. Add `from http.server import ...` and `import http.cookies`. Add ONE contiguous "ADMIN PANEL" section before `def main()` containing: auth state + functions, stats helpers, HTML renderers, `route_admin`, `AdminHandler`. Add a `/admin` branch in `handle_update`. Start the admin thread in `main()`.
- `tests/test_admin.py` — auth lifecycle, stats helpers, route_admin GET/POST.
- `config/bot.env.example` — document `ADMIN_PORT`.
- `docs/OPERATIONS.md` — cloudflared `/a/*` ingress step (append).

Run tests from repo root: `python3 -m unittest -v tests.test_admin`

---

### Task 1: Auth core (tokens, sessions, CSRF, cookie parse)

**Files:**
- Modify: `bot/bot.py` — add imports; add `ADMIN_PORT` to the ENV block; add the auth block at the start of a new "ADMIN PANEL" section placed just before `def main()`.
- Create: `tests/test_admin.py`

**Interfaces:**
- Produces:
  - `bot.mint_login(now:int|None) -> str`
  - `bot.consume_login(tok:str, now:int|None) -> bool` (single-use; expired/unknown → False)
  - `bot.new_session(now:int|None) -> (sid:str, csrf:str)`
  - `bot.session_csrf(sid:str|None, now:int|None) -> str|None` (None if missing/expired)
  - `bot.cookie_sid(cookie_header:str) -> str|None` (reads `mj_sess`)
  - Module state: `bot._login_tokens`, `bot._sessions`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin.py`:

```python
import os, sys, tempfile, unittest, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402


class TestAuth(unittest.TestCase):
    def setUp(self):
        bot._login_tokens.clear(); bot._sessions.clear()

    def test_login_token_single_use(self):
        tok = bot.mint_login(now=1000)
        self.assertTrue(bot.consume_login(tok, now=1001))
        self.assertFalse(bot.consume_login(tok, now=1002))  # already used

    def test_login_token_expires(self):
        tok = bot.mint_login(now=1000)
        self.assertFalse(bot.consume_login(tok, now=1000 + bot.LOGIN_TTL + 1))

    def test_session_and_csrf(self):
        sid, csrf = bot.new_session(now=1000)
        self.assertEqual(bot.session_csrf(sid, now=1001), csrf)
        self.assertIsNone(bot.session_csrf(sid, now=1000 + bot.SESS_TTL + 1))
        self.assertIsNone(bot.session_csrf("bogus", now=1001))

    def test_cookie_sid(self):
        self.assertEqual(bot.cookie_sid("mj_sess=abc; other=1"), "abc")
        self.assertIsNone(bot.cookie_sid(""))
        self.assertIsNone(bot.cookie_sid("other=1"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest -v tests.test_admin.TestAuth`
Expected: FAIL — `AttributeError: module 'bot' has no attribute 'mint_login'`.

- [ ] **Step 3: Add imports + ADMIN_PORT**

In `bot/bot.py`, extend the stdlib import line at the top (line ~6-8) by adding `http.cookies`:

```python
import http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
```

In the ENV block (after `POLL = int(...)`, ~line 30), add:

```python
ADMIN_PORT  = int(ENV.get("ADMIN_PORT", "8091"))
```

- [ ] **Step 4: Add the auth block**

In `bot/bot.py`, immediately before `def main():`, start the admin section:

```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m unittest -v tests.test_admin.TestAuth`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add bot/bot.py tests/test_admin.py
git commit -m "feat(bot/admin): auth core — one-time login tokens, sessions, CSRF, cookie parse"
```

---

### Task 2: Stats helpers (`daily_series`, `users_overview`)

**Files:**
- Modify: `bot/bot.py` — add to the ADMIN section, after the auth block.
- Modify: `tests/test_admin.py`

**Interfaces:**
- Produces:
  - `bot.daily_series(days:int=7, token:str|None=None, now:int|None=None) -> list[(day:str, bytes:int)]` — last `days` days, missing days filled with 0; panel-wide when `token` is None, else that user.
  - `bot.users_overview() -> list[dict]` with keys `token,label,used_bytes,limit_bytes,expiry_ts,disabled_ts,today`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_admin.py`:

```python
class TestStats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close()
        self._orig = bot.DB_PATH; bot.DB_PATH = self.tmp.name; bot.init_db()
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,base_bytes,last_raw,used_bytes,disabled_ts) "
                  "VALUES('t1','u','u_t1','One',1000,0,0,0,0,300,0)")
        day = bot.day_key()
        c.execute("INSERT INTO usage_daily(token,day,start_used,end_used) VALUES('t1',?,100,300)", (day,))
        c.commit(); c.close()

    def tearDown(self):
        bot.DB_PATH = self._orig; os.unlink(self.tmp.name)

    def test_daily_series_length_and_today(self):
        ser = bot.daily_series(days=7)
        self.assertEqual(len(ser), 7)
        self.assertEqual(ser[-1][0], bot.day_key())   # last entry is today
        self.assertEqual(ser[-1][1], 200)             # 300-100
        self.assertEqual(ser[0][1], 0)                # older day filled 0

    def test_users_overview(self):
        ov = bot.users_overview()
        self.assertEqual(len(ov), 1)
        self.assertEqual(ov[0]["today"], 200)
        self.assertEqual(ov[0]["used_bytes"], 300)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest -v tests.test_admin.TestStats`
Expected: FAIL — no `daily_series`.

- [ ] **Step 3: Add the helpers**

In `bot/bot.py` ADMIN section, after the auth block:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest -v tests.test_admin.TestStats`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add bot/bot.py tests/test_admin.py
git commit -m "feat(bot/admin): daily_series() + users_overview() stats helpers"
```

---

### Task 3: HTML renderers + `route_admin` GET (auth gate, dashboard, user, new, delete-confirm)

**Files:**
- Modify: `bot/bot.py` — add renderers + `route_admin` to the ADMIN section.
- Modify: `tests/test_admin.py`

**Interfaces:**
- Produces:
  - `bot.svg_bars(series:list[(str,int)]) -> str`
  - `bot.route_admin(method:str, path:str, query:dict, cookie_header:str, body:bytes, now:int|None=None) -> (status:int, headers:dict, body:bytes)`
  - Renderers `_page(title, inner)`, `render_expired()`, `render_dashboard()`, `render_user(token, csrf)`, `render_new(csrf)`, `render_delconfirm(token, csrf)` (internal).
- Consumes: Task 1 auth fns, Task 2 stats fns, existing `fmt_bytes/human_limit/human_expiry/sub_url/db`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_admin.py`:

```python
class TestRouteGet(TestStats):  # reuse the seeded DB from TestStats.setUp
    def _session_cookie(self):
        bot._sessions.clear()
        sid, csrf = bot.new_session(now=1000)
        return "mj_sess=%s" % sid, csrf

    def test_no_session_returns_expired(self):
        st, hdr, body = bot.route_admin("GET", "/a/", {}, "", b"", now=1000)
        self.assertEqual(st, 200)
        self.assertIn("منقضی", body.decode("utf-8"))

    def test_login_sets_cookie_and_redirects(self):
        tok = bot.mint_login(now=1000)
        st, hdr, body = bot.route_admin("GET", "/a/login/" + tok, {}, "", b"", now=1001)
        self.assertEqual(st, 302)
        self.assertEqual(hdr["Location"], "/a/")
        self.assertIn("mj_sess=", hdr["Set-Cookie"])
        self.assertIn("HttpOnly", hdr["Set-Cookie"])

    def test_dashboard_lists_user(self):
        cookie, csrf = self._session_cookie()
        st, hdr, body = bot.route_admin("GET", "/a/", {}, cookie, b"", now=1001)
        self.assertEqual(st, 200)
        self.assertIn("One", body.decode("utf-8"))        # the seeded label
        self.assertIn("<svg", body.decode("utf-8"))       # panel chart

    def test_user_detail(self):
        cookie, csrf = self._session_cookie()
        st, hdr, body = bot.route_admin("GET", "/a/user", {"token": "t1"}, cookie, b"", now=1001)
        self.assertEqual(st, 200)
        self.assertIn("One", body.decode("utf-8"))
        self.assertIn(csrf, body.decode("utf-8"))         # forms carry the csrf token
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest -v tests.test_admin.TestRouteGet`
Expected: FAIL — no `route_admin`.

- [ ] **Step 3: Add renderers + GET routing**

In `bot/bot.py` ADMIN section, after the stats helpers:

```python
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

def _html(inner, title="پنل"):
    return 200, {"Content-Type": "text/html; charset=utf-8"}, _page(title, inner).encode("utf-8")

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
                fmt_bytes(u["used_bytes"]), human_limit(u["limit_bytes"]), fmt_bytes(0),
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
    return route_admin_post(method, path, query, csrf, body, now)  # defined in Task 4
```

> Note: `route_admin` references `route_admin_post`, added in Task 4. Until then, POST paths will `NameError`; Task 3 tests only exercise GET, so this is fine. (If running the whole file before Task 4, GET still works.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest -v tests.test_admin.TestRouteGet`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add bot/bot.py tests/test_admin.py
git commit -m "feat(bot/admin): HTML renderers + route_admin GET (auth gate, dashboard, user, new, del-confirm)"
```

---

### Task 4: `route_admin_post` mutations + CSRF

**Files:**
- Modify: `bot/bot.py` — add `route_admin_post` to the ADMIN section (after `route_admin`).
- Modify: `tests/test_admin.py`

**Interfaces:**
- Produces: `bot.route_admin_post(method, path, query, csrf, body, now) -> (status, headers, body)`.
- Consumes: existing `create_user, delete_user, extend_volume, extend_time, set_unlimited, refresh_usage, maybe_reenable`.
- Behavior: verifies `form["csrf"] == csrf` (else 403); performs the op; returns 302 redirect (to `/a/user?token=…` for per-user ops, `/a/` after delete/new).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_admin.py`:

```python
class TestRoutePost(TestStats):
    def setUp(self):
        super().setUp()
        bot.xr_add_user = lambda *a, **k: True       # stub xray
        bot.xr_remove_user = lambda *a, **k: None
        bot.SUB_DIR = tempfile.mkdtemp()             # write_sub target
        bot._sessions.clear()
        self.sid, self.csrf = bot.new_session(now=1000)
        self.cookie = "mj_sess=%s" % self.sid

    def _post(self, path, fields):
        body = urllib.parse.urlencode(fields).encode()
        return bot.route_admin("POST", path, {}, self.cookie, body, now=1001)

    def test_addvol_increases_limit(self):
        st, hdr, _ = self._post("/a/addvol", {"token": "t1", "gb": "5", "csrf": self.csrf})
        self.assertEqual(st, 302)
        c = bot.db(); lim = c.execute("SELECT limit_bytes FROM users WHERE token='t1'").fetchone()["limit_bytes"]; c.close()
        self.assertEqual(lim, 1000 + 5 * bot.GB)

    def test_csrf_mismatch_rejected(self):
        st, hdr, _ = self._post("/a/addvol", {"token": "t1", "gb": "5", "csrf": "WRONG"})
        self.assertEqual(st, 403)

    def test_delete_requires_confirm_and_removes(self):
        st, _, _ = self._post("/a/delete", {"token": "t1", "confirm": "yes", "csrf": self.csrf})
        self.assertEqual(st, 302)
        c = bot.db(); n = c.execute("SELECT COUNT(*) c FROM users WHERE token='t1'").fetchone()["c"]; c.close()
        self.assertEqual(n, 0)

    def test_new_creates_link(self):
        st, hdr, _ = self._post("/a/new", {"gb": "10", "days": "30", "name": "Fresh", "csrf": self.csrf})
        self.assertEqual(st, 302)
        c = bot.db(); n = c.execute("SELECT COUNT(*) c FROM users WHERE label='Fresh'").fetchone()["c"]; c.close()
        self.assertEqual(n, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest -v tests.test_admin.TestRoutePost`
Expected: FAIL — `route_admin_post` not defined (NameError on POST).

- [ ] **Step 3: Add `route_admin_post`**

In `bot/bot.py` ADMIN section, after `route_admin`:

```python
def _redirect(loc):
    return 302, {"Location": loc}, b""

def route_admin_post(method, path, query, csrf, body, now):
    form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "ignore")).items()}
    if form.get("csrf") != csrf:
        return 403, {"Content-Type": "text/plain"}, b"forbidden"
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
    if path == "/a/delete":
        if form.get("confirm") == "yes" and token: delete_user(token)
        return _redirect("/a/")
    if path == "/a/new":
        gb = _num(form.get("gb"), float) or 0
        days = _num(form.get("days"), int) or 0
        name = (form.get("name") or "").strip()[:40] or None
        create_user(gb, days, label=name)
        return _redirect("/a/")
    return 404, {"Content-Type": "text/plain"}, b"not found"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest -v tests.test_admin.TestRoutePost`
Expected: PASS (4 tests).

- [ ] **Step 5: Full admin suite + commit**

Run: `python3 -m unittest -v tests.test_admin`
Expected: PASS (all).

```bash
git add bot/bot.py tests/test_admin.py
git commit -m "feat(bot/admin): route_admin_post mutations (addvol/addtime/rename/unlimit/delete/new) + CSRF"
```

---

### Task 5: HTTP handler + thread + `/admin` command

**Files:**
- Modify: `bot/bot.py` — add `AdminHandler` (after `route_admin_post`); start the server thread in `main()`; add a `/admin` branch in `handle_update`.

**Interfaces:**
- Consumes: `route_admin`, `SUB_BASE`, `mint_login`, `send`, `is_admin`, `ADMIN_PORT`.
- Produces: no unit-tested symbol (thin I/O shell); verified by compile + the already-tested `route_admin`.

- [ ] **Step 1: Add `AdminHandler`**

In `bot/bot.py` ADMIN section, after `route_admin_post`:

```python
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
```

- [ ] **Step 2: Add the `/admin` command**

In `handle_update`, just before `if text.startswith("/start"):` (~line 511), add:

```python
    if text.startswith("/admin"):
        tok = mint_login()
        send(chat, "🔐 لینک ورود به پنل (۱۰ دقیقه اعتبار، یک‌بار مصرف):\n<code>%s/a/login/%s</code>" % (SUB_BASE, tok)); return
```

- [ ] **Step 3: Start the thread in `main()`**

In `main()`, right after the enforcer thread line (`threading.Thread(target=enforcer, daemon=True).start()`), add:

```python
    threading.Thread(target=admin_server, daemon=True).start()
```

- [ ] **Step 4: Compile + full suite**

Run: `python3 -m py_compile bot/bot.py`
Expected: no output.

Run: `python3 -m unittest -v tests.test_daily tests.test_infoconfigs tests.test_admin`
Expected: PASS (all).

- [ ] **Step 5: Local smoke test (bind + one request, then kill)**

Run:
```bash
DPBOT_ENV=/nonexistent python3 - <<'PY'
import os, threading, time, urllib.request
os.environ["DPBOT_ENV"]="/nonexistent"
import sys; sys.path.insert(0,"bot"); import bot, tempfile
bot.DB_PATH=tempfile.mktemp(suffix=".db"); bot.init_db()
bot.ADMIN_PORT=8199
threading.Thread(target=bot.admin_server, daemon=True).start(); time.sleep(0.3)
tok=bot.mint_login()
r=urllib.request.urlopen("http://127.0.0.1:8199/a/login/"+tok)
print("login status", r.status, "set-cookie" in {k.lower() for k in dict(r.headers)})
PY
```
Expected: prints `login status 200 ...` (urllib follows the 302 to `/a/`, which returns the dashboard) — confirms the server binds and the auth flow works end-to-end.

- [ ] **Step 6: Commit**

```bash
git add bot/bot.py
git commit -m "feat(bot/admin): AdminHandler + server thread + /admin login-link command"
```

---

### Task 6: Ops — cloudflared ingress + env docs

**Files:**
- Modify: `config/bot.env.example` (document `ADMIN_PORT`)
- Modify: `docs/OPERATIONS.md` (append the ingress step)

**Interfaces:** none (documentation/config only).

- [ ] **Step 1: Document `ADMIN_PORT`**

Append to `config/bot.env.example`:

```
# Local port for the web admin panel (bot-hosted). Exposed only via the
# cloudflared /a/* ingress rule; never bind this publicly.
ADMIN_PORT=8091
```

- [ ] **Step 2: Append the ingress runbook to `docs/OPERATIONS.md`**

```markdown
## Web admin panel (/a/*)

The bot serves an admin panel on `127.0.0.1:${ADMIN_PORT:-8091}`. Route it
through the tunnel by adding an ingress rule **before** the catch-all in the
cloudflared config:

```yaml
ingress:
  - hostname: cdn.delplayer.ir
    path: /a/*
    service: http://127.0.0.1:8091
  # ... existing rule(s) ...
  - hostname: cdn.delplayer.ir
    service: http://127.0.0.1:8090   # sub server (catch-all, stays last)
  - service: http_status:404
```

Then `systemctl restart cloudflared` (or reload). Get a login link by sending
`/admin` to the bot. The link is one-time and expires in 10 minutes; it sets a
24h HttpOnly session cookie scoped to `/a`. A bot restart invalidates all
sessions — just run `/admin` again.
```

- [ ] **Step 3: Commit**

```bash
git add config/bot.env.example docs/OPERATIONS.md
git commit -m "docs(admin): ADMIN_PORT env + cloudflared /a/* ingress runbook"
```

---

## Self-Review

- **Spec coverage:** §6.1 hosting/routing → Tasks 5,6. §6.2 auth flow (one-time link → cookie) → Tasks 1,3,5. §6.3 CSRF + delete double-confirm → Tasks 3 (del-confirm page),4 (csrf check). §6.4 pages (dashboard/users/user/new + charts) → Tasks 2,3. §6.5 security summary (127.0.0.1 bind, neutral responses, stdlib) → Tasks 3,5. All addressed.
- **Placeholder scan:** none — full code in every step. The only forward-reference (`route_admin_post` in Task 3) is called out explicitly and resolved in Task 4.
- **Type consistency:** `route_admin(method,path,query,cookie_header,body,now) -> (status,headers,body)` and `route_admin_post(...)` share the same return shape; `session_csrf`/`cookie_sid`/`mint_login`/`consume_login` signatures match their uses in `route_admin`; `daily_series`/`users_overview` outputs match renderer consumption. `query` is the `parse_qs` dict (values are lists) — renderers use `query.get("token",[""])[0]`, consistent in Tasks 3 tests and code.

## Deployment (after all tasks pass)

Copy `bot/bot.py` to `/opt/dpbot/bot.py`, `systemctl restart dpbot`, add the cloudflared `/a/*` ingress rule + restart cloudflared. `ADMIN_PORT` defaults to 8091. Verify with `/admin` → open link → dashboard. (512MB box: the extra thread + ThreadingHTTPServer is light; no xray test procs involved.)
