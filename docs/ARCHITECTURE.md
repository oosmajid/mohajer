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
3. `write_sub` → base64 file of one config per endpoint × TLS/no-TLS port, IPs
   round-robined from `get_ips()`.
4. Insert the `users` row.

### Mint configs (`write_sub` → `_ws_link`)
For each endpoint: for each `tls_ports` emit a TLS config, for each `notls_ports` emit a
no-TLS config. `_ws_link` builds protocol-correct URIs:
- vless: `vless://<uuid>@<ip>:<port>?encryption=none&security=<tls|none>&type=<ws|xhttp>&host=<DOMAIN>[&sni=<DOMAIN>]&path=<path>[&mode=auto]#<label>`
- trojan: `trojan://<password>@<ip>:<port>?security=…&type=ws&host=<DOMAIN>&path=<path>#<label>`
- vmess: base64 of the standard vmess JSON (`add=<ip>`, `host/sni=<DOMAIN>`, `tls` on/off).

The host the client dials is the **clean IP**; the real hostname is carried in
`host=`/`sni=` so Cloudflare routes correctly.

### Usage accounting (counter-reset safe)
xray exposes cumulative `user>>>u_<token>.<tag>>>>traffic>{up,down}link`. The bot sums
all stats whose name starts with `user>>>u_<token>` → `raw`. If `raw < last_raw`
(xray restarted, counters zeroed), it folds `last_raw` into `base_bytes`. Reported
`used_bytes = base_bytes + raw`.

### Live clean-IP swap
`set_ips()` writes `meta.clean_ips`; `regenerate_all_subs()` rewrites every sub file
with the new IPs. The xray side is untouched (configs only differ by host IP), so no
restart and customers' links stay valid — they just `Update` in their client.

## Why these choices
- **stdlib only** → trivial to run on a tiny box, no dependency rot, easy to audit.
- **clients live in xray memory, not config.json** → instant add/remove, no restart;
  the enforcer's PID-change resync makes this durable across restarts/reboots.
- **Cloudflare tunnel** → hides/bypasses the throttled origin IP, works behind NAT.
- **one token, many configs** → one link to share; client fails over between configs.
