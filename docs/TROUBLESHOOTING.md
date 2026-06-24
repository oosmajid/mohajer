# Troubleshooting — symptom → cause → fix

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Bot doesn't respond at all | wrong/empty `BOT_TOKEN`, or you're not in `ADMIN_IDS` | check `journalctl -u dpbot`; bot prints `no BOT_TOKEN` and exits if missing. Confirm your numeric id. |
| Bot says "⛔️ این ربات خصوصی است" | your Telegram id isn't an admin | set `ADMIN_IDS` in `bot.env`, restart bot. |
| New link connects on TLS but not no-TLS | "Always Use HTTPS" is ON in Cloudflare | turn it OFF (Rules → Settings). no-TLS uses HTTP ports only. |
| All configs fail to connect | cloudflared down, or path mismatch | `systemctl status cloudflared`; verify the three-way `tag/port/path` match (DEPLOYMENT checklist). |
| Configs connect but 0 traffic / instant drop | xray inbound missing or client not added | `journalctl -u xray`; the enforcer re-adds users on PID change — `systemctl restart xray` to force a resync. |
| Usage always shows 0 | stats not enabled, or wrong `XRAY_API` | ensure `policy.levels.0.statsUserUplink/Downlink=true` and the `api` inbound on `127.0.0.1:10085`. |
| Usage jumped down / reset | xray restarted (counters zeroed) | expected — `base_bytes` folds in `last_raw`; `used_bytes` stays monotonic. |
| `adu` fails: "Listen on AnyIP but no Port(s) set" | incomplete inbound JSON sent to `adu` | the inbound template must include port + decryption + streamSettings (bot already does this). |
| Browser downloads a file instead of showing the page | request reached the raw path, or UA lacks "Mozilla" | the copy-page is UA-gated; normal browsers get HTML, clients get base64. `?raw` forces base64. |
| Mobile page scrolls sideways | (fixed) overflow on long config names | `subserver.py` already sets `overflow-x:hidden` + flex truncation. |
| SSH: "Connection timed out during banner exchange" | 512MB box OOM, sshd can't fork | see OPERATIONS.md — kill stray `/tmp` xray procs, or reboot. Not a network issue. |
| SSH hangs only from Iran | origin IP throttled | tunnel via local proxy: `-o "ProxyCommand=nc -x 127.0.0.1:10808 -X 5 %h %p"`. |
| Slow / unstable on Irancell | bad clean IPs | rescan with `cf-clean-ip-scan.sh`, set the fastest via the bot panel; consider the no-TLS configs (faster on Irancell). |
| Want REALITY back | — | don't. It can't traverse Cloudflare and tested broken on target ISPs. It was removed on purpose. |

## Quick health snapshot
```bash
systemctl is-active xray dpbot dpsub cloudflared
free -m
sqlite3 /opt/dpbot/dpbot.db "SELECT count(*) FROM users"
journalctl -u dpbot -n 20 --no-pager
```
