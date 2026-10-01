# Architecture

## Components & ports

```
                    Internet (Iran ISPs)
                            │
            clean CF edge IP : 443/2053/8443/2087   (TLS)
                            : 80/8080/8880           (no-TLS)
                            ▼
                    Cloudflare anycast
              (routes by Host/SNI + URL path)
                            │  outbound tunnel (cloudflared dials out)
                            ▼
   ┌──────────────────────── VPS 23.94.29.30 (512MB) ───────────────────────┐
   │  cloudflared  ── path ─►  xray-core inbounds (127.0.0.1 only)           │
   │     /1afb5cae5563  ─────►  vless-ws    :10000                            │
   │     /xh38900ed9    ─────►  vless-xh    :10001                            │
   │     /tr4b0be2a3    ─────►  trojan-ws   :10002                            │
   │     /vmd32dedfa    ─────►  vmess-ws    :10003                            │
   │     /sub-*         ─────►  sub-server  :8090   (subserver.py)            │
   │                                                                          │
   │  xray api (gRPC)  :10085  ◄── bot.py (adu / rmu / statsquery)            │
   │  bot.py  ── long-poll ──►  api.telegram.org                             │
   │  sqlite  dpbot.db   ◄── bot.py (rw), subserver.py (ro)                   │
   └──────────────────────────────────────────────────────────────────────────┘
```

## Process responsibilities

### bot.py (two threads)
- **Main thread**: Telegram long-poll loop (`getUpdates`, 50s timeout) → `handle_update`
  → inline-keyboard router `route_cb` and text-stage handler. Admin-gated by `is_admin`.
- **Enforcer thread** (`enforcer`, every `POLL_SECONDS`):
  1. If xray's `MainPID` changed (restart/reboot) → `resync_all()` re-adds every user
     to the running xray and rewrites their sub files.
  2. `refresh_all_usage()` — ONE `statsquery` for all users (avoids N subprocess forks),
     handles counter resets, updates `used_bytes`.
  3. For each user, if over `limit_bytes` or past `expiry_ts` → `delete_user()` + notify admin.

### subserver.py
- `GET /sub-u-<token>`: looks up the user in `dpbot.db` (read-only).
  - Proxy client (UA lacks "Mozilla", or `?raw`): returns the base64 config list +
    `Subscription-Userinfo: upload=…; download=…; total=…; expire=…`.
  - Browser: renders the mobile copy-page (per-config + copy-all + sub-link buttons,
    data and time progress bars computed from the db row).

## Key flows

### Create a link
`create_user(vol_gb, dur_days, label)`:
1. `token = secrets.token_hex(8)`, `secret = uuid4()`.
2. `xr_add_user` → `adu` to every endpoint inbound (email `u_<token>.<tag>`).
3. `write_sub` → base64 file of one config per endpoint × TLS/no-TLS port, CDN
   dial addresses (hostname or IPv4) round-robined from `get_ips()`.
4. Insert the `users` row.

### Mint configs (`write_sub` → `_ws_link`)
For each endpoint: for each `tls_ports` emit a TLS config, for each `notls_ports` emit a
no-TLS config. `_ws_link` builds protocol-correct URIs:
- vless: `vless://<uuid>@<address>:<port>?encryption=none&security=<tls|none>&type=<ws|xhttp>&host=<DOMAIN>[&sni=<DOMAIN>]&path=<path>[&mode=auto]#<label>`
- trojan: `trojan://<password>@<address>:<port>?security=…&type=ws&host=<DOMAIN>&path=<path>#<label>`
- vmess: base64 of the standard vmess JSON (`add=<address>`, `host/sni=<DOMAIN>`, `tls` on/off).

The client dials either a **Cloudflare-proxied hostname** (DNS picks the edge IP)
or a manually selected edge IPv4 address. `host=`/`sni=` carry the provisioned
hostname for Cloudflare routing and TLS. A working hostname must be configured
before switching a live subscription from IPs; a filtered domain will fail.

### Usage accounting (counter-reset safe)
xray exposes cumulative `user>>>u_<token>.<tag>>>>traffic>{up,down}link`. The bot sums
all stats whose name starts with `user>>>u_<token>` → `raw`. If `raw < last_raw`
(xray restarted, counters zeroed), it folds `last_raw` into `base_bytes`. Reported
`used_bytes = base_bytes + raw`.

A manual usage reset never changes that lifetime value. It stores the current
`used_bytes` in `usage_reset_bytes`; subscriber/admin displays and quota enforcement
use `max(used_bytes - usage_reset_bytes, 0)`. Dashboard totals and daily history
continue to use the lifetime counters.

### Live CDN-address swap
`set_ips()` writes the legacy `meta.clean_ips` field; it now accepts hostnames or
IPv4 addresses. `regenerate_all_subs()` rewrites every sub file with the new dial
addresses. The xray side is untouched, so no restart and customers' links stay valid
after they update in their client. DNS resolution does not guarantee that the
returned Cloudflare IP is reachable on every ISP.

## Why these choices
- **stdlib only** → trivial to run on a tiny box, no dependency rot, easy to audit.
- **clients live in xray memory, not config.json** → instant add/remove, no restart;
  the enforcer's PID-change resync makes this durable across restarts/reboots.
- **Cloudflare tunnel** → hides/bypasses the throttled origin IP, works behind NAT.
- **one token, many configs** → one link to share; client fails over between configs.
