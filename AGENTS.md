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
   Its enabled endpoints and external port slots determine which clients are added
   to Xray. New links have a separate credential per `(endpoint tag, TLS mode,
   external port)` in `slot_credentials`; legacy links retain their original
   per-endpoint credential until an endpoint needs revocation.
2. The **subscription link** the customer gets is ONE url
   (`https://<DOMAIN>/sub-u-<token>`). Behind it are N configs (one per endpoint ×
   per TLS/no-TLS port), and the URL keeps the same `<token>` across edits.
   Quota is aggregated across all `user>>>u_<token>.*` Xray emails. Each fresh
   external port slot has its own UUID/password, so removing that port invalidates
   its old imported URI. If multiple emitted configs share a slot and differ only
   by clean IP, they share that slot credential.
3. Clients connect to a **Cloudflare-proxied hostname or clean edge IPv4 address**
   (the link's dial address; the provisioned hostname rides in SNI/`host=`).
   When the address list mixes IPv4 and a hostname from `HOST_PROFILES`, `write_sub`
   keeps every old IP URI, adds a DNS URI per enabled CDN endpoint, and adds one
   URI using the final listed IPv4 with the new hostname as Host/SNI. Adding a
   hostname never changes `DOMAIN` or `SUB_BASE_URL`; old subscription URLs stay.
4. Quota/expiry are enforced by the **enforcer thread**, not by xray. xray just
   counts bytes; the bot reads the counters and deletes the user when over limit.

## 3. Production servers (updated 2026-10-04)

There are **four active Mohajer panels**. CDN3 users were merged into CDN2 on
2026-10-01 (50 total links at migration). The operator deleted the old CDN3 VPS
on 2026-10-03. Do not use `130.185.121.65` as an SSH target or jump host. The
old CDN3 hostnames and subscriptions still terminate on CDN2 and must be preserved.
The operator requested removal of **all relays** on 2026-10-04. Nicol at
`root@188.121.104.169:22` is retired: its relay unit and private key were archived,
TCP 8443 is closed, and its restricted SSH key on CDN2 was revoked. The VPS itself
still exists; do not restore a relay without a new operator request. The failed
earlier Nicol at `95.38.190.210` is also not an active relay target.
All four panels have no global or custom relay ingress entries. CDN2 has 46 links,
all using public settings; 92 Nicol URIs were removed. Each now contains 24 CDN
configs plus two direct REALITY configs, with two status rows added by the sub
server. Remaining URIs, credentials, quotas and subscription URLs were preserved.
Refresh the existing subscription URL in clients to remove cached Nicol configs.

| Public hostname | SSH target | Bot/env/DB | Bot/sub services |
|-----------------|------------|------------|------------------|
| `cdn.delplayer.ir` | `delplayer` (`root@23.94.29.30:49531`) | `/opt/dpbot/{bot.py,bot.env,dpbot.db}` | `dpbot` / `dpsub` (`/opt/dpsub`) |
| `cdn2.delplayer.ir` | `ubuntu@130.185.122.107` (sudo) | `/opt/mohajer/{bot/bot.py,bot.env,dpbot.db}` | `mohajer-bot` / `mohajer-sub` |
| `cdn3.delplayer.ir` | Old VPS deleted; no SSH target | Users/subscriptions served by CDN2 | CDN2 `cloudflared-cdn3`; no standalone panel |
| `nicol` (retired relay VPS) | `nicol` (`root@188.121.104.169:22`) | No panel/DB; relay keys archived root-only | No relay unit; public TCP 8443 closed |
| `cdn4.delplayer.ir` | `root@149.112.84.49:40273` | `/opt/mohajer/{bot/bot.py,bot.env,dpbot.db}` | `mohajer-bot` / `mohajer-sub` |
| `cdn5.delplayer.ir` | `root@23.94.29.30:50035` | `/opt/mohajer/{bot/bot.py,bot.env,dpbot.db}` | `mohajer-bot` / `mohajer-sub` |

The cdn2 password is in macOS Keychain service `codex-ssh-cdn2` (account `ubuntu`);
cdn5 uses Keychain service `ssh-cdn5` (account `root`). Nicol uses Keychain service
`codex-ssh-nicol` (account `root`), with SSH alias `nicol`. Never store its password
in repository files or handoffs. Other SSH credentials also
stay in Keychain/SSH config, never in this repository.
SSH from the operator's laptop may require SOCKS5 `127.0.0.1:10808`:
`-o "ProxyCommand=nc -x 127.0.0.1:10808 -X 5 %h %p"`.
Verify host keys; cdn4 uses port **40273**, not the default port 22.
`cdn.delplayer.ir` is a 512 MB VPS; avoid spawning extra Xray processes there.
All original public hostnames retain parallel old `cdn[2-5].delplayer.ir` and new
`cdn[2-5].windertop.cfd` Cloudflare Tunnel routes, with the old hostname retained
as `DOMAIN` and `SUB_BASE_URL`. (For the first box the hostname is `cdn`.)
`HOST_PROFILES` permits the matching new hostname on each bot. CDN2 also has
the imported CDN3 Host/SNI pairs and inbounds. cdn and cdn2 have REALITY
provisioned; cdn4/cdn5 do not. The former CDN3 REALITY relay is retired.
On all four panel hosts, the active `xray.service` reads
`/usr/local/etc/xray/config.json` (verified 2026-09-29). Always recheck
`bot.env` and the unit's `ExecStart` before a future deployment.

### Consolidated CDN3 and configurable relay ingress

- Old `cdn3.delplayer.ir/sub-u-<token>` URLs remain unchanged. Imported users have
  `meta.sub_base_<token>` and complete custom snapshots; `sub_url()` honors the
  per-link base. Never replace the old hostname or automatically add relay
  configs for all users.
- CDN2 runs `cloudflared-cdn3.service` alongside its original connector, using the
  same old tunnel and hostnames; imported paths terminate on ports 10200–10203.
  Imported REALITY terminates on loopback 10443 with its original keys.
- Nicol previously forwarded public TCP 8443 over restricted SSH to CDN2 loopback
  10443. That route was removed on 2026-10-04 through the normal panel save flow,
  including revocation of the unused backend identities. `meta.relay_ingresses`
  is empty on all four panels; no custom link contains relay routes. Nicol's
  `mohajer-relay.service` is disabled and removed, and its private key is archived.
  CDN2's `mohajer-relay` account is password-locked with an empty authorized_keys;
  the restrictive sshd Match block remains. Imported backend configuration stays
  prepared but its recipe is disabled/count 0 and its old Nicol dial address is
  historical metadata, not an active route. Source IP/name belong to deployment
  settings, never product code constants. All panel, Xray and Cloudflare service
  PIDs were unchanged during removal.
- Removal backups: CDN2 `/var/backups/mohajer-remove-relays-20261004-093700`
  (DB, subscriptions, environment, bot, Xray and before/after snapshots), Nicol
  `/var/backups/mohajer-relay-retired-20261004-093750` (unit and SSH keys), and CDN2
  `/var/backups/mohajer-relay-auth-retired-20261004-093753` (revoked key and sshd
  restriction). Backups are root-only; restore only on an explicit operator request.
- Current nicol setup backup on CDN2: `/var/backups/mohajer-nicol-20261003-190357`.
  Publication backup (DB, subscriptions, environment, Xray config and manifest):
  `/var/backups/mohajer-nicol-publish-20261003-190826`.
  The first failed replacement setup backup remains at
  `/var/backups/mohajer-nicol-20261003-184601`.
  Disk cleanup on 2026-10-03 freed about 3 GiB from archived journals, package
  cache and unused temporary downloads; old syslog/btmp archives were compressed.
  All Mohajer, Xray and tunnel units remained active; free space was about 3.1 GiB.
- `/a/config` and custom `/a/user-config` contain generic **relay ingresses**:
  label, address (IP or DNS), external port, prepared REALITY backend, on/off,
  and uncapped count. Multiple relays may share one backend. Public state is
  `meta.relay_ingresses`; custom snapshots use `ingresses`. Old custom snapshots
  without this key inherit no new routes. Default is off/count 0.
- Relay routes add authorization for the selected backend even when its ordinary
  profile is off. Backend identity uses the original REALITY port, not the relay
  external port; editing the dial address/port must not rotate credentials.
  Removing the final emitted route revokes that backend identity normally.
- The form configures client dial routes; another server must already forward
  to the selected backend. Entering an address alone does not provision a server.
- Imported endpoints use an environment `compatibility: true` flag. Unused ones
  are hidden in the form and preserved on save; active historical custom profiles
  remain editable. Internal Xray tags are never displayed as card captions.
- Source backup: `/var/backups/mohajer-relay-20261001-144949/retired-panel`.
  Target pre-migration and before-relay backups:
  `/var/backups/mohajer-relay-20261001-144950`; correction backup:
  `/var/backups/mohajer-ingress-20261001-155029`.
- Xray on CDN2 runs as `nobody`. Its active config must remain readable by that
  user (root:nogroup 0640); validate as the service user before declaring boot safe.

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
- **Per-user email format starts `u_<token>.<tag>`**. Port-scoped emails add a
  `.tls<port>`, `.none<port>`, or `.reality<port>` suffix and a generation. Usage
  must still be summed across the `user>>>u_<token>` stats prefix. The per-email
  ledger preserves lifetime usage when an old identity is revoked.

## 5. How to make common changes

- **Add/change a protocol or port:** edit `ENDPOINTS` in `bot.env` AND add the
  matching inbound to `xray.config.json` AND the ingress rule in cloudflared. Restart
  xray + cloudflared; the enforcer re-syncs users. `write_sub` auto-emits a config per
  TLS/no-TLS port.
- **Change CDN dial addresses:** use the bot panel or the web panel's `/a/config`
  page (both live, no restart) or set `IPS=` in `bot.env` as the default. Entries
  can be a Cloudflare-proxied hostname or IPv4. Stored override lives in the
  legacy `meta.clean_ips` key. Put the provisioned `cdn[2-5].windertop.cfd`
  alongside the existing IPv4 addresses; the code emits old IP URIs plus DNS and
  new-SNI IP companions. The last IPv4 in the list is used for that companion.
  Keep the new hostname in `HOST_PROFILES` and in Cloudflare Tunnel ingress.
  Use `scripts/cf-clean-ip-scan.sh <host>` only for manually pinned IPv4 addresses.
  Test a new hostname and matching Host/SNI before adding it to live subscriptions.
- **Config recipe (types & counts):** the web panel's `/a/config` page (stored in
  `meta.config_recipe` JSON) sets, per endpoint, `enabled` + `count` = how many
  configs of that type to emit. Default (no override) = one per TLS/no-TLS port, i.e.
  the legacy output. `count` is UNCAPPED; when it exceeds an endpoint's port-slots the
  extra configs cycle over ports × clean IPs (`write_sub` honors this). Saving
  regenerates every sub. `get_recipe()`/`set_recipe()` live next to `get_ips()`.
- **Per-link settings:** `/a/user-config?token=<token>` selects the public default
  or stores a full settings snapshot in `users.config_override`. A custom link has
  its own recipe, clean IP list, prepared endpoint options, relay ingress routes, and outbound routing;
  it shows an `اختصاصی` badge in the dashboard. Switching back to default deletes
  the snapshot, so later global changes apply again. Port and protocol removals
  revoke old Xray credentials; older links migrate the affected endpoint to slot
  credentials the first time its ports change.
- **Delete latency / resync:** deleting a custom link with copied global routing
  does not rewrite outbounds or resync other users. Divergent custom route cleanup
  is queued atomically in `outbound_sync_pending` after revocation; the enforcer
  handles it. `resync_all()` restores eligible users in one `api adu` batch with
  their stored legacy/per-slot secrets. Every batch result must be acknowledged;
  a zero CLI exit code alone does not prove registration succeeded.
- **Bulk quota/time:** dashboard **افزایش گروهی** opens `/a/bulk`. Review a positive
  decimal GB/day amount, then confirm the displayed set of finite links within
  ten minutes. Unlimited fields and pending deletion links are excluded; expired
  time starts at confirmation time, manual freeze remains. The confirmed set is
  stored in `meta.bulk_<operation>` and applied in one SQLite transaction with a
  durable result, so retrying a POST cannot extend twice. Credentials, usage and
  subscription files remain unchanged. The enforcer renews eligible disabled links.
- **TLS client settings:** public and custom endpoint settings store independent
  `fingerprint` (default `chrome`), `fragment_fm`, and `ech_enabled` (default false).
  Removing Fragment does not remove fingerprint or ALPN. ECH only applies to CDN
  TLS; no-TLS and REALITY do not receive it. `ech=` queries the actual SNI's HTTPS
  record via `ECH_DOH_URL` (default `https://sky.rethinkdns.com/dns-query`). The
  resolver and CDN must be reachable from the client. VMess keeps legacy base64
  JSON when ECH is off; opting into ECH emits the standard VMess URI because
  v2rayN/NG's legacy JSON parsers drop ECH. This needs an updated client. Credentials,
  ports, paths, and subscription URLs stay unchanged. The `tls_fields=1` form
  marker permits explicit ECH off; older open forms preserve saved TLS options.
- **First boot after the per-link migration:** existing legacy rows have no
  `active_slots`. `init_db()` snapshots *every provisioned port* for those rows,
  because the old bot had registered their shared secret on every endpoint even
  when the recipe hid some configs. It sets `membership_sync_pending`; the first
  enforcer pass reconciles and revokes hidden protocols/ports without requiring
  an Xray restart. Affected legacy clients need to refresh the same subscription
  URL when an endpoint moves to per-port credentials.
- **Outbounds / clean egress (`/a/config#outbounds`, `meta.outbounds`):** paste a
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
  `OB_TEST_PORT_BASE+i` (10810+) that the panel's test action dials through — that is how we
  test egress **without spawning a second xray** (see the golden rule above).
  `XRAY_CONF` **must** point at the config the Mohajer xray unit actually runs. On
  cdn2, both `bot.env` and the active `xray.service` use
  `/usr/local/etc/xray/config.json` (verified 2026-09-29). The previously recorded
  `/opt/mohajer/xray.json` path does not exist on that host.
  The outbound section is included below the global settings form on `/a/config`.
  `/a/outbounds` still renders that page for existing links. Outbound actions use AJAX:
  every action posts `ajax=1` and gets JSON back (`ok/msg/list`), so nothing reloads;
  the same routes still answer with redirects when JS is off.
- **Subscriber page extras:** `subserver.py` shows the customer's saved link label as
  the heading (escaped for HTML), then a quick-connect card: Happ and v2rayNG import
  buttons. Happ uses `happ://add/<sub url>`; v2rayNG uses
  `v2rayng://install-config?url=<encoded sub url>#<encoded label>`. Both derive
  the URL from the opened page, preserving old CDN3 links. Buttons share SVG
  icons and stack on mobile. Happ downloads (`HAPP_APPS`, GitHub
  `releases/latest/download/...` so they never go stale; iOS = App Store) and a QR of the
  sub URL. The QR encoder is vendored (`sub/qrcode.js`, qrcode-generator 2.0.4, MIT) and
  served at `/sub-qr.js` — keep it next to `subserver.py` when deploying (the tunnel only
  routes `/sub-*` to the sub server; a CDN script tag may be blocked in Iran).
- **Bot "new link" menu** starts with a one-tap test link (`TEST_LINK`: 500MB, 1 day, "Test").
- **Panel font:** both panels load the vendored Vazirmatn v33.003 variable WOFF2
  from `/sub-font-vazirmatn-v33.003.woff2`, served by the sub server through the
  existing `/sub-*` tunnel route. Deploy `sub/fonts/` (font + SIL OFL license)
  next to `subserver.py` on every host; no external font service is required.
- **Light/dark theme:** both the admin panel (`ADMIN_CSS`/`_page`) and the subscriber
  page (`subserver.py` `PAGE`) ship an SVG icon-only toggle at the top. Themes are
  driven by `data-theme` on `<html>`; an early head script sets it from `localStorage`
  (`mj-theme`), falling back to the OS `prefers-color-scheme` (no FOUC). The dark palette
  is a `:root[data-theme=dark]{…}` override of the same tokens. The admin panel uses
  blue action and chart tokens; the subscriber page uses the same blue palette. Charts
  use `currentColor` so bars follow the theme. Use palette tokens on themeable surfaces.
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
  uuid  TEXT,               -- original legacy credential; new links use slot_credentials
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
  credential_mode TEXT,     -- legacy or slots
  active_slots TEXT,        -- JSON snapshot of emitted endpoint/port identities
  usage_anchor INTEGER,     -- lifetime usage before per-email ledger starts
  auth_pending INTEGER,     -- retry incomplete Xray revocation
  pending_delete INTEGER    -- keep deletion state until access is revoked
)
slot_credentials(token, tag, security, external_port, email, secret)
slot_mode_tags(token, tag)         -- legacy endpoints migrated to per-port credentials
legacy_emails(token, tag, email)   -- rotated stats identity after freeze/disable
usage_ledger(email, token, last_raw, total_bytes)
meta(k TEXT PRIMARY KEY, v TEXT)   -- includes clean_ips, config_recipe,
                                   -- endpoint_settings, relay_ingresses, outbounds, xray_pid,
                                   -- membership_sync_pending, outbound_sync_pending
```

See `docs/ARCHITECTURE.md` for the full flow diagrams and `docs/OPERATIONS.md` for
the day-2 runbook.
