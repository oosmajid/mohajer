#!/usr/bin/env python3
"""Click through the Telegram shop on a laptop - no VPS, no xray, no cloudflared.

The real bot code runs against a real (TEST) Telegram bot, but the xray user/resync
calls are replaced by no-ops, so links, credentials and subscription files are created
in a local sqlite DB and never connect (usage reads still shell out to `xray` and simply
fail when it is not installed). Use a separate bot from @BotFather - NEVER the production
token or database; the runner refuses paths outside <repo>/dev and the production token.

    cp config/dev.env.example dev/dev.env      # then put the test bot token + your id in it
    python scripts/dev_shop.py                 # run the bot (Ctrl+C to stop)

Simulate traffic/time to see warnings, auto-renew and the grace period:

    python scripts/dev_shop.py links                     # list links
    python scripts/dev_shop.py use   <token> <GB>        # set the link's used data
    python scripts/dev_shop.py left  <token> <hours>     # set the time left (0 = ends now)

If api.telegram.org is filtered, set HTTPS_PROXY (urllib honours it), e.g.
HTTPS_PROXY=http://127.0.0.1:10809.
"""
import os, sys, time, threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(ROOT)                                   # dev.env paths are relative to the repo
os.environ.setdefault("DPBOT_ENV", os.path.join("dev", "dev.env"))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import bot  # noqa: E402  (reads DPBOT_ENV at import)

if bot.ENV.get("DEV_SHOP") != "1":
    sys.exit("refusing to run: %s must contain DEV_SHOP=1 (this runner fakes xray)" % os.environ["DPBOT_ENV"])
# The runner fakes xray and steals getUpdates, so it must never see a production DB, sub dir or token:
# every path has to live under <repo>/dev and the token must not be the one in a real bot.env.
DEV_DIR = os.path.join(ROOT, "dev")
for what, path in (("DB", bot.DB_PATH), ("SUB_DIR", bot.SUB_DIR)):
    if not os.path.realpath(path).startswith(DEV_DIR + os.sep):
        sys.exit("refusing to run: %s=%s is outside %s (would touch a real install)" % (what, path, DEV_DIR))
for prod_env in ("/opt/dpbot/bot.env", "/opt/mohajer/bot.env"):
    try:
        with open(prod_env) as f:
            if bot.TOKEN and ("BOT_TOKEN=%s" % bot.TOKEN) in f.read():
                sys.exit("refusing to run: BOT_TOKEN is the production token from %s" % prod_env)
    except OSError:
        pass

# ---- no xray here: pretend every proxy operation worked ----
bot._adu = lambda ep, secret, email: True
bot._rmu_email = lambda tag, email: True
bot.xr_add_users = lambda users: True
bot.force_disconnect = lambda tags: None
bot.xray_pid = lambda: "dev"
bot.refresh_usage = lambda token: None
bot.refresh_all_usage = lambda: None
bot.resync_all = lambda: None


def links():
    c = bot.db()
    for u in c.execute("SELECT u.*, s.tg_id FROM users u LEFT JOIN shop_services s ON s.token=u.token ORDER BY created_ts"):
        print("%s  owner=%-12s used=%6.2fGB limit=%6.2fGB left=%7.1fh %s %s" % (
            u["token"], u["tg_id"], bot.current_usage(u) / bot.GB, (u["limit_bytes"] or 0) / bot.GB,
            ((u["expiry_ts"] - time.time()) / 3600) if u["expiry_ts"] else float("inf"),
            "DISABLED" if u["disabled_ts"] else "", u["label"]))
    c.close()


def set_link(token, sql, value):
    c = bot.db()
    n = c.execute("UPDATE users SET %s WHERE token=?" % sql, (value, token)).rowcount
    c.commit(); c.close()
    print("updated" if n else "no such token")


def run():
    if not bot.TOKEN or "PUT-" in bot.TOKEN:
        sys.exit("put the TEST bot token in %s" % os.environ["DPBOT_ENV"])
    bot.meta_set("xray_pid", "dev")              # the enforcer must not try to resync
    me = bot.tg("getMe").get("result") or {}
    if not me:
        sys.exit("Telegram is not reachable or the token is wrong (set HTTPS_PROXY if it is filtered).")
    bot.tg("deleteWebhook")
    threading.Thread(target=bot.enforcer, daemon=True).start()   # expiry / quota / grace, without xray
    bot.shop.start_threads()
    print("dev shop running as @%s - open https://t.me/%s  (Ctrl+C to stop)" % (me.get("username"), me.get("username")), flush=True)
    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
            if offset: params["offset"] = offset
            for up in bot.tg("getUpdates", **params).get("result", []):
                offset = up["update_id"] + 1
                try: bot.handle_update(up)
                except Exception as e: print("handle err", repr(e), flush=True)
        except KeyboardInterrupt:
            return
        except Exception as e:
            print("poll err", e, flush=True); time.sleep(3)


if __name__ == "__main__":
    args = sys.argv[1:]
    os.makedirs(os.path.dirname(os.path.abspath(bot.DB_PATH)), exist_ok=True)
    os.makedirs(bot.SUB_DIR, exist_ok=True)
    bot.init_db()
    if not args: run()
    elif args[0] == "links": links()
    elif args[0] == "use" and len(args) == 3:
        set_link(args[1], "used_bytes=usage_reset_bytes+?", int(float(args[2]) * bot.GB))
    elif args[0] == "left" and len(args) == 3:
        set_link(args[1], "expiry_ts=?", int(time.time() + float(args[2]) * 3600))
    else:
        sys.exit(__doc__)
