"""Operational safety: grant retry queue, health checks and admin alerts."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from aiogram import Bot
from sqlalchemy import text

from bot.config import Plan, settings
from bot.db.database import session_maker
from bot.db.repo import (
    due_grant_queue,
    enqueue_grant,
    finalize_grant,
    health_counters,
    log_marzban,
    mark_grant_done,
    mark_grant_failed,
    mark_grant_processing,
    mark_promo_used,
    should_send_alert,
    update_payment_status,
)
from bot.services.marzban import MarzbanError, marzban
from bot.utils import fmt_date, fmt_traffic

logger = logging.getLogger(__name__)


def plan_from_queue(row) -> Plan:
    return Plan(
        row.plan_key,
        row.plan_title,
        int(row.days or 0),
        int(row.stars_amount or 0),
        int(row.traffic_gb or 0),
        int(row.devices or 1),
        visible=False,
        is_trial=bool(row.is_trial),
        traffic_only=bool(row.traffic_only),
        unlimited=bool(row.unlimited),
    )


async def notify_admins(bot: Bot, text_msg: str) -> None:
    for admin_id in settings.admin_id_set:
        try:
            await bot.send_message(admin_id, text_msg[:3900])
        except Exception:
            logger.exception("Не удалось отправить alert админу %s", admin_id)


async def alert_once(bot: Bot, key: str, text_msg: str, *, cooldown_minutes: int = 30) -> None:
    async with session_maker() as session:
        if await should_send_alert(session, key, cooldown_minutes=cooldown_minutes, message=text_msg):
            await notify_admins(bot, text_msg)


async def process_grant_queue_once(bot: Bot, *, limit: int = 20) -> int:
    processed = 0
    async with session_maker() as session:
        due = await due_grant_queue(session, limit=limit)

    for item in due:
        async with session_maker() as session:
            row = await mark_grant_processing(session, item.id)
            if row is None:
                continue
            plan = plan_from_queue(row)
            qid = row.id
            telegram_id = row.telegram_id
            payment_id = row.payment_id
            promo_code = row.promo_code
            charge_id = row.charge_id

        try:
            result = await marzban.create_or_renew(telegram_id, plan)
            expire_at = datetime.fromtimestamp(result["expire"], tz=timezone.utc)
            async with session_maker() as session:
                await finalize_grant(
                    session,
                    telegram_id=telegram_id,
                    marzban_username=result["username"],
                    subscription_url=result["subscription_url"],
                    plan_key=plan.key,
                    expire_at=expire_at,
                    traffic_limit=result["data_limit"],
                    is_trial=plan.is_trial,
                    clear_active_promo=bool(promo_code),
                    payment_id=payment_id,
                    log_message=f"queue_id={qid}; charge_id={charge_id}; plan={plan.key}",
                    log_paid=bool(row.stars_amount and row.stars_amount > 0),
                )
                if promo_code:
                    await mark_promo_used(session, telegram_id, promo_code)
                await mark_grant_done(session, qid)
            try:
                await bot.send_message(
                    telegram_id,
                    "✅ Подписка выдана после повторной проверки.\n\n"
                    f"Тариф: <b>{plan.title}</b>\n"
                    f"До: <b>{fmt_date(expire_at)}</b>\n"
                    f"Трафик: <b>{fmt_traffic(result['data_limit'])}</b>\n\n"
                    f"Ссылка:\n<code>{result['subscription_url']}</code>",
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("Не удалось уведомить пользователя о queue success %s", telegram_id)
            processed += 1
        except MarzbanError as exc:
            async with session_maker() as session:
                row = await mark_grant_failed(session, qid, str(exc), max_attempts=5)
                if payment_id:
                    await update_payment_status(session, payment_id, "marzban_error", str(exc))
                await log_marzban(session, telegram_id, "grant_queue", "error", f"queue_id={qid}; attempts={row.attempts if row else '?'}; {exc}")
            if row and row.status == "failed":
                await alert_once(
                    bot,
                    f"grant_queue_failed:{qid}",
                    "🚨 <b>Grant queue failed</b>\n"
                    f"User: <code>{telegram_id}</code>\n"
                    f"Plan: <code>{plan.key}</code>\n"
                    f"Charge: <code>{charge_id}</code>\n"
                    f"Attempts: <b>{row.attempts}</b>\n"
                    f"Error: <code>{str(exc)[:900]}</code>",
                    cooldown_minutes=1440,
                )
        except Exception as exc:
            logger.exception("Unexpected grant_queue error")
            async with session_maker() as session:
                row = await mark_grant_failed(session, qid, str(exc), max_attempts=5)
            if row and row.status == "failed":
                await alert_once(bot, f"grant_queue_failed:{qid}", f"🚨 Grant queue failed unexpectedly: {exc}", cooldown_minutes=1440)
    return processed


async def grant_queue_loop(bot: Bot) -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await process_grant_queue_once(bot)
        except Exception:
            logger.exception("grant_queue loop crashed")
        await asyncio.sleep(60)


async def _check_http(name: str, url: str, timeout: float = 8.0) -> dict:
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(url)
        return {"name": name, "ok": 200 <= resp.status_code < 400, "status": resp.status_code, "latency_ms": round((time.perf_counter() - start) * 1000)}
    except Exception as exc:
        return {"name": name, "ok": False, "status": "error", "latency_ms": round((time.perf_counter() - start) * 1000), "error": str(exc)[:300]}


async def collect_health(bot: Bot | None = None) -> dict:
    checks = {}
    # DB
    start = time.perf_counter()
    try:
        async with session_maker() as session:
            await session.execute(text("SELECT 1"))
            counters = await health_counters(session)
        checks["db"] = {"ok": True, "latency_ms": round((time.perf_counter() - start) * 1000)}
    except Exception as exc:
        counters = {}
        checks["db"] = {"ok": False, "latency_ms": round((time.perf_counter() - start) * 1000), "error": str(exc)[:300]}

    # Telegram API
    start = time.perf_counter()
    try:
        if bot is not None:
            me = await bot.get_me()
            checks["telegram"] = {"ok": True, "latency_ms": round((time.perf_counter() - start) * 1000), "username": me.username}
        else:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(f"https://api.telegram.org/bot{settings.bot_token}/getMe")
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            checks["telegram"] = {"ok": bool(data.get("ok")), "status": resp.status_code, "latency_ms": round((time.perf_counter() - start) * 1000)}
    except Exception as exc:
        checks["telegram"] = {"ok": False, "latency_ms": round((time.perf_counter() - start) * 1000), "error": str(exc)[:300]}

    # Marzban API
    start = time.perf_counter()
    try:
        servers = await marzban.servers_online()
        checks["marzban"] = {"ok": servers is not None, "latency_ms": round((time.perf_counter() - start) * 1000), "servers_online": servers}
    except Exception as exc:
        checks["marzban"] = {"ok": False, "latency_ms": round((time.perf_counter() - start) * 1000), "error": str(exc)[:300]}

    mini_url = settings.mini_app_url.rstrip("/") + "/healthz"
    checks["miniapp"] = await _check_http("miniapp", mini_url)
    parsed = urlparse(settings.mini_app_url)
    caddy_url = f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else settings.mini_app_url
    checks["caddy_proxy"] = await _check_http("caddy_proxy", caddy_url)

    def pay_obj(p):
        return None if not p else {"id": p.id, "telegram_id": p.telegram_id, "plan": p.plan, "stars": p.stars_amount, "charge_id": p.charge_id, "date": p.created_at.isoformat() if p.created_at else None}
    def log_obj(r):
        return None if not r else {"id": r.id, "telegram_id": r.telegram_id, "action": r.action, "message": r.message, "date": r.created_at.isoformat() if r.created_at else None}

    return {
        "ok": all(v.get("ok") for v in checks.values()),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "last_successful_payment": pay_obj(counters.get("last_success_payment")),
        "last_marzban_error": log_obj(counters.get("last_marzban_error")),
        "errors_24h": {
            "marzban": counters.get("marzban_errors_24h", 0),
            "payments": counters.get("payment_errors_24h", 0),
        },
        "errors_10m": {
            "marzban": counters.get("marzban_errors_10m", 0),
            "payments": counters.get("payment_errors_10m", 0),
        },
        "grant_queue": counters.get("grant_queue", {}),
    }


async def monitoring_loop(bot: Bot) -> None:
    await asyncio.sleep(45)
    while True:
        try:
            h = await collect_health(bot)
            for key, check in h["checks"].items():
                if not check.get("ok"):
                    await alert_once(bot, f"health:{key}", f"🚨 <b>{key}</b> недоступен/ошибка\n<code>{check}</code>", cooldown_minutes=30)
            if h["errors_10m"].get("marzban", 0) >= 3:
                await alert_once(bot, "many_marzban_errors", f"🚨 Много Marzban ошибок за 10 минут: {h['errors_10m']['marzban']}", cooldown_minutes=30)
            if h["errors_10m"].get("payments", 0) >= 3:
                await alert_once(bot, "many_payment_errors", f"🚨 Много failed payments за 10 минут: {h['errors_10m']['payments']}", cooldown_minutes=30)
        except Exception:
            logger.exception("monitoring loop crashed")
        await asyncio.sleep(5 * 60)
