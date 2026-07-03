# Phase 1 — Daily usage stats + info configs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track per-user daily usage, show a panel-wide total/today summary above the Telegram link list, and inject two always-present "info" configs (real working clones) into every subscription — with the real configs stripped once a link is disabled.

**Architecture:** Pure-function helpers added to the existing single-file `bot/bot.py` (writer) and `sub/subserver.py` (read-only). Daily usage lives in a new `usage_daily` sqlite table, upserted by the enforcer poll. Info configs are produced at serve time inside subserver by cloning the first real config and rewriting only its display name; no change to how sub files are written.

**Tech Stack:** Python 3 stdlib only (`sqlite3`, `http.server`, `base64`, `json`, `urllib`, `unittest`). No pip deps, no web framework, no DB server.

## Global Constraints

- Stdlib only — no pip packages, ever.
- `bot/bot.py` is the ONLY writer of the DB and of running xray. Do not add writers elsewhere.
- Target box has 512MB RAM — never spawn stray `xray` test processes; tests use a temp sqlite file only.
- Day boundary uses a fixed **UTC+03:30** offset (Iran has no DST since 2022) — no `zoneinfo`/`tzdata`.
- Daily history retention = **30 days**, rolling.
- Persian UI strings; keep existing emoji/format style.
- Info configs are **real clones** of `links[0]`, placed at the **top** of the list.
- Preserve existing function names/behavior; additions must be backward compatible (a fresh DB and an existing DB both work — `init_db` migrates).

## File Structure

- `bot/bot.py` — add `IRAN_OFFSET`, `day_key()`, `record_daily()`, `prune_daily()`, `panel_usage_summary()`; extend `init_db()`; call daily upsert+prune inside `refresh_all_usage()`; change the `list` handler header.
- `sub/subserver.py` — add `relabel()`, `status_name()`, `update_name()`, `decorate()`, `decode_links()`, `build_response()`; add `disabled_ts` to `user_info()`; refactor `do_GET` to call `build_response()`.
- `tests/__init__.py` — make tests a package.
- `tests/test_daily.py` — bot daily-usage + summary tests.
- `tests/test_infoconfigs.py` — subserver relabel/decorate/build_response tests.

All tests run from the repo root with: `python3 -m unittest -v tests.test_daily tests.test_infoconfigs`

---

### Task 1: Test harness + `day_key()`

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_daily.py`
- Modify: `bot/bot.py` (add `IRAN_OFFSET` + `day_key` in the helpers section, near `fmt_bytes` ~line 208)

**Interfaces:**
- Produces: `bot.day_key(ts: float|None) -> str` returning `"YYYY-MM-DD"` at UTC+03:30.

- [ ] **Step 1: Create the tests package**

Create `tests/__init__.py` (empty file).

- [ ] **Step 2: Write the failing test**

Create `tests/test_daily.py`:

```python
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.environ.setdefault("DPBOT_ENV", "/nonexistent-dpbot-env")
import bot  # noqa: E402

class TestDayKey(unittest.TestCase):
    def test_epoch_is_first_day(self):
        self.assertEqual(bot.day_key(0), "1970-01-01")

    def test_before_tehran_midnight(self):
        # UTC 20:00 -> +3:30 -> 23:30 -> same day
        self.assertEqual(bot.day_key(20 * 3600), "1970-01-01")

    def test_at_tehran_midnight(self):
        # UTC 20:30 -> +3:30 -> 00:00 -> next day
        self.assertEqual(bot.day_key(20 * 3600 + 1800), "1970-01-02")

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m unittest -v tests.test_daily` (from repo root)
Expected: FAIL — `AttributeError: module 'bot' has no attribute 'day_key'`.

- [ ] **Step 4: Add the implementation**

In `bot/bot.py`, just after `GB = 1024 ** 3` (line ~33), add:

```python
IRAN_OFFSET = 3 * 3600 + 30 * 60  # UTC+03:30; Iran has no DST since 2022

def day_key(ts=None):
    if ts is None:
        ts = time.time()
    return time.strftime("%Y-%m-%d", time.gmtime(ts + IRAN_OFFSET))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m unittest -v tests.test_daily`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add tests/__init__.py tests/test_daily.py bot/bot.py
git commit -m "feat(bot): add day_key() with fixed Iran UTC+3:30 offset"
```

---

### Task 2: `usage_daily` schema + `record_daily()` + summary reads

**Files:**
- Modify: `bot/bot.py` (`init_db` ~lines 42-53; add `record_daily`, `panel_usage_summary` in the db section)
- Modify: `tests/test_daily.py`

**Interfaces:**
- Produces:
  - `bot.record_daily(conn, token: str, used: int, day: str) -> None` — upsert; insert `start=end=used` on first row of the day, else bump `end_used` upward only.
  - `bot.panel_usage_summary() -> (total_used: int, today_used: int)` — reads `users` + `usage_daily`.
- Consumes: `bot.day_key` (Task 1), `bot.db()` (existing).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_daily.py` (before the `if __name__` line):

```python
class TestDailyRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._orig = bot.DB_PATH
        bot.DB_PATH = self.tmp.name
        bot.init_db()
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,base_bytes,last_raw,used_bytes) "
                  "VALUES('aa','u','u_aa','A',0,0,0,0,0,0)")
        c.commit(); c.close()

    def tearDown(self):
        bot.DB_PATH = self._orig
        os.unlink(self.tmp.name)

    def test_record_and_today_delta(self):
        day = bot.day_key()
        c = bot.db()
        bot.record_daily(c, "aa", 100, day)     # first poll of the day
        bot.record_daily(c, "aa", 500, day)     # later poll
        c.execute("UPDATE users SET used_bytes=500 WHERE token='aa'")
        c.commit(); c.close()
        total, today = bot.panel_usage_summary()
        self.assertEqual(total, 500)
        self.assertEqual(today, 400)            # 500 - 100

    def test_end_used_never_decreases(self):
        day = bot.day_key()
        c = bot.db()
        bot.record_daily(c, "aa", 500, day)
        bot.record_daily(c, "aa", 300, day)     # counter reset attempt -> ignored
        row = c.execute("SELECT start_used,end_used FROM usage_daily WHERE token='aa' AND day=?", (day,)).fetchone()
        c.close()
        self.assertEqual(row["start_used"], 500)
        self.assertEqual(row["end_used"], 500)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest -v tests.test_daily.TestDailyRecord`
Expected: FAIL — no `usage_daily` table / no `record_daily`.

- [ ] **Step 3: Extend `init_db()`**

In `bot/bot.py` `init_db()`, before `c.commit(); c.close()`, add:

```python
    c.execute("""CREATE TABLE IF NOT EXISTS usage_daily(
        token TEXT, day TEXT, start_used INTEGER, end_used INTEGER,
        PRIMARY KEY(token, day))""")
```

- [ ] **Step 4: Add `record_daily` and `panel_usage_summary`**

In `bot/bot.py`, after `refresh_all_usage` (~line 306) add:

```python
def record_daily(c, token, used, day):
    r = c.execute("SELECT start_used,end_used FROM usage_daily WHERE token=? AND day=?", (token, day)).fetchone()
    if r is None:
        c.execute("INSERT INTO usage_daily(token,day,start_used,end_used) VALUES(?,?,?,?)", (token, day, used, used))
    elif used > r["end_used"]:
        c.execute("UPDATE usage_daily SET end_used=? WHERE token=? AND day=?", (used, token, day))

def panel_usage_summary():
    c = db(); day = day_key()
    total = c.execute("SELECT COALESCE(SUM(used_bytes),0) v FROM users").fetchone()["v"]
    today = c.execute("SELECT COALESCE(SUM(max(end_used-start_used,0)),0) v FROM usage_daily WHERE day=?", (day,)).fetchone()["v"]
    c.close(); return int(total), int(today)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m unittest -v tests.test_daily.TestDailyRecord`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add bot/bot.py tests/test_daily.py
git commit -m "feat(bot): usage_daily table, record_daily(), panel_usage_summary()"
```

---

### Task 3: `prune_daily()` (30-day retention)

**Files:**
- Modify: `bot/bot.py` (add `prune_daily` next to `record_daily`)
- Modify: `tests/test_daily.py`

**Interfaces:**
- Produces: `bot.prune_daily(conn, keep_days: int = 30) -> None` — deletes rows with `day < day_key(now - keep_days*86400)`.

- [ ] **Step 1: Write the failing test**

Append to `TestDailyRecord` in `tests/test_daily.py`:

```python
    def test_prune_removes_old_days(self):
        c = bot.db()
        c.execute("INSERT INTO usage_daily(token,day,start_used,end_used) VALUES('aa','2000-01-01',0,10)")
        c.execute("INSERT INTO usage_daily(token,day,start_used,end_used) VALUES('aa',?,0,10)", (bot.day_key(),))
        c.commit()
        bot.prune_daily(c, keep_days=30)
        c.commit()
        rows = [r["day"] for r in c.execute("SELECT day FROM usage_daily").fetchall()]
        c.close()
        self.assertIn(bot.day_key(), rows)
        self.assertNotIn("2000-01-01", rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest -v tests.test_daily.TestDailyRecord.test_prune_removes_old_days`
Expected: FAIL — no `prune_daily`.

- [ ] **Step 3: Add `prune_daily`**

In `bot/bot.py`, right after `record_daily`:

```python
def prune_daily(c, keep_days=30):
    cutoff = day_key(time.time() - keep_days * 86400)
    c.execute("DELETE FROM usage_daily WHERE day < ?", (cutoff,))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest -v tests.test_daily.TestDailyRecord.test_prune_removes_old_days`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/bot.py tests/test_daily.py
git commit -m "feat(bot): prune_daily() 30-day retention"
```

---

### Task 4: Wire daily tracking into the enforcer + list-header summary

**Files:**
- Modify: `bot/bot.py` (`refresh_all_usage` ~lines 300-306; `list` handler ~lines 457-459)

**Interfaces:**
- Consumes: `record_daily`, `prune_daily`, `panel_usage_summary`, `day_key`, `fmt_bytes` (all existing by now).
- Produces: no new symbols; behavioral change only.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_daily.py`:

```python
class TestEnforcerHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close()
        self._orig = bot.DB_PATH; bot.DB_PATH = self.tmp.name; bot.init_db()
        c = bot.db()
        c.execute("INSERT INTO users(token,uuid,email,label,limit_bytes,expiry_ts,created_ts,base_bytes,last_raw,used_bytes) "
                  "VALUES('bb','u','u_bb','B',0,0,0,0,0,0)")
        c.commit(); c.close()

    def tearDown(self):
        bot.DB_PATH = self._orig; os.unlink(self.tmp.name)

    def test_refresh_all_usage_writes_daily(self):
        # stub xray usage to return a fixed byte count
        bot.xr_usage_all = lambda: {"bb": 250}
        bot.refresh_all_usage()
        total, today = bot.panel_usage_summary()
        self.assertEqual(total, 250)
        self.assertEqual(today, 250)   # start=0-day? first poll sets start=250 -> today 0? see note
```

> Note for implementer: on the FIRST poll of a brand-new day the row is created with `start=end=used`, so `today` for that first poll is 0. To make the test deterministic and meaningful, seed a start row first:

Replace the last method body with:

```python
    def test_refresh_all_usage_writes_daily(self):
        day = bot.day_key()
        c = bot.db(); bot.record_daily(c, "bb", 100, day); c.commit(); c.close()  # simulate earlier poll
        bot.xr_usage_all = lambda: {"bb": 250}
        bot.refresh_all_usage()
        total, today = bot.panel_usage_summary()
        self.assertEqual(total, 250)
        self.assertEqual(today, 150)   # 250 - 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest -v tests.test_daily.TestEnforcerHook`
Expected: FAIL — `today` is 0 because `refresh_all_usage` does not record daily yet.

- [ ] **Step 3: Update `refresh_all_usage`**

Replace the body of `refresh_all_usage` in `bot/bot.py` with:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest -v tests.test_daily.TestEnforcerHook`
Expected: PASS.

- [ ] **Step 5: Update the `list` handler header**

In `bot/bot.py`, replace the `if data == "list":` block (~lines 457-459) with:

```python
    if data == "list":
        answer(cbid); c = db(); n = c.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]; c.close()
        if n:
            total, today = panel_usage_summary()
            head = "📋 لینک‌های فعال:\n📊 مصرف کل: %s · امروز: %s" % (fmt_bytes(total), fmt_bytes(today))
        else:
            head = "هنوز لینکی نساخته‌ای. با ➕ شروع کن."
        edit(chat, mid, head, list_kb()); return
```

- [ ] **Step 6: Full suite + commit**

Run: `python3 -m unittest -v tests.test_daily`
Expected: PASS (all).

```bash
git add bot/bot.py tests/test_daily.py
git commit -m "feat(bot): record daily usage each poll + total/today summary in link list"
```

---

### Task 5: subserver `relabel()`

**Files:**
- Modify: `sub/subserver.py` (add `relabel` near `parse_label` ~line 76)
- Create: `tests/test_infoconfigs.py`

**Interfaces:**
- Produces: `subserver.relabel(link: str, name: str) -> str` — returns the same config with only its display name replaced (vmess `ps` field, or the `#fragment` for vless/trojan).

- [ ] **Step 1: Write the failing test**

Create `tests/test_infoconfigs.py`:

```python
import os, sys, base64, json, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sub"))
import subserver as s  # noqa: E402

VLESS = "vless://11111111-1111-1111-1111-111111111111@1.2.3.4:443?encryption=none&security=tls&type=ws&host=h&path=%2Fp#Old%20Name"
def make_vmess(ps):
    j = {"v":"2","ps":ps,"add":"1.2.3.4","port":"443","id":"x","aid":"0","net":"ws","type":"none","host":"h","path":"/p","tls":"tls"}
    return "vmess://" + base64.b64encode(json.dumps(j).encode()).decode()

class TestRelabel(unittest.TestCase):
    def test_vless_name_replaced(self):
        out = s.relabel(VLESS, "📦 NEW")
        self.assertEqual(s.parse_label(out)[0], "📦 NEW")
        self.assertTrue(out.startswith("vless://11111111-"))  # body untouched
        self.assertIn("host=h", out)

    def test_vmess_ps_replaced(self):
        out = s.relabel(make_vmess("Old"), "🔄 UP")
        self.assertEqual(s.parse_label(out)[0], "🔄 UP")
        raw = out[len("vmess://"):]
        j = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
        self.assertEqual(j["add"], "1.2.3.4")  # body untouched

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest -v tests.test_infoconfigs.TestRelabel`
Expected: FAIL — no `relabel`.

- [ ] **Step 3: Add `relabel`**

In `sub/subserver.py`, after `parse_label` (~line 87) add:

```python
def relabel(link, name):
    link = link.strip()
    if link.startswith("vmess://"):
        raw = link[8:]
        j = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore"))
        j["ps"] = name
        return "vmess://" + base64.b64encode(json.dumps(j).encode()).decode()
    base = link.rsplit("#", 1)[0] if "#" in link else link
    return base + "#" + urllib.parse.quote(name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest -v tests.test_infoconfigs.TestRelabel`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add sub/subserver.py tests/test_infoconfigs.py
git commit -m "feat(sub): relabel() to clone a config with a new display name"
```

---

### Task 6: `status_name`, `update_name`, `decorate`

**Files:**
- Modify: `sub/subserver.py` (add the three functions after `relabel`)
- Modify: `tests/test_infoconfigs.py`

**Interfaces:**
- Consumes: `relabel`, `fmt_bytes`, `human_left` (existing in subserver).
- Produces:
  - `subserver.status_name(info: dict) -> str`
  - `subserver.update_name(info: dict) -> str`
  - `subserver.decorate(links: list[str], info: dict|None) -> list[str]` — returns `[status, update, *links]` when active, `[status, update]` when `info["disabled_ts"]` is truthy, and `links` unchanged when `links` is empty or `info` is None.
  - `info` dict keys used: `used_bytes, limit_bytes, expiry_ts, disabled_ts`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_infoconfigs.py`:

```python
class TestDecorate(unittest.TestCase):
    def _info(self, **kw):
        base = {"used_bytes": 0, "limit_bytes": 0, "expiry_ts": 0, "disabled_ts": 0}
        base.update(kw); return base

    def test_active_prepends_two_working_clones(self):
        links = [VLESS, make_vmess("Real2")]
        out = s.decorate(links, self._info(limit_bytes=100, used_bytes=40))
        self.assertEqual(len(out), 4)
        self.assertTrue(out[0].startswith("vless://11111111-"))  # a real working clone
        self.assertIn("باقی‌مانده", s.parse_label(out[0])[0])
        self.assertIn("آپدیت", s.parse_label(out[1])[0])
        self.assertEqual(out[2:], links)  # real configs preserved, in order

    def test_disabled_keeps_only_info_configs(self):
        links = [VLESS, make_vmess("Real2")]
        out = s.decorate(links, self._info(disabled_ts=123))
        self.assertEqual(len(out), 2)
        self.assertIn("تمام شد", s.parse_label(out[0])[0])

    def test_empty_or_none_unchanged(self):
        self.assertEqual(s.decorate([], self._info()), [])
        self.assertEqual(s.decorate([VLESS], None), [VLESS])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest -v tests.test_infoconfigs.TestDecorate`
Expected: FAIL — no `decorate`.

- [ ] **Step 3: Add the three functions**

In `sub/subserver.py`, after `relabel`:

```python
def status_name(info):
    if info.get("disabled_ts"):
        return "⛔ اعتبار تمام شد — تمدید کنید"
    lim, used, exp = info["limit_bytes"], info["used_bytes"], info["expiry_ts"]
    voltxt = fmt_bytes(max(0, lim - used)) if (lim and lim > 0) else "نامحدود"
    timetxt = human_left(exp) if (exp and exp > 0) else "نامحدود"
    return "📦 باقی‌مانده: %s · ⏳ %s" % (voltxt, timetxt)

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest -v tests.test_infoconfigs.TestDecorate`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add sub/subserver.py tests/test_infoconfigs.py
git commit -m "feat(sub): status/update names + decorate() with disabled-state stripping"
```

---

### Task 7: `build_response()` + `do_GET` refactor + `disabled_ts` in `user_info`

**Files:**
- Modify: `sub/subserver.py` (`user_info` ~lines 18-30; add `decode_links` + `build_response`; rewrite `do_GET` body ~lines 153-179)
- Modify: `tests/test_infoconfigs.py`

**Interfaces:**
- Produces:
  - `subserver.decode_links(b64: str) -> list[str]`
  - `subserver.build_response(name: str, b64: str, info: dict|None, ua: str, wants_raw: bool) -> (code:int, ctype:str, body:bytes, extra:dict)`
- Consumes: `decorate`, `bars_html`, `parse_label`, `PAGE`, `ROW` (existing).
- Note: `user_info` now also returns `disabled_ts`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_infoconfigs.py`:

```python
class TestBuildResponse(unittest.TestCase):
    def _b64(self, links):
        return base64.b64encode("\n".join(links).encode()).decode()

    def _info(self, **kw):
        base = {"used_bytes": 40, "limit_bytes": 100, "expiry_ts": 0, "created_ts": 0, "label": "x", "disabled_ts": 0}
        base.update(kw); return base

    def test_raw_active_has_info_plus_real(self):
        b64 = self._b64([VLESS, make_vmess("Real2")])
        code, ctype, body, extra = s.build_response("sub-u-aa", b64, self._info(), "v2rayNG", False)
        self.assertEqual(code, 200)
        lines = base64.b64decode(body).decode().splitlines()
        self.assertEqual(len(lines), 4)  # 2 info + 2 real
        self.assertIn("Subscription-Userinfo", extra)

    def test_raw_disabled_only_info(self):
        b64 = self._b64([VLESS, make_vmess("Real2")])
        code, ctype, body, extra = s.build_response("sub-u-aa", b64, self._info(disabled_ts=99), "v2rayNG", False)
        lines = base64.b64decode(body).decode().splitlines()
        self.assertEqual(len(lines), 2)

    def test_html_for_browser(self):
        b64 = self._b64([VLESS])
        code, ctype, body, extra = s.build_response("sub-u-aa", b64, self._info(), "Mozilla/5.0", False)
        self.assertIn("text/html", ctype)
        self.assertIn("باقی‌مانده", body.decode("utf-8"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest -v tests.test_infoconfigs.TestBuildResponse`
Expected: FAIL — no `build_response`.

- [ ] **Step 3: Add `disabled_ts` to `user_info`**

In `sub/subserver.py` `user_info`, change the SELECT to include `disabled_ts`:

```python
        r = c.execute("SELECT used_bytes,limit_bytes,expiry_ts,created_ts,label,disabled_ts FROM users WHERE token=?", (token,)).fetchone()
```

- [ ] **Step 4: Add `decode_links` + `build_response`**

In `sub/subserver.py`, just above `class H(BaseHTTPRequestHandler)` (~line 144), add:

```python
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
```

- [ ] **Step 5: Rewrite `do_GET` to use `build_response`**

Replace the body of `do_GET` from the `b64 = open(fp).read().strip()` line to the end of the method with:

```python
        b64 = open(fp).read().strip()
        info = user_info(name)
        ua = self.headers.get("User-Agent", "")
        wants_raw = "raw" in urllib.parse.parse_qs(u.query)
        code, ctype, body, extra = build_response(name, b64, info, ua, wants_raw)
        self._send(code, ctype, body, extra)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m unittest -v tests.test_infoconfigs`
Expected: PASS (all classes).

- [ ] **Step 7: Byte-compile both files (smoke check, no xray)**

Run: `python3 -m py_compile bot/bot.py sub/subserver.py`
Expected: no output (success).

- [ ] **Step 8: Full suite + commit**

Run: `python3 -m unittest -v tests.test_daily tests.test_infoconfigs`
Expected: PASS (all).

```bash
git add sub/subserver.py tests/test_infoconfigs.py
git commit -m "feat(sub): serve-time info-config injection (build_response) + disabled_ts read"
```

---

## Deployment note (after all tasks pass locally)

Phase 1 changes only `bot/bot.py` and `sub/subserver.py`. To ship: copy both to the live paths (`/opt/dpbot/bot.py`, `/opt/dpsub/subserver.py`) and `systemctl restart dpbot dpsub`. `init_db()` creates `usage_daily` on start; the enforcer backfills it going forward. No cloudflared change in this phase. (See AGENTS.md §3 for path mapping and §6 for verification.)

## Self-Review

- **Spec coverage:** Feature 1 (list summary) → Tasks 2,4. Feature 3 (info configs, real clones, top placement, disabled stripping, serve-time freshness) → Tasks 5,6,7. Data model `usage_daily` + retention + Tehran offset → Tasks 1,2,3. Enforcer integration → Task 4. Feature 2 (web panel) is intentionally Phase 2 (separate plan). No Phase-1 spec item is unaddressed.
- **Placeholder scan:** none — every code/test step contains full code and exact run commands.
- **Type consistency:** `record_daily(conn, token, used, day)`, `panel_usage_summary() -> (int,int)`, `decorate(links, info) -> list`, `build_response(...) -> (code,ctype,body,extra)`, `relabel(link,name) -> str` are used consistently across tasks and tests.
