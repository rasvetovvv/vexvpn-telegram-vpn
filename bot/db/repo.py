"""Функции доступа к данным."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import AdminAuditLog, AlertLog, AbuseFlag, BonusClaim, BotEvent, DailyFreeClaim, GamificationState, GrantQueue, MarzbanLog, MiniAppDevice, Payment, PromoCode, PromoUse, ReminderLog, Subscription, SupportMessage, SupportTicket, TariffSetting, UsageSnapshot, User, UserAchievement


async def ensure_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    referred_by: int | None = None,
) -> User:
    """Найти или создать пользователя, обновив username и реферала."""
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        if referred_by == telegram_id:
            referred_by = None
        user = User(telegram_id=telegram_id, username=username, referred_by=referred_by)
        session.add(user)
    else:
        if user.username != username:
            user.username = username
        if referred_by and referred_by != telegram_id and not user.referred_by:
            user.referred_by = referred_by
    user.last_activity_at = datetime.now(timezone.utc)
    await session.commit()
    return user


async def get_user(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def get_subscription(session: AsyncSession, telegram_id: int) -> Subscription | None:
    return await session.scalar(
        select(Subscription).where(Subscription.telegram_id == telegram_id)
    )


async def upsert_subscription(
    session: AsyncSession,
    *,
    telegram_id: int,
    marzban_username: str,
    subscription_url: str,
    plan: str,
    expire_at: datetime,
    traffic_limit: int,
) -> Subscription:
    """Создать или обновить подписку пользователя."""
    sub = await get_subscription(session, telegram_id)
    if sub is None:
        sub = Subscription(telegram_id=telegram_id)
        session.add(sub)
    sub.marzban_username = marzban_username
    sub.subscription_url = subscription_url
    sub.plan = plan
    sub.expire_at = expire_at
    sub.traffic_limit = traffic_limit
    await session.commit()
    return sub


async def finalize_grant(
    session: AsyncSession,
    *,
    telegram_id: int,
    marzban_username: str,
    subscription_url: str,
    plan_key: str,
    expire_at: datetime,
    traffic_limit: int,
    is_trial: bool,
    clear_active_promo: bool,
    payment_id: int | None,
    log_message: str,
    log_paid: bool,
) -> None:
    """Атомарно зафиксировать успешную выдачу (один commit вместо 4–5).

    Подписка + trial_used + сброс активного промо + статус платежа + логи — в одной
    транзакции, чтобы не было частичного состояния при падении посередине.
    Пометка promo_uses делается отдельно (идемпотентно), чтобы гонка купона не
    откатывала уже выданную подписку.
    """
    sub = await get_subscription(session, telegram_id)
    if sub is None:
        sub = Subscription(telegram_id=telegram_id)
        session.add(sub)
    sub.marzban_username = marzban_username
    sub.subscription_url = subscription_url
    sub.plan = plan_key
    sub.expire_at = expire_at
    sub.traffic_limit = traffic_limit

    user = await get_user(session, telegram_id)
    if user:
        if is_trial:
            user.trial_used = True
        if clear_active_promo:
            user.active_promo_code = None

    if payment_id is not None:
        payment = await session.get(Payment, payment_id)
        if payment:
            payment.status = "success"
            payment.error_message = None

    session.add(MarzbanLog(telegram_id=telegram_id, action="create_or_renew", status="success", message=log_message))
    if log_paid:
        session.add(BotEvent(telegram_id=telegram_id, event="paid", value=plan_key))
    await session.commit()


async def get_payment_by_charge_id(session: AsyncSession, charge_id: str) -> Payment | None:
    return await session.scalar(select(Payment).where(Payment.charge_id == charge_id))


async def add_payment(
    session: AsyncSession,
    *,
    telegram_id: int,
    plan: str,
    stars_amount: int,
    charge_id: str,
    promo_code: str | None = None,
    status: str = "success",
    error_message: str | None = None,
) -> Payment:
    payment = Payment(
        telegram_id=telegram_id,
        plan=plan,
        stars_amount=stars_amount,
        charge_id=charge_id,
        promo_code=promo_code,
        status=status,
        error_message=error_message,
    )
    session.add(payment)
    try:
        await session.commit()
        payment._was_created = True
        return payment
    except IntegrityError:
        await session.rollback()
        existing = await get_payment_by_charge_id(session, charge_id)
        if existing:
            existing._was_created = False
            return existing
        raise


async def update_payment_status(session: AsyncSession, payment_id: int, status: str, error_message: str | None = None) -> None:
    payment = await session.get(Payment, payment_id)
    if payment:
        payment.status = status
        payment.error_message = error_message
        await session.commit()


async def set_trial_used(session: AsyncSession, telegram_id: int) -> None:
    user = await get_user(session, telegram_id)
    if user:
        user.trial_used = True
        await session.commit()


async def set_active_promo(session: AsyncSession, telegram_id: int, code: str | None) -> None:
    user = await get_user(session, telegram_id)
    if user:
        user.active_promo_code = code
        await session.commit()


async def mark_promo_used(session: AsyncSession, telegram_id: int, code: str) -> bool:
    session.add(PromoUse(telegram_id=telegram_id, code=code.upper()))
    try:
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        return False


async def has_used_promo(session: AsyncSession, telegram_id: int, code: str) -> bool:
    used = await session.scalar(
        select(PromoUse.id).where(
            PromoUse.telegram_id == telegram_id,
            PromoUse.code == code.upper(),
        )
    )
    return used is not None


async def reward_referral_once(session: AsyncSession, telegram_id: int, max_per_day: int = 5) -> int | None:
    """Вернуть telegram_id реферера, если ему ещё надо начислить бонус.

    Анти-фрод: реферал помечается «начисленным» в любом случае (один раз), но если
    реферер уже получил max_per_day бонусов за сутки — бонус не выдаём (None).
    """
    user = await get_user(session, telegram_id)
    if not user or not user.referred_by or user.referral_rewarded:
        return None

    since = datetime.now(timezone.utc) - timedelta(days=1)
    rewarded_recently = await session.scalar(
        select(func.count(User.id)).where(
            User.referred_by == user.referred_by,
            User.referral_rewarded.is_(True),
            User.created_at >= since,
        )
    ) or 0

    user.referral_rewarded = True
    await session.commit()

    if rewarded_recently >= max_per_day:
        return None
    return user.referred_by


async def get_stats(session: AsyncSession) -> dict:
    """Статистика для админ-панели."""
    now = datetime.now(timezone.utc)

    total_users = await session.scalar(select(func.count(User.id))) or 0
    total_payments = await session.scalar(select(func.count(Payment.id))) or 0
    total_stars = await session.scalar(select(func.coalesce(func.sum(Payment.stars_amount), 0))) or 0
    active_subs = await session.scalar(
        select(func.count(Subscription.id)).where(Subscription.expire_at > now)
    ) or 0
    support_new = await session.scalar(
        select(func.count(SupportTicket.id)).where(SupportTicket.status.in_(["new", "open"]))
    ) or 0
    errors = await session.scalar(
        select(func.count(MarzbanLog.id)).where(MarzbanLog.status == "error")
    ) or 0
    expired_subs = await session.scalar(
        select(func.count(Subscription.id)).where(Subscription.expire_at <= now)
    ) or 0
    no_purchase = await session.scalar(
        select(func.count(User.id)).where(
            ~select(Payment.id)
            .where(Payment.telegram_id == User.telegram_id, Payment.status == "success")
            .exists()
        )
    ) or 0

    return {
        "total_users": total_users,
        "total_payments": total_payments,
        "total_stars": total_stars,
        "active_subs": active_subs,
        "expired_subs": expired_subs,
        "support_new": support_new,
        "errors": errors,
        "no_purchase": no_purchase,
    }


async def users_for_broadcast(session: AsyncSession, segment: str) -> list[int]:
    """Получатели рассылки: all|active|expired|no_purchase|plan:<key>|bought:<key>."""
    now = datetime.now(timezone.utc)
    if segment == "all":
        return list(await session.scalars(select(User.telegram_id)))
    if segment == "active":
        return list(await session.scalars(select(Subscription.telegram_id).where(Subscription.expire_at > now)))
    if segment == "expired":
        return list(await session.scalars(select(Subscription.telegram_id).where(Subscription.expire_at <= now)))
    if segment == "no_purchase":
        return list(await session.scalars(
            select(User.telegram_id).where(
                ~select(Payment.id)
                .where(Payment.telegram_id == User.telegram_id, Payment.status == "success")
                .exists()
            )
        ))
    if segment.startswith("plan:"):
        key = segment.split(":", 1)[1]
        return list(await session.scalars(select(Subscription.telegram_id).where(Subscription.plan == key)))
    if segment.startswith("bought:"):
        key = segment.split(":", 1)[1]
        return list(await session.scalars(select(Payment.telegram_id).where(Payment.plan == key, Payment.status == "success").distinct()))
    return []


async def get_user_admin_snapshot(session: AsyncSession, telegram_id: int) -> dict:
    user = await get_user(session, telegram_id)
    sub = await get_subscription(session, telegram_id)
    last_payment = await session.scalar(
        select(Payment).where(Payment.telegram_id == telegram_id).order_by(Payment.created_at.desc()).limit(1)
    )
    payments_count = await session.scalar(select(func.count(Payment.id)).where(Payment.telegram_id == telegram_id)) or 0
    stars_sum = await session.scalar(select(func.coalesce(func.sum(Payment.stars_amount), 0)).where(Payment.telegram_id == telegram_id, Payment.status == "success")) or 0
    return {"user": user, "subscription": sub, "last_payment": last_payment, "payments_count": payments_count, "stars_sum": stars_sum}


async def upsert_promo_code(session: AsyncSession, promo) -> PromoCode:
    row = await session.scalar(select(PromoCode).where(PromoCode.code == promo.code.upper()))
    if row is None:
        row = PromoCode(code=promo.code.upper())
        session.add(row)
    row.title = promo.title
    row.kind = promo.kind
    row.percent = promo.percent
    row.value = promo.value
    row.free_plan_key = promo.free_plan_key
    row.once_per_user = promo.once_per_user
    row.global_limit = promo.global_limit
    row.enabled = promo.enabled
    row.new_only = promo.new_only
    row.old_only = promo.old_only
    row.disabled_at = None if promo.enabled else datetime.now(timezone.utc)
    await session.commit()
    return row


async def disable_promo_code(session: AsyncSession, code: str) -> bool:
    row = await session.scalar(select(PromoCode).where(PromoCode.code == code.upper()))
    if not row:
        return False
    row.enabled = False
    row.disabled_at = datetime.now(timezone.utc)
    await session.commit()
    return True


async def due_reminders(session: AsyncSession, hours_before: int) -> list[Subscription]:
    """Подписки, которым пора отправить напоминание."""
    now = datetime.now(timezone.utc)
    target = now + timedelta(hours=hours_before)
    window_start = target - timedelta(minutes=35)
    window_end = target + timedelta(minutes=35)
    marker = f"{hours_before}h"

    result = await session.scalars(
        select(Subscription).where(
            Subscription.expire_at >= window_start,
            Subscription.expire_at <= window_end,
            ~select(ReminderLog.id)
            .where(
                ReminderLog.telegram_id == Subscription.telegram_id,
                ReminderLog.marker == marker,
                ReminderLog.expire_at == Subscription.expire_at,
            )
            .exists(),
        )
    )
    return list(result)


async def mark_reminder_sent(
    session: AsyncSession, telegram_id: int, marker: str, expire_at: datetime
) -> None:
    session.add(ReminderLog(telegram_id=telegram_id, marker=marker, expire_at=expire_at))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()


async def reminder_already_sent(
    session: AsyncSession, telegram_id: int, marker: str, expire_at: datetime
) -> bool:
    found = await session.scalar(
        select(ReminderLog.id).where(
            ReminderLog.telegram_id == telegram_id,
            ReminderLog.marker == marker,
            ReminderLog.expire_at == expire_at,
        )
    )
    return found is not None


async def due_expired(session: AsyncSession, window_minutes: int) -> list[Subscription]:
    """Подписки, истёкшие в недавнем окне, которым ещё не слали уведомление об окончании."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=window_minutes)
    result = await session.scalars(
        select(Subscription).where(
            Subscription.expire_at <= now,
            Subscription.expire_at >= window_start,
            ~select(ReminderLog.id)
            .where(
                ReminderLog.telegram_id == Subscription.telegram_id,
                ReminderLog.marker == "expired",
                ReminderLog.expire_at == Subscription.expire_at,
            )
            .exists(),
        )
    )
    return list(result)


async def due_unconverted_users(session: AsyncSession, after_minutes: int = 60, limit: int = 200) -> list[int]:
    """Пользователи, которые стартовали, но не купили спустя after_minutes."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=after_minutes)
    rows = await session.scalars(
        select(User.telegram_id)
        .where(
            User.created_at <= cutoff,
            ~select(Payment.id)
            .where(Payment.telegram_id == User.telegram_id, Payment.status == "success")
            .exists(),
            ~select(BotEvent.id)
            .where(BotEvent.telegram_id == User.telegram_id, BotEvent.event == "no_purchase_offer")
            .exists(),
        )
        .limit(limit)
    )
    return list(rows)


async def active_finite_subs(session: AsyncSession, limit: int = 500) -> list[Subscription]:
    """Активные подписки с конечным лимитом трафика (для проверки остатка в Marzban)."""
    now = datetime.now(timezone.utc)
    result = await session.scalars(
        select(Subscription)
        .where(Subscription.expire_at > now, Subscription.traffic_limit > 0)
        .limit(limit)
    )
    return list(result)


async def set_support_state(
    session: AsyncSession,
    telegram_id: int,
    state: str | None,
    platform: str | None = None,
) -> None:
    user = await get_user(session, telegram_id)
    if user:
        user.support_state = state
        if platform is not None:
            user.support_platform = platform
        await session.commit()


async def add_support_ticket(
    session: AsyncSession,
    telegram_id: int,
    platform: str | None,
    message: str | None,
) -> SupportTicket:
    ticket = SupportTicket(telegram_id=telegram_id, platform=platform, message=message)
    session.add(ticket)
    await session.commit()
    return ticket


# ─── Поддержка: тикеты + двусторонний тред ───────────────────────────
SUPPORT_TOPICS = {"payment", "vpn", "promo", "other"}
SUPPORT_TOPIC_TITLES = {"payment": "Оплата / Stars", "vpn": "Не работает VPN", "promo": "Промокод", "other": "Другое"}


async def create_support_ticket(
    session: AsyncSession, telegram_id: int, topic: str, message: str, platform: str | None = None
) -> SupportTicket:
    """Создать тикет и первое сообщение треда (от пользователя)."""
    text = (message or "").strip()[:2000]
    ticket = SupportTicket(
        telegram_id=telegram_id,
        topic=topic if topic in SUPPORT_TOPICS else "other",
        platform=platform,
        message=text,
        status="open",
    )
    session.add(ticket)
    await session.flush()
    session.add(SupportMessage(ticket_id=ticket.id, sender="user", text=text))
    await session.commit()
    return ticket


async def add_support_message(
    session: AsyncSession, ticket_id: int, sender: str, text: str, admin_id: int | None = None
) -> tuple[SupportTicket | None, SupportMessage | None]:
    """Добавить сообщение в тред. Ответ админа → answered; сообщение юзера → open."""
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None:
        return None, None
    msg = SupportMessage(ticket_id=ticket_id, sender=sender, admin_id=admin_id, text=(text or "").strip()[:2000])
    session.add(msg)
    ticket.status = "answered" if sender == "admin" else "open"
    await session.commit()
    return ticket, msg


async def set_ticket_status(session: AsyncSession, ticket_id: int, status: str) -> SupportTicket | None:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket:
        ticket.status = status
        await session.commit()
    return ticket


async def get_ticket(session: AsyncSession, ticket_id: int) -> SupportTicket | None:
    return await session.get(SupportTicket, ticket_id)


async def get_ticket_messages(session: AsyncSession, ticket_id: int) -> list[SupportMessage]:
    rows = await session.scalars(
        select(SupportMessage).where(SupportMessage.ticket_id == ticket_id).order_by(SupportMessage.created_at.asc())
    )
    return list(rows)


async def list_user_tickets(session: AsyncSession, telegram_id: int, limit: int = 20) -> list[SupportTicket]:
    rows = await session.scalars(
        select(SupportTicket)
        .where(SupportTicket.telegram_id == telegram_id)
        .order_by(SupportTicket.updated_at.desc())
        .limit(limit)
    )
    return list(rows)


async def list_admin_tickets(session: AsyncSession, status: str | None = None, limit: int = 50) -> list[SupportTicket]:
    stmt = select(SupportTicket)
    if status == "open":
        stmt = stmt.where(SupportTicket.status.in_(["open", "new"]))
    elif status == "active":
        stmt = stmt.where(SupportTicket.status.in_(["open", "new", "answered"]))
    elif status in ("answered", "closed"):
        stmt = stmt.where(SupportTicket.status == status)
    rows = await session.scalars(stmt.order_by(SupportTicket.updated_at.desc()).limit(limit))
    return list(rows)


async def count_open_tickets(session: AsyncSession) -> int:
    return await session.scalar(
        select(func.count(SupportTicket.id)).where(SupportTicket.status.in_(["open", "new"]))
    ) or 0


async def log_event(session: AsyncSession, telegram_id: int, event: str, value: str | None = None) -> None:
    session.add(BotEvent(telegram_id=telegram_id, event=event, value=value))
    await session.commit()


async def log_marzban(session: AsyncSession, telegram_id: int | None, action: str, status: str, message: str | None = None) -> None:
    session.add(MarzbanLog(telegram_id=telegram_id, action=action, status=status, message=message))
    await session.commit()


async def log_admin_action(
    session: AsyncSession,
    admin_id: int,
    action: str,
    target: str | None = None,
    details: str | None = None,
) -> None:
    session.add(AdminAuditLog(admin_id=admin_id, action=action, target=target, details=details))
    await session.commit()


async def admin_audit_logs(
    session: AsyncSession,
    limit: int = 100,
    action: str | None = None,
    admin_id: int | None = None,
) -> list[AdminAuditLog]:
    stmt = select(AdminAuditLog)
    if action:
        stmt = stmt.where(AdminAuditLog.action == action)
    if admin_id:
        stmt = stmt.where(AdminAuditLog.admin_id == admin_id)
    rows = await session.scalars(stmt.order_by(AdminAuditLog.created_at.desc()).limit(limit))
    return list(rows)


# Дефолты тарифов из config.py статичны на время жизни процесса — синхронизируем один
# раз за процесс, чтобы не дёргать ~N запросов на каждый /api/config, pre_checkout и т.п.
_tariff_sync_done = False


async def ensure_tariff_settings(session: AsyncSession, plans: dict, *, force: bool = False) -> None:
    global _tariff_sync_done
    if _tariff_sync_done and not force:
        return

    # Seed недостающих тарифов через ON CONFLICT DO NOTHING: бот и miniapp стартуют
    # одновременно, и одновременная вставка одного ключа не должна ронять процесс.
    # Важно: НЕ обновляем существующие строки — иначе правки тарифа в админке
    # (в т.ч. is_trial/unlimited) откатывались бы к значениям из config.py при рестарте.
    # Новые тарифы засеиваются с корректными флагами сразу из config.py.
    rows = [
        {
            "key": p.key,
            "title": p.title,
            "days": p.days,
            "stars": p.stars,
            "traffic_gb": p.traffic_gb,
            "devices": p.devices,
            "badge": p.badge,
            "visible": p.visible,
            "is_trial": p.is_trial,
            "traffic_only": p.traffic_only,
            "unlimited": p.unlimited,
        }
        for p in plans.values()
    ]
    if rows:
        # on_conflict_do_nothing есть и в postgres, и в sqlite — выбираем по диалекту,
        # чтобы seed работал и в проде (Postgres), и в интеграционных тестах (SQLite).
        insert_fn = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
        await session.execute(
            insert_fn(TariffSetting).values(rows).on_conflict_do_nothing(index_elements=["key"])
        )
        await session.commit()
    _tariff_sync_done = True



async def enqueue_grant(
    session: AsyncSession,
    *,
    telegram_id: int,
    payment_id: int | None,
    charge_id: str,
    plan,
    stars_amount: int = 0,
    promo_code: str | None = None,
    last_error: str | None = None,
) -> GrantQueue:
    row = await session.scalar(select(GrantQueue).where(GrantQueue.charge_id == charge_id))
    if row is None:
        row = GrantQueue(
            telegram_id=telegram_id,
            payment_id=payment_id,
            charge_id=charge_id,
            plan_key=plan.key,
            plan_title=plan.title,
            days=plan.days,
            traffic_gb=plan.traffic_gb,
            devices=plan.devices,
            stars_amount=stars_amount,
            promo_code=promo_code,
            is_trial=plan.is_trial,
            traffic_only=plan.traffic_only,
            unlimited=plan.unlimited,
            status="pending",
            attempts=0,
            last_error=last_error,
            next_attempt_at=datetime.now(timezone.utc),
        )
        session.add(row)
    else:
        if row.status in {"done", "processing"}:
            return row
        row.status = "retrying" if row.attempts else "pending"
        row.last_error = last_error or row.last_error
        row.next_attempt_at = datetime.now(timezone.utc)
    await session.commit()
    return row


async def due_grant_queue(session: AsyncSession, limit: int = 20) -> list[GrantQueue]:
    now = datetime.now(timezone.utc)
    # Если процесс упал в статусе processing, возвращаем задание в retry после таймаута.
    await session.execute(
        update(GrantQueue)
        .where(GrantQueue.status == "processing", GrantQueue.next_attempt_at <= now, GrantQueue.attempts < 5)
        .values(status="retrying")
    )
    await session.commit()
    rows = await session.scalars(
        select(GrantQueue)
        .where(GrantQueue.status.in_(["pending", "retrying"]), GrantQueue.next_attempt_at <= now, GrantQueue.attempts < 5)
        .order_by(GrantQueue.next_attempt_at.asc())
        .limit(limit)
    )
    return list(rows)


async def mark_grant_processing(session: AsyncSession, queue_id: int) -> GrantQueue | None:
    row = await session.get(GrantQueue, queue_id)
    if not row or row.status not in {"pending", "retrying"}:
        return None
    row.status = "processing"
    row.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    await session.commit()
    return row


async def mark_grant_done(session: AsyncSession, queue_id: int) -> None:
    row = await session.get(GrantQueue, queue_id)
    if row:
        row.status = "done"
        row.last_error = None
        await session.commit()


async def mark_grant_failed(session: AsyncSession, queue_id: int, error: str, *, max_attempts: int = 5) -> GrantQueue | None:
    row = await session.get(GrantQueue, queue_id)
    if not row:
        return None
    row.attempts = (row.attempts or 0) + 1
    row.last_error = error[:2000]
    if row.attempts >= max_attempts:
        row.status = "failed"
    else:
        row.status = "retrying"
        row.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=min(5, row.attempts))
    await session.commit()
    return row


async def grant_queue_stats(session: AsyncSession) -> dict:
    rows = await session.execute(select(GrantQueue.status, func.count(GrantQueue.id)).group_by(GrantQueue.status))
    return {status: count for status, count in rows.all()}


async def should_send_alert(session: AsyncSession, key: str, *, cooldown_minutes: int = 30, message: str | None = None) -> bool:
    bucket_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    # группируем по cooldown window: alert_key + номер окна в минутах
    epoch_minutes = int(bucket_time.timestamp() // 60)
    bucket = str(epoch_minutes // cooldown_minutes)
    session.add(AlertLog(key=key[:64], bucket=bucket, message=message))
    try:
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        return False


async def record_miniapp_open(
    session: AsyncSession,
    telegram_id: int,
    *,
    fingerprint_hash: str | None,
    ip_hash: str | None,
    user_agent_hash: str | None,
    platform: str | None = None,
) -> MiniAppDevice | None:
    """Зафиксировать открытие MiniApp и привязать хеш устройства к пользователю."""
    user = await get_user(session, telegram_id)
    now = datetime.now(timezone.utc)
    if user:
        if user.miniapp_opened_at is None:
            user.miniapp_opened_at = now
        user.last_activity_at = now
    device = None
    if fingerprint_hash:
        device = await session.scalar(
            select(MiniAppDevice).where(
                MiniAppDevice.telegram_id == telegram_id,
                MiniAppDevice.fingerprint_hash == fingerprint_hash,
            )
        )
        if device is None:
            device = MiniAppDevice(
                telegram_id=telegram_id,
                fingerprint_hash=fingerprint_hash,
                ip_hash=ip_hash,
                user_agent_hash=user_agent_hash,
                platform=(platform or "")[:32] or None,
            )
            session.add(device)
        else:
            device.ip_hash = ip_hash or device.ip_hash
            device.user_agent_hash = user_agent_hash or device.user_agent_hash
            device.platform = (platform or device.platform or "")[:32] or None
            device.last_seen_at = now
    await session.commit()
    return device


async def log_abuse_flag(
    session: AsyncSession,
    *,
    telegram_id: int | None,
    kind: str,
    severity: str,
    fingerprint_hash: str | None = None,
    ip_hash: str | None = None,
    details: str | None = None,
) -> None:
    session.add(AbuseFlag(
        telegram_id=telegram_id,
        kind=kind[:32],
        severity=severity[:12],
        fingerprint_hash=fingerprint_hash,
        ip_hash=ip_hash,
        details=(details or "")[:2000] or None,
    ))
    await session.commit()


async def reserve_daily_free_claim(
    session: AsyncSession,
    *,
    telegram_id: int,
    day: str,
    fingerprint_hash: str | None,
    ip_hash: str | None,
    risk_score: int,
) -> DailyFreeClaim | None:
    claim = DailyFreeClaim(
        telegram_id=telegram_id,
        day=day,
        fingerprint_hash=fingerprint_hash,
        ip_hash=ip_hash,
        status="pending",
        risk_score=risk_score,
    )
    session.add(claim)
    try:
        await session.commit()
        return claim
    except IntegrityError:
        await session.rollback()
        return None


async def set_daily_free_claim_status(session: AsyncSession, claim_id: int, status: str, reason: str | None = None) -> None:
    claim = await session.get(DailyFreeClaim, claim_id)
    if claim:
        claim.status = status
        claim.reason = (reason or "")[:2000] or None
        await session.commit()


async def daily_free_claim_for_day(session: AsyncSession, telegram_id: int, day: str) -> DailyFreeClaim | None:
    return await session.scalar(select(DailyFreeClaim).where(DailyFreeClaim.telegram_id == telegram_id, DailyFreeClaim.day == day))


async def health_counters(session: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    day = now - timedelta(days=1)
    ten = now - timedelta(minutes=10)
    last_success = await session.scalar(select(Payment).where(Payment.status == "success").order_by(Payment.created_at.desc()).limit(1))
    last_m_error = await session.scalar(select(MarzbanLog).where(MarzbanLog.status == "error").order_by(MarzbanLog.created_at.desc()).limit(1))
    marzban_24h = await session.scalar(select(func.count(MarzbanLog.id)).where(MarzbanLog.status == "error", MarzbanLog.created_at >= day)) or 0
    payment_errors_24h = await session.scalar(select(func.count(Payment.id)).where(Payment.status.in_(["marzban_error", "validation_error"]), Payment.created_at >= day)) or 0
    payment_errors_10m = await session.scalar(select(func.count(Payment.id)).where(Payment.status.in_(["marzban_error", "validation_error"]), Payment.created_at >= ten)) or 0
    marzban_errors_10m = await session.scalar(select(func.count(MarzbanLog.id)).where(MarzbanLog.status == "error", MarzbanLog.created_at >= ten)) or 0
    return {
        "last_success_payment": last_success,
        "last_marzban_error": last_m_error,
        "marzban_errors_24h": marzban_24h,
        "payment_errors_24h": payment_errors_24h,
        "payment_errors_10m": payment_errors_10m,
        "marzban_errors_10m": marzban_errors_10m,
        "grant_queue": await grant_queue_stats(session),
    }


async def record_usage_snapshot(session: AsyncSession, telegram_id: int, day: str, used_traffic: int) -> None:
    """Сохранить суточный снимок израсходованного трафика (upsert по дню)."""
    row = await session.scalar(
        select(UsageSnapshot).where(UsageSnapshot.telegram_id == telegram_id, UsageSnapshot.day == day)
    )
    if row is None:
        session.add(UsageSnapshot(telegram_id=telegram_id, day=day, used_traffic=used_traffic))
    else:
        row.used_traffic = used_traffic
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()


async def get_usage_history(session: AsyncSession, telegram_id: int, days: int = 8) -> list[UsageSnapshot]:
    """Последние N суточных снимков (по возрастанию даты) для графика трафика."""
    rows = await session.scalars(
        select(UsageSnapshot)
        .where(UsageSnapshot.telegram_id == telegram_id)
        .order_by(UsageSnapshot.day.desc())
        .limit(days)
    )
    return list(reversed(list(rows)))


# ─── Геймификация ─────────────────────────────────────────────────
async def reserve_bonus_claim(session: AsyncSession, telegram_id: int, kind: str, day: str) -> BonusClaim | None:
    """Зарезервировать разовое действие (checkin/wheel) на день UTC.

    Возвращает строку при успехе или None, если на сегодня уже зарезервировано
    (защита от повторного бонуса/спина даже при гонке — через unique-индекс).
    """
    claim = BonusClaim(telegram_id=telegram_id, kind=kind, day=day)
    session.add(claim)
    try:
        await session.commit()
        return claim
    except IntegrityError:
        await session.rollback()
        return None


async def delete_bonus_claim(session: AsyncSession, claim_id: int) -> None:
    """Снять резервацию (например, если выдача в Marzban не удалась) — для повтора."""
    row = await session.get(BonusClaim, claim_id)
    if row:
        await session.delete(row)
        await session.commit()


async def set_bonus_reward(session: AsyncSession, claim_id: int, reward_kind: str, reward_value: int) -> None:
    row = await session.get(BonusClaim, claim_id)
    if row:
        row.reward_kind = reward_kind
        row.reward_value = reward_value
        await session.commit()


async def get_today_bonus_claims(session: AsyncSession, telegram_id: int, day: str) -> dict[str, BonusClaim]:
    rows = await session.scalars(
        select(BonusClaim).where(BonusClaim.telegram_id == telegram_id, BonusClaim.day == day)
    )
    return {r.kind: r for r in rows}


async def get_or_create_gami_state(session: AsyncSession, telegram_id: int) -> GamificationState:
    state = await session.get(GamificationState, telegram_id)
    if state is None:
        state = GamificationState(telegram_id=telegram_id, streak=0, total_checkins=0, total_spins=0)
        session.add(state)
        await session.commit()
    return state


async def get_user_achievements(session: AsyncSession, telegram_id: int) -> set[str]:
    rows = await session.scalars(select(UserAchievement.code).where(UserAchievement.telegram_id == telegram_id))
    return set(rows)


async def award_achievement(session: AsyncSession, telegram_id: int, code: str) -> bool:
    """Выдать достижение идемпотентно. True — если выдано впервые."""
    session.add(UserAchievement(telegram_id=telegram_id, code=code))
    try:
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        return False


async def successful_plan_keys(session: AsyncSession, telegram_id: int) -> set[str]:
    rows = await session.scalars(
        select(Payment.plan).where(
            Payment.telegram_id == telegram_id,
            Payment.status.in_(["success", "manual"]),
        )
    )
    return set(rows)


async def rewarded_referral_count(session: AsyncSession, telegram_id: int) -> int:
    return await session.scalar(
        select(func.count(User.id)).where(
            User.referred_by == telegram_id, User.referral_rewarded.is_(True)
        )
    ) or 0


async def payment_reconciliation(session: AsyncSession) -> dict:
    # success без локальной subscription
    success_without_sub = await session.scalars(
        select(Payment).where(
            Payment.status == "success",
            ~select(Subscription.id).where(Subscription.telegram_id == Payment.telegram_id).exists(),
        ).order_by(Payment.created_at.desc()).limit(100)
    )
    errors = await session.scalars(select(Payment).where(Payment.status.in_(["marzban_error", "validation_error"])).order_by(Payment.created_at.desc()).limit(100))
    no_charge = await session.scalars(select(Payment).where((Payment.charge_id == None) | (Payment.charge_id == "")).order_by(Payment.created_at.desc()).limit(100))
    dup_rows = await session.execute(select(Payment.charge_id, func.count(Payment.id)).group_by(Payment.charge_id).having(func.count(Payment.id) > 1).limit(50))
    strange = await session.scalars(select(Payment).where(Payment.stars_amount < 0).order_by(Payment.created_at.desc()).limit(100))
    return {
        "success_without_subscription": list(success_without_sub),
        "problem_payments": list(errors),
        "missing_charge_id": list(no_charge),
        "duplicate_charge_ids": list(dup_rows.all()),
        "strange_amounts": list(strange),
    }
