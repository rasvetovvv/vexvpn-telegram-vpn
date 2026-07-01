"""Помощники для оплаты Telegram Stars (валюта XTR)."""
from __future__ import annotations

from aiogram.types import LabeledPrice

from bot.config import Plan, Promo

INVOICE_PAYLOAD_PREFIX = "plan"


def price_with_promo(plan: Plan, promo: Promo | None = None) -> int:
    """Итоговая цена в Stars. Для XTR amount = число звёзд (без ×100)."""
    if promo and promo.percent:
        return max(1, round(plan.stars * (100 - promo.percent) / 100))
    return plan.stars


def build_prices(plan: Plan, promo: Promo | None = None) -> list[LabeledPrice]:
    amount = price_with_promo(plan, promo)
    label = f"VPN — {plan.title}"
    if promo and promo.percent:
        label += f" · {promo.code} -{promo.percent}%"
    return [LabeledPrice(label=label, amount=amount)]


def build_payload(plan: Plan, promo_code: str | None = None) -> str:
    payload = f"{INVOICE_PAYLOAD_PREFIX}:{plan.key}"
    if promo_code:
        payload += f":{promo_code.upper()}"
    return payload


def parse_payload(payload: str) -> tuple[str | None, str | None]:
    """Вернуть (ключ тарифа, промокод) из payload счёта."""
    parts = payload.split(":")
    if len(parts) >= 2 and parts[0] == INVOICE_PAYLOAD_PREFIX:
        return parts[1], (parts[2].upper() if len(parts) >= 3 and parts[2] else None)
    return None, None
