#!/bin/bash
# Mohajer — fresh-install helper for a Debian/Ubuntu VPS.
# Idempotent-ish; run as root. Assumes xray-core and cloudflared are already set up
# (see docs/DEPLOYMENT.md). This only lays down the bot + sub-server + systemd units.
set -e

ROOT=/opt/mohajer
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "[*] creating $ROOT ..."
mkdir -p "$ROOT/bot" "$ROOT/sub" "$ROOT/sub"
install -m 644 "$REPO_DIR/bot/bot.py"        "$ROOT/bot/bot.py"
install -m 644 "$REPO_DIR/sub/subserver.py"  "$ROOT/sub/subserver.py"

if [ ! -f "$ROOT/bot.env" ]; then
  install -m 600 "$REPO_DIR/config/bot.env.example" "$ROOT/bot.env"
  echo "[!] wrote $ROOT/bot.env from example — EDIT IT (token, admin id, domain, endpoints)."
fi

# SUB_DIR from bot.env (default /opt/mohajer/sub)
mkdir -p "$ROOT/sub"

echo "[*] installing systemd units ..."
install -m 644 "$REPO_DIR/systemd/mohajer-bot.service" /etc/systemd/system/mohajer-bot.service
install -m 644 "$REPO_DIR/systemd/mohajer-sub.service" /etc/systemd/system/mohajer-sub.service
systemctl daemon-reload
systemctl enable --now mohajer-sub.service
systemctl enable --now mohajer-bot.service

echo "[*] done. Check:  journalctl -u mohajer-bot -f"
echo "    xray config -> /usr/local/etc/xray/config.json (see config/xray.config.json)"
echo "    cloudflared -> /root/.cloudflared/config.yml   (see config/cloudflared.config.yml)"
