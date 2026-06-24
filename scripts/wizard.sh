#!/bin/bash
# =============================================================================
#  Mohajer — interactive install wizard.
#  Bare Debian/Ubuntu VPS  ->  working Cloudflare-fronted panel, one command.
#
#  Run as root from the repo root:   sudo bash scripts/wizard.sh
#
#  It installs xray-core + cloudflared, asks a few questions, then GENERATES all
#  three configs (xray / cloudflared / bot.env) from ONE source of truth so the
#  tag/port/path triple can never drift. Idempotent enough to re-run.
# =============================================================================
set -euo pipefail

# ---------- pretty ----------
B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
say(){ printf "%s\n" "${C}${B}==>${N} $*"; }
ok(){  printf "%s\n" "${G}  ✓${N} $*"; }
warn(){ printf "%s\n" "${Y}  !${N} $*"; }
die(){ printf "%s\n" "${R}  ✗ $*${N}" >&2; exit 1; }
ask(){ # ask "Prompt" "default" -> echoes answer
  local p="$1" d="${2:-}" a
  if [ -n "$d" ]; then read -r -p "$(printf '%s [%s]: ' "$p" "$d")" a || true; echo "${a:-$d}"
  else read -r -p "$(printf '%s: ' "$p")" a || true; echo "$a"; fi
}

[ "$(id -u)" = "0" ] || die "Run as root (sudo bash scripts/wizard.sh)."
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT=/opt/mohajer

cat <<BANNER
${B}${C}
  ┌────────────────────────────────────────────┐
  │            Mohajer — install wizard         │
  └────────────────────────────────────────────┘
${N}
This will install xray-core + cloudflared, build all configs, and start the
panel. You will need: a Cloudflare-managed domain, a Telegram bot token, and
your numeric Telegram admin id.
BANNER
[ "$(ask "Continue?" "y")" = "y" ] || exit 0

# ---------- 0. base deps ----------
say "Installing base packages (python3, sqlite3, curl, openssl, netcat) ..."
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 sqlite3 curl openssl ca-certificates netcat-openbsd >/dev/null
else
  warn "Non-apt system — make sure python3, sqlite3, curl, openssl are present."
fi
ok "base deps ready"

# ---------- 1. questions ----------
say "Configuration"
DOMAIN="$(ask "Public hostname (Cloudflare-proxied), e.g. cdn.example.ir")"
[ -n "$DOMAIN" ] || die "hostname is required"
BOT_TOKEN="$(ask "Telegram bot token (@BotFather)")"
[ -n "$BOT_TOKEN" ] || die "bot token is required"
ADMIN_IDS="$(ask "Your numeric admin id(s), comma-separated (@userinfobot)")"
[ -n "$ADMIN_IDS" ] || die "admin id is required"
CLEAN_IPS="$(ask "Default clean Cloudflare IPs (comma-separated)" "104.16.96.1,104.21.96.1,104.19.96.1")"
TUNNEL_NAME="$(ask "Cloudflare tunnel name" "mohajer")"

# ---------- 2. source of truth: endpoints ----------
# tag|proto|net|port|tls_ports|notls_ports|label   (paths are auto-randomised)
ENDPOINT_DEFS=(
  "vless-ws|vless|ws|10000|443,2053|80,8080|VLESS-WS"
  "vmess-ws|vmess|ws|10003|8443|80|VMESS-WS"
  "trojan-ws|trojan|ws|10002|2087|8880|TROJAN-WS"
  "vless-xh|vless|xhttp|10001|443||VLESS-XHTTP"
)
say "Generating random WS/XHTTP paths ..."
declare -A PATHS
for def in "${ENDPOINT_DEFS[@]}"; do
  tag="${def%%|*}"; PATHS[$tag]="/$(openssl rand -hex 6)"
done
for def in "${ENDPOINT_DEFS[@]}"; do tag="${def%%|*}"; ok "$tag -> ${PATHS[$tag]}"; done

# export for the python generator
export GEN_DOMAIN="$DOMAIN"
export GEN_ENDPOINTS_RAW="$(printf '%s\n' "${ENDPOINT_DEFS[@]}")"
export GEN_PATHS="$(for k in "${!PATHS[@]}"; do printf '%s=%s\n' "$k" "${PATHS[$k]}"; done)"

# ---------- 3. install xray ----------
if ! command -v xray >/dev/null && [ ! -x /usr/local/bin/xray ]; then
  say "Installing xray-core ..."
  bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/dev/null
fi
ok "xray present: $(/usr/local/bin/xray version 2>/dev/null | head -1)"

# ---------- 4. install cloudflared ----------
if ! command -v cloudflared >/dev/null; then
  say "Installing cloudflared ..."
  arch=$(uname -m); case "$arch" in x86_64) cfa=amd64;; aarch64|arm64) cfa=arm64;; *) cfa=amd64;; esac
  curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${cfa}" -o /usr/local/bin/cloudflared
  chmod +x /usr/local/bin/cloudflared
fi
ok "cloudflared present: $(cloudflared --version 2>/dev/null | head -1)"

# ---------- 5. cloudflared login + tunnel ----------
if [ ! -f /root/.cloudflared/cert.pem ]; then
  say "Cloudflare login — open the URL it prints in a browser and pick your domain:"
  cloudflared tunnel login
fi
[ -f /root/.cloudflared/cert.pem ] || die "cloudflared login did not complete (no cert.pem)."

if ! cloudflared tunnel list 2>/dev/null | grep -qw "$TUNNEL_NAME"; then
  say "Creating tunnel '$TUNNEL_NAME' ..."
  cloudflared tunnel create "$TUNNEL_NAME" >/dev/null
fi
CRED_FILE="$(ls -t /root/.cloudflared/*.json 2>/dev/null | head -1)"
[ -n "$CRED_FILE" ] || die "tunnel credentials file not found."
TUNNEL_UUID="$(basename "$CRED_FILE" .json)"
ok "tunnel UUID: $TUNNEL_UUID"

say "Routing DNS $DOMAIN -> tunnel ..."
cloudflared tunnel route dns "$TUNNEL_NAME" "$DOMAIN" 2>/dev/null || warn "DNS route may already exist — continuing."

# ---------- 6. generate the three configs ----------
say "Writing configs (single source of truth) ..."
mkdir -p "$ROOT/bot" "$ROOT/sub" /usr/local/etc/xray /root/.cloudflared

GEN_TUNNEL_UUID="$TUNNEL_UUID" GEN_CRED_FILE="$CRED_FILE" python3 - <<'PY'
import os, json
domain = os.environ["GEN_DOMAIN"]
paths = dict(l.split("=",1) for l in os.environ["GEN_PATHS"].splitlines() if l.strip())
defs = []
for line in os.environ["GEN_ENDPOINTS_RAW"].splitlines():
    if not line.strip(): continue
    tag,proto,net,port,tls,notls,label = line.split("|")
    defs.append(dict(tag=tag, proto=proto, net=net, port=int(port),
                     tls=[int(x) for x in tls.split(",") if x],
                     notls=[int(x) for x in notls.split(",") if x],
                     label=label, path=paths[tag]))

# --- xray config.json ---
inbounds = [{
    "tag":"api","listen":"127.0.0.1","port":10085,
    "protocol":"dokodemo-door","settings":{"address":"127.0.0.1"}}]
for e in defs:
    stream = {"network": e["net"], "security":"none"}
    if e["net"]=="ws":    stream["wsSettings"]={"path":e["path"]}
    elif e["net"]=="xhttp": stream["xhttpSettings"]={"path":e["path"],"mode":"auto"}
    settings = {"clients":[]}
    if e["proto"] in ("vless",): settings["decryption"]="none"
    inbounds.append({"tag":e["tag"],"listen":"127.0.0.1","port":e["port"],
                     "protocol":e["proto"],"settings":settings,"streamSettings":stream})
xray = {
    "log":{"loglevel":"warning"},
    "api":{"tag":"api","services":["HandlerService","StatsService"]},
    "stats":{},
    "policy":{"levels":{"0":{"statsUserUplink":True,"statsUserDownlink":True}},
              "system":{"statsInboundUplink":True,"statsInboundDownlink":True}},
    "inbounds":inbounds,
    "outbounds":[{"tag":"direct","protocol":"freedom","settings":{}},
                 {"tag":"blocked","protocol":"blackhole","settings":{}}],
    "routing":{"rules":[{"type":"field","inboundTag":["api"],"outboundTag":"api"}]},
}
open("/usr/local/etc/xray/config.json","w").write(json.dumps(xray, indent=2))

# --- cloudflared config.yml ---
lines = ["tunnel: %s" % os.environ["GEN_TUNNEL_UUID"],
         "credentials-file: %s" % os.environ["GEN_CRED_FILE"],
         "ingress:"]
for e in defs:
    lines += ["  - hostname: %s" % domain,
              "    path: ^%s" % e["path"],
              "    service: http://127.0.0.1:%d" % e["port"]]
lines += ["  - hostname: %s" % domain,
          "    path: ^/sub-",
          "    service: http://127.0.0.1:8090",
          "  - service: http_status:404", ""]
open("/root/.cloudflared/config.yml","w").write("\n".join(lines))

# --- ENDPOINTS json for bot.env ---
eps = [{"proto":e["proto"],"net":e["net"],"tag":e["tag"],"port":e["port"],
        "path":e["path"],"label":e["label"],
        "tls_ports":e["tls"],"notls_ports":e["notls"]} for e in defs]
open("/tmp/mohajer_endpoints.json","w").write(json.dumps(eps, separators=(",",":")))
print("configs written")
PY
ok "xray config.json, cloudflared config.yml, ENDPOINTS generated"

# ---------- 7. bot.env ----------
ENDPOINTS_JSON="$(cat /tmp/mohajer_endpoints.json)"; rm -f /tmp/mohajer_endpoints.json
if [ -f "$ROOT/bot.env" ]; then
  warn "$ROOT/bot.env exists — backing up to bot.env.bak"
  cp "$ROOT/bot.env" "$ROOT/bot.env.bak"
fi
cat > "$ROOT/bot.env" <<ENV
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
XRAY_BIN=/usr/local/bin/xray
XRAY_API=127.0.0.1:10085
DB=$ROOT/dpbot.db
SUB_DIR=$ROOT/sub
POLL_SECONDS=30
DOMAIN=$DOMAIN
SUB_BASE_URL=https://$DOMAIN
IPS=$CLEAN_IPS
ENDPOINTS=$ENDPOINTS_JSON
ENV
chmod 600 "$ROOT/bot.env"
ok "bot.env written (chmod 600)"

# ---------- 8. code + services ----------
say "Installing code and systemd units ..."
install -m 644 "$REPO_DIR/bot/bot.py"       "$ROOT/bot/bot.py"
install -m 644 "$REPO_DIR/sub/subserver.py" "$ROOT/sub/subserver.py"
# subserver reads SUB_DIR/DB from the env file (loaded by the unit) — no patching needed.

install -m 644 "$REPO_DIR/systemd/mohajer-bot.service" /etc/systemd/system/mohajer-bot.service
install -m 644 "$REPO_DIR/systemd/mohajer-sub.service" /etc/systemd/system/mohajer-sub.service

systemctl daemon-reload
systemctl enable --now xray            >/dev/null 2>&1 || true
systemctl restart xray
systemctl enable --now cloudflared     >/dev/null 2>&1 || cloudflared service install >/dev/null 2>&1 || true
systemctl restart cloudflared 2>/dev/null || true
systemctl enable --now mohajer-sub.service >/dev/null
systemctl enable --now mohajer-bot.service >/dev/null
sleep 2
ok "services started"

# ---------- 9. summary ----------
say "Health check"
for u in xray cloudflared mohajer-bot mohajer-sub; do
  st=$(systemctl is-active "$u" 2>/dev/null || echo dead)
  [ "$st" = active ] && ok "$u: active" || warn "$u: $st  (check: journalctl -u $u -n 30)"
done
cat <<DONE

${G}${B}Done.${N}
  • Telegram: open your bot and send ${B}/start${N} (only $ADMIN_IDS will get the menu).
  • Public host: ${B}https://$DOMAIN${N}
  • Logs:  journalctl -u mohajer-bot -f
  • Clean IPs are editable live from the bot's 🌐 menu.

${Y}Reminder:${N} in the Cloudflare dashboard set SSL/TLS = Full, and turn
"Always Use HTTPS" OFF if you want the no-TLS configs to work.
DONE
