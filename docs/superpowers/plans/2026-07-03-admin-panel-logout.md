# Admin Panel Logout Button — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CSRF-protected "خروج" (log out) button to the Mohajer web admin panel that invalidates the session server-side and clears the cookie.

**Architecture:** All changes live in `bot/bot.py`. A new `POST /a/logout` route (guarded by the panel's existing CSRF check) pops the session from the in-memory `_sessions` map and returns a "logged out" page with a cookie-clearing `Set-Cookie`. The header renderer `_top` grows an optional `csrf` param and, when present, renders the logout form; the router threads the session id into `route_admin_post`.

**Tech Stack:** Python 3 stdlib only (`http.server`, `http.cookies`), unittest.

## Global Constraints

- **stdlib only** — no pip dependencies. (copied from spec: "no-pip-deps codebase")
- **Files touched: `bot/bot.py` only.** Subserver, DB schema, and Telegram flow untouched.
- **Logout is POST + CSRF**, matching `/a/delete`, `/a/new`. GET logout is rejected.
- **Copy:** Persian, RTL. Logout button label: `خروج`. Logged-out page: `با موفقیت خارج شدی` + `برای ورودِ دوباره، در ربات دستور /admin را بزن.`
- Tests run with: `cd mohajer && python3 -m unittest tests.test_admin tests.test_daily tests.test_infoconfigs`

---

### Task 1: Logout route — server-side session invalidation + cookie clear

**Files:**
- Modify: `bot/bot.py` — `route_admin` (~827-844), `route_admin_post` signature+body (~849), add `render_loggedout` (near `render_expired` ~741)
- Test: `tests/test_admin.py`

**Interfaces:**
- Consumes: `_sessions` (dict sid→{"exp","csrf"}), `cookie_sid(cookie_header)->str|None`, `session_csrf(sid,now)->str|None`, `new_session(now)->(sid,csrf)`, `_page(title,inner)`, `_top(crumb="")`.
- Produces: `render_loggedout()->str`; new `route_admin_post(method, path, query, csrf, body, now, sid)` signature (adds trailing `sid`); `POST /a/logout` returning `(200, {..., "Set-Cookie": "mj_sess=; ...; Max-Age=0"}, <logged-out html bytes>)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_admin.py` inside `class TestRoutePost` (which already provides `self.sid`, `self.csrf`, `self.cookie`, and `self._post`):

```python
    def test_logout_invalidates_session(self):
        st, hdr, body = self._post("/a/logout", {"csrf": self.csrf})
        self.assertEqual(st, 200)
        self.assertNotIn(self.sid, bot._sessions)          # server-side invalidation
        self.assertIn("Max-Age=0", hdr["Set-Cookie"])      # cookie cleared
        self.assertIn("خارج", body.decode("utf-8"))

    def test_logout_csrf_mismatch_keeps_session(self):
        st, hdr, _ = self._post("/a/logout", {"csrf": "WRONG"})
        self.assertEqual(st, 403)
        self.assertIn(self.sid, bot._sessions)

    def test_after_logout_dashboard_denied(self):
        self._post("/a/logout", {"csrf": self.csrf})
        st, hdr, body = bot.route_admin("GET", "/a/", {}, self.cookie, b"", now=1002)
        self.assertEqual(st, 200)
        self.assertIn("منقضی", body.decode("utf-8"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mohajer && python3 -m unittest tests.test_admin -v`
Expected: FAIL — `/a/logout` currently hits the `route_admin_post` fallthrough → 404 (so `test_logout_invalidates_session` sees 404 not 200), session not popped.

- [ ] **Step 3: Add `render_loggedout` next to `render_expired`**

In `bot/bot.py`, immediately after the `render_expired` function (ends ~744), add:

```python
def render_loggedout():
    return _page("خروج", _top() + "<div class='card hero' style='text-align:center;padding:34px 18px'>"
                 "<div class=eyebrow>خروج</div><div class=title>با موفقیت خارج شدی</div>"
                 "<p style='color:var(--mut);margin:6px 0 0'>برای ورودِ دوباره، در ربات دستور <code>/admin</code> را بزن.</p></div>")
```

- [ ] **Step 4: Thread `sid` from `route_admin` into `route_admin_post`**

In `route_admin` (~835-844), the block currently reads:

```python
    csrf = session_csrf(cookie_sid(cookie_header), now)
    if not csrf:
        return 200, {"Content-Type": "text/html; charset=utf-8"}, render_expired().encode("utf-8")
    if method == "GET":
        ...
        return 404, {"Content-Type": "text/plain"}, b"not found"
    return route_admin_post(method, path, query, csrf, body, now)
```

Change the last line to pass the session id:

```python
    return route_admin_post(method, path, query, csrf, body, now, cookie_sid(cookie_header))
```

- [ ] **Step 5: Add the `sid` param and `/a/logout` branch to `route_admin_post`**

Change the signature (~849) from:

```python
def route_admin_post(method, path, query, csrf, body, now):
```

to:

```python
def route_admin_post(method, path, query, csrf, body, now, sid):
```

Then, right after the existing CSRF guard (the `if form.get("csrf") != csrf: return 403 ...` block ~851-852), add the logout branch:

```python
    if path == "/a/logout":
        _sessions.pop(sid, None)
        ck = "mj_sess=; HttpOnly; Secure; SameSite=Strict; Path=/a; Max-Age=0"
        return 200, {"Content-Type": "text/html; charset=utf-8", "Set-Cookie": ck}, render_loggedout().encode("utf-8")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd mohajer && python3 -m unittest tests.test_admin -v`
Expected: PASS (all three new tests green, existing tests still green).

- [ ] **Step 7: Verify the file compiles**

Run: `cd mohajer && python3 -m py_compile bot/bot.py && echo OK`
Expected: `OK`

- [ ] **Step 8: Commit**

```bash
git add bot/bot.py tests/test_admin.py
git commit -m "feat(bot/admin): add POST /a/logout — server-side session invalidation + cookie clear"
```

---

### Task 2: Header logout button + right-nav layout

**Files:**
- Modify: `bot/bot.py` — `_top` (~721-723), `.crumb` CSS (~661) + add `.rightnav`, `render_dashboard` (~763 + its `_top()` call ~778), `render_user`/`render_new`/`render_delconfirm` `_top(...)` calls, and `route_admin`'s `render_dashboard` call (~839)
- Test: `tests/test_admin.py`

**Interfaces:**
- Consumes: `render_loggedout` is unaffected; `_top` gains optional `csrf`.
- Produces: `_top(crumb="", csrf=None)->str` (renders logout form when `csrf` given); `render_dashboard(csrf)->str` (adds required `csrf` param).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_admin.py` inside `class TestRouteGet`:

```python
    def test_dashboard_has_logout(self):
        cookie, csrf = self._session_cookie()
        st, hdr, body = bot.route_admin("GET", "/a/", {}, cookie, b"", now=1001)
        page = body.decode("utf-8")
        self.assertIn("/a/logout", page)   # logout form present
        self.assertIn(csrf, page)          # and it carries the csrf token
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mohajer && python3 -m unittest tests.test_admin.TestRouteGet.test_dashboard_has_logout -v`
Expected: FAIL — dashboard has no `/a/logout` yet.

- [ ] **Step 3: Update `_top` to render the logout form when `csrf` is given**

Replace `_top` (~721-723) with:

```python
def _top(crumb="", csrf=None):
    right = ("<span class=crumb>%s</span>" % crumb) if crumb else ""
    if csrf:
        right += ("<form method=post action='/a/logout' style='margin:0'>"
                  "<input type=hidden name=csrf value='%s'>"
                  "<button class='btn ghost'>خروج</button></form>") % csrf
    rightnav = ("<span class=rightnav>%s</span>" % right) if right else ""
    return ("<header class=top><span class=brand><span class=dot-sig></span>Mohajer</span>%s</header>"
            % rightnav)
```

- [ ] **Step 4: Move the right-alignment from `.crumb` to a new `.rightnav`**

In `ADMIN_CSS`, change the `.crumb` rule (~661) from:

```
.crumb{margin-inline-start:auto;font-size:13px}
```

to (drop the auto margin — the wrapper now owns alignment):

```
.crumb{font-size:13px}
.rightnav{margin-inline-start:auto;display:flex;align-items:center;gap:10px}
```

- [ ] **Step 5: Give `render_dashboard` a `csrf` param and forward it**

Change `def render_dashboard():` (~763) to `def render_dashboard(csrf):`, and change its final line `return _page("پنل", _top() + hero + users)` (~778) to:

```python
    return _page("پنل", _top("", csrf) + hero + users)
```

Then in `route_admin` (~839) change:

```python
        if path in ("/a", "/a/"):      return _html(render_dashboard())
```

to:

```python
        if path in ("/a", "/a/"):      return _html(render_dashboard(csrf))
```

- [ ] **Step 6: Forward `csrf` to `_top` in the crumb pages**

- In `render_user` (~787 not-found branch and its main return): change `_top("<a href='/a/'>← داشبورد</a>")` calls to `_top("<a href='/a/'>← داشبورد</a>", csrf)`.
- In `render_new` (~816): change `_top("<a href='/a/'>← داشبورد</a>")` to `_top("<a href='/a/'>← داشبورد</a>", csrf)`.
- In `render_delconfirm` (~822): change `_top("<a href='/a/user?token=%s'>← بازگشت</a>" % token)` to `_top("<a href='/a/user?token=%s'>← بازگشت</a>" % token, csrf)`.

(Leave `render_expired` and `render_loggedout` calling `_top()` with no csrf — they are unauthenticated, so no button.)

- [ ] **Step 7: Run the full admin test suite**

Run: `cd mohajer && python3 -m unittest tests.test_admin -v`
Expected: PASS — `test_dashboard_has_logout` green, plus the still-passing `test_user_detail` (which asserts the csrf appears on the user page).

- [ ] **Step 8: Run the whole suite + compile**

Run: `cd mohajer && python3 -m unittest tests.test_admin tests.test_daily tests.test_infoconfigs && python3 -m py_compile bot/bot.py && echo OK`
Expected: all tests pass, `OK`.

- [ ] **Step 9: Commit**

```bash
git add bot/bot.py tests/test_admin.py
git commit -m "feat(bot/admin): logout button in panel header + right-nav layout"
```

---

## Self-Review

**Spec coverage:**
- Header button on every authed page → Task 2 (dashboard via `render_dashboard(csrf)`; user/new/del via forwarded csrf). ✓
- POST + CSRF → Task 1 (`/a/logout` sits after the CSRF guard). ✓
- Server-side `_sessions.pop` + cookie clear → Task 1 Step 5. ✓
- Logged-out page copy → Task 1 Step 3. ✓
- After-logout access denied → Task 1 test `test_after_logout_dashboard_denied`. ✓
- All four spec tests present (invalidate, csrf-mismatch, post-logout-denied, dashboard-has-form). ✓
- Subserver/DB/Telegram untouched. ✓

**Placeholder scan:** none — every code step shows full code.

**Type consistency:** `route_admin_post(..., now, sid)` defined in Task 1 Step 5 and called with the new arg in Task 1 Step 4. `_top(crumb="", csrf=None)` defined in Task 2 Step 3 and called with csrf in Steps 5-6; `render_expired`/`render_loggedout` intentionally call `_top()` (defaults hold). `render_dashboard(csrf)` defined and called with `csrf` in same task.
