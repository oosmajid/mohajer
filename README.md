# Mohajer

A tiny, dependency-free **Telegram admin panel** for selling/managing V2Ray
subscriptions on a single VPS, fronted by **Cloudflare** so it keeps working on
networks (e.g. Iranian ISPs) that throttle the server's own IP.

One admin talks to a Telegram bot → the bot mints a single **subscription link**
that bundles many configs (VLESS / VMess / Trojan over WebSocket, both **TLS** and
**no-TLS**, plus VLESS-XHTTP), enforces **per-user data quota + expiry live**, and
serves a clean **mobile copy-page** with data/time progress bars in the browser.

No database server, no web framework, no pip packages — just Python 3 stdlib,
`xray-core`, and a `cloudflared` tunnel.

---

## Why it exists / the core trick

The origin VPS IP is throttled from some ISPs. Instead of exposing the server
directly, a **`cloudflared` tunnel** dials out to Cloudflare (outbound-only, works
behind NAT). Clients connect to **clean Cloudflare edge IPs**; Cloudflare routes by
Host/SNI + path through the tunnel to local `xray` inbounds. Because Cloudflare's
anycast serves every hostname from any edge IP, you can point links at whichever
edge IPs are fastest from your users' networks — and swap them live from the bot.

```
  client (v2rayNG)                Cloudflare edge            VPS (NAT)
  ───────────────                 ──────────────            ─────────
  vless://…@<cleanIP>:443  ──TLS──►  edge:443  ──tunnel──►  cloudflared
        Host/SNI: cdn.example.ir       (routes by             │ path-routes to
        path: /1afb5cae5563             Host+path)            ▼
                                                       xray 127.0.0.1:10000  (vless-ws)
```

---

## Repo layout

```
mohajer/
├── bot/bot.py                  # the Telegram panel (single file, stdlib only)
├── sub/subserver.py            # subscription HTTP server (clients + browser copy-page)
├── config/
│   ├── bot.env.example         # all runtime config (copy → bot.env, fill, chmod 600)
│   ├── xray.config.json        # xray-core inbounds (bot manages clients live)
│   └── cloudflared.config.yml  # tunnel ingress rules (path → local inbound)
├── systemd/
│   ├── mohajer-bot.service
│   └── mohajer-sub.service
├── scripts/
│   ├── install.sh              # fresh-install helper (lays down bot+sub+units)
│   └── cf-clean-ip-scan.sh     # find fastest CF edge IPs from a client network
├── docs/
│   ├── ARCHITECTURE.md         # how every piece fits + data model + flows
│   ├── DEPLOYMENT.md           # from bare VPS to working panel, step by step
│   ├── OPERATIONS.md           # day-2 runbook (clean IPs, OOM/SSH recovery, …)
│   └── TROUBLESHOOTING.md      # symptom → cause → fix table
├── AGENTS.md                   # ⭐ start here if you are an AI agent
└── README.md                   # this file
```

## Quick start

1. Read **[AGENTS.md](AGENTS.md)** (orientation) and **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.
2. On the VPS: install `xray-core` + `cloudflared`, create the tunnel + DNS.
3. `cp config/bot.env.example /opt/mohajer/bot.env` and fill it in (`chmod 600`).
4. Drop `config/xray.config.json` → `/usr/local/etc/xray/config.json`,
   `config/cloudflared.config.yml` → `/root/.cloudflared/config.yml`.
5. `sudo bash scripts/install.sh` → `/start` your bot.

## Security notes

- The bot answers **only** `ADMIN_IDS`. Everyone else gets a polite refusal.
- `bot.env` holds the Telegram token — `chmod 600`, never commit (`.gitignore` covers it).
- no-TLS configs are unencrypted at the transport layer (the proxy protocol still
  obfuscates, but treat them as lower-security, higher-speed options).
