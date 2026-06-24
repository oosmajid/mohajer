<div align="center">

# 🛡️ Mohajer · مهاجر

**A tiny, dependency-free Telegram panel for running V2Ray subscriptions behind Cloudflare.**

**یک پنل تلگرامیِ سبک و بدون وابستگی برای مدیریت اشتراک‌های V2Ray از پشت کلادفلر.**

![python](https://img.shields.io/badge/python-3.x_stdlib_only-blue)
![deps](https://img.shields.io/badge/dependencies-none-success)
![proxy](https://img.shields.io/badge/xray--core-VLESS%2FVMess%2FTrojan-orange)
![front](https://img.shields.io/badge/fronted_by-Cloudflare-f38020)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

[English](#-english) · [فارسی](#-فارسی)

</div>

---

<a name="-english"></a>
## 🇬🇧 English

### What is it?

Mohajer is a single-VPS admin panel you drive entirely from **one Telegram bot**. The
admin creates a subscription with a chosen **data quota** and **expiry**, and the bot
mints a single **subscription link** that bundles many configs — VLESS / VMess / Trojan
over WebSocket (both **TLS** and **no-TLS**) plus **VLESS-XHTTP** — all fronted by
**Cloudflare** so they keep working on networks that throttle the server's raw IP.

No database server, no web framework, no pip packages — just **Python 3 stdlib**,
**xray-core**, and a **cloudflared** tunnel.

### ✨ Features

- 🤖 **Telegram-native admin panel** — create / list / extend / delete links from inline buttons. Answers **only** your admin id(s).
- 🧩 **Multi-protocol links** — one link bundles VLESS / VMess / Trojan over WS (TLS + no-TLS) + VLESS-XHTTP. Clients fail over automatically.
- 📊 **Live quota & expiry** — per-user data + time limits enforced live via the xray gRPC API; links auto-disable & delete when exhausted.
- ☁️ **Cloudflare-fronted** — outbound `cloudflared` tunnel (works behind NAT) hides the throttled origin IP.
- 🌐 **Live clean-IP swap** — edit the Cloudflare edge IPs for *every* link from the bot; customers just press *Update*. Includes a scanner to find the fastest IPs from your ISP.
- 📱 **Mobile copy-page** — open the sub link in a browser → a clean RTL page with per-config copy, copy-all, and data/time progress bars.
- 🪶 **Featherweight** — ~25MB RAM, stdlib only, runs comfortably on a 512MB box.
- ♻️ **Reboot-safe** — clients live in xray memory; the enforcer auto-resyncs everyone on restart, so links never change.

### 🏗️ How it works

The origin VPS IP is throttled from some ISPs. Instead of exposing the server directly,
a **`cloudflared` tunnel** dials out to Cloudflare (outbound-only, works behind NAT).
Clients connect to **clean Cloudflare edge IPs**; Cloudflare routes by Host/SNI + path
through the tunnel to local `xray` inbounds. Because Cloudflare anycast serves every
hostname from any edge IP, you can point links at whichever edge IPs are fastest from
your users' networks — and swap them live from the bot.

```
  client (v2rayNG)                Cloudflare edge            VPS (NAT)
  ───────────────                 ──────────────            ─────────
  vless://…@<cleanIP>:443  ──TLS──►  edge:443  ──tunnel──►  cloudflared
        Host/SNI: cdn.example.ir       (routes by             │ path-routes to
        path: /1afb5cae5563             Host+path)            ▼
                                                       xray 127.0.0.1:10000  (vless-ws)
```

### 🚀 Quick start — one wizard

On a fresh Debian/Ubuntu VPS, clone and run the wizard. It installs `xray-core` +
`cloudflared`, asks a handful of questions (domain, bot token, admin id), generates all
three configs from a single source of truth (so tag/port/path can never drift), and
starts everything:

```bash
git clone https://github.com/oosmajid/mohajer.git && cd mohajer
sudo bash scripts/wizard.sh
```

The only manual moment is the Cloudflare browser login the wizard launches. When it
finishes, open your bot and send `/start`.

> **Cloudflare dashboard:** set **SSL/TLS = Full**, and turn **"Always Use HTTPS" OFF**
> if you want the faster no-TLS configs to work.

Prefer to understand each step, or adopt an existing server? See
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

### 📁 Repo layout

```
mohajer/
├── bot/bot.py                  # the Telegram panel (single file, stdlib only)
├── sub/subserver.py            # subscription HTTP server (clients + browser copy-page)
├── config/
│   ├── bot.env.example         # all runtime config (copy → bot.env, fill, chmod 600)
│   ├── xray.config.json        # xray-core inbounds (bot manages clients live)
│   └── cloudflared.config.yml  # tunnel ingress rules (path → local inbound)
├── systemd/                    # mohajer-bot.service, mohajer-sub.service
├── scripts/
│   ├── wizard.sh               # ⭐ interactive end-to-end installer
│   ├── install.sh              # lower-level helper (bot + sub + units only)
│   └── cf-clean-ip-scan.sh     # find the fastest CF edge IPs from a client network
├── docs/                       # ARCHITECTURE · DEPLOYMENT · OPERATIONS · TROUBLESHOOTING
├── AGENTS.md / CLAUDE.md       # ⭐ start here if you are an AI agent
└── README.md
```

### 🔐 Security notes

- The bot answers **only** `ADMIN_IDS`; everyone else gets a polite refusal.
- `bot.env` holds the Telegram token — `chmod 600`, never commit (`.gitignore` covers it, plus `*.db` and `sub-*`).
- no-TLS configs are unencrypted at the transport layer (the proxy protocol still obfuscates) — treat them as lower-security, higher-speed options.

### 📚 Docs

| Doc | What's in it |
|-----|--------------|
| [AGENTS.md](AGENTS.md) | Orientation for AI agents: mental model, prod path map, golden rules |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data model, every flow with diagrams |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Bare VPS → working panel, wizard + manual |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Day-2 runbook: clean IPs, OOM/SSH recovery, backups |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Symptom → cause → fix table |

---

<a name="-فارسی"></a>
## 🇮🇷 فارسی

<div dir="rtl">

### مهاجر چیست؟

مهاجر یک پنل مدیریتیِ تک‌سروره که **کاملاً از طریق یک ربات تلگرام** اداره می‌شود. ادمین
یک اشتراک با **حجم** و **زمان** دلخواه می‌سازد و ربات یک **لینک ساب واحد** تولید می‌کند
که چندین کانفیگ را با هم دارد — VLESS / VMess / Trojan روی WebSocket (هم **با TLS** و
هم **بدون TLS**) به‌علاوه‌ی **VLESS-XHTTP** — همگی از پشت **کلادفلر**، تا روی شبکه‌هایی
که آی‌پی مستقیم سرور را throttle می‌کنند هم کار کنند.

بدون دیتابیس‌سرور، بدون فریم‌ورک وب، بدون هیچ پکیج pip — فقط **پایتون ۳ (کتابخانه‌ی
استاندارد)**، **xray-core** و یک تونل **cloudflared**.

### ✨ امکانات

- 🤖 **پنل تلگرامی** — ساخت / لیست / تمدید / حذف لینک با دکمه‌های شیشه‌ای. **فقط** به آی‌دی ادمین جواب می‌دهد.
- 🧩 **لینک چندپروتکلی** — یک لینک شامل VLESS / VMess / Trojan روی WS (با و بدون TLS) + VLESS-XHTTP؛ کلاینت خودش بینشان سوییچ می‌کند.
- 📊 **حجم و زمانِ زنده** — محدودیت حجم و زمانِ هر کاربر زنده از طریق xray اعمال می‌شود؛ لینک پس از اتمام خودکار غیرفعال و حذف می‌شود.
- ☁️ **پشت کلادفلر** — تونل خروجیِ `cloudflared` (پشت NAT هم کار می‌کند) آی‌پی throttle‌شده‌ی سرور را پنهان می‌کند.
- 🌐 **تعویض زنده‌ی آی‌پی تمیز** — آی‌پی‌های لبه‌ی کلادفلر را برای *همه‌ی* لینک‌ها از داخل ربات ویرایش کن؛ مشتری فقط Update می‌زند. یک اسکنر هم برای یافتن سریع‌ترین آی‌پی از روی اینترنت خودت دارد.
- 📱 **صفحه‌ی کپیِ موبایلی** — لینک ساب را در مرورگر باز کن → یک صفحه‌ی تمیز راست‌چین با کپی تکی/گروهی و نوار پیشرفت حجم و زمان.
- 🪶 **بسیار سبک** — حدود ۲۵ مگابایت رم، فقط stdlib، روی سرور ۵۱۲ مگابایتی راحت اجرا می‌شود.
- ♻️ **مقاوم در برابر ریبوت** — کاربرها در حافظه‌ی xray هستند؛ enforcer بعد از هر ری‌استارت همه را خودکار resync می‌کند و لینک‌ها تغییر نمی‌کنند.

### 🏗️ چطور کار می‌کند؟

آی‌پی سرور روی بعضی اینترنت‌ها throttle می‌شود. به‌جای در معرض گذاشتنِ مستقیمِ سرور، یک
تونل **`cloudflared`** از سمت سرور به کلادفلر وصل می‌شود (فقط خروجی، پشت NAT هم کار
می‌کند). کلاینت‌ها به **آی‌پی‌های تمیزِ لبه‌ی کلادفلر** وصل می‌شوند؛ کلادفلر بر اساس
Host/SNI و مسیر، ترافیک را از تونل به inboundهای محلیِ `xray` می‌رساند. چون anycastِ
کلادفلر هر دامنه را از هر آی‌پی لبه سرو می‌کند، می‌توانی لینک‌ها را به سریع‌ترین آی‌پی‌ها
برای شبکه‌ی کاربرانت اشاره بدهی — و آن‌ها را زنده از داخل ربات عوض کنی.

### 🚀 نصب سریع — فقط یک ویزارد

روی یک VPS تازه‌ی Debian/Ubuntu، ریپو را کلون کن و ویزارد را اجرا کن. خودش `xray-core`
و `cloudflared` را نصب می‌کند، چند سؤال می‌پرسد (دامنه، توکن ربات، آی‌دی ادمین)، **هر سه
کانفیگ را از یک منبع واحد می‌سازد** (تا tag/port/path هیچ‌وقت ناهماهنگ نشوند) و همه‌چیز را
استارت می‌کند:

```bash
git clone https://github.com/oosmajid/mohajer.git && cd mohajer
sudo bash scripts/wizard.sh
```

تنها قدم دستی، لاگین مرورگریِ کلادفلر است که خود ویزارد بازش می‌کند. در پایان، ربات را باز
کن و `/start` بزن.

> **در داشبورد کلادفلر:** حالت **SSL/TLS را Full** کن، و اگر می‌خواهی کانفیگ‌های سریع‌ترِ
> بدون TLS کار کنند، گزینه‌ی **«Always Use HTTPS» را خاموش** کن.

برای فهم قدم‌به‌قدم یا استفاده روی سرور موجود: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

### 🔐 نکات امنیتی

- ربات **فقط** به `ADMIN_IDS` جواب می‌دهد؛ بقیه پیام رد مؤدبانه می‌گیرند.
- توکن تلگرام در `bot.env` است — `chmod 600` و هرگز کامیت نشود (در `.gitignore` پوشش داده شده، به‌همراه `*.db` و `sub-*`).
- کانفیگ‌های بدون TLS در لایه‌ی انتقال رمزنگاری ندارند (هرچند پروتکل پراکسی همچنان مبهم‌سازی می‌کند) — آن‌ها را گزینه‌ی سریع‌تر اما کم‌امن‌تر در نظر بگیر.

</div>

---

<div align="center">
<sub>Built for staying connected. · ساخته‌شده برای متصل‌ماندن.</sub>
</div>
