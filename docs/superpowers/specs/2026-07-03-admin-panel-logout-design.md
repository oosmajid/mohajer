# Admin Panel Logout — Design

**Date:** 2026-07-03
**Scope:** Add a "خروج" (log out) button to the Mohajer web admin panel.
**Files touched:** `bot/bot.py` only. Subserver, DB schema, and Telegram flow untouched.

## Goal

Let the admin end their panel session on demand from any authenticated page,
instead of waiting for the 24h session cookie to expire.

## Behavior

- A "خروج" button sits in the panel header (`_top`), visible on every
  authenticated page (dashboard, user detail, new, delete-confirm).
- Clicking it:
  1. Removes the current session id from the in-memory `_sessions` map (server-side invalidation).
  2. Clears the browser cookie (`Set-Cookie: mj_sess=; ...; Max-Age=0`).
  3. Renders a "با موفقیت خارج شدی — برای ورودِ دوباره در ربات `/admin` بزن" page.
- After logout, any request to `/a/...` with the (now-cleared, and server-invalidated)
  session shows the existing access-denied page (`render_expired`).

## Security

- Logout is a **POST** guarded by the panel's existing CSRF token — the same
  mechanism `/a/delete`, `/a/new`, etc. already use. This prevents a
  cross-site request from force-logging-out the admin. GET-based logout is
  rejected as inconsistent with the codebase's posture.
- Server-side invalidation (`_sessions.pop`) is the source of truth; clearing
  the cookie is a convenience. Even if the cookie survived, the popped sid is dead.

## Implementation notes (all in `bot/bot.py`)

- **`_top(crumb="", csrf=None)`** — when `csrf` is provided, append a small
  `<form method=post action='/a/logout'>` carrying the hidden csrf input and a
  `btn ghost` "خروج" button, laid out at the header's opposite end from the brand.
  When `csrf` is `None` (unauthenticated pages like `render_expired`), no button.
- **`render_dashboard(csrf)`** — gains a `csrf` parameter so it can pass it to
  `_top`; `route_admin` already computes `csrf` and must now pass it here.
  `render_user`, `render_new`, `render_delconfirm` already receive `csrf`; update
  their `_top(...)` calls to forward it.
- **`route_admin`** — compute `sid = cookie_sid(cookie_header)` (already done inside
  `session_csrf` indirectly; make it explicit) and pass `sid` into `route_admin_post`.
- **`route_admin_post(method, path, query, csrf, body, now, sid)`** — new `/a/logout`
  branch after the existing CSRF check: `_sessions.pop(sid, None)`, return
  `200, {"Content-Type": ..., "Set-Cookie": "<cleared>"}, render_loggedout()`.
- **`render_loggedout()`** — small page mirroring `render_expired`'s shape with the
  logout copy.

## Testing (TDD, `tests/test_admin.py`)

1. POST `/a/logout` with the valid csrf → sid removed from `_sessions`; response
   `Set-Cookie` contains `Max-Age=0`; body contains the logout copy.
2. POST `/a/logout` with a wrong csrf → 403; session still present in `_sessions`.
3. After a successful logout, GET `/a/` with the same cookie → access-denied page
   (proves server-side invalidation, not just cookie clearing).
4. An authenticated dashboard render contains the logout form (`action='/a/logout'`
   and a csrf hidden input).

## Out of scope (YAGNI)

- "Log out everywhere / all sessions" (single-admin assumption; one session at a time in practice).
- Idle-timeout or activity-based session extension.
- Any change to the Telegram bot, subserver, or DB.
