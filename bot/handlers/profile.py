"""Профиль, инструкция, поддержка и сбор проблемы подключения."""
from __future__ import annotations

import io
from datetime import datetime, timezone
import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select

from bot import keyboards, texts
from bot.config import PLANS, settings
from bot.db.database import session_maker
from bot.db.models import SecurityEvent
from bot.db.repo import (
    SUPPORT_TOPIC_TITLES,
    add_support_message,
    create_support_ticket,
    get_subscription,
    get_ticket,
    get_ticket_messages,
    get_user,
    list_user_tickets,
    set_support_state,
    set_ticket_status,
)
from bot.services.marzban import marzban
from bot.utils import fmt_date, fmt_size, fmt_traffic, human_left

router = Router()

FAQ_TEXTS = {
    "reimport": texts.FAQ_REIMPORT,
    "network": texts.FAQ_NETWORK,
    "resetprofile": texts.FAQ_RESETPROFILE,
}


def _traffic_line(usage: dict | None, fallback_limit: int) -> str:
    """Строка трафика: использовано/осталось из live-данных Marzban, иначе лимит из БД."""
    if usage:
        limit = usage["data_limit"]
        used = usage["used_traffic"]
        if limit:
            remaining = max(0, limit - used)
            return f"{fmt_size(used)} из {fmt_traffic(limit)} · осталось {fmt_size(remaining)}"
        return f"{fmt_size(used)} использовано · ∞ безлимит"
    return fmt_traffic(fallback_limit)


async def _build_profile(telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with session_maker() as session:
        sub = await get_subscription(session, telegram_id)

    if sub is None:
        return texts.PROFILE_NONE, keyboards.profile_menu(has_active=False)

    plan = PLANS.get(sub.plan)
    plan_title = plan.title if plan else sub.plan
    expire = sub.expire_at if sub.expire_at.tzinfo else sub.expire_at.replace(tzinfo=timezone.utc)

    if expire > datetime.now(timezone.utc):
        usage = await marzban.get_usage(telegram_id)
        if usage and usage["data_limit"] and usage["used_traffic"] >= usage["data_limit"]:
            return texts.TRAFFIC_EXHAUSTED, keyboards.traffic_menu()
        text = texts.PROFILE_ACTIVE.format(
            plan=plan_title,
            expire=fmt_date(expire),
            left=human_left(expire),
            traffic=_traffic_line(usage, sub.traffic_limit),
            url=sub.subscription_url,
        )
        return text, keyboards.profile_menu(has_active=True)
    return texts.PROFILE_EXPIRED.format(plan=plan_title), keyboards.profile_menu(has_active=False)


@router.callback_query(F.data == "profile")
async def show_profile(cq: CallbackQuery) -> None:
    text, markup = await _build_profile(cq.from_user.id)
    await cq.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    await cq.answer()


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    text, markup = await _build_profile(message.from_user.id)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "security")
async def show_security(cq: CallbackQuery) -> None:
    async with session_maker() as session:
        sub = await get_subscription(session, cq.from_user.id)
        events = list(await session.scalars(
            select(SecurityEvent)
            .where(SecurityEvent.telegram_id == cq.from_user.id)
            .order_by(SecurityEvent.created_at.desc())
            .limit(5)
        ))
    has_active = bool(sub and (sub.expire_at if sub.expire_at.tzinfo else sub.expire_at.replace(tzinfo=timezone.utc)) > datetime.now(timezone.utc))
    lines = [
        "🛡 <b>Security Center</b>",
        "",
        "Что защищено сейчас:",
        "• browsing/DNS/destination logs выключены",
        "• access logs Mini App выключены",
        "• subscription link можно перевыпустить",
        "• резкий расход трафика отслеживается без raw IP",
        "• egress-защита блокирует private networks, SMTP abuse и BitTorrent",
        "",
        f"Сбросов ссылки: <b>{int(getattr(sub, 'reset_count', 0) or 0)}</b>" if sub else "Активная подписка не найдена",
    ]
    if events:
        lines += ["", "Последние события:"]
        for e in events:
            when = fmt_date(e.created_at) if e.created_at else "—"
            lines.append(f"• {html.escape(e.title)} · {when}")
    await cq.message.edit_text("\n".join(lines), reply_markup=keyboards.security_menu(has_active=has_active), disable_web_page_preview=True)
    await cq.answer()


@router.callback_query(F.data == "security_reset_confirm")
async def security_reset_confirm(cq: CallbackQuery) -> None:
    await cq.message.edit_text(
        "⚠️ <b>Перевыпустить VPN-ссылку?</b>\n\n"
        "Старые конфиги/QR на других устройствах перестанут работать. "
        "После сброса нужно заново импортировать ссылку в нужном клиенте.\n\n"
        "Используй это, если ссылка попала к другому человеку или нужно сменить устройство.",
        reply_markup=keyboards.security_reset_confirm_menu(),
        disable_web_page_preview=True,
    )
    await cq.answer()


@router.callback_query(F.data == "security_reset_do")
async def security_reset_do(cq: CallbackQuery) -> None:
    async with session_maker() as session:
        sub = await get_subscription(session, cq.from_user.id)
    if not sub:
        await cq.answer("Активная подписка не найдена", show_alert=True)
        return
    try:
        new_url = await marzban.revoke_sub(cq.from_user.id)
    except Exception:
        await cq.answer("Не удалось перевыпустить ссылку. Попробуй позже.", show_alert=True)
        return
    async with session_maker() as session:
        sub = await get_subscription(session, cq.from_user.id)
        if sub:
            sub.subscription_url = new_url
            sub.reset_count = int(getattr(sub, 'reset_count', 0) or 0) + 1
            sub.last_reset_at = datetime.now(timezone.utc)
            session.add(SecurityEvent(telegram_id=cq.from_user.id, kind="link_reset", severity="info", title="VPN-ссылка перевыпущена", details="Old subscription token revoked by user."))
            await session.commit()
    await cq.message.edit_text(
        "✅ <b>VPN-ссылка перевыпущена</b>\n\n"
        "Старые QR/конфиги больше не должны работать. Скопируй новую ссылку в профиле или открой QR заново.",
        reply_markup=keyboards.profile_menu(has_active=True),
        disable_web_page_preview=True,
    )
    await cq.answer("Готово")


@router.callback_query(F.data == "qr")
async def send_qr(cq: CallbackQuery) -> None:
    """QR-код подписки прямо в чате (PNG, без Pillow — через pypng)."""
    async with session_maker() as session:
        sub = await get_subscription(session, cq.from_user.id)
    if not sub or not sub.subscription_url:
        await cq.answer("Сначала оформи подписку", show_alert=True)
        return
    try:
        import qrcode
        from qrcode.image.pure import PyPNGImage

        img = qrcode.make(sub.subscription_url, image_factory=PyPNGImage, box_size=10, border=2)
        buf = io.BytesIO()
        img.save(buf)
        await cq.message.answer_photo(
            BufferedInputFile(buf.getvalue(), "vexvpn_qr.png"),
            caption=texts.QR_CAPTION,
        )
        await cq.answer()
    except Exception:
        await cq.answer("Не удалось сгенерировать QR. Скопируй ссылку из профиля.", show_alert=True)


@router.callback_query(F.data == "howto")
async def show_howto(cq: CallbackQuery) -> None:
    await cq.message.edit_text(texts.HOWTO_HAPP, reply_markup=keyboards.back_menu())
    await cq.answer()


@router.callback_query(F.data == "connected_ok")
async def connected_ok(cq: CallbackQuery) -> None:
    await cq.message.answer(texts.CONNECTED_OK, reply_markup=keyboards.back_menu())
    await cq.answer("Отлично!")


STATUS_LABEL = {"open": "🟡 открыт", "answered": "🟢 есть ответ", "closed": "⚪️ закрыт", "new": "🟡 открыт"}


def _render_thread(ticket, msgs) -> str:
    title = SUPPORT_TOPIC_TITLES.get(ticket.topic, "Другое")
    lines = [
        f"🆘 <b>Заявка #{ticket.id}</b> · {html.escape(title)}",
        f"Статус: {STATUS_LABEL.get(ticket.status, ticket.status)}",
        "",
    ]
    for m in msgs[-15:]:
        who = "🟦 Ты" if m.sender == "user" else "🟩 Поддержка"
        lines.append(f"<b>{who}:</b>\n{html.escape(m.text)}")
        lines.append("")
    return "\n".join(lines).strip()


async def _notify_admins_ticket(message: Message, ticket, body: str, *, kind: str) -> None:
    who = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    head = "🆘 <b>Новый тикет</b>" if kind == "new" else "💬 <b>Ответ пользователя</b>"
    text = (
        f"{head} #{ticket.id}\n"
        f"Тема: <b>{html.escape(SUPPORT_TOPIC_TITLES.get(ticket.topic, 'Другое'))}</b>\n"
        f"От: {html.escape(who)} / <code>{message.from_user.id}</code>\n\n"
        f"{html.escape(body[:900])}"
    )
    for admin_id in settings.admin_id_set:
        try:
            await message.bot.send_message(admin_id, text, reply_markup=keyboards.admin_ticket_kb(ticket.id))
        except Exception:
            pass


@router.callback_query(F.data.in_({"support", "connect_failed"}))
async def show_support(cq: CallbackQuery) -> None:
    await cq.message.edit_text(
        texts.SUPPORT_CHOOSE_TOPIC,
        reply_markup=keyboards.support_topic_menu(),
        disable_web_page_preview=True,
    )
    await cq.answer()


@router.callback_query(F.data.startswith("support_topic:"))
async def choose_support_topic(cq: CallbackQuery) -> None:
    topic = cq.data.split(":", 1)[1]
    if topic == "vpn":
        # для проблем с подключением сначала частые решения, потом — заявка
        await cq.message.edit_text(texts.SUPPORT_FAQ.format(platform="VPN"), reply_markup=keyboards.support_faq_menu())
    else:
        async with session_maker() as session:
            await set_support_state(session, cq.from_user.id, f"newticket:{topic}")
        title = SUPPORT_TOPIC_TITLES.get(topic, "Другое")
        await cq.message.edit_text(texts.SUPPORT_DESCRIBE_TOPIC.format(topic=title), reply_markup=keyboards.back_menu())
    await cq.answer()


@router.callback_query(F.data == "support_faq")
async def show_support_faq(cq: CallbackQuery) -> None:
    await cq.message.edit_text(texts.SUPPORT_FAQ.format(platform="VPN"), reply_markup=keyboards.support_faq_menu())
    await cq.answer()


@router.callback_query(F.data.startswith("faq:"))
async def show_faq_tip(cq: CallbackQuery) -> None:
    tip = FAQ_TEXTS.get(cq.data.split(":", 1)[1])
    if not tip:
        await cq.answer()
        return
    await cq.message.edit_text(tip, reply_markup=keyboards.support_faq_back_menu())
    await cq.answer()


@router.callback_query(F.data.startswith("support_describe:"))
async def support_describe(cq: CallbackQuery) -> None:
    topic = cq.data.split(":", 1)[1]
    async with session_maker() as session:
        await set_support_state(session, cq.from_user.id, f"newticket:{topic}")
    title = SUPPORT_TOPIC_TITLES.get(topic, "Другое")
    await cq.message.edit_text(texts.SUPPORT_DESCRIBE_TOPIC.format(topic=title), reply_markup=keyboards.back_menu())
    await cq.answer()


@router.callback_query(F.data == "mytickets")
async def my_tickets(cq: CallbackQuery) -> None:
    async with session_maker() as session:
        rows = await list_user_tickets(session, cq.from_user.id)
    if not rows:
        await cq.message.edit_text(texts.TICKETS_NONE, reply_markup=keyboards.support_topic_menu())
        await cq.answer()
        return
    await cq.message.edit_text(
        "🗂 <b>Мои обращения</b>\nВыбери заявку, чтобы посмотреть переписку и ответить:",
        reply_markup=keyboards.tickets_list_menu(rows),
    )
    await cq.answer()


@router.callback_query(F.data.startswith("uticket:"))
async def view_ticket(cq: CallbackQuery) -> None:
    ticket_id = int(cq.data.split(":", 1)[1])
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        if not t or t.telegram_id != cq.from_user.id:
            await cq.answer("Заявка не найдена", show_alert=True)
            return
        msgs = await get_ticket_messages(session, ticket_id)
    await cq.message.edit_text(
        _render_thread(t, msgs),
        reply_markup=keyboards.ticket_user_menu(ticket_id, closed=(t.status == "closed")),
        disable_web_page_preview=True,
    )
    await cq.answer()


@router.callback_query(F.data.startswith("ureply:"))
async def reply_ticket(cq: CallbackQuery) -> None:
    ticket_id = int(cq.data.split(":", 1)[1])
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        if not t or t.telegram_id != cq.from_user.id:
            await cq.answer("Заявка не найдена", show_alert=True)
            return
        await set_support_state(session, cq.from_user.id, f"reply:{ticket_id}")
    await cq.message.edit_text(texts.TICKET_REPLY_PROMPT.format(ticket_id=ticket_id), reply_markup=keyboards.back_menu())
    await cq.answer()


@router.callback_query(F.data.startswith("uclose:"))
async def close_ticket(cq: CallbackQuery) -> None:
    ticket_id = int(cq.data.split(":", 1)[1])
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        if not t or t.telegram_id != cq.from_user.id:
            await cq.answer("Заявка не найдена", show_alert=True)
            return
        await set_ticket_status(session, ticket_id, "closed")
    await cq.message.edit_text(texts.TICKET_CLOSED_USER.format(ticket_id=ticket_id), reply_markup=keyboards.back_menu())
    await cq.answer("Закрыто")


@router.message(F.text | F.photo | F.document)
async def collect_support_problem(message: Message) -> None:
    async with session_maker() as session:
        user = await get_user(session, message.from_user.id)
    state = user.support_state if user else None
    if not state or not (state.startswith("newticket:") or state.startswith("reply:")):
        return

    body = (message.text or message.caption or "Пользователь отправил скрин/файл без текста")[:1900]

    if state.startswith("newticket:"):
        topic = state.split(":", 1)[1]
        async with session_maker() as session:
            ticket = await create_support_ticket(session, message.from_user.id, topic, body)
            await set_support_state(session, message.from_user.id, None)
        await message.answer(texts.TICKET_CREATED_NEW.format(ticket_id=ticket.id), reply_markup=keyboards.ticket_user_menu(ticket.id))
        await _notify_admins_ticket(message, ticket, body, kind="new")
    else:  # reply:<id>
        ticket_id = int(state.split(":", 1)[1])
        async with session_maker() as session:
            t = await get_ticket(session, ticket_id)
            await set_support_state(session, message.from_user.id, None)
            if not t or t.telegram_id != message.from_user.id:
                await message.answer("Заявка не найдена.", reply_markup=keyboards.back_menu())
                return
            ticket, _ = await add_support_message(session, ticket_id, "user", body)
        await message.answer(texts.TICKET_USER_REPLY_SENT.format(ticket_id=ticket_id), reply_markup=keyboards.ticket_user_menu(ticket_id))
        await _notify_admins_ticket(message, ticket, body, kind="reply")
