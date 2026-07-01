<div align="center">

<img src="assets/bot/vexvpn_bot_avatar_640.png" width="160" alt="VexVPN logo" />

# VexVPN

### Telegram VPN subscription bot **+** Mini App — sell access with **Telegram Stars**, auto‑provision through **Marzban**.

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white">
  <img alt="aiogram" src="https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-Mini%20App-009688?logo=fastapi&logoColor=white">
  <img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white">
  <img alt="Telegram Stars" src="https://img.shields.io/badge/Payments-Telegram%20Stars%20(XTR)-FFD43B?logo=telegram&logoColor=black">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
</p>

<a href="https://github.com/rasvetovvv/vexvpn-telegram-vpn/actions/workflows/ci.yml">
  <img alt="CI" src="https://github.com/rasvetovvv/vexvpn-telegram-vpn/actions/workflows/ci.yml/badge.svg">
</a>

<br/>
<br/>

<img src="assets/bot/vexvpn_bot_preview_1280x640.png" alt="VexVPN preview" width="820" />

</div>

---

## ✨ Overview

**VexVPN** is a production‑ready, self‑hosted **Telegram VPN store**. Users buy and renew VPN
subscriptions right inside Telegram — paying with **Telegram Stars (XTR)**, no card
processor required — and access is **provisioned automatically** through the
[Marzban](https://github.com/Gozargah/Marzban) API. It ships with a polished dark
**Mini App** dashboard, a full **admin panel**, gamification, retention notifications,
referral anti‑fraud, and Stars refunds.

> A complete monetization + provisioning layer for any Marzban‑based VPN, in one Docker Compose stack.

---

## 📑 Table of Contents

- [Features](#-features)
- [Tech Stack](#-tech-stack)
- [Screenshots](#-screenshots)
- [Quick Start](#-quick-start)
- [Configuration](#-configuration)
- [Project Structure](#-project-structure)
- [Payment Flow](#-payment-flow)
- [Promo & Gamification](#-promo--gamification)
- [Admin Panel & API](#-admin-panel--api)
- [Security](#-security)
- [Tests & CI](#-tests--ci)
- [Troubleshooting](#-troubleshooting)
- [Changelog](#-changelog)
- [License](#-license)

---

## 🚀 Features

| | |
|---|---|
| 💫 **Telegram Stars payments** | Native `XTR` invoices from both the bot and the Mini App — no payment provider, no cards. |
| ⚡ **Auto‑provisioning** | Creates / renews Marzban users on payment; days extend from `max(now, current_expire)`. |
| 📊 **Live traffic usage** | Real used / remaining pulled from Marzban (cached), shown in the bot profile and Mini App. |
| 🎁 **Promo system** | `FREE7` free one‑time grant (failure‑safe) and `SALE30` next‑invoice discount. |
| 📦 **Traffic add‑ons** | Buy extra GB without changing subscription expiry. |
| 🎮 **Gamification** | Server‑authoritative daily streak, fortune wheel, and achievements — idempotent & anti‑fraud gated. |
| 🔔 **Retention notifications** | Subscription‑expiry notice + low‑traffic (≥90%) alert, with one‑tap renew. |
| 🤝 **Referral anti‑fraud** | Bonus only for real purchases (≥ min Stars) and capped per referrer per day. |
| 💬 **Two‑way support** | Ticket threads with topics & status, answerable from **both** the bot and the web panel. |
| 🛠️ **Admin panel** | Users, payments, tariffs, manual grants, audit log, broadcast, CSV export, real "servers online". |
| ↩️ **Stars refunds** | `refundStarPayment` from the admin panel for failed / incorrect payments. |
| 📱 **One‑tap import** | Mini App opens configs directly in **Happ / Hiddify**; QR PNG straight in the chat. |
| 🛡️ **Security Center** | Bot + Mini App status for link resets, device warnings, traffic-spike events and recent security history. |
| 🔗 **Subscription gateway** | Public `/sub/{token}` and `/happ/{token}` endpoints serve browser status pages, raw configs and Happ-friendly VLESS links. |
| 🌗 **Telegram‑native UI** | Mini App follows Telegram light/dark theme and chrome colors. |
| 🔒 **Hardened** | initData HMAC, payment idempotency, atomic grants, rate limits, SSRF protection, CSP/security headers. |

---

## 🧱 Tech Stack

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white">
  <img alt="aiogram" src="https://img.shields.io/badge/aiogram-3-2CA5E0?logo=telegram&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-+%20Uvicorn-009688?logo=fastapi&logoColor=white">
  <img alt="SQLAlchemy" src="https://img.shields.io/badge/SQLAlchemy-2%20async-D71F00?logo=sqlalchemy&logoColor=white">
  <img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white">
  <img alt="Redis" src="https://img.shields.io/badge/Redis-rate%20limits-DC382D?logo=redis&logoColor=white">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white">
</p>

- **Bot** — `aiogram 3`
- **Mini App / API** — `FastAPI` + `Uvicorn`
- **Database** — `PostgreSQL 16` via `SQLAlchemy 2` (async) + `asyncpg`
- **Rate limiting** — `Redis` shared limiter when `REDIS_URL` is set, in-memory fallback for local dev
- **Provisioning** — `Marzban` API
- **Deploy** — `Docker Compose`

---

## 🖼️ Screenshots

<div align="center">

| Bot avatar / branding | Promo banner |
|:---:|:---:|
| <img src="assets/bot/vexvpn_bot_avatar_640.png" width="260" alt="VexVPN avatar" /> | <img src="assets/bot/vexvpn_bot_preview_1280x640.png" width="360" alt="VexVPN preview" /> |

</div>

> The Mini App ships a dark VPN dashboard with a traffic progress bar, plans, one‑tap import,
> a gamification card (streak / wheel / achievements) and a support center.

---

## 🚀 Quick Start

```bash
# 1. Clone
git clone https://github.com/rasvetovvv/vexvpn-telegram-vpn.git
cd vexvpn-telegram-vpn

# 2. Configure
cp .env.example .env        # then fill in BOT_TOKEN, ADMIN_IDS, MARZBAN_* ...

# 3. Launch
docker compose up -d --build
docker compose ps
docker compose logs -f bot miniapp
```

Health / smoke check:

```bash
docker compose exec -T miniapp python3 - <<'PY'
import urllib.request
for url in ['http://127.0.0.1:8080/', 'http://127.0.0.1:8080/api/config']:
    r = urllib.request.urlopen(url, timeout=8)
    print(url, r.status)
PY
```

> 📖 Need the full server walkthrough (Marzban install, Xray keys, reverse proxy)? See **[DEPLOY.md](DEPLOY.md)**.

---

## ⚙️ Configuration

Create `.env` from the example and fill in the important variables:

```env
BOT_TOKEN=123:telegram_bot_token
BOT_USERNAME=YourBot
ADMIN_IDS=944763501
SUPER_ADMIN_IDS=            # optional; empty = all admins are super
SENTRY_DSN=                # optional error tracking (needs sentry-sdk)
MINI_APP_URL=https://proxy.example.com
SUBSCRIPTION_PUBLIC_BASE_URL=  # optional; empty = MINI_APP_URL for /sub/<token>
DATABASE_URL=postgresql+asyncpg://vpnbot:vpnbot@db:5432/vpnbot
REDIS_URL=redis://redis:6379/0
MARZBAN_BASE_URL=https://vpn.example.com
MARZBAN_USERNAME=admin
MARZBAN_PASSWORD=password
MARZBAN_TLS_CA_FILE=       # optional CA bundle for private/self-signed Marzban TLS
TRUSTED_PROXY_IPS=172.23.0.0/16,127.0.0.1/32,::1/128
TELEGRAM_INIT_DATA_MAX_AGE_SECONDS=21600
SUPPORT_USERNAME=your_support
```

Notes:

- For Telegram Stars use `currency="XTR"` and an **empty** `provider_token`.
- `amount` is the number of Stars directly, **not** cents.
- The Mini App API requires valid Telegram `initData` for user endpoints.

**Retention & anti‑fraud tunables** (`bot/config.py`):

```python
REFERRAL_MIN_PAYMENT_STARS = 50   # referral bonus only for purchases >= this
REFERRAL_MAX_PER_DAY       = 5    # max rewarded referrals per referrer per 24h
TRAFFIC_ALERT_THRESHOLD    = 0.9  # notify when used >= 90% of the limit
EXPIRED_NOTICE_WINDOW_MIN  = 90   # "just expired" window for the expiry notice
```

**Gamification tunables** (`bot/config.py`): `GAMI_DAILY_GOAL`, `GAMI_DAILY_REWARD_DAYS`, `GAMI_WHEEL_SEGMENTS`, `GAMI_WHEEL_PROMO_CODE`.

---

## 🗂️ Project Structure

```text
vexvpn-telegram-vpn/
├─ bot/
│  ├─ main.py                    # bot entrypoint
│  ├─ config.py                  # settings, default plans, promos
│  ├─ handlers/
│  │  ├─ start.py
│  │  ├─ payments.py             # Stars invoices, pre_checkout, successful_payment
│  │  ├─ profile.py              # profile, support tickets
│  │  └─ admin.py                # bot admin commands
│  ├─ services/
│  │  ├─ marzban.py              # Marzban create/renew logic + random identities + live usage
│  │  ├─ payments.py             # invoice payload/price helpers
│  │  ├─ plans.py                # effective tariff source from DB
│  │  ├─ gamification.py         # streak / wheel / achievements
│  │  ├─ promos.py · notify.py · ops.py
│  ├─ web/
│  │  ├─ main.py                 # Mini App API, security headers, policy pages, subscription gateway
│  │  ├─ admin.py                # Web admin API
│  │  ├─ auth.py                 # Telegram initData validation + replay window
│  │  └─ rate_limit.py           # Redis-backed rate limiter with memory fallback
│  └─ db/
│     ├─ models.py
│     ├─ repo.py
│     └─ database.py             # create_all + soft migrations
├─ miniapp/static/               # index/app/styles + admin panel
├─ tests/                        # unit + SQLite integration + web smoke
├─ .github/workflows/ci.yml      # CI (installs requirements + aiosqlite)
├─ docker-compose.yml
├─ Dockerfile
├─ requirements.txt
└─ .env.example
```

---

## 💳 Payment Flow

1. User selects a plan in the bot or Mini App.
2. Bot / WebApp creates a Telegram **Stars** invoice.
3. `pre_checkout` validates **before** Telegram captures payment:
   - tariff exists and is visible;
   - trial not already used;
   - traffic‑only add‑on requires an active subscription;
   - promo is valid / not used;
   - amount and currency match the current tariff.
4. `successful_payment` checks **idempotency** by `charge_id`.
5. Marzban creates / renews the user.
6. The DB subscription / payment row is updated.

**Protections**

- `payments.charge_id` has a unique DB index — duplicate Telegram events never grant twice.
- Tariffs resolve from `tariff_settings` as the single effective source (`is_trial`, `traffic_only`, `unlimited`).
- Traffic add‑ons add to the existing limit and never reduce unlimited traffic.
- Subscription days extend from `max(now, current_expire)`.

---

## 🎁 Promo & Gamification

**Promo**

- `FREE7` — failure‑safe free grant: a local `pending` payment is created, Marzban is called, and the
  promo is only burned **after** Marzban succeeds (on failure it stays usable, payment → `marzban_error`).
- `SALE30` — applied to the next invoice and cleared after a successful payment.

**Gamification** (server‑authoritative, idempotent, anti‑fraud gated)

- **Daily check‑in** — UTC‑day streak; hitting the goal grants a free day. Rolls back on Marzban failure.
- **Fortune wheel** — 1 spin/day, weighted server‑side RNG (nothing / +GB / +day / discount promo).
- **Achievements** — server‑evaluated badges (`first_launch`, `active_vpn`, `referral`, `first_month`, `unlimited`).
- Day/traffic rewards require an **active** subscription (no free‑VPN farming); 1 action/day enforced by a DB unique index.

---

## 🛡️ Admin Panel & API

Admin‑sensitive actions are written to `admin_audit_logs` (grants, tariff updates, refunds, reset/disable/enable/delete, support replies).

```text
GET  /api/admin/summary                         # servers_online, is_super
GET  /api/admin/users?q=&limit=&offset=
GET  /api/admin/users.csv
GET  /api/admin/user/usage?telegram_id=         # live used/limit/status from Marzban
POST /api/admin/user/reset-traffic { telegram_id }
POST /api/admin/user/disable       { telegram_id }
POST /api/admin/user/enable        { telegram_id }
POST /api/admin/user/delete        { telegram_id }   # super-admin only
POST /api/admin/grant              { telegram_id, days, traffic_gb, ... }
POST /api/admin/refund             { charge_id }      # super-admin only
GET  /api/admin/audit-log?limit=&action=&admin_id=
GET  /api/admin/tickets?status=active|open|answered|closed
GET  /api/admin/ticket/{id}                      # full message thread
POST /api/admin/ticket/{id}/reply { text }       # → user notified, status=answered
POST /api/admin/ticket/{id}/close                # → user notified, status=closed
```

**Roles**

- `ADMIN_IDS` — full admin access (panel, grants, broadcast, user actions).
- `SUPER_ADMIN_IDS` — required for destructive actions (delete user, refund). Empty ⇒ all admins are super.

**Refunds** — `refundStarPayment(user_id, charge_id)`. Only real Stars payments are refundable;
`PROMO-` / `TEST-` / `ADMIN-` and zero‑Stars rows are rejected. On success the row becomes `refunded`.

---

## 🔐 Security

- Telegram Mini App **`initData` HMAC** validation requires `auth_date`; replayed payloads older than `TELEGRAM_INIT_DATA_MAX_AGE_SECONDS` are rejected.
- **Rate limits** on `POST /api/promo`, `POST /api/invoice-link`, `GET /api/check-vpn`, device reset, support endpoints, public `/sub|/happ` gateway and public QR endpoints.
- `REDIS_URL` enables shared production rate limits across workers/containers; without Redis the limiter falls back to local memory for single-process development.
- **SSRF protection**: `check-vpn` only fetches subscription URLs from the exact configured Marzban origin: HTTPS scheme, same hostname and same effective port, with redirects disabled.
- Public `/sub/{token}` fetches raw configs from Marzban with TLS verification enabled; private/self-signed Marzban installs can set `MARZBAN_TLS_CA_FILE`.
- Client IP extraction only trusts `X-Forwarded-For` / `X-Real-IP` when the direct peer is inside `TRUSTED_PROXY_IPS`; public endpoint rate-limit keys use salted hashes, not raw IP strings.
- User support messages are **HTML‑escaped** before Telegram output; promo codes validated against `^[A-Z0-9_]{2,32}$`.
- The Mini App cabinet escapes all server/admin‑editable fields before `innerHTML`.
- New Marzban accounts use random `vxu_<24 hex>` usernames; existing stored mappings and legacy `tg_<telegram_id>` accounts continue to work.
- Device notes written to Marzban use `vexvpn | devices:<N>` instead of embedding Telegram IDs.
- `httpx` / `httpcore` routine logs are lowered to `WARNING` in bot and Mini App processes.
- Block-level free-VPN anti-fraud spikes notify admins without raw IPs, fingerprints or subscription tokens.
- **Security headers**: `Content-Security-Policy`, `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`.
- Public privacy pages are served from the Mini App backend: `GET /privacy`, `GET /no-logs`, `GET /transparency`.
- Mini App access logs are disabled in Docker Compose with `uvicorn --no-access-log`.

**Security Center & link protection**

- Bot profile now has a `Безопасность` flow with recent `security_events`, reset count, QR access after reset and a confirmation step before link rotation.
- Mini App calls `GET /api/security` and renders protections, device-count warnings, reset metadata and the latest security events.
- `subscriptions` stores first-connection and security fields: `first_connected_at`, `first_connect_notified_at`, `first_seen_traffic`, `last_usage_bytes`, `last_usage_checked_at`, `security_last_alert_at`, `reset_count`, `last_reset_at`.
- `security_events` stores user-visible aggregate events (`traffic_spike`, `link_reset`) without raw IP addresses, DNS queries or destination hosts.
- `POST /api/device/reset` and the bot reset flow call `MarzbanClient.revoke_sub()`, update `subscription_url`, increment `reset_count`, set `last_reset_at` and write a `link_reset` event.
- The bot reminder loop detects traffic spikes from Marzban usage deltas, sends a throttled Security Center alert and records only the aggregate delta event.
- `first_connect_loop` watches active subscriptions for first real usage and sends a one-shot "VPN works" message after traffic/online status appears.
- `/sub/{token}` proxies Marzban subscriptions: browsers receive a status/instruction page; VPN clients receive raw config. `/happ/{token}` and `?happ=1` return cleaned plain VLESS links for Happ.

> ⚠️ **Never commit your real `.env`.** It is gitignored by default. Rotate any token/password that has ever been shared.

---

## 🧪 Tests & CI

```bash
# inside the app image
docker compose run --rm bot python -m unittest discover -s tests -v

# locally (set a SQLite URL first so asyncpg isn't required)
# e.g. DATABASE_URL=sqlite+aiosqlite:///./test.db
python -m unittest discover -s tests -v
```

Coverage includes invoice payload roundtrips, `SALE30` pricing, `FREE7` as a free grant, Marzban
renewal preserving unlimited traffic, traffic add‑on rules, device‑note handling, `get_usage` parsing
& caching, random Marzban usernames, gamification idempotency / anti‑fraud, the two‑way support thread,
atomic `finalize_grant`, Telegram `initData` replay checks, trusted-proxy IP handling, HSTS,
strict Marzban origin checks, public endpoint throttling and the Redis limiter path.

CI runs the full suite on every push / PR via [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

---

## 🩺 Troubleshooting

| Symptom | Check |
|---|---|
| Paid but Marzban failed | `payments.status = 'marzban_error'` → grant manually from the panel. |
| Charged but nothing granted | `payments.status = 'validation_error'` (problem filter) → refund or grant manually. |
| WebApp purchase won't open | Open the Mini App **inside Telegram**; confirm `/api/invoice-link` isn't rate‑limited. |
| Trial paid twice | Check `tariff_settings.is_trial` for the trial tariff and `users.trial_used`. |
| Traffic got reduced | Check Marzban `data_limit`; code preserves `0` as unlimited and adds finite add‑ons. |
| Notifications don't arrive | Reminder loop runs every 30 min; expiry notice only within `EXPIRED_NOTICE_WINDOW_MIN`; low‑traffic needs `traffic_limit > 0` and usage ≥ threshold. |

---

## 📜 Changelog

### 2026-07-01 — Security hardening: TLS, replay window, Redis limits and private identities

- **Marzban identity privacy**: new accounts use random `vxu_<24 hex>` usernames; `subscriptions.marzban_username` is the local Telegram→Marzban mapping, existing mappings are preserved, and legacy `tg_<telegram_id>` accounts remain a fallback.
- **Marzban notes**: device-limit notes no longer contain Telegram IDs; new/updated notes use `vexvpn | devices:<N>`.
- **DB migration**: startup ensures unique index `uq_subscriptions_marzban_username` on `subscriptions.marzban_username`.
- **Gateway TLS**: public `/sub/{token}` fetches Marzban configs with certificate verification enabled; `MARZBAN_TLS_CA_FILE` supports private/self-signed CA bundles.
- **Trusted proxy IPs**: `_client_ip()` trusts `X-Forwarded-For` / `X-Real-IP` only from `TRUSTED_PROXY_IPS`; public rate-limit keys hash the hardened client IP.
- **Telegram replay protection**: Mini App `initData` now requires `auth_date`, rejects missing/invalid/future values, and enforces `TELEGRAM_INIT_DATA_MAX_AGE_SECONDS`.
- **Public endpoint limits**: `/sub/{token}`, `/happ/{token}`, `/api/qr.svg` and `/api/qr.png` are IP-rate-limited before expensive upstream/QR work.
- **Strict SSRF origin check**: `check-vpn` now requires exact HTTPS scheme, hostname and effective port match with `MARZBAN_BASE_URL`.
- **Shared rate limiting**: `REDIS_URL` enables a Redis fixed-window limiter across workers/containers; in-memory limiting remains as local fallback. Docker Compose now includes `redis` + `redisdata`, and `redis==5.2.1` is added.
- **Headers and alerts**: Mini App responses include HSTS; bot/Mini App lower `httpx` logs to `WARNING`; block-level anti-fraud spikes send admin alerts without raw IPs, fingerprints or subscription tokens.
- **Tests**: added `tests/test_web_security_hardening.py` plus Marzban-username regression coverage in `tests/test_payment_promo_traffic.py`.

### 2026-07-01 — Security Center, subscription gateway and link-protection events

- **Data model**: `subscriptions` now tracks first-connect state, usage checkpoint fields, security alert cooldown, reset count and last reset time. New `security_events` table stores aggregate user-facing events without raw IPs, DNS queries or destination hosts.
- **Bot Security Center**: profile menu gained `Безопасность`; users can view recent security events, open Privacy Center, confirm VPN-link rotation, regenerate the subscription URL through `MarzbanClient.revoke_sub()` and reopen QR after reset.
- **Mini App Security Center**: dashboard block calls `GET /api/security` and shows protection status, device-count warnings, reset metadata and recent events.
- **Security notifications**: the reminder loop now compares Marzban usage deltas, sends a throttled traffic-spike alert, stores a `traffic_spike` event and updates usage checkpoints without storing browsing data.
- **First connection notification**: new `first_connect_loop` detects the first real VPN usage (`used_traffic` / `online_at`) and sends a one-time "VPN works" message with quick actions.
- **Subscription gateway**: new `/sub/{token}` route serves a browser status page or raw Marzban config depending on the client; `/happ/{token}` and `?happ=1` convert subscriptions into Happ-friendly plain VLESS links.
- **Privacy pages & logs**: Mini App backend serves `/privacy`, `/no-logs`, `/transparency`; Docker Compose starts Uvicorn with `--no-access-log` so subscription tokens and QR query data are not written to HTTP access logs.
- **Configuration**: `SUBSCRIPTION_PUBLIC_BASE_URL` can override the public base used for `/sub/<token>` links; empty value falls back to `MINI_APP_URL`.

### 2026-06-30 — Full two-way support system (tickets with topics, status, threads)

A complete support system across the bot **and** the WebApp, with admin reply/close from **both** the bot and the web panel.

- **Data model**: `support_tickets` extended with `topic` (payment/vpn/promo/other), `status` (open/answered/closed), `updated_at`; new `support_messages` table holds the two-way thread (sender user/admin). Soft migration adds the columns and maps legacy `new` → `open`.
- **Status flow**: new ticket → `open`; admin reply → `answered`; user reply (or reply to a closed ticket) → `open`; either side can `close`. Counters drive the bot's "🆘 Новых заявок" and the panel badge.
- **WebApp (user)**: Support Center reworked — pick a topic, write a message, create a ticket; "Мои обращения" lists tickets with status badges; tapping one opens a **thread modal** with the full conversation, a reply box and a close button. New endpoints: `GET /api/support/tickets`, `GET/POST /api/support/ticket/{id}`, `POST /api/support/ticket`, `POST /api/support/ticket/{id}/message|close` (rate-limited, ownership-checked).
- **Bot (user)**: topic-based support flow (`support_topic:*`), VPN topic still shows the connection FAQ first; "Мои обращения" lists tickets, opens the thread, and lets the user reply (FSM via `support_state`) or close.
- **Bot (admin)**: new-ticket / user-reply notifications carry inline **Ответить / Закрыть** buttons; `/tickets` lists active tickets; `aticket:` shows the thread; `areply:` (in-memory pending, `/cancel` to abort) sends a reply → user notified; `aclose:` closes → user notified.
- **Web admin panel**: new "Тикеты поддержки" card with a status filter and open-count badge; clicking a ticket opens a thread modal to reply (→ user notified via Telegram) or close. Endpoints under `/api/admin/tickets`, `/api/admin/ticket/{id}` (+`/reply`, `/close`), audited in `admin_audit_logs` (`support_reply`, `support_close`).
- **Notifications**: new `bot/services/notify.py` (`tg_send`, `notify_admins`) lets the web process message Telegram directly (the bot is a separate process); all user/admin text is HTML-escaped.
- **Tests**: +1 unit (ticket thread + status transitions + listings) and a full web smoke (user create → admin reply → user reply → admin close, status flow + foreign-access 404). Full suite 27/27 green.

### 2026-06-30 — Cabinet quick-wins (1-click renew, device reset, QR PNG, promo banner, traffic sparkline)

- **1-click renew**: `/api/me` now exposes `subscription.plan_key`; the "Продлить в 1 клик" button creates an invoice for the current tariff directly (only if it's a visible, non-trial, non-traffic-only plan — otherwise it falls back to the catalog).
- **Device reset / change device** (self-service, replaces a support ticket): `MarzbanClient.revoke_sub()` re-issues the subscription token (old device configs stop working). New `POST /api/device/reset` (rate-limited 3/5 min) updates the stored `subscription_url`; cabinet button with a confirm dialog (`tg.showConfirm`).
- **QR PNG download**: new `GET /api/qr.png` (pypng) for a real downloadable PNG; the "Скачать QR" button now saves PNG instead of inline SVG.
- **Promo banner with countdown**: `config.py:PROMO_BANNER` (title/subtitle/code/`until`) is served via `/api/config` and rendered as a banner above the plans, with a live deadline timer when `until` is set.
- **Traffic sparkline**: new `usage_snapshots` table + daily capture in the bot reminder loop (piggybacks the existing `get_usage` pass for finite-traffic active subs). `/api/me` returns `subscription.usage_history`; the cabinet renders a per-day consumption sparkline (deltas, peak highlighted). Populates over a few days.
- **Tests**: +2 (`revoke_sub` returns the new full URL; usage-snapshot upsert + ascending history) and a web smoke (config banner, `/api/me` plan_key + history, device reset URL+DB update, qr.png bytes). Full suite 26/26 green.

Deferred (need more than a quick win): region/server picker + live ping (**blocked until Marzban nodes exist**), two-way support tickets (needs an admin-reply flow through the bot), RU/EN i18n (full string refactor of `texts.py` + frontend).

### 2026-06-30 — Gamification (daily streak, fortune wheel, achievements)

Server-authoritative gamification in the Mini App cabinet — all rewards are computed and granted on the server (the client is never trusted), idempotent, and anti-fraud gated.

- **New tables** (`bot/db/models.py`): `gamification_state` (streak + counters), `bonus_claims` (unique `telegram_id+kind+day` → max 1 check-in and 1 spin per UTC day, race-safe), `user_achievements` (badges, unique per code). Auto-created via `create_all`.
- **Service** (`bot/services/gamification.py`):
  - **Daily check-in** — UTC-day streak; reaching `GAMI_DAILY_GOAL` (3) consecutive days grants `+GAMI_DAILY_REWARD_DAYS` (1) day. On Marzban failure the streak/reservation is rolled back so the user can retry.
  - **Fortune wheel** — 1 spin/day, weighted server-side RNG over `GAMI_WHEEL_SEGMENTS` (nothing / +5 GB / +1 day / −15 % promo). Day/traffic rewards go through the same Marzban path as normal grants and preserve the user's current tariff name; promo reward sets the `WHEEL15` (repeatable) discount as the active promo.
  - **Achievements** — server-evaluated badges (`first_launch`, `active_vpn`, `referral`, `first_month`, `unlimited`), awarded idempotently.
  - **Anti-fraud**: day/traffic rewards require an **active** subscription (no free-VPN farming for non-customers); traffic-without-active-sub auto-converts to a day; 1 action/day enforced by a DB unique index; reward economy is tunable in `config.py`.
- **API** (`bot/web/main.py`, rate-limited): `GET /api/gamification`, `POST /api/bonus/daily/claim`, `POST /api/wheel/spin`. `GamificationError` maps to `409` (already/not-eligible) or `502` (panel down).
- **Mini App UI** (`index.html`, `styles.css`, `app.js`): the `game-card` now has a daily check-in with streak dots, an animated fortune wheel, and a live achievements grid; the old `daily-bonus` stub is wired to the real endpoints. All gamification fields are `esc()`-escaped.
- **Tests**: +3 (bonus-claim idempotency per day, achievement idempotency, wheel segment validity) + an end-to-end smoke (eligibility, grant path, daily/spin idempotency, anti-fraud gate). Full suite 24/24 green.

Tunables in `config.py`: `GAMI_DAILY_GOAL`, `GAMI_DAILY_REWARD_DAYS`, `GAMI_WHEEL_SEGMENTS`, `GAMI_WHEEL_PROMO_CODE`.

### 2026-06-30 — Audit fixes round 2 (consistency, load, XSS-hardening)

- **Tariff admin-edits are durable** (`bot/db/repo.py`): `ensure_tariff_settings` no longer re-syncs `is_trial`/`unlimited` over existing rows on every start — admin panel edits to these flags survive a restart (previously they silently reverted to `config.py`). New tariffs are still seeded with correct flags via `INSERT … ON CONFLICT DO NOTHING`. Seed is now dialect-aware (Postgres in prod, SQLite in tests).
- **WebApp promo grant unified with the bot** (`bot/web/main.py`): `/api/promo` free-grant now goes through `finalize_grant` + `enqueue_grant` like the bot path. This fixes three divergences at once — it now sets `trial_used` for trial promos, queues an auto-retry on Marzban failure (instead of just erroring), and writes atomically in a single commit.
- **`get_usage` is cached** (`bot/services/marzban.py`): live traffic/usage is cached per `telegram_id` for ~45s, so the Mini App auto-refresh (every 60s per open cabinet) and the bot profile stop hammering the panel. Cache is invalidated on grant/renew, reset-traffic, status change and delete; network errors are not cached (fast recovery).
- **Stored-XSS hardening**:
  - promo code is now validated against `^[A-Z0-9_]{2,32}$` in `promo_create` (`bot/services/promos.py`) — markup/script chars rejected before a code can reach invoices or the cabinet;
  - the Mini App cabinet (`miniapp/static/app.js`) now escapes all server/admin-editable fields (plans, coupons, promo/payment/ticket history) via a new `esc()` helper before `innerHTML` (the admin panel already escaped everywhere).
- **Tests**: +3 cases — tariff admin-edit persistence, promo-code charset rejection, `get_usage` caching/invalidation. Full suite 21/21 green.

### 2026-06-30 — Admin ops, reliability & CI

- **Per-user admin actions**: live usage (`/api/admin/user/usage`), reset traffic, disable/enable, delete (`/api/admin/user/{reset-traffic|disable|enable|delete}`) + buttons in the admin users table. New Marzban client methods `reset_traffic`, `set_status`, `delete_user`.
- **Admin roles**: `SUPER_ADMIN_IDS` (empty = all admins are super). Destructive actions — **delete user** and **refund** — require super-admin; UI hides the delete button for non-super.
- **Real "servers online"**: `MarzbanClient.servers_online()` (nodes, 5-min cache) shown in the admin summary; falls back to `SERVERS_ONLINE`.
- **Users CSV export**: `GET /api/admin/users.csv` + "CSV пользователей" button.
- **Audit-log filters**: `GET /api/admin/audit-log?action=&admin_id=` + filter input.
- **Broadcast 2.0** (bot): segments `all|active|expired|plan:<key>`, `TelegramRetryAfter` retry, blocked-user counting, progress updates.
- **Atomic grant**: `finalize_grant()` writes subscription + trial + promo-clear + payment status + logs in one transaction (was 4–5 separate commits).
- **Health & resilience**: `GET /healthz` (DB ping) + docker healthcheck for the miniapp; graceful background-task shutdown; optional Sentry via `SENTRY_DSN` (lazy, needs `sentry-sdk`).
- **Tests & CI**: SQLite integration tests (atomic grant, payment idempotency, promo validator) + GitHub Actions workflow (`.github/workflows/ci.yml`).

### 2026-06-30 — UX & connection

- **Bot command menu** (`set_my_commands`): `/start`, `/buy`, `/profile`, `/promo`, `/help` (new `/buy`, `/profile`, `/help` handlers).
- **`?start=buy` deeplink** opens the plan picker directly.
- **Promo input via button**: the "🎁 Промокод" button starts an FSM prompt (`PromoFlow`) — no need to type `/promo CODE`.
- **QR in chat**: "📷 QR-код" button sends the subscription QR as a PNG (via `pypng`, no Pillow). Added `pypng` to `requirements.txt`.
- **Mini App traffic bar**: visual used/limit progress bar (turns amber at ≥90%).
- **Referral share**: "🚀 Поделиться в Telegram" button using `t.me/share/url`.
- **One-tap import**: Mini App "Открыть в Happ / Hiddify" buttons (`happ://add/…`, `hiddify://import/…`) + per-OS install buttons.
- **Telegram theme**: Mini App follows `colorScheme` (dark/light) and Telegram chrome colors.
- **Support FAQ**: common fixes (re-import / switch network / reset profile) are shown before a ticket is created.

### 2026-06-30 — Retention & money features

- **Stars refunds**: `POST /api/admin/refund` + admin-panel button (`refundStarPayment`); refunded payments marked `refunded`.
- **Live traffic usage**: `MarzbanClient.get_usage()`; bot profile and Mini App now show used / remaining.
- **Expiry notification**: bot messages the user when the subscription expires, with a one-tap renew button.
- **Low-traffic notification**: alert at ≥90% usage (live from Marzban), deduped per limit value.
- **Referral anti-fraud**: bonus only for purchases ≥ `REFERRAL_MIN_PAYMENT_STARS` and capped at `REFERRAL_MAX_PER_DAY` per referrer/day.
- **Charged-but-not-granted** payments now recorded as `validation_error` and surfaced in the admin "problem" filter/metric.

### 2026-06-30 — Audit fixes

- **Traffic reset bug**: added explicit `Plan.unlimited` flag (and `tariff_settings.unlimited` column + soft migration). Days-only top-ups (referral/admin) preserve the current limit instead of wiping it to unlimited.
- **`/promo FREE7` in bot**: validator now accepts free promos; deterministic `charge_id` prevents double grants.
- **Traffic add-on**: keeps the real plan name and expiry instead of overwriting them.
- **Tariff edits live in bot**: bot menu/texts now read tariffs from DB (`get_visible_plans`) — no bot restart needed.
- **`ensure_tariff_settings`**: `INSERT … ON CONFLICT DO NOTHING` + per-process cache (no startup race, no ~N queries per request).
- **Bot username**: Mini App resolves it via cached `getMe`, independent of `BOT_USERNAME` env.
- **Device limit**: written to Marzban user `note` (panel-visible, IP-limiter-ready).

---

## 📄 License

Released under the **MIT License** — see [LICENSE](LICENSE).

<div align="center">
<br/>
<sub>Built with ❤️ for the Telegram + Marzban ecosystem · <b>VexVPN</b></sub>
</div>
