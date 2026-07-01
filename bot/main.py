"""Точка входа: запуск бота."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from sqlalchemy import select

from bot import keyboards, texts
from bot.config import (
    EXPIRED_NOTICE_WINDOW_MIN,
    PLANS,
    REMINDER_HOURS,
    TRAFFIC_ALERT_THRESHOLD,
    TRAFFIC_ALERT_THRESHOLDS,
    settings,
)
from bot.db.database import init_db, session_maker
from bot.db.models import SecurityEvent, Subscription
from bot.db.repo import (
    active_finite_subs,
    due_expired,
    due_reminders,
    due_unconverted_users,
    ensure_tariff_settings,
    log_event,
    mark_reminder_sent,
    record_usage_snapshot,
    reminder_already_sent,
)
from bot.handlers import admin, payments, profile, start
from bot.services.marzban import marzban
from bot.services.ops import grant_queue_loop, monitoring_loop
from bot.utils import fmt_date, fmt_size, fmt_traffic, human_left

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


MSK = timezone(timedelta(hours=3))


def fmt_date_msk(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


async def reminder_loop(bot: Bot) -> None:
    """Фоновые уведомления: скорое окончание, факт окончания, остаток трафика."""
    await asyncio.sleep(10)
    while True:
        try:
            async with session_maker() as session:
                # 1) Напоминания за 3 дня / 1 день / 3 часа до окончания.
                for hours in REMINDER_HOURS:
                    for sub in await due_reminders(session, hours):
                        try:
                            await bot.send_message(
                                sub.telegram_id,
                                texts.REMINDER.format(
                                    expire=fmt_date(sub.expire_at),
                                    left=human_left(sub.expire_at),
                                ),
                                reply_markup=keyboards.profile_menu(has_active=True),
                            )
                            await mark_reminder_sent(session, sub.telegram_id, f"{hours}h", sub.expire_at)
                        except Exception:
                            logger.exception("Не удалось отправить напоминание %s", sub.telegram_id)

                # 2) Уведомление в момент окончания подписки + кнопка продления.
                for sub in await due_expired(session, EXPIRED_NOTICE_WINDOW_MIN):
                    plan = PLANS.get(sub.plan)
                    try:
                        await bot.send_message(
                            sub.telegram_id,
                            texts.EXPIRED_NOTICE.format(plan=plan.title if plan else sub.plan),
                            reply_markup=keyboards.profile_menu(has_active=False),
                        )
                        await mark_reminder_sent(session, sub.telegram_id, "expired", sub.expire_at)
                    except Exception:
                        logger.exception("Не удалось отправить уведомление об окончании %s", sub.telegram_id)

                # 3) Уведомления о трафике: 80% и 95% использовано.
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                for sub in await active_finite_subs(session):
                    usage = await marzban.get_usage(sub.telegram_id)
                    if not usage:
                        continue
                    # суточный снимок израсходованного трафика — для графика по дням
                    used_now = int(usage["used_traffic"] or 0)
                    await record_usage_snapshot(session, sub.telegram_id, today_str, used_now)

                    # Security alert без raw IP/destination: резкий скачок трафика может означать,
                    # что subscription link попал другому человеку или устройство скомпрометировано.
                    checked_at = sub.last_usage_checked_at
                    last_used = int(sub.last_usage_bytes or 0)
                    if checked_at and checked_at.tzinfo is None:
                        checked_at = checked_at.replace(tzinfo=timezone.utc)
                    if checked_at and used_now >= last_used:
                        elapsed = max(60, int((datetime.now(timezone.utc) - checked_at).total_seconds()))
                        delta = used_now - last_used
                        alert_cooldown_ok = not sub.security_last_alert_at or (datetime.now(timezone.utc) - (sub.security_last_alert_at if sub.security_last_alert_at.tzinfo else sub.security_last_alert_at.replace(tzinfo=timezone.utc))).total_seconds() > 6 * 3600
                        # Порог: >20GB за час или >8GB за 10 минут. Это не блокировка, только предупреждение.
                        if alert_cooldown_ok and delta >= 8 * 1024**3 and (delta / elapsed) >= (20 * 1024**3 / 3600):
                            try:
                                await bot.send_message(
                                    sub.telegram_id,
                                    "🛡 <b>Security alert</b>\n\n"
                                    "Мы заметили необычно быстрый расход трафика на твоей VPN-ссылке. "
                                    "Мы не видим сайты/IP назначения, но такой скачок может означать, что ссылка попала на другое устройство.\n\n"
                                    "Если это был не ты — открой Security Center и перевыпусти VPN-ссылку.",
                                    reply_markup=keyboards.security_menu(has_active=True),
                                )
                                sub.security_last_alert_at = datetime.now(timezone.utc)
                                session.add(SecurityEvent(
                                    telegram_id=sub.telegram_id,
                                    kind="traffic_spike",
                                    severity="warning",
                                    title="Необычно быстрый расход трафика",
                                    details=f"Traffic delta: {fmt_size(delta)} in {elapsed // 60} min. No raw IP or destination stored.",
                                ))
                            except Exception:
                                logger.exception("Не удалось отправить security alert %s", sub.telegram_id)
                    sub.last_usage_bytes = used_now
                    sub.last_usage_checked_at = datetime.now(timezone.utc)

                    if not usage["data_limit"]:
                        continue
                    ratio = used_now / usage["data_limit"]
                    for threshold in TRAFFIC_ALERT_THRESHOLDS:
                        if ratio < threshold:
                            continue
                        percent = int(threshold * 100)
                        gb = usage["data_limit"] // 1024**3
                        marker = f"tr{percent}_{gb}g"[:32]
                        if await reminder_already_sent(session, sub.telegram_id, marker, sub.expire_at):
                            continue
                        remaining = max(0, usage["data_limit"] - usage["used_traffic"])
                        try:
                            await bot.send_message(
                                sub.telegram_id,
                                texts.TRAFFIC_LOW.format(
                                    percent=percent,
                                    used=fmt_size(usage["used_traffic"]),
                                    limit=fmt_traffic(usage["data_limit"]),
                                    remaining=fmt_size(remaining),
                                ),
                                reply_markup=keyboards.traffic_menu(),
                            )
                            await mark_reminder_sent(session, sub.telegram_id, marker, sub.expire_at)
                        except Exception:
                            logger.exception("Не удалось отправить уведомление о трафике %s", sub.telegram_id)

                # 4) Мягкое предложение через 1 час после /start, если покупки не было.
                for tg_id in await due_unconverted_users(session, after_minutes=60):
                    try:
                        await bot.send_message(tg_id, texts.NO_PURCHASE_OFFER, reply_markup=keyboards.no_purchase_menu())
                        await log_event(session, tg_id, "no_purchase_offer")
                    except Exception:
                        logger.exception("Не удалось отправить no_purchase_offer %s", tg_id)
        except Exception:
            logger.exception("Ошибка цикла уведомлений")
        await asyncio.sleep(30 * 60)



async def first_connect_loop(bot: Bot) -> None:
    """Отправить пользователю one-shot сообщение, когда VPN реально впервые начал использоваться."""
    await asyncio.sleep(45)
    while True:
        try:
            now = datetime.now(timezone.utc)
            async with session_maker() as session:
                subs = list(await session.scalars(
                    select(Subscription)
                    .where(Subscription.expire_at > now)
                    .where(Subscription.first_connect_notified_at.is_(None))
                    .order_by(Subscription.created_at.asc())
                    .limit(300)
                ))
                for sub in subs:
                    try:
                        usage = await marzban.get_usage(sub.telegram_id, max_age=5.0)
                    except Exception:
                        logger.exception("Не удалось проверить первое подключение %s", sub.telegram_id)
                        continue
                    if not usage:
                        continue
                    used = int(usage.get("used_traffic") or 0)
                    online_at = usage.get("online_at")
                    if used <= 0 and not online_at:
                        continue
                    connected_at = now
                    sub.first_connected_at = sub.first_connected_at or connected_at
                    sub.first_seen_traffic = max(int(sub.first_seen_traffic or 0), used)
                    try:
                        await bot.send_message(
                            sub.telegram_id,
                            "✅ <b>Всё готово — VPN работает</b>\n\n"
                            f"Пробный период активен до {fmt_date_msk(sub.expire_at)}\n\n"
                            "Продлить подписку можно в разделе «Моя подписка».",
                            reply_markup=keyboards.vpn_ready_menu(),
                        )
                        sub.first_connect_notified_at = now
                        await log_event(session, sub.telegram_id, "first_vpn_connected")
                    except Exception:
                        logger.exception("Не удалось отправить уведомление о первом подключении %s", sub.telegram_id)
                await session.commit()
        except Exception:
            logger.exception("Ошибка first_connect_loop")
        await asyncio.sleep(5 * 60)

def _init_sentry() -> None:
    """Опциональный трекинг ошибок (SENTRY_DSN + установленный sentry-sdk)."""
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.0)
    except Exception:
        logger.warning("Не удалось инициализировать Sentry", exc_info=True)


async def main() -> None:
    _init_sentry()
    logger.info("Инициализация базы данных…")
    await init_db()
    async with session_maker() as session:
        from bot.config import PLANS

        await ensure_tariff_settings(session, PLANS)

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_routers(
        start.router,
        payments.router,
        admin.router,
        profile.router,
    )

    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Кабинет", web_app=WebAppInfo(url=settings.mini_app_url))
        )
    except Exception:
        logger.exception("Не удалось установить кнопку Mini App")
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Меню / перезапуск"),
            BotCommand(command="buy", description="Купить или продлить VPN"),
            BotCommand(command="profile", description="Моя подписка и трафик"),
            BotCommand(command="promo", description="Ввести промокод"),
            BotCommand(command="help", description="Помощь и инструкция"),
        ])
    except Exception:
        logger.exception("Не удалось установить команды бота")
    logger.info("Бот @%s запущен", me.username)

    reminders = asyncio.create_task(reminder_loop(bot))
    first_connect = asyncio.create_task(first_connect_loop(bot))
    grant_queue = asyncio.create_task(grant_queue_loop(bot))
    monitoring = asyncio.create_task(monitoring_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        # Аккуратное завершение фоновой задачи и сессии бота.
        reminders.cancel()
        first_connect.cancel()
        grant_queue.cancel()
        monitoring.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reminders
        with contextlib.suppress(asyncio.CancelledError):
            await first_connect
        with contextlib.suppress(asyncio.CancelledError):
            await grant_queue
        with contextlib.suppress(asyncio.CancelledError):
            await monitoring
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено")
