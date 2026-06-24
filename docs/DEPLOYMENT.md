# Deployment — bare VPS → working panel

Target: a small Debian/Ubuntu VPS. Everything listens on `127.0.0.1`; the only thing
facing the internet is the `cloudflared` outbound tunnel. ~512MB RAM is enough but
leaves little headroom (see OPERATIONS.md).

## 0. Prerequisites
- A domain on **Cloudflare** (orange-cloud/proxied). Example: `cdn.example.ir`.
- A Telegram bot token (@BotFather) and your numeric admin id (@userinfobot).
- Root SSH on the VPS.

## 1. Install xray-core
```bash
bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
```
Put `config/xray.config.json` at `/usr/local/etc/xray/config.json`. It defines the
inbounds with **empty clients** (the bot fills them live) and the gRPC `api` inbound
on `127.0.0.1:10085` with stats enabled. Then:
```bash
systemctl enable --now xray
systemctl restart xray && journalctl -u xray -n 30 --no-pager
```

> Optional throughput tweak used in production: enable **BBR**
> (`net.core.default_qdisc=fq`, `net.ipv4.tcp_congestion_control=bbr` in
> `/etc/sysctl.conf`, then `sysctl -p`).

## 2. Set up the Cloudflare tunnel
```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
cloudflared tunnel login                 # browser auth, picks the zone
cloudflared tunnel create mohajer        # creates <UUID>.json credentials
```
Put `config/cloudflared.config.yml` at `/root/.cloudflared/config.yml`; replace
`<TUNNEL-UUID>` and `cdn.example.ir`. Route DNS + run as a service:
```bash
cloudflared tunnel route dns mohajer cdn.example.ir
cloudflared service install
systemctl enable --now cloudflared
```
The last ingress rule MUST be a bare `service: http_status:404` with **no hostname**,
or cloudflared refuses to start ("last rule must match all URLs").

### Cloudflare zone settings
- SSL/TLS mode: **Full** (tunnel terminates locally over plain HTTP — that's fine).
- For **no-TLS** configs to work: **Rules → Settings → "Always Use HTTPS" = OFF**.
- WebSocket: enabled (default on). gRPC not needed.

## 3. Install Mohajer
```bash
git clone <your-repo> /opt/mohajer-src && cd /opt/mohajer-src
cp config/bot.env.example /opt/mohajer/bot.env   # (install.sh also does this)
chmod 600 /opt/mohajer/bot.env
$EDITOR /opt/mohajer/bot.env    # token, ADMIN_IDS, DOMAIN, SUB_BASE_URL, ENDPOINTS, IPS
sudo bash scripts/install.sh
```
`install.sh` lays down `bot.py`, `subserver.py`, the two systemd units, and starts them.

## 4. Verify
```bash
journalctl -u mohajer-bot -f          # should print "dpbot started; endpoints=N"
```
- In Telegram, `/start` the bot → menu appears (only for your admin id).
- Create a test link → open `https://<DOMAIN>/sub-u-<token>` in a browser → copy-page.
- Add the link to v2rayNG → connect.

## 5. Map the production names (if adopting the existing server)
The live server uses legacy paths/units (`/opt/dpbot`, `/opt/dpsub`, `dpbot`,
`dpsub`, host `cdn.delplayer.ir`). See the table in `AGENTS.md §3`. Either keep them
or migrate to the `mohajer-*` names — the code is path-agnostic via `bot.env`.

## Consistency checklist (the three-way match)
For every endpoint, these MUST agree:
| field | xray.config.json | bot.env ENDPOINTS | cloudflared ingress |
|-------|------------------|-------------------|---------------------|
| tag   | inbound `tag`    | `tag`             | (n/a)               |
| port  | inbound `port`   | `port`            | `service: http://127.0.0.1:<port>` |
| path  | `…Settings.path` | `path`            | `path: ^<path>`     |
