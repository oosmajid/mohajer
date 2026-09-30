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

1. A "user"/"link" = one row in sqlite `users` + one `sub-u-<token>` file.
   Each emitted configuration has its own Xray credential in
   `emission_credentials`, keyed by endpoint tag and config index. A durable
   `active_emissions` snapshot records each emitted configuration's signature.
   A one-time startup migration retires older shared per-endpoint and per-port
   credentials, then gives every current configuration its own identity.
2. The **subscription link** the customer gets is ONE url
   (`https://<DOMAIN>/sub-u-<token>`). Behind it are the configurations selected
   by the recipe, and the URL keeps the same `<token>` across edits.
   Quota is aggregated across all `user>>>u_<token>.*` Xray emails. Distinct
   emitted configurations have distinct UUIDs/passwords, even when they share
   a protocol and external port. Removing one configuration, or changing its
   connection URI (including its clean IP), revokes its old imported URI.
3. Clients connect to **clean Cloudflare edge IPs** (the link's host is the IP; the
   real hostname rides in SNI/`host=`). The bot can rewrite the IPs of every link at
   once from the "🌐 آی‌پی‌های تمیز" panel without changing anyone's link.
4. Quota/expiry are enforced by the **enforcer thread**, not by xray. xray just
   counts bytes; the bot reads the counters and deletes the user when over limit.

## 3. Production servers (verified 2026-09-29)

There are **five active Mohajer instances**. Keep this inventory when planning
changes or deployments; each has its own users, `bot.env`, Xray, and cloudflared.
All five running bot/sub files matched repository commit `2297d9d` when checked.

| Public hostname | SSH target | Bot/env/DB | Bot/sub services |
|-----------------|------------|------------|------------------|
| `cdn.delplayer.ir` | `delplayer` (`root@23.94.29.30:49531`) | `/opt/dpbot/{bot.py,bot.env,dpbot.db}` | `dpbot` / `dpsub` (`/opt/dpsub`) |
| `cdn2.delplayer.ir` | `ubuntu@130.185.122.107` (sudo) | `/opt/mohajer/{bot/bot.py,bot.env,dpbot.db}` | `mohajer-bot` / `mohajer-sub` |
| `cdn3.delplayer.ir` | `ubuntu@130.185.121.65` (sudo) | `/opt/mohajer/{bot/bot.py,bot.env,dpbot.db}` | `mohajer-bot` / `mohajer-sub` |
| `cdn4.delplayer.ir` | `root@149.112.84.49:40273` | `/opt/mohajer/{bot/bot.py,bot.env,dpbot.db}` | `mohajer-bot` / `mohajer-sub` |
| `cdn5.delplayer.ir` | `root@23.94.29.30:50035` | `/opt/mohajer/{bot/bot.py,bot.env,dpbot.db}` | `mohajer-bot` / `mohajer-sub` |

The cdn2 password is in macOS Keychain service `codex-ssh-cdn2` (account `ubuntu`);
cdn5 uses Keychain service `ssh-cdn5` (account `root`). Other SSH credentials also
stay in Keychain/SSH config, never in this repository.
SSH from the operator's laptop may require SOCKS5 `127.0.0.1:10808`:
`-o "ProxyCommand=nc -x 127.0.0.1:10808 -X 5 %h %p"`.
Verify host keys; cdn4 uses port **40273**, not the default port 22.
`cdn.delplayer.ir` is a 512 MB VPS; avoid spawning extra Xray processes there.
Each server currently has one provisioned domain and one path per endpoint; cdn,
cdn2 and cdn3 have REALITY, while cdn4 and cdn5 do not.
On all five hosts, the active `xray.service` reads
`/usr/local/etc/xray/config.json` (verified 2026-09-29). Always recheck
`bot.env` and the unit's `ExecStart` before a future deployment.

## 4. Golden rules / constraints (do NOT relearn these the hard way)

- **REALITY is optional and OFF by default.** It cannot traverse Cloudflare, so it is a
  *direct* link to the server IP (exposes that IP). A box gets it only when `REALITY=`
  (JSON: `port`, `addr`, `ext_port`, `pbk`, `priv`, `sid`, `sni`) is set in `bot.env`
  AND the matching `vless-reality` inbound (listen `0.0.0.0`) exists in xray — add it
  to the running xray with `xray api adi` (no restart) and to `config.json` so it
  survives restarts, then run `resync_all()` so existing users get added to it. It then
  appears in `/a/config` with count **0**; the admin chooses how many links to emit.
  NAT boxes (cdn, cdn4, cdn5) can only use a port the provider forwards.
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
- **Per-user email format starts `u_<token>.<tag>`**. Emitted-configuration
  credentials have a config index and a fresh generation in their email; older
  port-scoped emails use `.tls<port>`, `.none<port>`, or `.reality<port>` suffixes.
  Usage must still be summed across the `user>>>u_<token>` stats prefix. The
  per-email ledger preserves lifetime usage when an old identity is revoked.

## 5. How to make common changes

- **Add/change a protocol or port:** edit `ENDPOINTS` in `bot.env` AND add the
  matching inbound to `xray.config.json` AND the ingress rule in cloudflared. Restart
  xray + cloudflared; the enforcer re-syncs users. `write_sub` auto-emits a config per
  TLS/no-TLS port.
- **Change clean IPs:** use the bot panel or the web panel's `/a/config` page
  (both live, no restart) or set `IPS=` in `bot.env` as the default. Stored override
  lives in `meta.clean_ips`. Use `scripts/cf-clean-ip-scan.sh <host>` from a client
  network to pick them. A changed emitted URI needs a fresh credential; apply
  the change through reconciliation before publishing regenerated subscriptions.
- **Config recipe (types & counts):** the web panel's `/a/config` page (stored in
  `meta.config_recipe` JSON) sets, per endpoint, `enabled` + `count` = how many
  configs of that type to emit. Default (no override) = one per TLS/no-TLS port, i.e.
  the legacy output. The limit is 16 per endpoint and 64 enabled configurations
  per link. When `count` exceeds an endpoint's port-slots, the extra configs cycle
  over ports × clean IPs (`write_sub` honors this). Every
  extra configuration has its own credential; reducing `count` revokes the
  removed identities even when other configurations use the same port. Saving
  reconciles credentials and regenerates subscriptions. `get_recipe()`/
  `set_recipe()` live next to `get_ips()`.
- **Per-link settings:** `/a/user-config?token=<token>` selects the public default
  or stores a full settings snapshot in `users.config_override`. A custom link has
  its own recipe, clean IP list, prepared endpoint options, and outbound routing;
  it shows an `اختصاصی` badge in the dashboard. Switching back to default deletes
  the snapshot, so later global changes apply again. Removed or changed emitted
  configurations revoke their old Xray credentials. A signature change to a
  connection parameter (protocol, port, clean IP, Host, SNI, or path) rotates the
  affected credential. Changing only the display label keeps that credential.
- **First boot after the per-configuration migration:** `init_db()` snapshots
  every provisioned port for legacy links missing `active_slots`, because the old
  bot registered their shared secret on every endpoint even when the recipe hid
  some configs. It also snapshots the current output in `active_emissions` and
  marks every pre-emission link with `emission_migration_pending` and
  `membership_sync_pending`. The enforcer retires all older shared identities,
  including those of frozen or disabled links, and provisions separate identities
  for current configurations. This proactively revokes old imported URIs whose
  prior count or clean-IP edits cannot be reconstructed from SQLite. **Every
  existing customer must refresh the same subscription URL after this one-time
  migration.** Normal API reconciliation does not require an Xray restart; a
  failed revocation can trigger a controlled restart and resync.
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
  `XRAY_CONF` **must** point at the config the Mohajer xray unit actually runs. On
  cdn2, both `bot.env` and the active `xray.service` use
  `/usr/local/etc/xray/config.json` (verified 2026-09-29). The previously recorded
  `/opt/mohajer/xray.json` path does not exist on that host.
  The page is AJAX: every action posts `ajax=1` and gets JSON back (`ok/msg/list`), so
  nothing reloads; the same routes still answer with redirects when JS is off.
- **Subscriber page extras:** `subserver.py` shows a quick-connect card: a Happ deep-link
  button (`happ://add/<sub url>`), Happ downloads (`HAPP_APPS`, GitHub
  `releases/latest/download/...` so they never go stale; iOS = App Store) and a QR of the
  sub URL. The QR encoder is vendored (`sub/qrcode.js`, qrcode-generator 2.0.4, MIT) and
  served at `/sub-qr.js` — keep it next to `subserver.py` when deploying (the tunnel only
  routes `/sub-*` to the sub server; a CDN script tag may be blocked in Iran).
- **Bot "new link" menu** starts with a one-tap test link (`TEST_LINK`: 500MB, 1 day, "Test").
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
- **Inspect state:** `sqlite3 <db> "SELECT label,used_bytes,max(used_bytes-usage_reset_bytes,0) AS current_used,limit_bytes,expiry_ts FROM users"`.

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
  uuid  TEXT,               -- original legacy credential; new links use emission_credentials
  email TEXT UNIQUE,        -- "u_<token>" (db bookkeeping)
  label TEXT,               -- human name ("Fifi", "Me", …)
  limit_bytes INTEGER,      -- 0 = unlimited
  expiry_ts   INTEGER,      -- unix; 0 = never
  created_ts  INTEGER,
  base_bytes  INTEGER,      -- carried-over usage across xray counter resets
  last_raw    INTEGER,      -- last raw counter value seen (reset detection)
  used_bytes  INTEGER,      -- lifetime traffic: base + last_raw
  usage_reset_bytes INTEGER, -- lifetime baseline; UIs/quota use max(used_bytes-this, 0)
  config_override TEXT,     -- NULL = public default; JSON = full per-link snapshot
  credential_mode TEXT,     -- legacy, slots, or emissions
  active_slots TEXT,        -- JSON snapshot of legacy endpoint/port identities
  active_emissions TEXT,    -- JSON [tag,index,signature] snapshot of emitted configs
  usage_anchor INTEGER,     -- lifetime usage before per-email ledger starts
  auth_pending INTEGER,     -- retry incomplete Xray revocation
  pending_delete INTEGER,   -- keep deletion state until access is revoked
  emission_migration_pending INTEGER -- retire pre-emission shared credentials
)
slot_credentials(token, tag, security, external_port, email, secret)
slot_mode_tags(token, tag)         -- legacy endpoints migrated to per-port credentials
emission_credentials(token, tag, config_index, signature, email, secret)
emission_mode_tags(token, tag)     -- endpoints migrated to per-config credentials
legacy_emails(token, tag, email)   -- rotated stats identity after freeze/disable
usage_ledger(email, token, last_raw, total_bytes)
meta(k TEXT PRIMARY KEY, v TEXT)   -- includes clean_ips, config_recipe,
                                   -- endpoint_settings, outbounds, xray_pid,
                                   -- membership_sync_pending, outbound_sync_pending
```

See `docs/ARCHITECTURE.md` for the full flow diagrams and `docs/OPERATIONS.md` for
the day-2 runbook.
