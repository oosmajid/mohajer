# AGENTS.md — operating guide for AI agents working on Mohajer

> If you are an AI coding agent picking up this repo, read this file fully before
> touching anything. It encodes the non-obvious facts, the production layout, and
> the hard-won constraints that aren't visible from the code alone.
> (`CLAUDE.md` is a copy of this file.)

---

## 1. What this project is

A single-VPS V2Ray subscription panel driven entirely from one Telegram bot. Two
small Python processes + `xray-core` + a `cloudflared` tunnel. No pip deps, no web
framework, no DB server. Everything is configured through one env file (`bot.env`).

- **`bot/bot.py`** — the admin-only Telegram bot. Long-polls `getUpdates`, renders
  inline-keyboard menus, mints/lists/deletes subscription links, edits clean IPs,
  and runs a background **enforcer** thread (quota/expiry/resync).
- **`sub/subserver.py`** — read-only HTTP server on `127.0.0.1:8090`. Serves each
  `sub-u-<token>` file as raw base64 to proxy clients (+ `Subscription-Userinfo`
  header) or as a mobile HTML copy-page to browsers (UA sniff on `"Mozilla"`).
- **`xray-core`** — the actual proxy. Inbounds listen on `127.0.0.1` only; clients
  are **not** in `config.json` — the bot injects/removes them live via the gRPC API.
- **`cloudflared`** — outbound tunnel mapping Cloudflare paths → local xray inbounds.

## 2. The mental model (read this twice)

1. A "user"/"link" = one row in sqlite `users` + one `sub-u-<token>` file + a client
   added to **every** xray inbound (one per endpoint) under email `u_<token>.<tag>`.
2. The **subscription link** the customer gets is ONE url
   (`https://<DOMAIN>/sub-u-<token>`). Behind it are N configs (one per endpoint ×
   per TLS/no-TLS port), all sharing the same uuid/password `secret` and the same
   `<token>`. That's why quota is aggregated by the `user>>>u_<token>` prefix.
3. Clients connect to **clean Cloudflare edge IPs** (the link's host is the IP; the
   real hostname rides in SNI/`host=`). The bot can rewrite the IPs of every link at
   once from the "🌐 آی‌پی‌های تمیز" panel without changing anyone's link.
4. Quota/expiry are enforced by the **enforcer thread**, not by xray. xray just
   counts bytes; the bot reads the counters and deletes the user when over limit.

## 3. Production server (the live deployment)

The canonical running instance ("delplayer") differs from this repo's fresh-install
defaults — **mind the path mapping** when SSHing in:

| Thing                | This repo (fresh install) | Live server (delplayer)        |
|----------------------|---------------------------|--------------------------------|
| bot dir / file       | `/opt/mohajer/bot/bot.py` | `/opt/dpbot/bot.py`            |
| sub server           | `/opt/mohajer/sub/...`    | `/opt/dpsub/subserver.py`      |
| env file             | `/opt/mohajer/bot.env`    | `/opt/dpbot/bot.env`           |
| sqlite db            | `/opt/mohajer/dpbot.db`   | `/opt/dpbot/dpbot.db`          |
| sub files dir        | `/opt/mohajer/sub`        | `/opt/dpsub`                   |
| systemd units        | `mohajer-bot/-sub`        | `dpbot` / `dpsub`              |
| xray config          | `/usr/local/etc/xray/config.json` (same) |                 |
| cloudflared config   | `/root/.cloudflared/config.yml` (same)   |                 |
| public hostname      | `cdn.example.ir`          | `cdn.delplayer.ir`             |

SSH: `ssh -p 49531 root@23.94.29.30`. **The box has only 512MB RAM.** Under memory
pressure `sshd` can't fork and you get *"Connection timed out during banner
exchange"* even though the TCP port is open. When the direct route is throttled
from Iran, tunnel SSH through the operator's local proxy:

```
ssh -p 49531 -o "ProxyCommand=nc -x 127.0.0.1:10808 -X 5 %h %p" root@23.94.29.30
```

(`10808` = SOCKS5, `10809` = HTTP, on the operator's laptop.)

## 4. Golden rules / constraints (do NOT relearn these the hard way)

- **xray-core only — no REALITY.** REALITY was tried and fully removed: it does its
  own TLS forgery and CANNOT traverse Cloudflare, and field-tested broken on the
  target ISP. Don't re-add it. Remnant strings, if any, are dead.
- **Never leave stray `xray run -config /tmp/...` test processes on the server.**
  On a 512MB box they cause OOM, which kills `sshd`'s ability to fork → the banner
  timeout above. A past outage was exactly this. Always `kill` test procs in a
  `finally`/trap. Prefer NOT spawning extra xray on the box at all.
- **The bot adds clients to the *running* xray only** (`xray api adu`), not to
  `config.json`. So a plain `systemctl restart xray` would drop every user — but the
  enforcer detects the xray MainPID change and **re-syncs all users automatically**
  (`resync_all`) on the next poll. Customers keep the SAME links across restarts/reboots.
- **no-TLS configs require "Always Use HTTPS" = OFF** in the Cloudflare zone, and use
  HTTP ports (80/8080/8880/2052/2082/2095). They were empirically faster on Irancell.
- **Endpoint `tag` must match three places**: `xray.config.json` inbound tag,
  `bot.env` `ENDPOINTS[].tag`, and is what `adu/rmu/statsquery` key off. Same for
  `path` (xray inbound ↔ ENDPOINTS ↔ cloudflared ingress rule).
- **Single admin assumption** keeps the in-memory `pending` dict tiny (≈1 entry).
  Don't turn this into a multi-tenant service without revisiting that.
- **Per-user email format is `u_<token>.<tag>`**; usage is summed across tags by the
  `user>>>u_<token>` stats prefix. Keep that scheme or quota breaks.

## 5. How to make common changes

- **Add/change a protocol or port:** edit `ENDPOINTS` in `bot.env` AND add the
  matching inbound to `xray.config.json` AND the ingress rule in cloudflared. Restart
  xray + cloudflared; the enforcer re-syncs users. `write_sub` auto-emits a config per
  TLS/no-TLS port.
- **Change clean IPs:** use the bot panel or the web panel's `/a/config` page
  (both live, no restart) or set `IPS=` in `bot.env` as the default. Stored override
  lives in `meta.clean_ips`. Use `scripts/cf-clean-ip-scan.sh <host>` from a client
  network to pick them.
- **Config recipe (types & counts):** the web panel's `/a/config` page (stored in
  `meta.config_recipe` JSON) sets, per endpoint, `enabled` + `count` = how many
  configs of that type to emit. Default (no override) = one per TLS/no-TLS port, i.e.
  the legacy output. `count` is UNCAPPED; when it exceeds an endpoint's port-slots the
  extra configs cycle over ports × clean IPs (`write_sub` honors this). Saving
  regenerates every sub. `get_recipe()`/`set_recipe()` live next to `get_ips()`.
- **Outbounds / clean egress (`/a/outbounds`, `meta.outbounds`):** paste a
  `vless/trojan/ss/socks/http` link → it becomes an xray outbound tagged **`mj-<name>`**.
  Per outbound you list domains; **an empty list makes it the catch-all** (it is written
  as xray's FIRST outbound, which is where unmatched traffic goes) and only the first
  empty one wins. `apply_xray_outbounds()` rewrites **only** `outbounds`/`routing` plus
  our `mjtest-*` inbounds; it keeps every real inbound and every routing rule it doesn't
  own — **critically `inboundTag:[api] → outboundTag:api`, without which the gRPC API
  dies and the bot can no longer manage users.** Ownership is decided purely by the
  `mj-`/`mjtest-` prefixes, so deleting an outbound removes exactly its own rules. The
  new config is validated with `xray -test` and never written if invalid (timestamped
  `.bak` kept). Each outbound also gets a **loopback-only** SOCKS inbound on
  `OB_TEST_PORT_BASE+i` (10810+) that the panel's 🔎 test dials through — that is how we
  test egress **without spawning a second xray** (see the golden rule above).
  `XRAY_CONF` **must** point at the config the Mohajer xray unit actually runs; on cdn2
  that is `/opt/mohajer/xray.json`, NOT the default (which belongs to another stack).
  The page is AJAX: every action posts `ajax=1` and gets JSON back (`ok/msg/list`), so
  nothing reloads; the same routes still answer with redirects when JS is off.
- **Light/dark theme:** both the admin panel (`ADMIN_CSS`/`_page`) and the subscriber
  page (`subserver.py` `PAGE`) ship an icon-only toggle (🌙/☀️) at the top. Themes are
  driven by `data-theme` on `<html>`; an early head script sets it from `localStorage`
  (`mj-theme`), falling back to the OS `prefers-color-scheme` (no FOUC). The dark palette
  is a `:root[data-theme=dark]{…}` override of the same tokens. Always-yellow surfaces
  (`.hero`, primary `.btn`/`button`) pin dark ink so they stay high-contrast in dark, and
  charts use `currentColor` so bars follow the theme. Never hardcode `#111111` on a
  themeable surface — use `var(--ink)`.
- **Edit bot logic:** it's one file, stdlib only. After editing, copy to the server
  path (see table) and `systemctl restart dpbot` (live) / `mohajer-bot` (fresh).
- **Inspect state:** `sqlite3 <db> "SELECT label,used_bytes,limit_bytes,expiry_ts FROM users"`.

## 6. Verifying a change without breaking prod

- The bot is idempotent on restart and re-syncs users, so restarts are safe.
- Check logs: `journalctl -u dpbot -f` (or `mohajer-bot`).
- Memory is the scarce resource: `free -m` should show headroom; bot RSS is ~25MB
  and flat (audited — no leak; sqlite conns freed by refcounting, `pending` ≤1).
- To confirm egress/identity of a link, the simplest real test is connecting a client
  to it; loopback tests on the box give false negatives (no NAT hairpin).

## 7. Data model (sqlite, `dpbot.db`)

```
users(
  token TEXT PRIMARY KEY,   -- 16 hex chars; identifies the link everywhere
  uuid  TEXT,               -- the shared secret (uuid for vless/vmess, password for trojan)
  email TEXT UNIQUE,        -- "u_<token>" (db bookkeeping; xray emails are u_<token>.<tag>)
  label TEXT,               -- human name ("Fifi", "Me", …)
  limit_bytes INTEGER,      -- 0 = unlimited
  expiry_ts   INTEGER,      -- unix; 0 = never
  created_ts  INTEGER,
  base_bytes  INTEGER,      -- carried-over usage across xray counter resets
  last_raw    INTEGER,      -- last raw counter value seen (reset detection)
  used_bytes  INTEGER       -- base + last_raw (what UIs show)
)
meta(k TEXT PRIMARY KEY, v TEXT)   -- clean_ips, config_recipe, xray_pid, admin_id (bootstrap)
```

See `docs/ARCHITECTURE.md` for the full flow diagrams and `docs/OPERATIONS.md` for
the day-2 runbook.
