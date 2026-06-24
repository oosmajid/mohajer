#!/bin/bash
# Mohajer — clean Cloudflare IP scanner.
# Run from a client network (e.g. your laptop in Iran, NO proxy) to find the
# Cloudflare edge IPs that are fastest/cleanest for YOUR ISP right now.
# Ranks by full TLS handshake time (connect + TLS = 2 round-trips) against your
# own CF-fronted host, which is what actually governs proxy responsiveness.
#
# Usage:  ./cf-clean-ip-scan.sh cdn.example.ir
# Output: top IPs sorted best-first, with average handshake ms + ICMP ping.
#
# Then paste the winners into the bot's "🌐 آی‌پی‌های تمیز" panel.

HOST="${1:-cdn.example.ir}"
SAMPLES=3

# A spread of candidate IPs across Cloudflare /13 blocks. Add/remove freely.
CANDIDATES="104.16.96.1 104.17.96.1 104.18.96.1 104.19.96.1 104.20.96.1 \
104.21.96.1 104.22.96.1 104.24.96.1 104.25.96.1 104.26.96.1 104.27.96.1 \
172.64.96.1 172.65.96.1 172.66.96.1 172.67.96.1 188.114.96.1 188.114.97.1 \
162.159.0.1 162.159.192.1 141.101.64.1"

echo "Scanning ${HOST} across $(echo $CANDIDATES | wc -w | tr -d ' ') IPs ..."
for ip in $CANDIDATES; do
  tot=0; n=0; ok=1
  for i in $(seq 1 $SAMPLES); do
    t=$(curl -s -o /dev/null -w '%{time_appconnect}' --resolve "${HOST}:443:${ip}" \
        "https://${HOST}/" --max-time 8 2>/dev/null)
    if [ -z "$t" ] || [ "$t" = "0.000000" ]; then ok=0; break; fi
    tot=$(awk -v a="$tot" -v b="$t" 'BEGIN{print a+b}'); n=$((n+1))
  done
  [ "$ok" != "1" ] && continue
  avg=$(awk -v t="$tot" -v n="$n" 'BEGIN{printf "%.0f", t/n*1000}')
  png=$(ping -c 3 -t 3 "$ip" 2>/dev/null | tail -1 | awk -F'/' '{printf "%.0f",$5}')
  [ -z "$png" ] && png="?"
  printf "%s %s ping=%sms\n" "$avg" "$ip" "$png"
done | sort -n | awk 'BEGIN{print "handshake_ms  ip               ping"} {printf "%-13s %-16s %s\n",$1,$2,$3}'
