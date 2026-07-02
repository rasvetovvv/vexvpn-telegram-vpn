"""Оплата Telegram Stars и выдача подписки."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery

from bot import keyboards, texts
from bot.config import (
    PLANS,
    PROMOS,
    REFERRAL_BONUS_DAYS,
    REFERRAL_MAX_PER_DAY,
    REFERRAL_MIN_PAYMENT_STARS,
    Plan,
    settings,
)
from bot.db.database import session_maker
from bot.db.repo import (
    add_payment,
    enqueue_grant,
    ensure_user,
    finalize_grant,
    get_payment_by_charge_id,
    get_subscription,
    get_user,
    has_used_promo,
    log_event,
    log_marzban,
    mark_promo_used,
    reward_referral_once,
    set_active_promo,
    update_payment_status,
    upsert_subscription,
)
from bot.services import payments as pay
from bot.services.marzban import MarzbanError, marzban
from bot.services.plans import get_effective_plan, get_visible_plans
from bot.services.promos import get_promo, validate_promo_for_user
from bot.utils import fmt_date, fmt_traffic

logger = logging.getLogger(__name__)
router = Router()
_BACKGROUND_TASKS: set[asyncio.Task] = set()

DAILY_FREE_DAYS = 1
DAILY_FREE_TRAFFIC_GB = 100
DAILY_FREE_PLAN = Plan(
    "daily_free",
    "Ежедневный бесплатный VPN",
    DAILY_FREE_DAYS,
    0,
    DAILY_FREE_TRAFFIC_GB,
    1,
    "каждый день",
    visible=False,
)


async def _notify_admins(bot, text: str) -> None:
    for admin_id in settings.admin_id_set:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.exception("Не удалось уведомить админа %s", admin_id)


async def _post_purchase_followup(bot, telegram_id: int) -> None:
    await asyncio.sleep(10 * 60)
    try:
        await bot.send_message(telegram_id, texts.POST_PURCHASE_CHECK, reply_markup=keyboards.success_menu())
    except Exception:
        logger.exception("Не удалось отправить follow-up после покупки %s", telegram_id)


def _schedule_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def _validate_plan_for_user(
    session,
    telegram_id: int,
    plan: Plan,
    promo_code: str | None = None,
    *,
    allow_trial_repeat: bool = False,
    promo_already_reserved: bool = False,
) -> str | None:
    """Вернуть текст ошибки, если счёт/выдача сейчас недопустимы."""
    user = await get_user(session, telegram_id)
    if plan.is_trial and user and user.trial_used and not allow_trial_repeat:
        return "Пробный период доступен только один раз"

    sub = await get_subscription(session, telegram_id)
    now = datetime.now(timezone.utc)
    if plan.traffic_only and (not sub or (sub.expire_at if sub.expire_at.tzinfo else sub.expire_at.replace(tzinfo=timezone.utc)) <= now):
        return "Пакет трафика можно купить только к активной подписке"

    if promo_code and not promo_already_reserved:
        promo = await get_promo(session, promo_code)
        # допускаем скидочные и бесплатные промокоды
        if not promo or not (promo.percent or promo.free_plan_key or promo.kind in {"days", "traffic", "trial"}):
            return "Промокод для этого счёта недействителен"
        err = await validate_promo_for_user(session, telegram_id, promo)
        if err:
            return err

    return None


async def _grant_subscription(
    target: Message,
    telegram_id: int,
    plan: Plan,
    *,
    stars_amount: int,
    charge_id: str,
    promo_code: str | None = None,
    allow_trial_repeat: bool = False,
    promo_already_reserved: bool = False,
) -> None:
    """Создать/продлить подписку и ответить пользователю."""
    async with session_maker() as session:
        await ensure_user(session, telegram_id, target.chat.username if hasattr(target.chat, "username") else None)
        existing_payment = await get_payment_by_charge_id(session, charge_id)
        if existing_payment and existing_payment.status in {"success", "manual", "pending"}:
            await target.answer("✅ Этот платёж уже обработан. Повторно дни/трафик не начисляю, чтобы не было дубля.")
            return

        error = await _validate_plan_for_user(
            session,
            telegram_id,
            plan,
            promo_code,
            allow_trial_repeat=allow_trial_repeat,
            promo_already_reserved=promo_already_reserved,
        )
        if error:
            # Если деньги уже списаны (реальная оплата), но выдать нельзя —
            # фиксируем платёж со статусом-ошибкой, чтобы он был виден админу для рефанда.
            if stars_amount > 0:
                await add_payment(
                    session,
                    telegram_id=telegram_id,
                    plan=plan.key,
                    stars_amount=stars_amount,
                    charge_id=charge_id,
                    promo_code=promo_code,
                    status="validation_error",
                    error_message=error,
                )
            await target.answer(f"⚠️ {error}. Если Stars уже списались — напиши в поддержку: @{settings.support_username}")
            return

        if existing_payment and existing_payment.status == "marzban_error":
            payment = existing_payment
            await update_payment_status(session, payment.id, "pending")
        else:
            payment = await add_payment(
                session,
                telegram_id=telegram_id,
                plan=plan.key,
                stars_amount=stars_amount,
                charge_id=charge_id,
                promo_code=promo_code,
                status="pending",
            )
            if getattr(payment, "_was_created", True) is False:
                await target.answer("⏳ Этот платёж уже обрабатывается. Повторно дни/трафик не начисляю.")
                return

    try:
        result = await marzban.create_or_renew(telegram_id, plan)
    except MarzbanError as exc:
        logger.exception("Marzban не выдал подписку для %s", telegram_id)
        async with session_maker() as session:
            await update_payment_status(session, payment.id, "marzban_error", str(exc))
            await enqueue_grant(
                session,
                telegram_id=telegram_id,
                payment_id=payment.id,
                charge_id=charge_id,
                plan=plan,
                stars_amount=stars_amount,
                promo_code=promo_code,
                last_error=str(exc),
            )
            await log_marzban(session, telegram_id, "create_or_renew", "error", str(exc))
        await target.answer(texts.ERROR_MARZBAN.format(support=settings.support_username, charge_id=charge_id) + "\n\n⏳ Я поставил выдачу в очередь. Бот будет повторять автоматически каждые 1–5 минут.")
        await _notify_admins(
            target.bot,
            texts.ADMIN_MARZBAN_ERROR.format(
                telegram_id=telegram_id,
                plan=plan.key,
                charge_id=charge_id,
                error=str(exc)[:900],
            ),
        )
        return

    expire_at = datetime.fromtimestamp(result["expire"], tz=timezone.utc)

    async with session_maker() as session:
        # Докупка трафика не меняет тариф и дату окончания — сохраняем прежний план.
        store_plan_key = plan.key
        if plan.traffic_only:
            existing_sub = await get_subscription(session, telegram_id)
            if existing_sub and existing_sub.plan:
                store_plan_key = existing_sub.plan
        # Атомарно: подписка + trial + сброс промо + статус платежа + логи (один commit).
        await finalize_grant(
            session,
            telegram_id=telegram_id,
            marzban_username=result["username"],
            subscription_url=result["subscription_url"],
            plan_key=store_plan_key,
            expire_at=expire_at,
            traffic_limit=result["data_limit"],
            is_trial=plan.is_trial,
            clear_active_promo=bool(promo_code),
            payment_id=payment.id,
            log_message=f"plan={plan.key}; payment_id={payment.id}",
            log_paid=stars_amount > 0,
        )
        # Free promo grants reserve PromoUse before Marzban work to close
        # validation/grant races. Paid/discount flows still mark after success.
        if promo_code and not promo_already_reserved:
            await mark_promo_used(session, telegram_id, promo_code)

    await target.answer(
        texts.PAYMENT_SUCCESS.format(
            title=plan.title,
            expire=fmt_date(expire_at),
            traffic=fmt_traffic(result["data_limit"]),
            devices=plan.devices_label,
            url=result["subscription_url"],
        ),
        reply_markup=keyboards.success_menu(),
        disable_web_page_preview=True,
    )

    if stars_amount > 0:
        _schedule_background(_post_purchase_followup(target.bot, telegram_id))

    # Реферальный бонус: только за реальную покупку (анти-фрод против фарма триалов)
    # и не чаще REFERRAL_MAX_PER_DAY начислений одному рефереру в сутки.
    if stars_amount >= REFERRAL_MIN_PAYMENT_STARS:
        async with session_maker() as session:
            referrer_id = await reward_referral_once(session, telegram_id, REFERRAL_MAX_PER_DAY)
        if referrer_id:
            bonus_plan = Plan("ref_bonus_3d", "Реферальный бонус", REFERRAL_BONUS_DAYS, 0, 0, 1, visible=False)
            try:
                bonus = await marzban.create_or_renew(referrer_id, bonus_plan)
                bonus_expire = datetime.fromtimestamp(bonus["expire"], tz=timezone.utc)
                async with session_maker() as session:
                    await upsert_subscription(
                        session,
                        telegram_id=referrer_id,
                        marzban_username=bonus["username"],
                        subscription_url=bonus["subscription_url"],
                        plan=bonus_plan.key,
                        expire_at=bonus_expire,
                        traffic_limit=bonus["data_limit"],
                    )
                await target.bot.send_message(referrer_id, texts.REFERRAL_BONUS)
            except Exception:
                logger.exception("Не удалось начислить реферальный бонус %s", referrer_id)


async def _grant_daily_free(target: Message, telegram_id: int) -> None:
    """Ежедневный бесплатный доступ: 1 день + 100 ГБ, не чаще 1 раза в UTC-день."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    charge_id = f"DAILY-FREE-{day}-{telegram_id}"

    async with session_maker() as session:
        await ensure_user(session, telegram_id, target.chat.username if hasattr(target.chat, "username") else None)
        user = await get_user(session, telegram_id)
        if not user or not user.miniapp_opened_at:
            await target.answer(
                "📲 Бесплатный VPN теперь выдаётся только через MiniApp, чтобы защититься от мультиаккаунтов.\n\n"
                "Открой WebApp, нажми «Получить бесплатный VPN сегодня» — там проверится устройство.",
                reply_markup=keyboards.main_menu(),
            )
            return
        existing_payment = await get_payment_by_charge_id(session, charge_id)
        if existing_payment and existing_payment.status == "success":
            await target.answer(
                "🎁 Бесплатный день на сегодня уже получен.\n\n"
                "Возвращайся завтра — снова дам +1 день и +100 ГБ. Если нужно больше, выбери платный тариф.",
                reply_markup=keyboards.daily_free_already_menu(),
            )
            return
        if existing_payment and existing_payment.status == "pending":
            await target.answer("⏳ Бесплатный день уже обрабатывается. Проверь подписку через минуту.")
            return
        if existing_payment and existing_payment.status == "marzban_error":
            payment = existing_payment
            await update_payment_status(session, payment.id, "pending")
        else:
            payment = await add_payment(
                session,
                telegram_id=telegram_id,
                plan=DAILY_FREE_PLAN.key,
                stars_amount=0,
                charge_id=charge_id,
                promo_code=None,
                status="pending",
            )
            if getattr(payment, "_was_created", True) is False:
                await target.answer("⏳ Бесплатный день уже обрабатывается. Проверь подписку через минуту.")
                return

    try:
        result = await marzban.create_or_renew(telegram_id, DAILY_FREE_PLAN)
    except MarzbanError as exc:
        logger.exception("Daily free grant failed for %s", telegram_id)
        async with session_maker() as session:
            await update_payment_status(session, payment.id, "marzban_error", str(exc))
            await enqueue_grant(
                session,
                telegram_id=telegram_id,
                payment_id=payment.id,
                charge_id=charge_id,
                plan=DAILY_FREE_PLAN,
                stars_amount=0,
                promo_code=None,
                last_error=str(exc),
            )
            await log_marzban(session, telegram_id, "daily_free", "error", str(exc))
        await target.answer(
            "⚠️ VPN-сервер временно не выдал бесплатный доступ. Я поставил выдачу в очередь — бот повторит автоматически.",
            reply_markup=keyboards.back_menu(),
        )
        await _notify_admins(target.bot, f"⚠️ Daily free grant error\nUser: <code>{telegram_id}</code>\nError: <code>{str(exc)[:900]}</code>")
        return

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
            log_message=f"daily_free={day}; payment_id={payment.id}",
            log_paid=False,
        )
        await log_event(session, telegram_id, "daily_free", day)

    await target.answer(
        texts.DAILY_FREE_SUCCESS.format(
            expire=fmt_date(expire_at),
            traffic=fmt_traffic(result["data_limit"]),
            url=result["subscription_url"],
        ),
        reply_markup=keyboards.success_menu(),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "daily_free")
async def daily_free_callback(cq: CallbackQuery) -> None:
    await cq.answer("Проверяю бесплатный доступ…")
    await _grant_daily_free(cq.message, cq.from_user.id)


@router.message(Command("free"))
async def cmd_free(message: Message) -> None:
    await _grant_daily_free(message, message.from_user.id)

@router.callback_query(F.data == "buy")
async def show_plans(cq: CallbackQuery) -> None:
    async with session_maker() as session:
        plans = await get_visible_plans(session)
    await cq.message.edit_text(
        texts.CHOOSE_PLAN.format(plans=texts.plans_block(plans)),
        reply_markup=keyboards.plans_menu(plans, settings.is_admin(cq.from_user.id)),
    )
    await cq.answer()


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    async with session_maker() as session:
        plans = await get_visible_plans(session)
    await message.answer(
        texts.CHOOSE_PLAN.format(plans=texts.plans_block(plans)),
        reply_markup=keyboards.plans_menu(plans, settings.is_admin(message.from_user.id)),
    )


@router.callback_query(F.data.startswith("plan:"))
async def send_invoice(cq: CallbackQuery) -> None:
    key = cq.data.split(":", 1)[1]
    async with session_maker() as session:
        plan = await get_effective_plan(session, key)
        if plan is None or not plan.visible:
            await cq.answer("Тариф не найден", show_alert=True)
            return
        await ensure_user(session, cq.from_user.id, cq.from_user.username)
        await log_event(session, cq.from_user.id, "plan_select", key)
        error = await _validate_plan_for_user(session, cq.from_user.id, plan)
        if error:
            await cq.answer(error, show_alert=True)
            return
        user = await get_user(session, cq.from_user.id)
        promo = await get_promo(session, user.active_promo_code) if user and user.active_promo_code else None
        if promo:
            err = await validate_promo_for_user(session, cq.from_user.id, promo)
            if err:
                promo = None
                await set_active_promo(session, cq.from_user.id, None)

    await cq.message.answer_invoice(
        title=f"VexVPN — {plan.title}",
        description=(
            f"Добавка трафика: {plan.traffic_label}. Дата окончания подписки не меняется."
            if plan.traffic_only
            else texts.INVOICE_DESC.format(
                title=plan.title,
                days=plan.days,
                traffic=plan.traffic_label,
                devices=plan.devices_label,
            )
        ),
        payload=pay.build_payload(plan, promo.code if promo and promo.percent else None),
        currency="XTR",
        prices=pay.build_prices(plan, promo if promo and promo.percent else None),
        provider_token="",
    )
    await cq.answer()


@router.callback_query(F.data.startswith("test:"))
async def test_grant(cq: CallbackQuery) -> None:
    """Тестовая выдача подписки без оплаты (только для админов)."""
    if not settings.is_admin(cq.from_user.id):
        await cq.answer("Только для администратора", show_alert=True)
        return

    key = cq.data.split(":", 1)[1]
    plan = PLANS.get(key)
    if plan is None:
        await cq.answer("Тариф не найден", show_alert=True)
        return

    await cq.answer("Выдаю тестовую подписку…")
    await _grant_subscription(
        cq.message,
        cq.from_user.id,
        plan,
        stars_amount=0,
        charge_id=f"TEST-{uuid.uuid4().hex[:8]}",
        allow_trial_repeat=True,
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    key, promo_code = pay.parse_payload(query.invoice_payload)
    if not key:
        await query.answer(ok=False, error_message="Некорректный счёт. Создай новый счёт в боте или WebApp.")
        return

    async with session_maker() as session:
        plan = await get_effective_plan(session, key)
        if plan is None or not plan.visible:
            await query.answer(ok=False, error_message="Тариф больше недоступен. Выбери тариф заново.")
            return
        await ensure_user(session, query.from_user.id, query.from_user.username)
        error = await _validate_plan_for_user(session, query.from_user.id, plan, promo_code)
        if error:
            await query.answer(ok=False, error_message=error)
            return
        promo = await get_promo(session, promo_code) if promo_code else None
        expected_amount = pay.price_with_promo(plan, promo if promo and promo.percent else None)

    if query.currency != "XTR" or query.total_amount != expected_amount:
        await query.answer(ok=False, error_message="Цена счёта устарела. Создай новый счёт.")
        return

    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message) -> None:
    if not message.from_user:
        logger.warning("successful_payment without from_user: chat_id=%s", getattr(message.chat, "id", None))
        return
    sp = message.successful_payment
    key, promo_code = pay.parse_payload(sp.invoice_payload)
    async with session_maker() as session:
        plan = await get_effective_plan(session, key) if key else None

    if plan is None:
        logger.error("Неизвестный payload оплаты: %s", sp.invoice_payload)
        await message.answer(texts.ERROR_MARZBAN.format(support=settings.support_username, charge_id=sp.telegram_payment_charge_id))
        return

    await _grant_subscription(
        message,
        message.from_user.id,
        plan,
        stars_amount=sp.total_amount,
        charge_id=sp.telegram_payment_charge_id,
        promo_code=promo_code,
    )
