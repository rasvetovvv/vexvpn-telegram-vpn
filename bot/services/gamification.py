"""Геймификация кабинета: ежедневный стрик, колесо фортуны, достижения.

Принципы:
- всё считается и выдаётся НА СЕРВЕРЕ — клиенту не верим;
- идемпотентность: не больше 1 чек-ина и 1 спина в сутки (UTC) на пользователя,
  через unique-индекс bonus_claims (защита от гонки/спама);
- анти-фрод: награды (дни/трафик) только при активной подписке — чтобы нельзя
  было фармить бесплатный VPN без покупки;
- награды-дни/трафик идут через тот же Marzban-путь, что и обычные выдачи, и
  сохраняют имя текущего тарифа пользователя;
- при сбое Marzban резервация откатывается, чтобы пользователь мог повторить.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from bot.config import (
    GAMI_DAILY_GOAL,
    GAMI_DAILY_REWARD_DAYS,
    GAMI_WHEEL_PROMO_CODE,
    GAMI_WHEEL_SEGMENTS,
    PLANS,
    Plan,
)
from bot.db.database import session_maker
from bot.db.repo import (
    award_achievement,
    delete_bonus_claim,
    get_or_create_gami_state,
    get_subscription,
    get_today_bonus_claims,
    get_user_achievements,
    reserve_bonus_claim,
    set_bonus_reward,
)

logger = logging.getLogger(__name__)


class GamificationError(Exception):
    """Пользовательская ошибка геймификации (маппится в HTTP в эндпоинте)."""

    def __init__(self, message: str, *, code: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# Достижения — бейджи (без выдачи наград, чтобы не раздувать экономику).
ACHIEVEMENTS: tuple[dict, ...] = (
    {"code": "first_launch", "title": "Первый запуск", "desc": "Открыл кабинет"},
    {"code": "active_vpn", "title": "Активный VPN", "desc": "Есть активная подписка"},
    {"code": "referral", "title": "Пригласил друга", "desc": "Друг оформил подписку"},
    {"code": "first_month", "title": "Первый месяц", "desc": "Тариф на 30+ дней"},
    {"code": "unlimited", "title": "Безлимит", "desc": "Купил безлимитный тариф"},
)


# ── чистые помощники (без БД, удобно тестировать) ────────────────────
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def pick_segment() -> dict:
    """Выбрать сегмент колеса по весам (нормируются по сумме весов)."""
    segments = GAMI_WHEEL_SEGMENTS
    total = sum(s["weight"] for s in segments)
    r = random.uniform(0, total)
    upto = 0.0
    for s in segments:
        upto += s["weight"]
        if r <= upto:
            return s
    return segments[-1]


def reward_message(kind: str, value: int) -> str:
    if kind == "days":
        return f"Выпало +{value} дн.! Уже начислено."
    if kind == "traffic":
        return f"Выпало +{value} ГБ! Уже начислено."
    if kind == "promo":
        return f"Выпал промокод −{value}% — применится к следующей покупке."
    return "Повезёт в следующий раз 🍀"


def _is_active(sub) -> bool:
    if not sub:
        return False
    expire = sub.expire_at if sub.expire_at.tzinfo else sub.expire_at.replace(tzinfo=timezone.utc)
    return expire > datetime.now(timezone.utc)


# ── выдача награды через Marzban ─────────────────────────────────────
async def _apply_grant_reward(telegram_id: int, kind: str, value: int) -> dict:
    """Начислить день/трафик через Marzban, сохранив имя текущего тарифа.

    Бросает MarzbanError при сбое панели (вызывающий откатывает резервацию).
    Трафик без активной подписки невозможен — конвертируем в день.
    """
    from bot.db.repo import upsert_subscription
    from bot.services.marzban import marzban

    async with session_maker() as session:
        sub = await get_subscription(session, telegram_id)
    if kind == "traffic" and not _is_active(sub):
        kind, value = "days", GAMI_DAILY_REWARD_DAYS

    if kind == "days":
        plan = Plan("bonus_game", "Игровой бонус", value, 0, 0, 1, visible=False)
    else:
        plan = Plan("bonus_game", "Игровой бонус", 0, 0, value, 1, visible=False, traffic_only=True)

    result = await marzban.create_or_renew(telegram_id, plan)
    expire_at = datetime.fromtimestamp(result["expire"], tz=timezone.utc)
    async with session_maker() as session:
        sub = await get_subscription(session, telegram_id)
        store_key = sub.plan if sub and sub.plan else plan.key
        await upsert_subscription(
            session,
            telegram_id=telegram_id,
            marzban_username=result["username"],
            subscription_url=result["subscription_url"],
            plan=store_key,
            expire_at=expire_at,
            traffic_limit=result["data_limit"],
        )
    return {"kind": kind, "value": value, "expire_at": expire_at.isoformat()}


async def _eligibility(telegram_id: int) -> tuple[bool, str | None]:
    async with session_maker() as session:
        sub = await get_subscription(session, telegram_id)
    if not _is_active(sub):
        return False, "Бонусы доступны при активной подписке. Оформи тариф — и крути колесо каждый день."
    return True, None


# ── достижения ───────────────────────────────────────────────────────
async def evaluate_achievements(telegram_id: int) -> set[str]:
    """Выдать недостающие достижения по условиям. Возвращает все полученные коды."""
    from bot.db.repo import rewarded_referral_count, successful_plan_keys

    async with session_maker() as session:
        earned = await get_user_achievements(session, telegram_id)
        sub = await get_subscription(session, telegram_id)
        keys = await successful_plan_keys(session, telegram_id)
        refs = await rewarded_referral_count(session, telegram_id)

    all_keys = set(keys) | ({sub.plan} if sub and sub.plan else set())

    def _has(pred) -> bool:
        return any(pred(PLANS.get(k)) for k in all_keys if PLANS.get(k))

    unlocked = {
        "first_launch": True,
        "active_vpn": _is_active(sub),
        "referral": refs >= 1,
        "first_month": _has(lambda p: p and p.days >= 30),
        "unlimited": _has(lambda p: p and p.unlimited),
    }

    newly: set[str] = set()
    async with session_maker() as session:
        for code, ok in unlocked.items():
            if ok and code not in earned and await award_achievement(session, telegram_id, code):
                newly.add(code)
    return earned | newly


# ── публичные операции ───────────────────────────────────────────────
async def claim_daily(telegram_id: int) -> dict:
    eligible, reason = await _eligibility(telegram_id)
    if not eligible:
        raise GamificationError(reason or "Недоступно", code="not_eligible")

    day = _today()
    async with session_maker() as session:
        claim = await reserve_bonus_claim(session, telegram_id, "checkin", day)
    if claim is None:
        raise GamificationError("Сегодня бонус уже получен. Возвращайся завтра!", code="already")

    async with session_maker() as session:
        state = await get_or_create_gami_state(session, telegram_id)
        prev_day, prev_streak = state.last_checkin_day, state.streak or 0
        new_streak = prev_streak + 1 if prev_day == _yesterday() else 1
        state.streak = new_streak
        state.last_checkin_day = day
        state.total_checkins = (state.total_checkins or 0) + 1
        await session.commit()

    reached = new_streak % GAMI_DAILY_GOAL == 0
    reward = {"kind": "none", "value": 0}
    if reached:
        try:
            reward = await _apply_grant_reward(telegram_id, "days", GAMI_DAILY_REWARD_DAYS)
        except Exception as exc:  # MarzbanError и прочие сбои выдачи
            async with session_maker() as session:
                state = await get_or_create_gami_state(session, telegram_id)
                state.streak = prev_streak
                state.last_checkin_day = prev_day
                state.total_checkins = max(0, (state.total_checkins or 1) - 1)
                await session.commit()
                await delete_bonus_claim(session, claim.id)
            logger.warning("daily reward grant failed for %s", telegram_id, exc_info=True)
            raise GamificationError("VPN-сервер временно недоступен, попробуй через минуту — стрик сохранён.", code="marzban") from exc

    async with session_maker() as session:
        await set_bonus_reward(session, claim.id, reward["kind"], reward.get("value", 0))

    return {
        "ok": True,
        "streak": new_streak,
        "goal": GAMI_DAILY_GOAL,
        "granted": reached,
        "reward": reward,
        "message": (
            f"Стрик {new_streak} 🔥 +{GAMI_DAILY_REWARD_DAYS} дн. начислено!"
            if reached
            else f"Засчитано! Стрик: {new_streak % GAMI_DAILY_GOAL or GAMI_DAILY_GOAL}/{GAMI_DAILY_GOAL}"
        ),
    }


async def spin_wheel(telegram_id: int) -> dict:
    eligible, reason = await _eligibility(telegram_id)
    if not eligible:
        raise GamificationError(reason or "Недоступно", code="not_eligible")

    day = _today()
    async with session_maker() as session:
        claim = await reserve_bonus_claim(session, telegram_id, "wheel", day)
    if claim is None:
        raise GamificationError("Колесо уже крутили сегодня. Возвращайся завтра!", code="already")

    segment = pick_segment()
    reward = {"kind": segment["kind"], "value": segment["value"]}
    try:
        if segment["kind"] == "promo":
            from bot.db.repo import set_active_promo

            async with session_maker() as session:
                await set_active_promo(session, telegram_id, GAMI_WHEEL_PROMO_CODE)
            reward["code"] = GAMI_WHEEL_PROMO_CODE
        elif segment["kind"] in {"days", "traffic"}:
            reward.update(await _apply_grant_reward(telegram_id, segment["kind"], segment["value"]))
    except Exception as exc:
        async with session_maker() as session:
            await delete_bonus_claim(session, claim.id)
        logger.warning("wheel reward grant failed for %s", telegram_id, exc_info=True)
        raise GamificationError("VPN-сервер временно недоступен, попробуй ещё раз через минуту.", code="marzban") from exc

    async with session_maker() as session:
        state = await get_or_create_gami_state(session, telegram_id)
        state.total_spins = (state.total_spins or 0) + 1
        await session.commit()
        await set_bonus_reward(session, claim.id, reward["kind"], reward.get("value", 0))

    return {
        "ok": True,
        "segment": {"key": segment["key"], "label": segment["label"]},
        "won": segment["kind"] != "none",
        "reward": reward,
        "message": reward_message(segment["kind"], segment["value"]),
    }


async def get_status(telegram_id: int) -> dict:
    eligible, reason = await _eligibility(telegram_id)
    day = _today()
    async with session_maker() as session:
        state = await get_or_create_gami_state(session, telegram_id)
        claims = await get_today_bonus_claims(session, telegram_id, day)
        streak = state.streak or 0
        total_spins = state.total_spins or 0

    earned = await evaluate_achievements(telegram_id)
    checkin_done = "checkin" in claims
    wheel_done = "wheel" in claims
    return {
        "eligible": eligible,
        "reason": reason,
        "checkin": {
            "streak": streak,
            "goal": GAMI_DAILY_GOAL,
            "progress": streak % GAMI_DAILY_GOAL,
            "reward_days": GAMI_DAILY_REWARD_DAYS,
            "claimed_today": checkin_done,
            "can_claim": eligible and not checkin_done,
        },
        "wheel": {
            "spun_today": wheel_done,
            "can_spin": eligible and not wheel_done,
            "total_spins": total_spins,
            "segments": [{"key": s["key"], "label": s["label"]} for s in GAMI_WHEEL_SEGMENTS],
        },
        "achievements": [{**a, "earned": a["code"] in earned} for a in ACHIEVEMENTS],
    }
