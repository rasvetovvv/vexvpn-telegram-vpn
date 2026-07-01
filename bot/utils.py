"""Вспомогательные функции форматирования."""
from __future__ import annotations

from datetime import datetime, timezone


def fmt_date(dt: datetime) -> str:
    """ДД.ММ.ГГГГ ЧЧ:ММ (UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%d.%m.%Y %H:%M")


def human_left(expire: datetime) -> str:
    """Сколько осталось до окончания подписки."""
    if expire.tzinfo is None:
        expire = expire.replace(tzinfo=timezone.utc)
    delta = expire - datetime.now(timezone.utc)
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes <= 0:
        return "истекла"
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days} дн. {hours} ч."
    if hours:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин."


def fmt_traffic(data_limit: int) -> str:
    """Лимит трафика в человекочитаемом виде (0 = безлимит)."""
    if not data_limit:
        return "∞ безлимит"
    gb = data_limit / 1024**3
    if gb >= 1:
        return f"{gb:.0f} ГБ"
    mb = data_limit / 1024**2
    return f"{mb:.0f} МБ"


def fmt_size(num_bytes: int) -> str:
    """Объём в байтах в человекочитаемом виде (0 → '0 МБ', не безлимит)."""
    num_bytes = max(0, int(num_bytes or 0))
    gb = num_bytes / 1024**3
    if gb >= 1:
        return f"{gb:.1f} ГБ"
    mb = num_bytes / 1024**2
    return f"{mb:.0f} МБ"
