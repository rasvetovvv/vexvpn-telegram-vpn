"""Отправка уведомлений в Telegram из web-процесса (без инстанса бота).

Web (miniapp) и бот — разные процессы; web не имеет объекта Bot, поэтому шлёт
сообщения напрямую через Telegram Bot API. Inline-кнопки в reply_markup ведут на
callback'и, которые обрабатывает админ-роутер бота (например, aticket:<id>).
"""
from __future__ import annotations

import logging

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)


async def tg_send(chat_id: int, text: str, reply_markup: dict | None = None) -> bool:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{settings.bot_token}/sendMessage", json=payload
            )
        return bool(resp.json().get("ok"))
    except Exception:
        logger.warning("tg_send failed for %s", chat_id, exc_info=True)
        return False


async def notify_admins(text: str, reply_markup: dict | None = None) -> None:
    for admin_id in settings.admin_id_set:
        await tg_send(admin_id, text, reply_markup)
