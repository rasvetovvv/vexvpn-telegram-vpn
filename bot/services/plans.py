"""Единый источник тарифов для WebApp, callback-инвойсов и successful_payment."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import PLANS, Plan
from bot.db.models import TariffSetting
from bot.db.repo import ensure_tariff_settings


def plan_from_tariff(t: TariffSetting) -> Plan:
    """Собрать runtime Plan из tariff_settings."""
    return Plan(
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


async def get_effective_plan(session: AsyncSession, key: str) -> Plan | None:
    """Вернуть тариф из БД, синхронизировав дефолты из config.py.

    Это нужно, чтобы WebApp invoice, Telegram callback invoice, pre_checkout и
    successful_payment использовали одинаковые дни/трафик/is_trial.
    """
    await ensure_tariff_settings(session, PLANS)
    tariff = await session.get(TariffSetting, key)
    if tariff:
        return plan_from_tariff(tariff)
    return PLANS.get(key)


async def get_visible_plans(session: AsyncSession) -> list[Plan]:
    """Видимые тарифы из БД для меню/текстов бота (единый источник с Mini App).

    Порядок: сначала как в config.py (стабильная витрина), затем тарифы,
    добавленные только через админку.
    """
    await ensure_tariff_settings(session, PLANS)
    tariffs = {t.key: t for t in await session.scalars(select(TariffSetting))}
    plans: list[Plan] = []
    seen: set[str] = set()
    for key in PLANS:
        t = tariffs.get(key)
        if t and t.visible:
            plans.append(plan_from_tariff(t))
            seen.add(key)
    for key, t in tariffs.items():
        if key not in seen and t.visible:
            plans.append(plan_from_tariff(t))
    return plans
