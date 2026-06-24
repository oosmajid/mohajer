# Operations — day-2 runbook

Production server: `ssh -p 49531 root@23.94.29.30` (512MB RAM). Live unit names are
`dpbot` / `dpsub`; paths `/opt/dpbot`, `/opt/dpsub`. See `AGENTS.md §3`.

## Everyday tasks

| Task | How |
|------|-----|
| Tail bot logs | `journalctl -u dpbot -f` |
| Restart bot | `systemctl restart dpbot` (safe; users unaffected) |
| Restart xray | `systemctl restart xray` (users auto-resync on next poll) |
| List links | bot → 📋 لیست لینک‌ها, or `sqlite3 /opt/dpbot/dpbot.db "SELECT label,used_bytes,limit_bytes,expiry_ts FROM users"` |
| Change clean IPs | bot → 🌐 آی‌پی‌های تمیز → ✏️ ویرایش (live, no restart) |
| Find fast IPs | run `scripts/cf-clean-ip-scan.sh cdn.delplayer.ir` from a client network |
| Memory check | `free -m` (watch for low "available") |
| Bot RSS | `ps -o rss= -C python3` (≈25MB, flat — audited, no leak) |

## Clean-IP workflow (the recurring one)
1. From the operator's laptop on the target ISP (NO proxy):
   `bash scripts/cf-clean-ip-scan.sh cdn.delplayer.ir`
2. Take the top 3 IPs (ranked by TLS-handshake time).
3. Bot → 🌐 آی‌پی‌های تمیز → ✏️ → paste `ip1, ip2, ip3`.
4. All links rewrite instantly; customers press **Update** in their client.

## ⚠️ Memory pressure / SSH "banner exchange timeout"
Symptom: `ssh` hangs then `Connection timed out during banner exchange`, but the TCP
port is open (`nc -z` succeeds). Cause: the 512MB box is out of RAM, so `sshd` can't
fork. This is **server-side**, not network.

Mitigations / recovery:
- If the direct route is throttled from Iran, tunnel through the operator's local proxy:
  `ssh -p 49531 -o "ProxyCommand=nc -x 127.0.0.1:10808 -X 5 %h %p" root@23.94.29.30`
- The usual culprit is **leaked `xray run -config /tmp/...` test processes** from
  debugging. If you can get a shell: `pkill -f 'xray run -config /tmp'` and recheck
  `free -m`. NEVER leave such processes running.
- If totally unreachable, reboot from the provider panel. Recovery is clean: on
  reboot the enforcer detects the new xray PID and **re-syncs every user** + rewrites
  subs, so all customer links keep working with the SAME URLs.

## Reboot expectations
- Customers do **not** need new links. `resync_all()` re-adds them to the fresh xray.
- Services come back via systemd (`enable`d). Verify: `systemctl status xray dpbot dpsub cloudflared`.

## Backups
The only stateful file is `dpbot.db`. Back it up:
```bash
sqlite3 /opt/dpbot/dpbot.db ".backup /root/dpbot.db.bak"
```
Sub files are regenerated from the db (`regenerate_all_subs()` / per-create), so the
db alone is enough to rebuild everything.

## SSH-over-proxy note (operator's laptop)
The operator's machine runs a local proxy: `127.0.0.1:10808` (SOCKS5),
`127.0.0.1:10809` (HTTP). `api.github.com` is blocked on that network — route `gh`
through the HTTP proxy. zsh does NOT word-split unquoted vars, so pass ssh options
inline or via a bash array, not via an unquoted `$SSH` string.
