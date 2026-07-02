"""Команда /start, промокоды, рефералка и навигация."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import keyboards, texts
from bot.config import settings
from bot.db.database import session_maker
from bot.db.repo import ensure_user, get_user, log_event, set_active_promo
from bot.handlers.payments import _grant_subscription
from bot.services.plans import get_visible_plans
from bot.services.promos import get_promo, promo_to_plan, reserve_promo_for_user, validate_promo_for_user

router = Router()


class PromoFlow(StatesGroup):
    waiting_code = State()


def _welcome_text() -> str:
    return texts.WELCOME.format(servers=settings.servers_online)


async def _show_plans(message: Message) -> None:
    async with session_maker() as session:
        plans = await get_visible_plans(session)
    await message.answer(
        texts.CHOOSE_PLAN.format(plans=texts.plans_block(plans)),
        reply_markup=keyboards.plans_menu(plans, settings.is_admin(message.from_user.id)),
    )


@router.message(CommandStart(deep_link=True))
async def cmd_start_ref(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    referred_by = None
    if command.args and command.args.startswith("ref_"):
        try:
            referred_by = int(command.args.removeprefix("ref_"))
        except ValueError:
            referred_by = None
    async with session_maker() as session:
        await ensure_user(session, message.from_user.id, message.from_user.username, referred_by)
        await log_event(session, message.from_user.id, "start", command.args or "")
    # Deeplink ?start=buy открывает сразу выбор тарифа.
    if command.args == "buy":
        await _show_plans(message)
        return
    await message.answer(_welcome_text(), reply_markup=keyboards.main_menu(settings.is_admin(message.from_user.id)))


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with session_maker() as session:
        await ensure_user(session, message.from_user.id, message.from_user.username)
        await log_event(session, message.from_user.id, "start", "")
    await message.answer(_welcome_text(), reply_markup=keyboards.main_menu(settings.is_admin(message.from_user.id)))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=keyboards.main_menu(settings.is_admin(message.from_user.id)), disable_web_page_preview=True)


@router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject) -> None:
    code = (command.args or "").strip().upper()
    if not code:
        await message.answer(texts.PROMO_HELP, reply_markup=keyboards.back_menu())
        return
    await _apply_promo(message, code)


async def _apply_promo(message: Message, code: str) -> None:
    code = code.strip().upper()
    if not code:
        await message.answer(texts.PROMO_INVALID)
        return

    async with session_maker() as session:
        await ensure_user(session, message.from_user.id, message.from_user.username)
        promo = await get_promo(session, code)
        user = await get_user(session, message.from_user.id)
        err = await validate_promo_for_user(session, message.from_user.id, promo) if promo else "Промокод не найден"

    if not promo or err:
        await message.answer(err or texts.PROMO_INVALID)
        return

    if promo.free_plan_key or promo.kind in {"days", "traffic", "trial"}:
        async with session_maker() as session:
            plan = await promo_to_plan(session, promo)
            if plan is None:
                await message.answer(texts.PROMO_INVALID)
                return
            reserve_err = await reserve_promo_for_user(session, message.from_user.id, promo)
        if reserve_err:
            await message.answer(reserve_err)
            return
        await message.answer(f"🎁 Промокод {code} принят. Выдаю «{plan.title}» бесплатно…")
        # Детерминированный charge_id — защита от повторной выдачи.
        await _grant_subscription(
            message,
            message.from_user.id,
            plan,
            stars_amount=0,
            charge_id=f"PROMO-{code}-{message.from_user.id}",
            promo_code=code,
            allow_trial_repeat=not plan.is_trial,
            promo_already_reserved=True,
        )
        return

    # Скидочный промокод применяется к следующему счёту.
    if user is None:
        await message.answer(texts.PROMO_INVALID)
        return
    async with session_maker() as session:
        await set_active_promo(session, message.from_user.id, code)
        plans = await get_visible_plans(session)
    await message.answer(
        texts.PROMO_APPLIED.format(code=code, details=f"Скидка <b>{promo.percent}%</b> на следующую покупку."),
        reply_markup=keyboards.plans_menu(plans, settings.is_admin(message.from_user.id)),
    )


@router.callback_query(F.data == "promo")
async def show_promo(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoFlow.waiting_code)
    await cq.message.edit_text(texts.PROMO_ENTER, reply_markup=keyboards.back_menu())
    await cq.answer()


@router.message(PromoFlow.waiting_code, F.text & ~F.text.startswith("/"))
async def promo_code_received(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _apply_promo(message, message.text or "")


@router.callback_query(F.data == "referral")
async def show_referral(cq: CallbackQuery) -> None:
    me = await cq.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{cq.from_user.id}"
    await cq.message.edit_text(texts.REFERRAL.format(link=link), reply_markup=keyboards.back_menu())
    await cq.answer()


@router.callback_query(F.data == "back")
async def back_to_menu(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cq.message.edit_text(_welcome_text(), reply_markup=keyboards.main_menu(settings.is_admin(cq.from_user.id)))
    await cq.answer()


@router.callback_query(F.data == "close")
async def close_menu(cq: CallbackQuery) -> None:
    await cq.message.delete()
    await cq.answer()
