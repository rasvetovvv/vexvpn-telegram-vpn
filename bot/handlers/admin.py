"""Админ-панель: статистика и рассылка."""
from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import keyboards, texts
from bot.config import Plan, settings
from bot.db.database import session_maker
from bot.db.repo import (
    SUPPORT_TOPIC_TITLES,
    add_support_message,
    count_open_tickets,
    disable_promo_code,
    get_stats,
    get_ticket,
    get_ticket_messages,
    get_user_admin_snapshot,
    list_admin_tickets,
    log_admin_action,
    payment_reconciliation,
    set_ticket_status,
    upsert_promo_code,
    upsert_subscription,
    users_for_broadcast,
)
from bot.services.marzban import MarzbanError, marzban
from bot.services.promos import get_promo, parse_promo_create_args, promo_use_count

router = Router()
_ADMIN_RATE: dict[tuple[int, str], float] = {}
_PENDING_TTL_SECONDS = 30 * 60
_PENDING_BROADCAST: dict[int, tuple[float, str, str, list[int]]] = {}
_PENDING_REPLY: dict[int, tuple[float, int]] = {}  # admin_id -> (created_at, ticket_id)


def _admin_limited(admin_id: int, action: str, seconds: int = 20) -> bool:
    now = asyncio.get_event_loop().time()
    key = (admin_id, action)
    last = _ADMIN_RATE.get(key, 0)
    if now - last < seconds:
        return True
    _ADMIN_RATE[key] = now
    return False


def _now() -> float:
    return asyncio.get_event_loop().time()


def _cleanup_pending() -> None:
    now = _now()
    for admin_id, (created_at, *_rest) in list(_PENDING_BROADCAST.items()):
        if now - created_at > _PENDING_TTL_SECONDS:
            _PENDING_BROADCAST.pop(admin_id, None)
    for admin_id, (created_at, _ticket_id) in list(_PENDING_REPLY.items()):
        if now - created_at > _PENDING_TTL_SECONDS:
            _PENDING_REPLY.pop(admin_id, None)


def _pending_reply_admin_ids() -> set[int]:
    _cleanup_pending()
    return set(_PENDING_REPLY)


def _set_pending_broadcast(admin_id: int, segment: str, body: str, ids: list[int]) -> None:
    _cleanup_pending()
    _PENDING_BROADCAST[admin_id] = (_now(), segment, body, ids)


def _pop_pending_broadcast(admin_id: int) -> tuple[str, str, list[int]] | None:
    _cleanup_pending()
    payload = _PENDING_BROADCAST.pop(admin_id, None)
    if not payload:
        return None
    created_at, segment, body, ids = payload
    if _now() - created_at > _PENDING_TTL_SECONDS:
        return None
    return segment, body, ids


def _set_pending_reply(admin_id: int, ticket_id: int) -> None:
    _cleanup_pending()
    _PENDING_REPLY[admin_id] = (_now(), ticket_id)


def _pop_pending_reply(admin_id: int) -> int | None:
    _cleanup_pending()
    payload = _PENDING_REPLY.pop(admin_id, None)
    if not payload:
        return None
    created_at, ticket_id = payload
    if _now() - created_at > _PENDING_TTL_SECONDS:
        return None
    return ticket_id


def _confirm_markup(action: str, telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin_confirm:{action}:{telegram_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data="admin_confirm:cancel:0")],
    ])


def _broadcast_confirm_markup(admin_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить рассылку", callback_data=f"broadcast_confirm:{admin_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data="broadcast_confirm:cancel")],
    ])


@router.callback_query(F.data == "admin")
async def show_admin(cq: CallbackQuery) -> None:
    if not settings.is_admin(cq.from_user.id):
        await cq.answer("Нет доступа", show_alert=True)
        return

    async with session_maker() as session:
        stats = await get_stats(session)

    await cq.message.edit_text(texts.ADMIN_STATS.format(**stats), reply_markup=keyboards.admin_menu())
    await cq.answer()


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not settings.is_admin(message.from_user.id):
        return
    async with session_maker() as session:
        stats = await get_stats(session)
    await message.answer(texts.ADMIN_STATS.format(**stats), reply_markup=keyboards.admin_menu())


@router.callback_query(F.data == "admin_broadcast_help")
async def broadcast_help(cq: CallbackQuery) -> None:
    if not settings.is_admin(cq.from_user.id):
        await cq.answer("Нет доступа", show_alert=True)
        return
    await cq.message.answer(
        "📣 <b>Рассылка</b>\n\n"
        "<code>/broadcast &lt;сегмент&gt; текст</code>\n\n"
        "Сегменты:\n"
        "• <code>all</code> — все пользователи\n"
        "• <code>active</code> — активные подписки\n"
        "• <code>expired</code> — истёкшие подписки\n"
        "• <code>no_purchase</code> — стартовали, но не покупали\n"
        "• <code>plan:standard_30d</code> — текущий тариф\n"
        "• <code>bought:standard_30d</code> — когда-либо покупали тариф\n\n"
        "Пример:\n"
        "<code>/broadcast expired Вернись со скидкой 30% — промокод SALE30</code>"
    )
    await cq.answer()


_VALID_SEGMENTS = {"all", "active", "expired", "no_purchase"}


@router.message(Command("broadcast"))
async def broadcast(message: Message) -> None:
    if not settings.is_admin(message.from_user.id):
        return

    raw = message.text or ""
    parts = raw.split(maxsplit=2)
    segment = parts[1] if len(parts) >= 2 else ""
    if len(parts) < 3 or not (segment in _VALID_SEGMENTS or segment.startswith("plan:") or segment.startswith("bought:")):
        await message.answer(
            "Формат: <code>/broadcast &lt;сегмент&gt; текст</code>\n"
            "Сегменты: <code>all</code>, <code>active</code>, <code>expired</code>, <code>plan:КЛЮЧ</code>"
        )
        return

    body = parts[2]
    async with session_maker() as session:
        ids = await users_for_broadcast(session, segment)

    if not ids:
        await message.answer("В этом сегменте нет получателей.")
        return
    if _admin_limited(message.from_user.id, "broadcast", 30):
        await message.answer("⏳ Rate limit: подожди 30 секунд перед новой рассылкой.")
        return
    _set_pending_broadcast(message.from_user.id, segment, body, ids)
    await message.answer(
        f"📣 <b>Preview рассылки</b>\nСегмент: <code>{html.escape(segment)}</code>\nПолучателей: <b>{len(ids)}</b>\n\n{html.escape(body[:1000])}",
        reply_markup=_broadcast_confirm_markup(message.from_user.id),
    )
    async with session_maker() as session:
        await log_admin_action(session, message.from_user.id, "broadcast_preview", segment, f"recipients={len(ids)}")
    return


async def _send_broadcast(message: Message, segment: str, body: str, ids: list[int]) -> None:
    sent = 0
    failed = 0
    blocked = 0
    status = await message.answer(f"📣 Рассылка «{segment}»: {len(ids)} получателей…")
    for i, tg_id in enumerate(ids, 1):
        try:
            await message.bot.send_message(tg_id, body, disable_web_page_preview=True)
            sent += 1
        except TelegramRetryAfter as exc:
            # Флуд-лимит Telegram — ждём и повторяем один раз.
            await asyncio.sleep(exc.retry_after + 1)
            try:
                await message.bot.send_message(tg_id, body, disable_web_page_preview=True)
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            blocked += 1  # пользователь заблокировал бота
        except Exception:
            failed += 1
        if i % 25 == 0:
            try:
                await status.edit_text(f"📣 Рассылка «{segment}»: {i}/{len(ids)}…")
            except Exception:
                pass
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"✅ Рассылка «{segment}» завершена\n"
        f"Отправлено: <b>{sent}</b>\n"
        f"Заблокировали бота: <b>{blocked}</b>\n"
        f"Ошибок: <b>{failed}</b>"
    )



@router.callback_query(F.data.startswith("broadcast_confirm:"))
async def broadcast_confirm(cq: CallbackQuery) -> None:
    if not settings.is_admin(cq.from_user.id):
        await cq.answer("Нет доступа", show_alert=True)
        return
    action = cq.data.split(":", 1)[1]
    if action == "cancel":
        _PENDING_BROADCAST.pop(cq.from_user.id, None)
        await cq.message.edit_text("Рассылка отменена.")
        await cq.answer()
        return
    if int(action) != cq.from_user.id:
        await cq.answer("Это подтверждение не для тебя", show_alert=True)
        return
    payload = _pop_pending_broadcast(cq.from_user.id)
    if not payload:
        await cq.answer("Preview устарел. Запусти /broadcast заново.", show_alert=True)
        return
    segment, body, ids = payload
    async with session_maker() as session:
        await log_admin_action(session, cq.from_user.id, "broadcast_confirm", segment, f"recipients={len(ids)}")
    await _send_broadcast(cq.message, segment, body, ids)
    await cq.answer()


@router.message(Command("promo_create"))
async def promo_create(message: Message) -> None:
    if not settings.is_admin(message.from_user.id):
        return
    code, result = parse_promo_create_args(message.text or "")
    if code is None:
        await message.answer(str(result))
        return
    async with session_maker() as session:
        await upsert_promo_code(session, result)
        await log_admin_action(session, message.from_user.id, "promo_create", code, result.title)
    flags = []
    if result.new_only:
        flags.append("только новые")
    if result.old_only:
        flags.append("только старые")
    if not result.once_per_user:
        flags.append("многоразовый")
    await message.answer(
        f"Промокод <code>{html.escape(code)}</code> создан\n"
        f"Тип: <b>{result.kind}</b>\n"
        f"Значение: <b>{result.percent or result.value or result.free_plan_key}</b>\n"
        f"Ограничения: {', '.join(flags) if flags else 'одноразовый на пользователя'}"
    )


@router.message(Command("promo_stats"))
async def promo_stats(message: Message) -> None:
    if not settings.is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: <code>/promo_stats FREE30</code>")
        return
    code = parts[1].strip().upper()
    async with session_maker() as session:
        promo = await get_promo(session, code)
        used = await promo_use_count(session, code)
    if not promo:
        await message.answer("Промокод не найден или отключён.")
        return
    await message.answer(
        f"<b>{html.escape(promo.code)}</b>\n"
        f"Название: {html.escape(promo.title)}\n"
        f"Тип: <code>{promo.kind}</code>\n"
        f"Скидка: <b>{promo.percent}%</b>\n"
        f"Значение: <b>{promo.value}</b>\n"
        f"Активаций: <b>{used}</b>" + (f" / {promo.global_limit}" if promo.global_limit else "")
    )


@router.message(Command("promo_disable"))
async def promo_disable(message: Message) -> None:
    if not settings.is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: <code>/promo_disable FREE30</code>")
        return
    code = parts[1].strip().upper()
    async with session_maker() as session:
        ok = await disable_promo_code(session, code)
        await log_admin_action(session, message.from_user.id, "promo_disable", code, "ok" if ok else "not_found")
    await message.answer("Промокод отключён." if ok else "Динамический промокод не найден. Статические FREE7/SALE30 отключаются через config.")


@router.message(Command("payments_check"))
async def cmd_payments_check(message: Message) -> None:
    if not settings.is_admin(message.from_user.id):
        return
    if _admin_limited(message.from_user.id, "payments_check", 15):
        await message.answer("⏳ Подожди 15 секунд перед повторной проверкой.")
        return
    async with session_maker() as session:
        report = await payment_reconciliation(session)
        await log_admin_action(session, message.from_user.id, "payments_check", str(message.from_user.id))
    await message.answer(
        "🧾 <b>Payments reconciliation</b>\n"
        f"success без subscription: <b>{len(report['success_without_subscription'])}</b>\n"
        f"problem/marzban_error: <b>{len(report['problem_payments'])}</b>\n"
        f"дубли charge_id: <b>{len(report['duplicate_charge_ids'])}</b>\n"
        f"без charge_id: <b>{len(report['missing_charge_id'])}</b>\n"
        f"странные суммы: <b>{len(report['strange_amounts'])}</b>"
    )


@router.message(Command("user"))
async def user_lookup(message: Message) -> None:
    if not settings.is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Формат: <code>/user 944763501</code>")
        return
    telegram_id = int(parts[1].strip())
    async with session_maker() as session:
        snap = await get_user_admin_snapshot(session, telegram_id)
    user = snap["user"]
    sub = snap["subscription"]
    last = snap["last_payment"]
    now = datetime.now(timezone.utc)
    if sub:
        exp = sub.expire_at if sub.expire_at.tzinfo else sub.expire_at.replace(tzinfo=timezone.utc)
        sub_text = (
            f"Статус: <b>{'active' if exp > now else 'expired'}</b>\n"
            f"Тариф: <code>{html.escape(sub.plan)}</code>\n"
            f"Трафик: <b>{sub.traffic_limit}</b> bytes\n"
            f"До: <b>{sub.expire_at}</b>"
        )
    else:
        sub_text = "Подписки нет"
    await message.answer(
        f"<b>User {telegram_id}</b>\n"
        f"Username: @{html.escape(user.username) if user and user.username else '—'}\n"
        f"Покупок: <b>{snap['payments_count']}</b>\n"
        f"Stars: <b>{snap['stars_sum']}</b>\n\n"
        f"{sub_text}",
        reply_markup=keyboards.admin_user_menu(telegram_id, bool(last and last.charge_id)),
    )


@router.callback_query(F.data.startswith("admin_confirm:"))
async def admin_confirm_action(cq: CallbackQuery) -> None:
    if not settings.is_admin(cq.from_user.id):
        await cq.answer("Нет доступа", show_alert=True)
        return
    _, action, tg = cq.data.split(":", 2)
    if action == "cancel":
        await cq.message.edit_text("Действие отменено.")
        await cq.answer()
        return
    if action not in {"reset", "disable", "refund"}:
        await cq.answer("Unknown action", show_alert=True)
        return
    telegram_id = int(tg)
    try:
        if action == "reset":
            await marzban.reset_traffic(telegram_id)
            async with session_maker() as session:
                await log_admin_action(session, cq.from_user.id, "user_reset_traffic", str(telegram_id))
            await cq.message.edit_text(f"✅ Трафик сброшен для {telegram_id}")
            await cq.answer("Готово", show_alert=True)
        elif action == "disable":
            await marzban.set_status(telegram_id, "disabled")
            async with session_maker() as session:
                await log_admin_action(session, cq.from_user.id, "user_disable", str(telegram_id))
            await cq.message.edit_text(f"✅ Пользователь {telegram_id} отключён")
            await cq.answer("Готово", show_alert=True)
        elif action == "refund":
            if not settings.is_super_admin(cq.from_user.id):
                await cq.answer("Только super-admin", show_alert=True)
                return
            async with session_maker() as session:
                snap = await get_user_admin_snapshot(session, telegram_id)
            last = snap["last_payment"]
            if not last or not last.charge_id:
                await cq.answer("Нет платежа для refund", show_alert=True)
                return
            await cq.bot.refund_star_payment(user_id=telegram_id, telegram_payment_charge_id=last.charge_id)
            async with session_maker() as session:
                await log_admin_action(session, cq.from_user.id, "user_refund", str(telegram_id), last.charge_id)
            await cq.message.edit_text(f"✅ Refund отправлен для {telegram_id}")
            await cq.answer("Refund отправлен", show_alert=True)
    except MarzbanError as exc:
        await cq.answer(f"Marzban error: {exc}", show_alert=True)
    except Exception as exc:
        await cq.answer(f"Ошибка: {exc}", show_alert=True)


# ─── Поддержка: ответы/закрытие тикетов из бота ──────────────────────
def _render_admin_thread(t, msgs) -> str:
    title = SUPPORT_TOPIC_TITLES.get(t.topic, "Другое")
    lines = [
        f"🆘 <b>Тикет #{t.id}</b> · {html.escape(title)}",
        f"User: <code>{t.telegram_id}</code> · статус: <b>{t.status}</b>",
        "",
    ]
    for m in msgs[-15:]:
        who = "🟦 User" if m.sender == "user" else "🟩 Admin"
        lines.append(f"<b>{who}:</b>\n{html.escape(m.text)}")
        lines.append("")
    return "\n".join(lines).strip()


@router.message(Command("tickets"))
async def cmd_tickets(message: Message) -> None:
    if not settings.is_admin(message.from_user.id):
        return
    async with session_maker() as session:
        rows = await list_admin_tickets(session, status="active", limit=30)
        open_count = await count_open_tickets(session)
    if not rows:
        await message.answer(texts.ADMIN_TICKETS_NONE)
        return
    await message.answer(
        f"🆘 <b>Обращения</b> (открытых: <b>{open_count}</b>)\nВыбери тикет:",
        reply_markup=keyboards.admin_tickets_list_kb(rows),
    )


@router.callback_query(F.data.startswith("aticket:"))
async def admin_view_ticket(cq: CallbackQuery) -> None:
    if not settings.is_admin(cq.from_user.id):
        await cq.answer("Нет доступа", show_alert=True)
        return
    ticket_id = int(cq.data.split(":", 1)[1])
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        if not t:
            await cq.answer("Тикет не найден", show_alert=True)
            return
        msgs = await get_ticket_messages(session, ticket_id)
    await cq.message.answer(_render_admin_thread(t, msgs), reply_markup=keyboards.admin_ticket_kb(ticket_id))
    await cq.answer()


@router.callback_query(F.data.startswith("areply:"))
async def admin_reply_start(cq: CallbackQuery) -> None:
    if not settings.is_admin(cq.from_user.id):
        await cq.answer("Нет доступа", show_alert=True)
        return
    ticket_id = int(cq.data.split(":", 1)[1])
    _set_pending_reply(cq.from_user.id, ticket_id)
    await cq.message.answer(f"✍️ Напиши ответ для тикета #{ticket_id} одним сообщением. Отмена — /cancel")
    await cq.answer()


@router.callback_query(F.data.startswith("aclose:"))
async def admin_close_ticket(cq: CallbackQuery) -> None:
    if not settings.is_admin(cq.from_user.id):
        await cq.answer("Нет доступа", show_alert=True)
        return
    ticket_id = int(cq.data.split(":", 1)[1])
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        if not t:
            await cq.answer("Тикет не найден", show_alert=True)
            return
        target = t.telegram_id
        await set_ticket_status(session, ticket_id, "closed")
        await log_admin_action(session, cq.from_user.id, "support_close", str(target), f"ticket={ticket_id}")
    _PENDING_REPLY.pop(cq.from_user.id, None)
    try:
        await cq.bot.send_message(target, f"✅ Заявка #{ticket_id} закрыта поддержкой. Если вопрос остался — напиши снова.")
    except Exception:
        pass
    await cq.answer("Закрыто", show_alert=True)


@router.message(Command("cancel"))
async def admin_cancel_reply(message: Message) -> None:
    if _pop_pending_reply(message.from_user.id) is not None:
        await message.answer("Ответ отменён.")


@router.message(lambda m: bool(m.from_user) and m.from_user.id in _pending_reply_admin_ids(), F.text)
async def admin_reply_collect(message: Message) -> None:
    ticket_id = _pop_pending_reply(message.from_user.id)
    if ticket_id is None:
        return
    body = (message.text or "")[:1900]
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        if not t:
            await message.answer("Тикет не найден.")
            return
        target = t.telegram_id
        await add_support_message(session, ticket_id, "admin", body, admin_id=message.from_user.id)
        await log_admin_action(session, message.from_user.id, "support_reply", str(target), f"ticket={ticket_id}")
    try:
        await message.bot.send_message(
            target,
            f"💬 <b>Ответ поддержки</b> по заявке #{ticket_id}:\n\n{html.escape(body[:1500])}",
            reply_markup=keyboards.ticket_user_menu(ticket_id),
        )
    except Exception:
        pass
    await message.answer(f"✅ Ответ отправлен в тикет #{ticket_id}.")


@router.callback_query(F.data.startswith("admin_user:"))
async def admin_user_action(cq: CallbackQuery) -> None:
    if not settings.is_admin(cq.from_user.id):
        await cq.answer("Нет доступа", show_alert=True)
        return
    _, action, tg = cq.data.split(":", 2)
    telegram_id = int(tg)
    try:
        if action in {"add7", "add30"}:
            days = 7 if action == "add7" else 30
            plan = Plan(f"admin_{days}d", f"Admin +{days} дней", days, 0, 0, 1, visible=False)
            result = await marzban.create_or_renew(telegram_id, plan)
            expire_at = datetime.fromtimestamp(result["expire"], tz=timezone.utc)
            async with session_maker() as session:
                await upsert_subscription(session, telegram_id=telegram_id, marzban_username=result["username"], subscription_url=result["subscription_url"], plan=plan.key, expire_at=expire_at, traffic_limit=result["data_limit"])
                await log_admin_action(session, cq.from_user.id, f"user_{action}", str(telegram_id), f"+{days}d")
            await cq.answer(f"Начислено +{days} дней", show_alert=True)
        elif action == "reset":
            await cq.message.answer(f"Подтвердить reset traffic для {telegram_id}?", reply_markup=_confirm_markup("reset", telegram_id))
            await cq.answer()
            return
        elif action == "reset_confirmed":
            await marzban.reset_traffic(telegram_id)
            async with session_maker() as session:
                await log_admin_action(session, cq.from_user.id, "user_reset_traffic", str(telegram_id))
            await cq.answer("Трафик сброшен", show_alert=True)
        elif action == "disable":
            await cq.message.answer(f"Подтвердить disable user {telegram_id}?", reply_markup=_confirm_markup("disable", telegram_id))
            await cq.answer()
            return
        elif action == "disable_confirmed":
            await marzban.set_status(telegram_id, "disabled")
            async with session_maker() as session:
                await log_admin_action(session, cq.from_user.id, "user_disable", str(telegram_id))
            await cq.answer("Пользователь отключён", show_alert=True)
        elif action == "refund":
            await cq.message.answer(f"Подтвердить refund last payment для {telegram_id}?", reply_markup=_confirm_markup("refund", telegram_id))
            await cq.answer()
            return
        elif action == "refund_confirmed":
            if not settings.is_super_admin(cq.from_user.id):
                await cq.answer("Только super-admin", show_alert=True)
                return
            async with session_maker() as session:
                snap = await get_user_admin_snapshot(session, telegram_id)
            last = snap["last_payment"]
            if not last or not last.charge_id:
                await cq.answer("Нет платежа для refund", show_alert=True)
                return
            await cq.bot.refund_star_payment(user_id=telegram_id, telegram_payment_charge_id=last.charge_id)
            async with session_maker() as session:
                await log_admin_action(session, cq.from_user.id, "user_refund", str(telegram_id), last.charge_id)
            await cq.answer("Refund отправлен", show_alert=True)
    except MarzbanError as exc:
        await cq.answer(f"Marzban error: {exc}", show_alert=True)
    except Exception as exc:
        await cq.answer(f"Ошибка: {exc}", show_alert=True)
