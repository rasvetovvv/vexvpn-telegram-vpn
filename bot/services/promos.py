"""Dynamic promo helpers: static config + DB promo_codes."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from bot.config import PROMOS, PLANS, Plan, Promo
from bot.db.models import Payment, PromoCode, PromoUse, Subscription, User
from bot.db.repo import has_used_promo


def _to_promo(row: PromoCode) -> Promo:
    return Promo(
        code=row.code.upper(),
        title=row.title,
        percent=row.percent or 0,
        free_plan_key=row.free_plan_key,
        once_per_user=bool(row.once_per_user),
        kind=row.kind or "discount",
        value=row.value or 0,
        global_limit=row.global_limit or 0,
        enabled=bool(row.enabled),
        new_only=bool(row.new_only),
        old_only=bool(row.old_only),
    )


async def get_promo(session: AsyncSession, code: str | None) -> Promo | None:
    if not code:
        return None
    code = code.strip().upper()
    row = await session.scalar(select(PromoCode).where(PromoCode.code == code))
    if row:
        promo = _to_promo(row)
        return promo if promo.enabled else None
    promo = PROMOS.get(code)
    return promo if promo and promo.enabled else None


async def list_promos(session: AsyncSession) -> list[Promo]:
    rows = list(await session.scalars(select(PromoCode).where(PromoCode.enabled.is_(True)).order_by(PromoCode.created_at.desc())))
    dynamic = [_to_promo(r) for r in rows]
    dynamic_codes = {p.code for p in dynamic}
    return dynamic + [p for p in PROMOS.values() if p.code not in dynamic_codes and p.enabled]


async def payment_count(session: AsyncSession, telegram_id: int) -> int:
    return await session.scalar(select(func.count(Payment.id)).where(Payment.telegram_id == telegram_id, Payment.status == "success")) or 0


async def promo_use_count(session: AsyncSession, code: str) -> int:
    return await session.scalar(select(func.count(PromoUse.id)).where(PromoUse.code == code.upper())) or 0


async def reserve_promo_for_user(session: AsyncSession, telegram_id: int, promo: Promo) -> str | None:
    """Atomically reserve a promo use for free/manual promo grants.

    Validation-only checks are race-prone: two concurrent /promo requests can both
    see no PromoUse row or a not-yet-exhausted global limit. PostgreSQL advisory
    lock serializes reservations per promo code, then the PromoUse unique
    constraint enforces once-per-user at the database layer.
    """
    code = promo.code.upper()
    if session.bind and session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"promo:{code}"})
    err = await validate_promo_for_user(session, telegram_id, promo)
    if err:
        return err
    session.add(PromoUse(telegram_id=telegram_id, code=code))
    try:
        await session.commit()
        return None
    except IntegrityError:
        await session.rollback()
        return "Промокод уже использован"


async def validate_promo_for_user(session: AsyncSession, telegram_id: int, promo: Promo) -> str | None:
    if not promo.enabled:
        return "Промокод отключён"
    if promo.once_per_user and await has_used_promo(session, telegram_id, promo.code):
        return "Промокод уже использован"
    if promo.global_limit and await promo_use_count(session, promo.code) >= promo.global_limit:
        return "Лимит активаций промокода закончился"
    paid = await payment_count(session, telegram_id)
    if promo.new_only and paid > 0:
        return "Промокод доступен только новым пользователям"
    if promo.old_only and paid == 0:
        return "Промокод доступен только клиентам с покупкой"
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if promo.kind == "trial" and user and user.trial_used:
        return "Пробный период уже использован"
    if promo.kind == "traffic":
        sub = await session.scalar(select(Subscription).where(Subscription.telegram_id == telegram_id))
        now = datetime.now(timezone.utc)
        if not sub or (sub.expire_at if sub.expire_at.tzinfo else sub.expire_at.replace(tzinfo=timezone.utc)) <= now:
            return "Бесплатный трафик можно начислить только к активной подписке"
    return None


async def promo_to_plan(session: AsyncSession, promo: Promo) -> Plan | None:
    if promo.free_plan_key:
        # Prefer admin-editable effective plan when available.
        from bot.services.plans import get_effective_plan
        return await get_effective_plan(session, promo.free_plan_key)
    if promo.kind in {"days", "trial"} and promo.value > 0:
        return Plan(
            key=f"promo_{promo.code.lower()}",
            title=promo.title,
            days=promo.value,
            stars=0,
            traffic_gb=0,
            devices=1,
            visible=False,
            is_trial=promo.kind == "trial",
            unlimited=False,
        )
    if promo.kind == "traffic" and promo.value > 0:
        return Plan(
            key=f"promo_{promo.code.lower()}",
            title=promo.title,
            days=0,
            stars=0,
            traffic_gb=promo.value,
            devices=1,
            visible=False,
            traffic_only=True,
        )
    return None


def parse_promo_create_args(text: str) -> tuple[str, Promo] | tuple[None, str]:
    """Parse /promo_create CODE 30d|100gb|30% [new|old|global|once|multi|trial]."""
    parts = (text or "").split()
    if len(parts) < 3:
        return None, "Формат: /promo_create FREE30 30d [new|old|trial|global|multi]"
    code = parts[1].strip().upper()[:32]
    # Только латиница/цифры/подчёркивание: код попадает в подписи, инвойсы и в HTML
    # кабинета — нельзя допускать символы разметки/скрипта (защита от stored-XSS).
    if not re.fullmatch(r"[A-Z0-9_]{2,32}", code):
        return None, "Код промокода: только латиница, цифры и _, длина 2–32 (например FREE30)"
    spec = parts[2].lower()
    flags = {p.lower() for p in parts[3:]}
    kind = "discount"
    percent = value = 0
    title = ""
    free_plan_key = None
    if spec.endswith("%"):
        kind = "discount"; percent = int(spec[:-1]); title = f"-{percent}% на покупку"
    elif spec.endswith("d"):
        kind = "trial" if "trial" in flags else "days"; value = int(spec[:-1]); title = f"{value} дней бесплатно" if kind == "days" else f"Trial {value} дней"
    elif spec.endswith("gb"):
        kind = "traffic"; value = int(spec[:-2]); title = f"+{value} ГБ бесплатно"
    elif spec in PLANS:
        kind = "trial" if "trial" in flags else "days"; free_plan_key = spec; title = f"Бесплатный тариф {PLANS[spec].title}"
    else:
        return None, "Не понял тип. Примеры: 30d, 100gb, 30%, lite_7d"
    if percent and not 1 <= percent <= 99:
        return None, "Скидка должна быть 1–99%"
    if value < 0:
        return None, "Значение должно быть положительным"
    promo = Promo(
        code=code,
        title=title,
        percent=percent,
        free_plan_key=free_plan_key,
        once_per_user="multi" not in flags,
        kind=kind,
        value=value,
        global_limit=1 if "global" in flags else 0,
        enabled=True,
        new_only="new" in flags,
        old_only="old" in flags,
    )
    return code, promo
