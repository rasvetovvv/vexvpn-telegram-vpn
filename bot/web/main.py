"""FastAPI backend + static frontend for Telegram Mini App."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import html
import ipaddress
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import httpx
import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select, text

from bot.config import PLANS, PROMOS, PROMO_BANNER, Plan, REFERRAL_BONUS_DAYS, settings
from bot.db.database import init_db, session_maker
from bot.db.models import AbuseFlag, BotEvent, DailyFreeClaim, MiniAppDevice, Payment, PromoUse, SecurityEvent, Subscription, SupportTicket, User
from bot.db.repo import (
    SUPPORT_TOPIC_TITLES,
    SUPPORT_TOPICS,
    add_payment,
    add_support_message,
    create_support_ticket,
    enqueue_grant,
    ensure_user,
    finalize_grant,
    get_subscription,
    get_ticket,
    get_ticket_messages,
    get_user,
    has_used_promo,
    get_payment_by_charge_id,
    get_usage_history,
    list_user_tickets,
    log_abuse_flag,
    log_marzban,
    mark_promo_used,
    record_miniapp_open,
    reserve_daily_free_claim,
    set_active_promo,
    set_daily_free_claim_status,
    set_ticket_status,
    should_send_alert,
    update_payment_status,
)
from bot.services import gamification as gami
from bot.services import payments as pay
from bot.services.gamification import GamificationError
from bot.services.marzban import MarzbanError, marzban
from bot.services.notify import notify_admins
from bot.services.promos import get_promo, list_promos, promo_to_plan, validate_promo_for_user
from bot.utils import fmt_date, fmt_size, fmt_traffic, human_left
from bot.web.auth import telegram_user
from bot.web.admin import router as admin_router, get_admin_plan
from bot.web.rate_limit import RateLimit, require_rate_limit

BASE_DIR = Path(__file__).resolve().parents[2]
STATIC_DIR = BASE_DIR / "miniapp" / "static"

app = FastAPI(title="VexVPN Mini App", version="1.0.0")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(admin_router)

PROMO_RATE = RateLimit(limit=5, window_seconds=60)
INVOICE_RATE = RateLimit(limit=8, window_seconds=60)
CHECK_VPN_RATE = RateLimit(limit=6, window_seconds=60)
PUBLIC_SUB_RATE = RateLimit(limit=30, window_seconds=60)  # /sub and /happ hit Marzban; protect upstream panel
PUBLIC_QR_RATE = RateLimit(limit=60, window_seconds=60)  # QR render is CPU-bound and unauthenticated
GAMI_RATE = RateLimit(limit=12, window_seconds=60)
DEVICE_RESET_RATE = RateLimit(limit=3, window_seconds=300)
SUPPORT_RATE = RateLimit(limit=10, window_seconds=60)
ABUSE_BLOCK_SPIKE_THRESHOLD = 3
ABUSE_BLOCK_SPIKE_WINDOW_SECONDS = 10 * 60

_bot_username_cache: str | None = None


async def _resolve_bot_username() -> str:
    """Реальный username бота через getMe (кэш), фолбэк — settings.bot_username.

    Так реф-ссылки в Mini App не зависят от того, выставлен ли BOT_USERNAME в .env.
    """
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"https://api.telegram.org/bot{settings.bot_token}/getMe")
        data = resp.json()
        username = data.get("result", {}).get("username") if data.get("ok") else None
        if username:
            _bot_username_cache = username
            return username
    except Exception:
        pass
    return settings.bot_username


def _effective_port(parsed) -> int | None:
    """Return explicit or scheme-default port; None for unsupported schemes."""
    if parsed.port:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return None


def _is_own_subscription_url(url: str) -> bool:
    """Allow check-vpn only for the exact configured Marzban origin.

    The origin check is intentionally strict: scheme + hostname + effective port
    must match settings.marzban_base_url. This prevents a URL on the same host
    but a different port/service from passing SSRF validation.
    """
    try:
        parsed = urlparse(url)
        allowed = urlparse(settings.marzban_base_url)
    except Exception:
        return False
    if parsed.scheme != "https" or allowed.scheme != "https":
        return False
    if not parsed.hostname or not allowed.hostname:
        return False
    if parsed.hostname.lower() != allowed.hostname.lower():
        return False
    return _effective_port(parsed) == _effective_port(allowed)


DAILY_FREE_PLAN = Plan("daily_free", "Ежедневный бесплатный VPN", 1, 0, 100, 1, "каждый день", visible=False)
MAX_FREE_DEVICES_PER_USER = 3
MAX_FREE_ACCOUNTS_PER_DEVICE_DAY = 2
MAX_FREE_ACCOUNTS_PER_IP_DAY = 5
MIN_LOW_RISK_AGE_MINUTES = 30


def _hash_value(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    return hashlib.sha256(f"{settings.bot_token}:{value}".encode()).hexdigest()


def _trusted_proxy_networks() -> tuple[ipaddress._BaseNetwork, ...]:
    """Configured reverse proxies allowed to supply client IP headers.

    X-Forwarded-For/X-Real-IP are user-controlled unless the direct peer is a
    trusted reverse proxy. Keep this list narrow: exact proxy IPs or Docker/LAN
    CIDRs that are not reachable by arbitrary internet clients.
    """
    networks: list[ipaddress._BaseNetwork] = []
    for item in (settings.trusted_proxy_ips or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            # Invalid config should fail closed: ignore bad entries and fall back
            # to the direct peer address instead of trusting spoofable headers.
            continue
    return tuple(networks)


def _is_trusted_proxy(peer_ip: str | None) -> bool:
    if not peer_ip:
        return False
    try:
        ip = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(ip in network for network in _trusted_proxy_networks())


def _first_valid_forwarded_ip(header_value: str) -> str | None:
    for part in (header_value or "").split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        return candidate
    return None


def _client_ip(request: Request) -> str:
    peer_ip = request.client.host if request.client else ""
    if _is_trusted_proxy(peer_ip):
        forwarded = _first_valid_forwarded_ip(request.headers.get("x-forwarded-for", ""))
        real_ip = _first_valid_forwarded_ip(request.headers.get("x-real-ip", ""))
        return forwarded or real_ip or peer_ip
    return peer_ip


def _require_public_ip_rate_limit(request: Request, namespace: str, rule: RateLimit) -> None:
    """Rate-limit public unauthenticated endpoints by hashed client IP.

    Raw client IP is not stored in the in-memory rate-limit key; the hash uses
    the bot token as salt via _hash_value(). XFF is only considered when the
    direct peer is a trusted proxy, because _client_ip() enforces that.
    """
    ip_hash = _hash_value(_client_ip(request)) or "unknown"
    require_rate_limit(f"public:{namespace}:ip:{ip_hash}", rule)


def _marzban_tls_verify() -> bool | str:
    """TLS verification setting for Marzban subscription fetches.

    Default is strict certificate verification (True). If Marzban uses a private
    CA/self-signed certificate, set MARZBAN_TLS_CA_FILE to a mounted CA bundle.
    We intentionally do not support a "disable verification" switch here.
    """
    ca_file = (settings.marzban_tls_ca_file or "").strip()
    return ca_file or True


def _seconds_until_next_utc_day(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).date()
    next_day = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)
    return max(0, int((next_day - now).total_seconds()))


def _fingerprint_hash(payload: FingerprintIn | None, request: Request) -> str | None:
    fp = (payload.fingerprint if payload else None) or request.headers.get("x-miniapp-fingerprint")
    if not fp or len(fp) < 12:
        return None
    return _hash_value(fp[:512])


async def _record_open_from_request(session, telegram_id: int, tg_user: dict, request: Request, payload: FingerprintIn | None = None) -> tuple[str | None, str | None]:
    fp_hash = _fingerprint_hash(payload, request)
    ip_hash = _hash_value(_client_ip(request))
    ua_hash = _hash_value(request.headers.get("user-agent", "")[:256])
    platform = (payload.platform if payload else None) or request.headers.get("x-miniapp-platform")
    await record_miniapp_open(session, telegram_id, fingerprint_hash=fp_hash, ip_hash=ip_hash, user_agent_hash=ua_hash, platform=platform)
    return fp_hash, ip_hash


async def _log_abuse_flag_and_alert(
    session,
    *,
    telegram_id: int | None,
    kind: str,
    severity: str,
    fingerprint_hash: str | None = None,
    ip_hash: str | None = None,
    details: str | None = None,
) -> None:
    """Log abuse flag and alert admins when block events spike.

    The alert intentionally does not include raw IP, fingerprint, subscription
    token or other sensitive identifiers. Hash prefixes are enough for operators
    to correlate repeated abuse without storing/displaying raw values.
    """
    await log_abuse_flag(
        session,
        telegram_id=telegram_id,
        kind=kind,
        severity=severity,
        fingerprint_hash=fingerprint_hash,
        ip_hash=ip_hash,
        details=details,
    )
    if severity != "block":
        return
    since = datetime.now(timezone.utc) - timedelta(seconds=ABUSE_BLOCK_SPIKE_WINDOW_SECONDS)
    blocks = await session.scalar(select(func.count(AbuseFlag.id)).where(AbuseFlag.severity == "block", AbuseFlag.created_at >= since)) or 0
    if blocks < ABUSE_BLOCK_SPIKE_THRESHOLD:
        return
    key = f"abuse-block-spike:{ABUSE_BLOCK_SPIKE_WINDOW_SECONDS}"
    msg = (
        "🚨 <b>VexVPN anti-fraud spike</b>\n"
        f"Block-level abuse flags in last {ABUSE_BLOCK_SPIKE_WINDOW_SECONDS // 60} min: <b>{int(blocks)}</b>\n"
        f"Latest kind: <code>{html.escape(kind[:32])}</code>\n"
        f"Telegram ID: <code>{telegram_id or 'unknown'}</code>\n"
        f"Fingerprint hash: <code>{(fingerprint_hash or '-')[:12]}</code>\n"
        f"IP hash: <code>{(ip_hash or '-')[:12]}</code>\n"
        "This may indicate farming against the free VPN flow. Check admin abuse flags and rate-limit dashboards."
    )
    if await should_send_alert(session, key, cooldown_minutes=15, message=msg):
        await notify_admins(msg)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' https://telegram.org; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' https://api.telegram.org; "
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org; "
        "base-uri 'self'; form-action 'self'; object-src 'none'",
    )
    return response


class PromoIn(BaseModel):
    code: str


class InvoiceIn(BaseModel):
    plan_key: str


class FingerprintIn(BaseModel):
    fingerprint: str | None = None
    platform: str | None = None


class TicketCreateIn(BaseModel):
    topic: str
    message: str


class TicketReplyIn(BaseModel):
    text: str


def _init_sentry() -> None:
    """Опциональный трекинг ошибок: включается, если задан SENTRY_DSN и установлен sentry-sdk."""
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.0)
    except Exception:
        import logging

        logging.getLogger(__name__).warning("Не удалось инициализировать Sentry", exc_info=True)


@app.get("/healthz")
async def healthz() -> dict:
    try:
        async with session_maker() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="db unavailable") from exc
    return {"ok": True}


@app.on_event("startup")
async def startup() -> None:
    _init_sentry()
    await init_db()
    async with session_maker() as session:
        from bot.db.repo import ensure_tariff_settings

        await ensure_tariff_settings(session, PLANS)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin")
async def admin_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")



def _policy_page(title: str, subtitle: str, sections: list[tuple[str, str]]) -> str:
    items = "".join(f"<section class='card'><h2>{html.escape(h)}</h2><p>{body}</p></section>" for h, body in sections)
    css = """
    body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;background:radial-gradient(800px 420px at 12% 0%,rgba(34,158,217,.3),transparent),linear-gradient(135deg,#060914,#11192f);color:#f7fbff;min-height:100vh}a{color:#9ee7ff}.wrap{max-width:920px;margin:0 auto;padding:34px 16px 56px}.hero,.card{border:1px solid rgba(255,255,255,.14);background:linear-gradient(180deg,rgba(255,255,255,.11),rgba(255,255,255,.055));border-radius:26px;padding:24px;box-shadow:0 24px 70px rgba(0,0,0,.25)}.hero{margin-bottom:16px}.eyebrow{color:#9ee7ff;text-transform:uppercase;letter-spacing:.12em;font-size:12px;font-weight:800}h1{font-size:clamp(34px,7vw,62px);line-height:.98;margin:12px 0}h2{margin:0 0 10px}.sub,p,li{color:#b9c6dc;line-height:1.72}.card{margin:12px 0}code{background:#070b18;border:1px solid rgba(255,255,255,.14);padding:2px 6px;border-radius:8px;color:#bdefff}.nav{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}.nav a{border:1px solid rgba(255,255,255,.14);border-radius:999px;padding:10px 13px;text-decoration:none;color:#fff;background:rgba(255,255,255,.07)}.ok{color:#58e08d;font-weight:800}.warn{color:#ffc857;font-weight:800}
    """
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)} · VexVPN</title><style>{css}</style></head><body><main class='wrap'><section class='hero'><span class='eyebrow'>VexVPN privacy center</span><h1>{html.escape(title)}</h1><p class='sub'>{html.escape(subtitle)}</p><nav class='nav'><a href='/privacy'>Privacy Policy</a><a href='/no-logs'>No-logs Policy</a><a href='/transparency'>Transparency</a><a href='/'>Dashboard</a></nav></section>{items}<section class='card'><p><span class='warn'>Important:</span> VPN повышает приватность, но не делает человека полностью анонимным. Сервер технически видит входящее сетевое соединение во время подключения, но VexVPN настроен так, чтобы не хранить browsing history, DNS history или содержимое трафика.</p></section></main></body></html>"""


@app.get("/privacy")
async def privacy_policy_page() -> Response:
    return Response(_policy_page("Privacy Policy", "Какие данные VexVPN минимально обрабатывает и как мы защищаем пользователя.", [
        ("Что мы не собираем", "Мы не логируем посещённые сайты, содержимое трафика, DNS-историю, IP назначения, поисковые запросы или приложения пользователя."),
        ("Что нужно для работы сервиса", "Для подписки используются Telegram ID, статус тарифа, дата окончания, объём использованного трафика из Marzban и платёжное состояние. Для anti-abuse Mini App сохраняет только salted hashes IP/fingerprint/User-Agent, не raw значения."),
        ("IP пользователя", "Обычный VPN-сервер технически видит source IP во время сетевого соединения. Цель VexVPN — не сохранять raw IP в базе и не писать его в access logs. Для антиабуза используется только hash."),
        ("Subscription links", "Ссылка подписки является ключом доступа. Мы не показываем токены в публичных ответах, отключаем Mini App access logs и даём пользователю reset-device flow для перевыпуска ссылки."),
        ("Безопасность", "Включены Telegram initData HMAC validation, rate limits, CSP/security headers, SSRF protection для VPN-check endpoints и escaped support messages."),
    ]), media_type="text/html; charset=utf-8")


@app.get("/no-logs")
async def no_logs_policy_page() -> Response:
    return Response(_policy_page("No-logs Policy", "Техническая политика минимизации логов VexVPN.", [
        ("No browsing logs", "VexVPN не хранит browsing history, DNS queries, destination IP/hostnames и payload traffic."),
        ("Xray logs", "Xray/Marzban переведён с debug на warning-level и без access log. Это снижает риск записи пользовательских подключений и destinations."),
        ("Application logs", "Mini App Uvicorn access logs отключены, чтобы не сохранять subscription tokens, QR query data и client IP в Docker logs."),
        ("Operational logs", "Сервис может хранить технические ошибки платежей/выдачи подписки, admin audit log, health checks и агрегированное использование трафика — без истории посещённых ресурсов."),
        ("Retention", "Рекомендуемый режим: anti-abuse hashes 30 дней, Docker/application logs 7 дней, payment records по billing/legal необходимости, VPN access logs disabled."),
    ]), media_type="text/html; charset=utf-8")


@app.get("/transparency")
async def transparency_page() -> Response:
    return Response(_policy_page("Transparency", "Честно о возможностях, ограничениях и текущей защите VexVPN.", [
        ("Current requests", "Government/data requests: <span class='ok'>0</span>. Traffic inspection: <span class='ok'>disabled/not performed</span>. Browsing logs: <span class='ok'>not stored</span>."),
        ("Network protection", "VPN транспорт использует VLESS Reality encryption. DNS/routing в Xray настроены на protected resolvers и IPv4/IPv6 routing without disabling IPv6."),
        ("Limits of anonymity", "VPN скрывает активность от провайдера пользователя и меняет внешний IP, но не защищает от login correlation, browser fingerprinting, cookies, compromised devices или добровольного входа в личные аккаунты."),
        ("User safety tips", "Используйте Kill Switch/Always-on VPN в клиенте, обновляйте приложение, проверяйте DNS/WebRTC leaks, не передавайте subscription link другим людям, используйте device reset при подозрении на утечку."),
        ("Next privacy upgrades", "Random VPN identity вместо tg_* usernames, encrypted sensitive DB fields, privacy score, leak-check page, suspicious usage alerts и optional multi-hop nodes."),
    ]), media_type="text/html; charset=utf-8")


@app.get("/api/qr.svg")
async def qr_svg(request: Request, data: str = Query(..., min_length=1, max_length=1200)) -> Response:
    """SVG QR-код для ссылки подписки без внешних сервисов."""
    _require_public_ip_rate_limit(request, "qr", PUBLIC_QR_RATE)
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(data, image_factory=factory, box_size=12, border=2)
    return Response(content=img.to_string(encoding="unicode"), media_type="image/svg+xml")


@app.get("/api/qr.png")
async def qr_png(request: Request, data: str = Query(..., min_length=1, max_length=1200)) -> Response:
    """PNG QR-код подписки (для скачивания), без внешних сервисов (pypng)."""
    _require_public_ip_rate_limit(request, "qr", PUBLIC_QR_RATE)
    import io

    from qrcode.image.pure import PyPNGImage

    img = qrcode.make(data, image_factory=PyPNGImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Content-Disposition": "attachment; filename=vexvpn_qr.png"},
    )


MSK = timezone(timedelta(hours=3))


def _sub_token(url: str) -> str:
    return urlparse(url).path.rstrip('/').split('/')[-1]


def _looks_like_browser(request: Request) -> bool:
    accept = request.headers.get('accept', '').lower()
    ua = request.headers.get('user-agent', '').lower()
    if 'text/html' in accept:
        return True
    return any(x in ua for x in ('mozilla', 'safari', 'chrome', 'firefox', 'telegrambot'))


def _looks_like_happ(request: Request) -> bool:
    ua = request.headers.get('user-agent', '').lower()
    return 'happ' in ua


def _clean_vless_link_for_happ(link: str) -> str:
    """Happ is stricter than Hiddify: remove Vision flow and empty Marzban params."""
    parsed = urlparse(link.strip())
    if parsed.scheme != 'vless':
        return link.strip()
    keep = []
    drop_empty = {'path', 'host', 'headerType', 'flow', 'spx'}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key == 'flow':
            continue
        if key in drop_empty and not value:
            continue
        keep.append((key, value))
    query = urlencode(keep)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment))


def _happ_plain_subscription(content: bytes) -> bytes | None:
    """Convert Marzban base64 subscription to plain cleaned vless links for Happ."""
    raw = content.strip()
    if not raw:
        return None
    text = raw.decode('utf-8', errors='ignore')
    if 'vless://' not in text:
        try:
            text = base64.b64decode(raw + b'=' * ((4 - len(raw) % 4) % 4), validate=False).decode('utf-8', errors='ignore')
        except Exception:
            return None
    links = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('vless://'):
            links.append(_clean_vless_link_for_happ(line))
    if not links:
        return None
    return ('\n'.join(links) + '\n').encode('utf-8')



def _status_page_html(*, token: str, raw_url: str, public_url: str, subscription: dict | None, raw_ok: bool, servers_online: int | None) -> str:
    now = datetime.now(timezone.utc)
    sub = subscription or {}
    expire = sub.get('expire_at')
    expire_dt = datetime.fromisoformat(expire.replace('Z', '+00:00')) if isinstance(expire, str) else expire
    if expire_dt and expire_dt.tzinfo is None:
        expire_dt = expire_dt.replace(tzinfo=timezone.utc)
    active = bool(expire_dt and expire_dt > now and sub.get('status', 'active') == 'active')
    left = human_left(expire_dt) if expire_dt else 'неизвестно'
    expire_msk = expire_dt.astimezone(MSK).strftime('%d.%m.%Y %H:%M МСК') if expire_dt else 'неизвестно'
    used = int(sub.get('used_traffic') or 0)
    limit = int(sub.get('data_limit') or sub.get('traffic_limit') or 0)
    remaining = max(0, limit - used) if limit else 0
    traffic_line = f"{fmt_size(used)} использовано · {'∞ безлимит' if not limit else fmt_size(remaining) + ' осталось'}"
    server_state = 'online' if raw_ok and (servers_online or 1) else 'checking'
    dot_color = '#35d07f' if active else '#ff5263'
    safe_public = html.escape(public_url)
    qr_url = f"/api/qr.svg?data={quote(public_url, safe='')}"
    hiddify = f"hiddify://import/{quote(public_url, safe='')}"
    status_title = 'VPN работает' if active else 'VPN не активен'
    status_text = 'Активна' if active else 'Не активна / истекла'
    css = (
        "body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;background:radial-gradient(900px 500px at 10% 0%,rgba(34,158,217,.35),transparent),linear-gradient(135deg,#050816,#11182c 55%,#07101f);color:#f7fbff;min-height:100vh}"
        "a{color:inherit;text-decoration:none}.wrap{max-width:980px;margin:0 auto;padding:28px 16px 42px}.hero{display:grid;grid-template-columns:1.15fr .85fr;gap:18px;align-items:stretch}.card{border:1px solid rgba(255,255,255,.14);background:linear-gradient(180deg,rgba(255,255,255,.11),rgba(255,255,255,.055));box-shadow:0 30px 80px rgba(0,0,0,.28);border-radius:28px;padding:24px}.eyebrow{color:#9ee7ff;font-size:13px;text-transform:uppercase;letter-spacing:.12em}h1{font-size:clamp(32px,6vw,62px);line-height:.96;margin:14px 0}.subtitle,.muted,.step p{color:#9fb0c8;line-height:1.6}.status{display:flex;align-items:center;gap:10px;font-weight:800;font-size:18px}.dot{width:12px;height:12px;border-radius:50%;display:inline-block;background:" + dot_color + ";box-shadow:0 0 20px " + dot_color + "}.stats,.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:18px}.stat,.step{border:1px solid rgba(255,255,255,.14);border-radius:18px;padding:16px;background:rgba(255,255,255,.055)}.stat b{display:block;font-size:20px}.stat span{color:#9fb0c8;font-size:12px;text-transform:uppercase;letter-spacing:.08em}.qr{display:grid;place-items:center;text-align:center}.qr img{width:220px;max-width:100%;background:white;border-radius:20px;padding:12px}.btns{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}.btn{border:1px solid rgba(255,255,255,.14);border-radius:14px;padding:12px 15px;background:rgba(255,255,255,.08);font-weight:800}.primary{background:linear-gradient(135deg,#229ed9,#7c5cff);border:0}.ok{background:linear-gradient(135deg,#18c074,#35d07f);border:0;color:#03130b}.section{margin-top:18px}code{display:block;word-break:break-all;border:1px solid rgba(255,255,255,.14);background:#050816;border-radius:14px;padding:12px;color:#bdefff}.warn{border-color:rgba(255,197,66,.35);background:rgba(255,197,66,.08)}@media(max-width:760px){.hero,.steps,.stats{grid-template-columns:1fr}.card{border-radius:22px;padding:18px}}"
    )
    parts = [
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>VexVPN subscription</title><style>" + css + "</style></head><body><main class='wrap'>",
        f"<section class='hero'><article class='card'><span class='eyebrow'>🚀 VexVPN subscription</span><h1>{html.escape(status_title)}</h1><p class='subtitle'>Статус подписки, остаток дней, трафик и инструкция подключения для Hiddify, Happ Proxy Network и Incy.</p><div class='status'><i class='dot'></i>{html.escape(status_text)} · осталось {html.escape(left)}</div><div class='stats'><div class='stat'><b>{html.escape(expire_msk)}</b><span>активна до</span></div><div class='stat'><b>{html.escape(traffic_line)}</b><span>трафик</span></div><div class='stat'><b>{html.escape(server_state)}</b><span>сервер</span></div></div><div class='btns'><a class='btn primary' href='{safe_public}?raw=1'>Скачать конфиг</a><a class='btn ok' href='{html.escape(hiddify)}'>Открыть в Hiddify</a><a class='btn' href='{safe_public}?happ=1'>Конфиг для Happ</a><button class='btn' onclick=\"navigator.clipboard.writeText('{safe_public}?happ=1');this.textContent='Happ ссылка скопирована'\">Скопировать Happ</button></div></article><article class='card qr'><img src='{qr_url}' alt='QR'><p class='muted'>Отсканируй QR в приложении VPN или скопируй ссылку подписки.</p></article></section>",
        f"<section class='card section'><h2>Инструкция подключения</h2><div class='steps'><div class='step'><b>1. Hiddify</b><p>Импортируй обычную ссылку подписки.</p></div><div class='step'><b>2. Happ Proxy Network</b><p>Используй ссылку с <code>?happ=1</code> или кнопку “Конфиг для Happ”. Она отдаёт plain VLESS без base64 и без XTLS Vision flow.</p></div><div class='step'><b>3. Incy / другие клиенты</b><p>Если клиент не поддерживает VLESS Reality — нужен отдельный Trojan/VMess compatible сервер.</p></div></div><p class='muted'>Обычная ссылка:</p><code>{safe_public}</code><p class='muted'>Happ-compatible ссылка:</p><code>{safe_public}?happ=1</code></section>",
        "<section class='card section warn'><b>Если всё равно не подключается</b><p class='muted'>Тогда клиент, вероятно, не поддерживает VLESS Reality. Следующий шаг — добавить второй compatible inbound: Trojan или VMess WebSocket TLS.</p></section></main></body></html>",
    ]
    return ''.join(parts)


@app.get('/sub/{token}')
async def subscription_gateway(token: str, request: Request, raw: int = Query(0), happ: int = Query(0)) -> Response:
    """Публичная ссылка подписки: браузеру красивая страница, VPN-клиенту raw Marzban config."""
    _require_public_ip_rate_limit(request, "subscription", PUBLIC_SUB_RATE)
    token = token.strip().strip('/')[:512]
    raw_url = f"{settings.marzban_base_url.rstrip('/')}/sub/{token}"
    public_url = f"{settings.sub_public_base}/sub/{token}"
    want_html = _looks_like_browser(request) and not raw and not happ
    raw_resp = None
    raw_ok = False
    try:
        async with httpx.AsyncClient(timeout=8.0, verify=_marzban_tls_verify()) as client:
            raw_resp = await client.get(raw_url, headers={'user-agent': request.headers.get('user-agent', 'VexVPN-Gateway/1.0')})
        raw_ok = raw_resp.status_code == 200
    except Exception:
        raw_resp = None
    if not want_html:
        if not raw_resp:
            raise HTTPException(status_code=502, detail='subscription upstream unavailable')
        headers = {k: v for k, v in raw_resp.headers.items() if k.lower() in {'subscription-userinfo', 'profile-title', 'profile-update-interval', 'support-url'}}
        headers['profile-web-page-url'] = public_url
        if happ or _looks_like_happ(request):
            happ_body = _happ_plain_subscription(raw_resp.content)
            if happ_body:
                headers['profile-title'] = 'base64:VmV4VlBOIEhhcHA='
                return Response(content=happ_body, status_code=200, media_type='text/plain; charset=utf-8', headers=headers)
        return Response(content=raw_resp.content, status_code=raw_resp.status_code, media_type=raw_resp.headers.get('content-type', 'text/plain'), headers=headers)

    subscription = None
    servers_online = None
    async with session_maker() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.subscription_url.like(f'%/sub/{token}%')))
    if sub:
        usage = await marzban.get_usage(sub.telegram_id, max_age=10.0) or {}
        servers_online = await marzban.servers_online()
        subscription = {'expire_at': sub.expire_at, 'status': usage.get('status') or 'active', 'used_traffic': usage.get('used_traffic') or 0, 'data_limit': usage.get('data_limit') or sub.traffic_limit, 'traffic_limit': sub.traffic_limit}
    html_page = _status_page_html(token=token, raw_url=raw_url, public_url=public_url, subscription=subscription, raw_ok=raw_ok, servers_online=servers_online)
    return Response(content=html_page, media_type='text/html; charset=utf-8')


@app.get('/happ/{token}')
async def happ_subscription_gateway(token: str, request: Request) -> Response:
    """Always return a Happ-friendly plain VLESS subscription."""
    return await subscription_gateway(token=token, request=request, raw=1, happ=1)


@app.post("/api/device/reset")
async def device_reset(tg_user: dict = Depends(telegram_user)) -> dict:
    """Self-service «сменить устройство»: перевыпуск ссылки подписки (revoke_sub).

    Старые конфиги на прежних устройствах перестают работать.
    """
    telegram_id = int(tg_user["id"])
    require_rate_limit(f"device-reset:{telegram_id}", DEVICE_RESET_RATE)
    async with session_maker() as session:
        sub = await get_subscription(session, telegram_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Активная подписка не найдена")
    try:
        new_url = await marzban.revoke_sub(telegram_id)
    except MarzbanError as exc:
        raise HTTPException(status_code=502, detail="Не удалось сбросить профиль, попробуй позже") from exc
    async with session_maker() as session:
        sub = await get_subscription(session, telegram_id)
        if sub and new_url:
            sub.subscription_url = new_url
            sub.reset_count = int(getattr(sub, "reset_count", 0) or 0) + 1
            sub.last_reset_at = datetime.now(timezone.utc)
            session.add(SecurityEvent(telegram_id=telegram_id, kind="link_reset", severity="info", title="VPN-ссылка перевыпущена", details="Old subscription token revoked from Mini App."))
            await session.commit()
    return {"ok": True, "subscription_url": new_url}


@app.get("/api/security")
async def security_status(tg_user: dict = Depends(telegram_user)) -> dict:
    telegram_id = int(tg_user["id"])
    async with session_maker() as session:
        sub = await get_subscription(session, telegram_id)
        device_count = await session.scalar(select(func.count(MiniAppDevice.id)).where(MiniAppDevice.telegram_id == telegram_id)) or 0
        events = list(await session.scalars(
            select(SecurityEvent)
            .where(SecurityEvent.telegram_id == telegram_id)
            .order_by(SecurityEvent.created_at.desc())
            .limit(10)
        ))
    plan = PLANS.get(sub.plan) if sub else None
    device_limit = plan.devices if plan else 1
    warnings = []
    if device_count > device_limit:
        warnings.append("Mini App opened from more devices than the plan limit. If this was not you, reset the VPN link.")
    if sub and getattr(sub, "reset_count", 0):
        warnings.append("VPN link was reset before; old QR/configs should be considered invalid.")
    return {
        "ok": True,
        "device_count": int(device_count),
        "device_limit": int(device_limit),
        "reset_count": int(getattr(sub, "reset_count", 0) or 0) if sub else 0,
        "last_reset_at": sub.last_reset_at.isoformat() if sub and sub.last_reset_at else None,
        "protections": [
            "No browsing/DNS/destination logs",
            "Mini App access logs disabled",
            "Subscription link reset available",
            "Traffic spike alerts without raw IP storage",
            "Egress blocks private networks, SMTP abuse and BitTorrent",
            "IPv4/IPv6 routing enabled without disabling IPv6",
        ],
        "warnings": warnings,
        "events": [
            {"kind": e.kind, "severity": e.severity, "title": e.title, "details": e.details, "created_at": e.created_at.isoformat() if e.created_at else None}
            for e in events
        ],
    }


@app.get("/api/config")
async def config() -> dict:
    from bot.db.models import TariffSetting
    from bot.db.repo import ensure_tariff_settings

    async with session_maker() as session:
        await ensure_tariff_settings(session, PLANS)
        tariffs = list(await session.scalars(select(TariffSetting).order_by(TariffSetting.key)))
        promos = await list_promos(session)
    plans = []
    for t in tariffs:
        if not t.visible:
            continue
        p = Plan(
            key=t.key,
            title=t.title,
            days=t.days,
            stars=t.stars,
            traffic_gb=t.traffic_gb,
            devices=t.devices,
            badge=t.badge or "",
            visible=t.visible,
            is_trial=t.is_trial,
            traffic_only=t.traffic_only,
            unlimited=t.unlimited,
        )
        plans.append({
            "key": p.key,
            "title": p.title,
            "days": p.days,
            "stars": p.stars,
            "traffic": p.traffic_label,
            "traffic_gb": p.traffic_gb,
            "devices": p.devices,
            "devices_label": p.devices_label,
            "badge": p.badge,
            "is_trial": p.is_trial,
            "traffic_only": p.traffic_only,
        })
    return {
        "bot_username": await _resolve_bot_username(),
        "support_username": settings.support_username,
        "promo_banner": PROMO_BANNER if PROMO_BANNER.get("enabled") else None,
        "plans": plans,
        "promos": [
            {"code": promo.code, "title": promo.title, "percent": promo.percent, "free": bool(promo.free_plan_key or promo.kind in {"days", "traffic", "trial"})}
            for promo in promos
        ],
    }


@app.get("/api/me")
async def me(request: Request, tg_user: dict = Depends(telegram_user)) -> dict:
    telegram_id = int(tg_user["id"])
    async with session_maker() as session:
        await ensure_user(session, telegram_id, tg_user.get("username"))
        fp_hash, ip_hash = await _record_open_from_request(session, telegram_id, tg_user, request)
        user = await get_user(session, telegram_id)
        sub = await get_subscription(session, telegram_id)
        payments = list(
            await session.scalars(
                select(Payment).where(Payment.telegram_id == telegram_id).order_by(Payment.created_at.desc()).limit(10)
            )
        )
        promo_uses = list(
            await session.scalars(
                select(PromoUse).where(PromoUse.telegram_id == telegram_id).order_by(PromoUse.created_at.desc()).limit(10)
            )
        )
        tickets = list(
            await session.scalars(
                select(SupportTicket)
                .where(SupportTicket.telegram_id == telegram_id)
                .order_by(SupportTicket.created_at.desc())
                .limit(5)
            )
        )
        referral_total = await session.scalar(
            select(func.count(User.id)).where(User.referred_by == telegram_id)
        ) or 0
        referral_rewarded = await session.scalar(
            select(func.count(User.id)).where(User.referred_by == telegram_id, User.referral_rewarded.is_(True))
        ) or 0
        usage_rows = await get_usage_history(session, telegram_id) if sub else []
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_claim = await session.scalar(select(DailyFreeClaim).where(DailyFreeClaim.telegram_id == telegram_id, DailyFreeClaim.day == today))
        device_count = await session.scalar(select(func.count(MiniAppDevice.id)).where(MiniAppDevice.telegram_id == telegram_id)) or 0
        fp_accounts_today = 0
        ip_accounts_today = 0
        if fp_hash:
            fp_accounts_today = await session.scalar(
                select(func.count(func.distinct(DailyFreeClaim.telegram_id))).where(
                    DailyFreeClaim.day == today,
                    DailyFreeClaim.fingerprint_hash == fp_hash,
                    DailyFreeClaim.status == "success",
                )
            ) or 0
        if ip_hash:
            ip_accounts_today = await session.scalar(
                select(func.count(func.distinct(DailyFreeClaim.telegram_id))).where(
                    DailyFreeClaim.day == today,
                    DailyFreeClaim.ip_hash == ip_hash,
                    DailyFreeClaim.status == "success",
                )
            ) or 0

    now = datetime.now(timezone.utc)
    subscription = None
    if sub:
        expire = sub.expire_at if sub.expire_at.tzinfo else sub.expire_at.replace(tzinfo=timezone.utc)
        plan = PLANS.get(sub.plan)
        total_days = plan.days if plan else 30
        left_seconds = max(0, int((expire - now).total_seconds()))
        progress = max(0, min(100, round(left_seconds / max(1, total_days * 86400) * 100)))
        subscription = {
            "active": expire > now,
            "plan": plan.title if plan else sub.plan,
            "plan_key": sub.plan,
            "usage_history": [{"day": r.day, "used": r.used_traffic} for r in usage_rows],
            "expire": fmt_date(expire),
            "expire_at": expire.isoformat(),
            "server_now": now.isoformat(),
            "left": human_left(expire),
            "left_seconds": left_seconds,
            "left_days": max(0, left_seconds // 86400),
            "traffic": fmt_traffic(sub.traffic_limit),
            "traffic_limit": sub.traffic_limit,
            "subscription_url": sub.subscription_url,
            "progress": progress,
        }

        # Live-остаток трафика из Marzban (best-effort): использовано/осталось.
        usage = await marzban.get_usage(telegram_id)
        if usage:
            live_limit = usage["data_limit"]
            used = usage["used_traffic"]
            subscription["traffic_used"] = used
            subscription["traffic_used_label"] = fmt_size(used)
            if live_limit:
                remaining = max(0, live_limit - used)
                subscription["traffic_limit"] = live_limit
                subscription["traffic"] = fmt_traffic(live_limit)
                subscription["traffic_remaining"] = remaining
                subscription["traffic_remaining_label"] = fmt_size(remaining)
            else:
                subscription["traffic_remaining_label"] = "∞ безлимит"

    bot_username = await _resolve_bot_username()
    referral_link = f"https://t.me/{bot_username}?start=ref_{telegram_id}"
    return {
        "user": {
            "id": telegram_id,
            "username": tg_user.get("username"),
            "first_name": tg_user.get("first_name"),
            "photo_url": tg_user.get("photo_url"),
            "trial_used": bool(user.trial_used) if user else False,
            "active_promo_code": user.active_promo_code if user else None,
        },
        "subscription": subscription,
        "payments": [
            {
                "plan": PLANS.get(p.plan).title if PLANS.get(p.plan) else p.plan,
                "stars": p.stars_amount,
                "promo": p.promo_code,
                "status": p.status,
                "date": fmt_date(p.created_at),
                "charge_id": p.charge_id[:12] + "…" if len(p.charge_id) > 12 else p.charge_id,
            }
            for p in payments
        ],
        "promo_history": [
            {
                "code": pu.code,
                "title": PROMOS.get(pu.code).title if PROMOS.get(pu.code) else "Промокод",
                "status": "used",
                "date": fmt_date(pu.created_at),
            }
            for pu in promo_uses
        ],
        "tickets": [
            {
                "id": t.id,
                "topic": t.platform or "support",
                "status": "open" if t.status == "new" else t.status,
                "date": fmt_date(t.created_at),
            }
            for t in tickets
        ],
        "referral": {
            "total": referral_total,
            "rewarded": referral_rewarded,
            "bonus_days": REFERRAL_BONUS_DAYS,
        },
        "daily_free": {
            "available_today": not bool(daily_claim and daily_claim.status in {"pending", "success"}),
            "claimed_today": bool(daily_claim and daily_claim.status == "success"),
            "pending": bool(daily_claim and daily_claim.status == "pending"),
            "blocked": bool(daily_claim and daily_claim.status == "blocked"),
            "reason": daily_claim.reason if daily_claim else None,
            "next_seconds": _seconds_until_next_utc_day(now),
            "reward_days": 1,
            "reward_traffic_gb": 100,
            "device_count": device_count,
            "max_devices": MAX_FREE_DEVICES_PER_USER,
            "device_accounts_today": fp_accounts_today,
            "ip_accounts_today": ip_accounts_today,
        },
        "referral_link": referral_link,
        "bot_links": {
            "buy": f"https://t.me/{bot_username}?start=buy",
            "support": f"https://t.me/{bot_username}",
        },
    }


@app.post("/api/daily-free/claim")
async def daily_free_claim(payload: FingerprintIn, request: Request, tg_user: dict = Depends(telegram_user)) -> dict:
    """Выдать бесплатный VPN на сегодня только из MiniApp с fingerprint/IP антиабузом."""
    telegram_id = int(tg_user["id"])
    require_rate_limit(f"daily-free:{telegram_id}", GAMI_RATE)
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")

    async with session_maker() as session:
        await ensure_user(session, telegram_id, tg_user.get("username"))
        fp_hash, ip_hash = await _record_open_from_request(session, telegram_id, tg_user, request, payload)
        user = await get_user(session, telegram_id)
        existing_claim = await session.scalar(select(DailyFreeClaim).where(DailyFreeClaim.telegram_id == telegram_id, DailyFreeClaim.day == day))
        if existing_claim and existing_claim.status == "success":
            raise HTTPException(status_code=409, detail="Бесплатный VPN на сегодня уже получен. Ниже можно выбрать платный тариф.")
        if existing_claim and existing_claim.status == "pending":
            raise HTTPException(status_code=409, detail="Бесплатная выдача уже обрабатывается, проверь подписку через минуту.")

        risk = 0
        reasons: list[str] = []
        if not fp_hash:
            risk += 60; reasons.append("нет MiniApp fingerprint")
        if not tg_user.get("username"):
            risk += 10; reasons.append("нет username")
        first_seen = user.created_at if user and user.created_at else now
        first_seen = first_seen if first_seen.tzinfo else first_seen.replace(tzinfo=timezone.utc)
        age_minutes = max(0, int((now - first_seen).total_seconds() // 60))
        if age_minutes < MIN_LOW_RISK_AGE_MINUTES:
            risk += 15; reasons.append(f"новый пользователь {age_minutes} мин")
        events = await session.scalar(select(func.count(BotEvent.id)).where(BotEvent.telegram_id == telegram_id)) or 0
        if events < 1:
            risk += 10; reasons.append("мало активности")
        referral_total = await session.scalar(select(func.count(User.id)).where(User.referred_by == telegram_id)) or 0
        if referral_total >= 10:
            risk += 10; reasons.append(f"много рефералов: {referral_total}")

        device_count = await session.scalar(select(func.count(MiniAppDevice.id)).where(MiniAppDevice.telegram_id == telegram_id)) or 0
        if device_count > MAX_FREE_DEVICES_PER_USER:
            reasons.append(f"у аккаунта устройств {device_count}/{MAX_FREE_DEVICES_PER_USER}")
            await _log_abuse_flag_and_alert(session, telegram_id=telegram_id, kind="too_many_devices", severity="block", fingerprint_hash=fp_hash, ip_hash=ip_hash, details="; ".join(reasons))
            raise HTTPException(status_code=403, detail="Free VPN доступен максимум на 3 устройства. Напиши в поддержку, если это ошибка.")

        fp_accounts_today = 0
        ip_accounts_today = 0
        if fp_hash:
            fp_accounts_today = await session.scalar(
                select(func.count(func.distinct(DailyFreeClaim.telegram_id))).where(
                    DailyFreeClaim.day == day,
                    DailyFreeClaim.fingerprint_hash == fp_hash,
                    DailyFreeClaim.status == "success",
                    DailyFreeClaim.telegram_id != telegram_id,
                )
            ) or 0
        if ip_hash:
            ip_accounts_today = await session.scalar(
                select(func.count(func.distinct(DailyFreeClaim.telegram_id))).where(
                    DailyFreeClaim.day == day,
                    DailyFreeClaim.ip_hash == ip_hash,
                    DailyFreeClaim.status == "success",
                    DailyFreeClaim.telegram_id != telegram_id,
                )
            ) or 0
        if fp_accounts_today >= MAX_FREE_ACCOUNTS_PER_DEVICE_DAY:
            details = f"device accounts today={fp_accounts_today}; " + "; ".join(reasons)
            await _log_abuse_flag_and_alert(session, telegram_id=telegram_id, kind="device_farm", severity="block", fingerprint_hash=fp_hash, ip_hash=ip_hash, details=details)
            raise HTTPException(status_code=403, detail="С этого устройства уже получали бесплатный VPN на несколько аккаунтов. Доступ ограничен.")
        if ip_accounts_today >= MAX_FREE_ACCOUNTS_PER_IP_DAY:
            details = f"ip accounts today={ip_accounts_today}; " + "; ".join(reasons)
            await log_abuse_flag(session, telegram_id=telegram_id, kind="ip_farm", severity="warn", fingerprint_hash=fp_hash, ip_hash=ip_hash, details=details)
        if risk >= 70:
            await _log_abuse_flag_and_alert(session, telegram_id=telegram_id, kind="high_risk_free", severity="block", fingerprint_hash=fp_hash, ip_hash=ip_hash, details="; ".join(reasons))
            raise HTTPException(status_code=403, detail="Не удалось проверить устройство для бесплатного VPN. Открой MiniApp из Telegram и попробуй ещё раз.")
        if risk >= 25 or fp_accounts_today or ip_accounts_today >= 2:
            await log_abuse_flag(session, telegram_id=telegram_id, kind="risky_free", severity="warn", fingerprint_hash=fp_hash, ip_hash=ip_hash, details=f"risk={risk}; device_today={fp_accounts_today}; ip_today={ip_accounts_today}; " + "; ".join(reasons))

        if existing_claim and existing_claim.status in {"error", "blocked"}:
            existing_claim.status = "pending"
            existing_claim.reason = None
            existing_claim.risk_score = risk
            existing_claim.fingerprint_hash = fp_hash
            existing_claim.ip_hash = ip_hash
            await session.commit()
            claim = existing_claim
        else:
            claim = await reserve_daily_free_claim(session, telegram_id=telegram_id, day=day, fingerprint_hash=fp_hash, ip_hash=ip_hash, risk_score=risk)
        if claim is None:
            raise HTTPException(status_code=409, detail="Бесплатный VPN на сегодня уже получен или обрабатывается.")
        claim_id = claim.id
        charge_id = f"DAILY-FREE-{day}-{telegram_id}"
        payment = await add_payment(session, telegram_id=telegram_id, plan=DAILY_FREE_PLAN.key, stars_amount=0, charge_id=charge_id, status="pending")
        if getattr(payment, "_was_created", True) is False and payment.status == "success":
            await set_daily_free_claim_status(session, claim_id, "success", "payment already success")
            raise HTTPException(status_code=409, detail="Бесплатный VPN на сегодня уже получен.")

    try:
        result = await marzban.create_or_renew(telegram_id, DAILY_FREE_PLAN)
    except MarzbanError as exc:
        async with session_maker() as session:
            await update_payment_status(session, payment.id, "marzban_error", str(exc))
            await set_daily_free_claim_status(session, claim_id, "error", str(exc))
            await enqueue_grant(session, telegram_id=telegram_id, payment_id=payment.id, charge_id=charge_id, plan=DAILY_FREE_PLAN, stars_amount=0, last_error=str(exc))
            await log_marzban(session, telegram_id, "daily_free", "error", str(exc))
        raise HTTPException(status_code=502, detail="VPN-сервер временно не выдал доступ. Выдача поставлена в очередь и повторится автоматически.") from exc

    expire_at = datetime.fromtimestamp(result["expire"], tz=timezone.utc)
    async with session_maker() as session:
        existing_sub = await get_subscription(session, telegram_id)
        store_plan_key = existing_sub.plan if existing_sub and existing_sub.plan else DAILY_FREE_PLAN.key
        await finalize_grant(
            session,
            telegram_id=telegram_id,
            marzban_username=result["username"],
            subscription_url=result["subscription_url"],
            plan_key=store_plan_key,
            expire_at=expire_at,
            traffic_limit=result["data_limit"],
            is_trial=False,
            clear_active_promo=False,
            payment_id=payment.id,
            log_message=f"daily_free_webapp={day}; payment_id={payment.id}; risk={risk}",
            log_paid=False,
        )
        await set_daily_free_claim_status(session, claim_id, "success")
    return {"ok": True, "message": "Бесплатный VPN активирован: +1 день и +100 ГБ", "expire_at": expire_at.isoformat(), "traffic": fmt_traffic(result["data_limit"]), "next_seconds": _seconds_until_next_utc_day()}


@app.post("/api/promo")
async def apply_promo(payload: PromoIn, tg_user: dict = Depends(telegram_user)) -> dict:
    telegram_id = int(tg_user["id"])
    require_rate_limit(f"promo:{telegram_id}", PROMO_RATE)

    code = payload.code.strip().upper()
    async with session_maker() as session:
        promo = await get_promo(session, code)
    if not promo:
        raise HTTPException(status_code=404, detail="Промокод не найден")

    async with session_maker() as session:
        await ensure_user(session, telegram_id, tg_user.get("username"))
        err = await validate_promo_for_user(session, telegram_id, promo)
        if err:
            raise HTTPException(status_code=409, detail=err)

    # FREE7 — моментальная бесплатная выдача. Важно: не помечаем промокод
    # использованным до успешного ответа Marzban, иначе сбой Marzban сожжёт купон.
    if promo.free_plan_key or promo.kind in {"days", "traffic", "trial"}:
        async with session_maker() as session:
            plan = await promo_to_plan(session, promo)
        if plan is None:
            raise HTTPException(status_code=404, detail="Тариф промокода не найден")

        charge_id = f"PROMO-{code}-{telegram_id}"
        async with session_maker() as session:
            existing_payment = await get_payment_by_charge_id(session, charge_id)
            if existing_payment and existing_payment.status == "success":
                raise HTTPException(status_code=409, detail="Промокод уже использован")
            if existing_payment and existing_payment.status == "pending":
                raise HTTPException(status_code=409, detail="Промокод уже обрабатывается, попробуй через минуту")
            if existing_payment and existing_payment.status == "marzban_error":
                payment = existing_payment
                await update_payment_status(session, payment.id, "pending")
            else:
                payment = await add_payment(
                    session,
                    telegram_id=telegram_id,
                    plan=plan.key,
                    stars_amount=0,
                    charge_id=charge_id,
                    promo_code=code,
                    status="pending",
                )
                if getattr(payment, "_was_created", True) is False:
                    raise HTTPException(status_code=409, detail="Промокод уже обрабатывается, попробуй через минуту")

        try:
            result = await marzban.create_or_renew(telegram_id, plan)
        except MarzbanError as exc:
            # Как и бот: фиксируем ошибку и ставим выдачу в очередь авто-ретраев,
            # чтобы WebApp-промокоды тоже дожимались при временной недоступности панели.
            async with session_maker() as session:
                await update_payment_status(session, payment.id, "marzban_error", str(exc))
                await enqueue_grant(
                    session,
                    telegram_id=telegram_id,
                    payment_id=payment.id,
                    charge_id=charge_id,
                    plan=plan,
                    stars_amount=0,
                    promo_code=code,
                    last_error=str(exc),
                )
                await log_marzban(session, telegram_id, "create_or_renew", "error", str(exc))
            raise HTTPException(status_code=502, detail="Сервер VPN временно не выдал доступ. Промокод НЕ сгорел — я поставил выдачу в очередь, она повторится автоматически.") from exc

        expire_at = datetime.fromtimestamp(result["expire"], tz=timezone.utc)
        async with session_maker() as session:
            # Атомарно (один commit), как в боте: подписка + trial_used (для trial-промо)
            # + сброс активного промо + статус платежа + логи.
            await finalize_grant(
                session,
                telegram_id=telegram_id,
                marzban_username=result["username"],
                subscription_url=result["subscription_url"],
                plan_key=plan.key,
                expire_at=expire_at,
                traffic_limit=result["data_limit"],
                is_trial=plan.is_trial,
                clear_active_promo=True,
                payment_id=payment.id,
                log_message=f"promo={code}; payment_id={payment.id}",
                log_paid=False,
            )
            # Пометка купона — отдельно (идемпотентно), чтобы гонка не откатывала выдачу.
            await mark_promo_used(session, telegram_id, code)
        return {"ok": True, "code": code, "title": promo.title, "free": True, "granted": True}

    # SALE30 — скидка на следующий счёт. Повторный ввод просто оставляет один активный купон.
    async with session_maker() as session:
        await set_active_promo(session, telegram_id, code)
    return {"ok": True, "code": code, "title": promo.title, "percent": promo.percent, "free": False, "granted": False}


@app.post("/api/invoice-link")
async def create_invoice_link(payload: InvoiceIn, tg_user: dict = Depends(telegram_user)) -> dict:
    """Создать ссылку на оплату Telegram Stars прямо для WebApp.

    WebApp затем открывает её через Telegram.WebApp.openInvoice().
    """
    telegram_id = int(tg_user["id"])
    require_rate_limit(f"invoice:{telegram_id}", INVOICE_RATE)

    async with session_maker() as session:
        plan = await get_admin_plan(session, payload.plan_key)
    if plan is None or not plan.visible:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    async with session_maker() as session:
        await ensure_user(session, telegram_id, tg_user.get("username"))
        user = await get_user(session, telegram_id)
        sub = await get_subscription(session, telegram_id)
        now = datetime.now(timezone.utc)
        if plan.is_trial and user and user.trial_used:
            raise HTTPException(status_code=409, detail="Пробный период доступен только один раз")
        if plan.traffic_only and (not sub or (sub.expire_at if sub.expire_at.tzinfo else sub.expire_at.replace(tzinfo=timezone.utc)) <= now):
            raise HTTPException(status_code=409, detail="Пакет трафика можно купить только к активной подписке")
        promo = await get_promo(session, user.active_promo_code) if user and user.active_promo_code else None
        if promo:
            err = await validate_promo_for_user(session, telegram_id, promo)
            if err:
                promo = None
                await set_active_promo(session, telegram_id, None)

    invoice_payload = pay.build_payload(plan, promo.code if promo and promo.percent else None)
    prices = pay.build_prices(plan, promo if promo and promo.percent else None)
    description = (
        f"Добавка трафика: {plan.traffic_label}. Дата окончания подписки не меняется."
        if plan.traffic_only
        else f"Тариф {plan.title}: {plan.days} дней, {plan.traffic_label}, {plan.devices_label}."
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{settings.bot_token}/createInvoiceLink",
            json={
                "title": f"VexVPN — {plan.title}",
                "description": description,
                "payload": invoice_payload,
                "currency": "XTR",
                "prices": [{"label": price.label, "amount": price.amount} for price in prices],
                "provider_token": "",
            },
        )
    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(status_code=502, detail=data.get("description", "Telegram invoice error"))
    return {"ok": True, "invoice_link": data["result"], "plan_key": plan.key, "amount": prices[0].amount}


@app.get("/api/check-vpn")
async def check_vpn(tg_user: dict = Depends(telegram_user)) -> dict:
    telegram_id = int(tg_user["id"])
    require_rate_limit(f"check-vpn:{telegram_id}", CHECK_VPN_RATE)

    async with session_maker() as session:
        sub = await get_subscription(session, telegram_id)
    if not sub:
        return {"ok": False, "status": "no_subscription", "message": "Активная подписка не найдена"}

    expire = sub.expire_at if sub.expire_at.tzinfo else sub.expire_at.replace(tzinfo=timezone.utc)
    if expire <= datetime.now(timezone.utc):
        return {"ok": False, "status": "expired", "message": "Подписка истекла"}

    if not _is_own_subscription_url(sub.subscription_url):
        return {"ok": False, "status": "unsafe_url", "message": "Ссылка подписки не принадлежит нашему VPN-серверу. Напиши в поддержку."}

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            resp = await client.get(sub.subscription_url)
        return {
            "ok": resp.status_code == 200,
            "status": "subscription_online" if resp.status_code == 200 else "subscription_error",
            "http_status": resp.status_code,
            "message": "Ссылка подписки доступна. Если приложение не подключается — обнови профиль в Hiddify/Happ." if resp.status_code == 200 else "Сервер подписки ответил ошибкой.",
        }
    except Exception as exc:
        return {"ok": False, "status": "network_error", "message": f"Не удалось проверить ссылку: {exc.__class__.__name__}"}


def _gami_http_status(code: str) -> int:
    """Маппинг кода ошибки геймификации в HTTP-статус."""
    return 409 if code in {"already", "not_eligible"} else 502


@app.get("/api/gamification")
async def gamification_status(tg_user: dict = Depends(telegram_user)) -> dict:
    telegram_id = int(tg_user["id"])
    async with session_maker() as session:
        await ensure_user(session, telegram_id, tg_user.get("username"))
    return await gami.get_status(telegram_id)


@app.post("/api/bonus/daily/claim")
async def gamification_daily_claim(tg_user: dict = Depends(telegram_user)) -> dict:
    telegram_id = int(tg_user["id"])
    require_rate_limit(f"gami-daily:{telegram_id}", GAMI_RATE)
    async with session_maker() as session:
        await ensure_user(session, telegram_id, tg_user.get("username"))
    try:
        return await gami.claim_daily(telegram_id)
    except GamificationError as exc:
        raise HTTPException(status_code=_gami_http_status(exc.code), detail=exc.message) from exc


@app.post("/api/wheel/spin")
async def gamification_wheel_spin(tg_user: dict = Depends(telegram_user)) -> dict:
    telegram_id = int(tg_user["id"])
    require_rate_limit(f"gami-wheel:{telegram_id}", GAMI_RATE)
    async with session_maker() as session:
        await ensure_user(session, telegram_id, tg_user.get("username"))
    try:
        return await gami.spin_wheel(telegram_id)
    except GamificationError as exc:
        raise HTTPException(status_code=_gami_http_status(exc.code), detail=exc.message) from exc


# ── Поддержка: тикеты с двусторонним тредом ──────────────────────────
def _fmt_dt(dt) -> str | None:
    if not dt:
        return None
    return fmt_date(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def _ticket_dict(t, messages=None) -> dict:
    d = {
        "id": t.id,
        "topic": t.topic,
        "topic_title": SUPPORT_TOPIC_TITLES.get(t.topic, "Другое"),
        "status": "open" if t.status == "new" else t.status,
        "created": _fmt_dt(t.created_at),
        "updated": _fmt_dt(t.updated_at),
        "preview": (t.message or "")[:120],
    }
    if messages is not None:
        d["messages"] = [
            {"sender": m.sender, "text": m.text, "date": _fmt_dt(m.created_at)} for m in messages
        ]
    return d


def _admin_ticket_kb(ticket_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✍️ Ответить", "callback_data": f"areply:{ticket_id}"},
        {"text": "✅ Закрыть", "callback_data": f"aclose:{ticket_id}"},
    ]]}


async def _notify_admins_ticket(kind: str, telegram_id: int, username: str | None, ticket, body: str) -> None:
    who = f"@{username}" if username else str(telegram_id)
    head = "🆘 <b>Новый тикет</b>" if kind == "new" else "💬 <b>Ответ пользователя</b>"
    text = (
        f"{head} #{ticket.id}\n"
        f"Тема: <b>{html.escape(SUPPORT_TOPIC_TITLES.get(ticket.topic, 'Другое'))}</b>\n"
        f"От: {html.escape(who)} / <code>{telegram_id}</code>\n\n"
        f"{html.escape(body[:900])}"
    )
    await notify_admins(text, _admin_ticket_kb(ticket.id))


@app.get("/api/support/tickets")
async def support_tickets(tg_user: dict = Depends(telegram_user)) -> dict:
    telegram_id = int(tg_user["id"])
    async with session_maker() as session:
        rows = await list_user_tickets(session, telegram_id)
    return {"items": [_ticket_dict(t) for t in rows], "topics": SUPPORT_TOPIC_TITLES}


@app.get("/api/support/ticket/{ticket_id}")
async def support_ticket(ticket_id: int, tg_user: dict = Depends(telegram_user)) -> dict:
    telegram_id = int(tg_user["id"])
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        if not t or t.telegram_id != telegram_id:
            raise HTTPException(status_code=404, detail="Тикет не найден")
        msgs = await get_ticket_messages(session, ticket_id)
    return _ticket_dict(t, msgs)


@app.post("/api/support/ticket")
async def support_create(payload: TicketCreateIn, tg_user: dict = Depends(telegram_user)) -> dict:
    telegram_id = int(tg_user["id"])
    require_rate_limit(f"support:{telegram_id}", SUPPORT_RATE)
    topic = payload.topic if payload.topic in SUPPORT_TOPICS else "other"
    message = (payload.message or "").strip()
    if len(message) < 3:
        raise HTTPException(status_code=400, detail="Опиши проблему чуть подробнее (минимум 3 символа)")
    async with session_maker() as session:
        await ensure_user(session, telegram_id, tg_user.get("username"))
        ticket = await create_support_ticket(session, telegram_id, topic, message[:2000])
    await _notify_admins_ticket("new", telegram_id, tg_user.get("username"), ticket, message)
    return _ticket_dict(ticket)


@app.post("/api/support/ticket/{ticket_id}/message")
async def support_reply(ticket_id: int, payload: TicketReplyIn, tg_user: dict = Depends(telegram_user)) -> dict:
    telegram_id = int(tg_user["id"])
    require_rate_limit(f"support:{telegram_id}", SUPPORT_RATE)
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустое сообщение")
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        if not t or t.telegram_id != telegram_id:
            raise HTTPException(status_code=404, detail="Тикет не найден")
        if t.status == "closed":
            await set_ticket_status(session, ticket_id, "open")
        ticket, _ = await add_support_message(session, ticket_id, "user", text[:2000])
    await _notify_admins_ticket("reply", telegram_id, tg_user.get("username"), ticket, text)
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        msgs = await get_ticket_messages(session, ticket_id)
    return _ticket_dict(t, msgs)


@app.post("/api/support/ticket/{ticket_id}/close")
async def support_close(ticket_id: int, tg_user: dict = Depends(telegram_user)) -> dict:
    telegram_id = int(tg_user["id"])
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        if not t or t.telegram_id != telegram_id:
            raise HTTPException(status_code=404, detail="Тикет не найден")
        await set_ticket_status(session, ticket_id, "closed")
    return {"ok": True}
