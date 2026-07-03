# Design — Web admin panel, daily usage stats, and always-on info configs

Date: 2026-07-03
Status: Approved (pending written-spec review)
Repo: `mohajer` (single-VPS V2Ray subscription panel)

## خلاصه‌ی فارسی (TL;DR)

سه فیچر به پنل Mohajer اضافه می‌شود:

1. زیر «📋 لینک‌های فعال:» یک خطِ خلاصه: **مصرفِ کل** + **مصرفِ امروز** (کلِ پنل).
2. دستور `/admin` یک لینکِ موقتِ یک‌بارمصرف می‌دهد که وارد یک **پنلِ وبِ ادمین** می‌شوی: آمارِ تفکیکیِ کاربران + ساخت/ویرایش/حذفِ لینک.
3. دو **کانفیگِ اطلاعاتیِ همیشگی** در هر لینک که **کپیِ عینِ یک کانفیگِ واقعیِ همان لینک‌اند** (پس واقعاً کار می‌کنند)، ولی نامشان «حجم/زمانِ باقی‌مانده» و «هر روز آپدیت کنید» را نشان می‌دهد. وقتی اعتبار تمام شد، کانفیگ‌های واقعی حذف و فقط همین دو می‌مانند.

---

## 1. Goals & non-goals

**Goals**
- Show panel-wide **total used** and **today's used** volume above the Telegram link list.
- Add a **web admin panel**, reachable through a one-time, expiring link minted by `/admin`, that shows detailed per-user stats and can create / edit / delete links.
- Keep two **always-present info configs** in every subscription; they are real, working clones of one of the link's configs, but their display name carries live status ("remaining volume/time", "update daily"). When a link is exhausted, the real configs are dropped and only these two remain.

**Non-goals**
- No multi-admin / RBAC. Single-admin assumption stays (see AGENTS.md §4).
- No external web framework, no pip deps, no DB server — stdlib only, per project constraints.
- The Telegram bot UI is **not** replaced; the web panel is additive.
- No REALITY, no changes to the Cloudflare-fronted transport model.

## 2. Current-state facts this design relies on

- `bot/bot.py` is the **only writer** of the sqlite DB and of the running xray (`xray api adu/rmu`). An `enforcer()` thread polls every `POLL` seconds, refreshes `used_bytes`, and disables/deletes on quota/expiry with a 48h grace window (`disabled_ts`).
- `sub/subserver.py` is a **read-only** HTTP server on `127.0.0.1:8090`. It serves each `sub-u-<token>` file as raw base64 to proxy clients (+ `Subscription-Userinfo` header) or as an HTML page to browsers. It already reads `dpbot.db` read-only and already has `fmt_bytes`, `human_left`, `parse_label`.
- Every config in a sub shares the same secret and `<token>`; configs differ only by endpoint/port/IP. So **any** real config is a valid clone source.
- `used_bytes` is monotonic and reset-corrected (via `base_bytes`/`last_raw`), so per-day deltas taken from it are robust against xray counter resets.
- cloudflared currently routes the public hostname to subserver:8090.

## 3. Architecture

```
                         Cloudflare (cdn.delplayer.ir)
                                   │  cloudflared ingress
                 ┌─────────────────┴───────────────────┐
        path: /a/*                              (everything else)
                 │                                       │
     127.0.0.1:8091  (NEW)                     127.0.0.1:8090
     admin web server                          subserver.py (read-only)
     — runs INSIDE bot.py                       — serves sub files
       (own thread), reuses                     — NEW: injects 2 info
       create/delete/extend/…                     configs at serve time
     — single writer preserved
                 │
        writes DB + xray (via existing bot functions)
```

Key decision — **the admin web server lives inside the bot process**, not in subserver. Rationale: the bot is the sole writer of DB and xray; hosting the mutating panel there preserves the single-writer invariant and reuses `create_user`, `delete_user`, `extend_volume`, `extend_time`, `set_unlimited`, `get_ips`, `set_ips` directly. subserver stays strictly read-only (a security feature we keep).

Rejected alternative — giving subserver write access: violates the single-writer golden rule (two processes calling `xray api` / writing DB), risks races with the enforcer's resync, and breaks the mental model in AGENTS.md. Not done.

## 4. Data model changes (sqlite `dpbot.db`)

New table (created idempotently in `init_db()`):

```sql
usage_daily(
  token      TEXT,      -- FK to users.token
  day        TEXT,      -- 'YYYY-MM-DD' in Iran local time
  start_used INTEGER,   -- users.used_bytes at first poll of that day
  end_used   INTEGER,   -- users.used_bytes at latest poll of that day
  PRIMARY KEY(token, day)
)
```

- **today's usage(token)** = `end_used - start_used` for `day = today`.
- **panel today** = Σ over tokens.
- **historical day(token, d)** = `end_used - start_used` for that row.
- Retention: rows with `day < today-30` are deleted (cheap `DELETE` once per day).
- **Day boundary** is computed with a fixed **UTC+03:30** offset (Iran abolished DST in 2022), so no `tzdata`/`zoneinfo` dependency is needed and the boundary is stable regardless of the server's system timezone.

Enforcer integration (in `refresh_all_usage()` or right after it, same poll, same DB txn where practical): for each user, upsert today's row — set `start_used` on insert, always update `end_used = used_bytes`. On a fresh `used_bytes < previous end_used` (shouldn't happen because it's monotonic, but guard anyway) clamp so daily delta never goes negative.

Sessions and one-time login tokens are **in-memory** dicts in the bot process (like `pending`), each entry carrying an expiry timestamp:
- `login_tokens: {token -> expires_ts}` (TTL 10 min, single-use).
- `sessions: {sid -> expires_ts}` (TTL 24h).
Restarting the bot clears both (acceptable and safer; you just run `/admin` again).

## 5. Feature 1 — usage summary above the link list

In the `list` handler ([bot.py] `route_cb`, `data == "list"`), when there are links, prepend a summary line to the header text:

```
📋 لینک‌های فعال:
📊 مصرف کل: {fmt_bytes(total_used)} · امروز: {fmt_bytes(today_used)}
```

- `total_used = SELECT SUM(used_bytes) FROM users`.
- `today_used = Σ (end_used - start_used) FROM usage_daily WHERE day = today`.
- Uses existing `fmt_bytes`. No keyboard change; only the message text changes.

## 6. Feature 2 — web admin panel

### 6.1 Routing & hosting
- New thread in `bot.py` starts `ThreadingHTTPServer(("127.0.0.1", 8091), AdminHandler)`.
- cloudflared config gains an ingress rule **before** the catch-all:
  ```yaml
  - hostname: cdn.delplayer.ir
    path: /a/*
    service: http://127.0.0.1:8091
  # existing catch-all → http://127.0.0.1:8090 stays last
  ```
- Port `8091` is configurable via `bot.env` (`ADMIN_PORT`, default 8091) and the ingress path prefix is documented as `/a`.

### 6.2 Auth flow (Model A — one-time link → session cookie)
1. Admin sends `/admin` in Telegram → bot mints `token = secrets.token_urlsafe(24)`, stores `login_tokens[token] = now+600`, replies with `https://cdn.delplayer.ir/a/login/<token>`.
2. `GET /a/login/<token>`: if token exists and not expired → create `sid = secrets.token_urlsafe(24)`, `sessions[sid] = now+86400`, delete the login token (single use), `Set-Cookie: mj_sess=<sid>; HttpOnly; Secure; SameSite=Strict; Path=/a; Max-Age=86400`, then `302 → /a/`. Else → neutral "link expired / run /admin again" page (HTTP 200, no info leak).
3. Every other `/a/*` request requires a valid `mj_sess` cookie; otherwise → the same neutral expired page. No token ever appears in a URL after login.

### 6.3 CSRF & dangerous actions
- Each session has a `csrf` value (derived once, kept with the session). All mutations are `POST` and must carry the matching `csrf` hidden field; mismatch → 403.
- **Delete requires a second confirmation** (a confirm page / explicit "yes, delete" POST). Extends/renames/create do not.

### 6.4 Pages (server-rendered HTML, RTL, dark, mobile-first; inline CSS/JS only)
- **`GET /a/`** dashboard: panel total used, today used, #links, #active, #disabled; a 7-day panel-usage bar chart as **inline SVG** (no external chart lib).
- **`GET /a/users`** list: sortable table — label, used/limit, today, time left, status (🔗/⏸). Row → user detail.
- **`GET /a/user?token=…`** detail: full stats + a 30-day usage bar chart (inline SVG) + the sub URL. Actions (POST):
  - `+volume` (GB), `+time` (days), `rename`, `set unlimited` (volume/time), **delete** (confirmed). These map 1:1 onto existing bot functions — no new mutation semantics. (Arbitrary "set exact value" edits and "reset usage" are intentionally out of scope for now — YAGNI; can be added later if wanted.)
- **`GET /a/new` / `POST /a/new`**: create-link form (volume GB, duration days, name) → calls `create_user`, shows the resulting sub URL.
- All actions call the **existing** bot functions in-process; after any change, `refresh_usage(token)` + `maybe_reenable(token)` as the bot already does.

### 6.5 Security summary
HttpOnly+Secure+SameSite=Strict session cookie · one-time 10-min login token · 24h session · CSRF on all POST · delete double-confirm · neutral 404/expired responses (no enumeration) · admin server bound to `127.0.0.1` (only reachable via the tunnel) · stdlib-only server-rendered HTML, no third-party JS/CSS/fonts.

## 7. Feature 3 — always-on info configs (real working clones)

Implemented **entirely in subserver.py at serve time** (both the raw-base64 response and the HTML page). `write_sub` in bot.py is **unchanged**; the sub file keeps holding only the real configs.

Algorithm when serving `sub-u-<token>`:
1. Decode the base64 file into `links` (the real configs).
2. Read live user row: `used_bytes, limit_bytes, expiry_ts, created_ts, disabled_ts`.
3. Choose a **template** = `links[0]` (first real config; any works since they share the secret). If `links` is empty, skip injection.
4. Build two info configs by cloning the template and rewriting only its display name:
   - **Status config** name:
     - active: `📦 باقی‌مانده: {fmt_bytes(limit-used)} · ⏳ {human_left(expiry)}` (use `نامحدود` where limit/expiry is 0).
     - disabled/exhausted: `⛔ اعتبار تمام شد — تمدید کنید`.
   - **Update config** name:
     - active: `🔄 هر روز یک‌بار آپدیت کنید`.
     - disabled: `🔄 بعد از تمدید، آپدیت کنید`.
5. Output order:
   - active user → `[status, update, *real_links]`.
   - disabled user → `[status, update]` **only** (real configs stripped).
6. The raw response re-base64-encodes this list; the HTML page renders these rows (existing `parse_label` already extracts names for both vmess and vless/trojan).

Name-rewrite helper (new, in subserver.py):
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

Why serve-time injection: remaining volume/time is always fresh (computed on each fetch), no enforcer churn, no rewrite of sub files on disable, and the disabled-state stripping falls out of the existing `disabled_ts` flag with zero bot changes. Because the info configs are real clones, they connect while active and (naturally) stop when the user is removed from xray on exhaustion — exactly the intended behavior.

## 8. Assumptions (confirmed with user)
1. Timezone for "today" / daily boundaries = **Asia/Tehran**.
2. Telegram bot UI stays; web panel is additive.
3. Info configs are **real working clones** of `links[0]`, placed at the **top** of the list.
4. Daily history retention = **30 days**, rolling.
5. Sessions/login tokens are in-memory (bot restart ⇒ re-run `/admin`).

## 9. Build order (phases)
- **Phase 1 — data + feature 1 + feature 3** (small, low risk):
  - `usage_daily` table + enforcer upsert + 30-day prune.
  - List-header summary line.
  - subserver `relabel` + info-config injection + disabled stripping.
- **Phase 2 — feature 2 (web panel)** (larger, security-sensitive):
  - Admin HTTP server thread, auth flow, pages, CSRF, cloudflared ingress rule.

Each phase is independently shippable; Phase 1 needs only `dpbot` + `dpsub` restarts, Phase 2 additionally needs a cloudflared ingress edit + reload.

## 10. Testing / verification
- **Daily usage:** simulate two polls with increasing `used_bytes`; assert `today = end-start`; simulate a counter reset (raw drops) and assert daily delta never negative and cumulative stays monotonic.
- **List summary:** seed users, assert header shows correct total + today.
- **Info configs:** unit-test `relabel` for vmess and vless/trojan (name round-trips via `parse_label`); assert active output = `[status, update, *real]` and disabled output = `[status, update]`; assert clones keep the same secret/host/path as the template.
- **Auth:** expired/unknown login token → neutral page; valid token → cookie set + single-use (second visit fails); `/a/*` without cookie → neutral page; POST without CSRF → 403; delete without confirm → no-op.
- **Local only:** admin server binds `127.0.0.1`; verify it is not reachable except through the tunnel path. Do not leave stray xray test procs (512MB box — see AGENTS.md §4).
- Prefer testing bot/subserver logic locally with a temp sqlite DB; avoid loopback proxy tests on the box (no NAT hairpin ⇒ false negatives).

## 11. Files touched
- `bot/bot.py` — `init_db` (new table), enforcer upsert + prune, list-header summary, admin web server (thread + handler + auth + pages), env `ADMIN_PORT`.
- `sub/subserver.py` — `relabel`, serve-time info-config injection + disabled stripping (raw + HTML paths).
- `config/bot.env.example` — document `ADMIN_PORT`.
- cloudflared config — new `/a/*` ingress rule (ops step, documented in `docs/OPERATIONS.md`).
- `docs/` — note the panel URL scheme and the `/admin` flow.
