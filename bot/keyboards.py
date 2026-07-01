"""Inline-клавиатуры."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import Plan, settings

PRIMARY = "primary"
SUCCESS = "success"
DANGER = "danger"

# Premium custom emoji IDs for Bot API 9.4 inline-button icons.
# Text stays clean; Telegram renders the premium icon from icon_custom_emoji_id.
ICON = {
    "app": "5879583266426973039",          # Internet
    "buy": "5958376256788592078",          # Telegram Stars
    "profile": "5879770735999717115",      # Profile
    "promo": "5985433648810171091",        # Tag
    "referral": "5942877472163892475",     # People
    "install": "5899757765743615694",      # Install / Download
    "support": "5884510167986343358",      # Support chat
    "admin": "5877260591993177342",        # Settings
    "close": "5872829476143804491",        # Ban / cancel
    "back": "5875082500823258804",         # Back
    "plan": "5769403330761593044",         # Wallet
    "trial": "5913787972200698358",        # Lab
    "renew": "5877418604225924969",        # Updates
    "qr": "5897817196462113507",           # QR
    "ok": "5825794181183836432",           # Check
    "failed": "5872829476143804491",       # Ban / cancel
    "ios": "5875465628285931233",          # Telegram / paper plane
    "android": "5771887475421009729",      # Dog-like @ icon (closest in set)
    "desktop": "5879583266426973039",      # Internet
    "network": "5898997763331591703",      # Speaker / signal-like
    "reimport": "5877418604225924969",     # Updates
    "resetprofile": "5877465816030515018", # Link
    "describe": "5879841310902324730",     # Pencil
    "webadmin": "5877448998091971030",     # Data
    "broadcast": "5771699636411847302",    # Announcement
    "home": "5875465628285931233",         # Telegram
    "shield": "5879770735999717115",       # Shield/security fallback
}


def _button(kb: InlineKeyboardBuilder, *, text: str, icon: str | None = None, **kwargs) -> None:
    """Add a Bot API 9.4 button with optional premium icon.

    aiogram 3.28+ knows `icon_custom_emoji_id` and `style`; old Telegram clients
    will still show the plain text, so we keep labels readable without simple emoji.
    """
    if icon:
        kwargs["icon_custom_emoji_id"] = ICON[icon]
    kb.button(text=text, **kwargs)


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _button(kb, text="Кабинет Mini App", icon="app", web_app=WebAppInfo(url=settings.mini_app_url), style=SUCCESS)
    _button(kb, text="🎁 Получить VPN на сегодня", icon="trial", callback_data="daily_free", style=SUCCESS)
    _button(kb, text="Купить VPN", icon="buy", callback_data="buy", style=PRIMARY)
    _button(kb, text="Моя подписка", icon="profile", callback_data="profile")
    _button(kb, text="Промокод", icon="promo", callback_data="promo")
    _button(kb, text="Рефералка", icon="referral", callback_data="referral")
    _button(kb, text="Инструкция", icon="install", callback_data="howto")
    _button(kb, text="Поддержка", icon="support", callback_data="support")
    if is_admin:
        _button(kb, text="Админ-панель", icon="admin", callback_data="admin")
    _button(kb, text="Закрыть", icon="close", callback_data="close", style=DANGER)
    kb.adjust(1, 1, 2, 2, 1, 1)
    return kb.as_markup()


def plans_menu(plans: list[Plan], is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for plan in plans:
        if not plan.visible:
            continue
        _button(
            kb,
            text=f"{plan.stars} Stars — {plan.title} · {plan.traffic_label}",
            icon="plan",
            callback_data=f"plan:{plan.key}",
            style=PRIMARY,
        )
    if is_admin:
        _button(kb, text="Тест без оплаты (1 день)", icon="trial", callback_data="test:trial_1d", style=SUCCESS)
    _button(kb, text="Промокод", icon="promo", callback_data="promo")
    _button(kb, text="Назад", icon="back", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()


def profile_menu(has_active: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _button(kb, text="Открыть кабинет", icon="app", web_app=WebAppInfo(url=settings.mini_app_url), style=SUCCESS)
    _button(kb, text="Получить +1 день сегодня", icon="trial", callback_data="daily_free", style=SUCCESS)
    _button(kb, text="Продлить / купить больше", icon="renew", callback_data="buy", style=PRIMARY)
    if has_active:
        _button(kb, text="QR-код", icon="qr", callback_data="qr")
        _button(kb, text="Безопасность", icon="shield", callback_data="security")
        _button(kb, text="Я подключился", icon="ok", callback_data="connected_ok", style=SUCCESS)
        _button(kb, text="Не получилось", icon="failed", callback_data="connect_failed", style=DANGER)
    _button(kb, text="Инструкция", icon="install", callback_data="howto")
    _button(kb, text="Назад", icon="back", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()


def security_menu(has_active: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _button(kb, text="Открыть Privacy Center", icon="app", web_app=WebAppInfo(url=f"{settings.mini_app_url.rstrip('/')}/privacy"), style=SUCCESS)
    if has_active:
        _button(kb, text="Сбросить VPN-ссылку / устройства", icon="resetprofile", callback_data="security_reset_confirm", style=DANGER)
        _button(kb, text="QR после сброса", icon="qr", callback_data="qr")
    _button(kb, text="Моя подписка", icon="profile", callback_data="profile", style=PRIMARY)
    _button(kb, text="Поддержка", icon="support", callback_data="support")
    _button(kb, text="Назад", icon="back", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()


def security_reset_confirm_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _button(kb, text="Да, перевыпустить ссылку", icon="resetprofile", callback_data="security_reset_do", style=DANGER)
    _button(kb, text="Отмена", icon="back", callback_data="security")
    kb.adjust(1)
    return kb.as_markup()


def daily_free_already_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _button(kb, text="Купить больше дней/трафика", icon="buy", callback_data="buy", style=PRIMARY)
    _button(kb, text="Моя подписка", icon="profile", callback_data="profile")
    _button(kb, text="В меню", icon="home", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()


def success_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _button(kb, text="Открыть кабинет", icon="app", web_app=WebAppInfo(url=settings.mini_app_url), style=SUCCESS)
    _button(kb, text="QR-код", icon="qr", callback_data="qr")
    _button(kb, text="Я подключился", icon="ok", callback_data="connected_ok", style=SUCCESS)
    _button(kb, text="Не получилось", icon="failed", callback_data="connect_failed", style=DANGER)
    _button(kb, text="Моя подписка", icon="profile", callback_data="profile", style=PRIMARY)
    _button(kb, text="Инструкция", icon="install", callback_data="howto")
    _button(kb, text="В меню", icon="home", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()


def vpn_ready_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _button(kb, text="Моя подписка", icon="profile", callback_data="profile", style=PRIMARY)
    _button(kb, text="Продлить VPN", icon="renew", callback_data="buy", style=SUCCESS)
    _button(kb, text="Кабинет Mini App", icon="app", web_app=WebAppInfo(url=settings.mini_app_url))
    _button(kb, text="Инструкция", icon="install", callback_data="howto")
    kb.adjust(1)
    return kb.as_markup()


def support_topic_menu() -> InlineKeyboardMarkup:
    """Темы обращения + вход в «Мои обращения»."""
    kb = InlineKeyboardBuilder()
    _button(kb, text="Оплата / Stars", icon="buy", callback_data="support_topic:payment")
    _button(kb, text="Не работает VPN", icon="network", callback_data="support_topic:vpn")
    _button(kb, text="Промокод", icon="promo", callback_data="support_topic:promo")
    _button(kb, text="Другое", icon="describe", callback_data="support_topic:other")
    _button(kb, text="Мои обращения", icon="profile", callback_data="mytickets", style=PRIMARY)
    _button(kb, text="Назад", icon="back", callback_data="back")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def support_faq_menu() -> InlineKeyboardMarkup:
    """Частые решения перед созданием заявки (тема VPN)."""
    kb = InlineKeyboardBuilder()
    _button(kb, text="Обновить подписку", icon="reimport", callback_data="faq:reimport")
    _button(kb, text="Сменить сеть", icon="network", callback_data="faq:network")
    _button(kb, text="Переимпортировать профиль", icon="resetprofile", callback_data="faq:resetprofile")
    _button(kb, text="Не помогло — описать проблему", icon="describe", callback_data="support_describe:vpn", style=PRIMARY)
    _button(kb, text="Назад", icon="back", callback_data="support")
    kb.adjust(1)
    return kb.as_markup()


def support_faq_back_menu() -> InlineKeyboardMarkup:
    """Под подсказкой: вернуться к списку или описать проблему."""
    kb = InlineKeyboardBuilder()
    _button(kb, text="К подсказкам", icon="back", callback_data="support_faq")
    _button(kb, text="Описать проблему", icon="describe", callback_data="support_describe:vpn", style=PRIMARY)
    kb.adjust(1)
    return kb.as_markup()


def tickets_list_menu(tickets) -> InlineKeyboardMarkup:
    """Список обращений пользователя кнопками (callback uticket:<id>)."""
    icons = {"open": "🟡", "answered": "🟢", "closed": "⚪️", "new": "🟡"}
    kb = InlineKeyboardBuilder()
    for t in tickets:
        title = {"payment": "Оплата", "vpn": "VPN", "promo": "Промокод", "other": "Другое"}.get(t.topic, "Тема")
        kb.button(text=f"{icons.get(t.status, '🟡')} #{t.id} · {title}", callback_data=f"uticket:{t.id}")
    _button(kb, text="Новая тема", icon="describe", callback_data="support")
    _button(kb, text="Назад", icon="back", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()


def ticket_user_menu(ticket_id: int, closed: bool = False) -> InlineKeyboardMarkup:
    """Действия пользователя в тикете."""
    kb = InlineKeyboardBuilder()
    if not closed:
        _button(kb, text="Ответить / дополнить", icon="describe", callback_data=f"ureply:{ticket_id}", style=PRIMARY)
        _button(kb, text="Закрыть заявку", icon="ok", callback_data=f"uclose:{ticket_id}")
    _button(kb, text="К обращениям", icon="back", callback_data="mytickets")
    kb.adjust(1)
    return kb.as_markup()


def admin_ticket_kb(ticket_id: int) -> InlineKeyboardMarkup:
    """Кнопки админа на уведомлении/просмотре тикета."""
    kb = InlineKeyboardBuilder()
    _button(kb, text="Ответить", icon="describe", callback_data=f"areply:{ticket_id}", style=PRIMARY)
    _button(kb, text="Закрыть", icon="ok", callback_data=f"aclose:{ticket_id}")
    kb.adjust(2)
    return kb.as_markup()


def admin_tickets_list_kb(tickets) -> InlineKeyboardMarkup:
    icons = {"open": "🟡", "answered": "🟢", "closed": "⚪️", "new": "🟡"}
    kb = InlineKeyboardBuilder()
    for t in tickets:
        kb.button(text=f"{icons.get(t.status, '🟡')} #{t.id} · {t.telegram_id}", callback_data=f"aticket:{t.id}")
    _button(kb, text="Назад", icon="back", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()


def admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _button(kb, text="Открыть web-админку", icon="webadmin", web_app=WebAppInfo(url=f"{settings.mini_app_url.rstrip('/')}/admin"), style=SUCCESS)
    _button(kb, text="Рассылка всем", icon="broadcast", callback_data="admin_broadcast_help")
    _button(kb, text="Назад", icon="back", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()


def traffic_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _button(kb, text="Купить пакет трафика", icon="buy", callback_data="buy", style=PRIMARY)
    _button(kb, text="Открыть кабинет", icon="app", web_app=WebAppInfo(url=settings.mini_app_url), style=SUCCESS)
    _button(kb, text="Поддержка", icon="support", callback_data="support")
    kb.adjust(1)
    return kb.as_markup()


def no_purchase_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _button(kb, text="Выбрать тариф", icon="buy", callback_data="buy", style=PRIMARY)
    _button(kb, text="Открыть Mini App", icon="app", web_app=WebAppInfo(url=settings.mini_app_url), style=SUCCESS)
    _button(kb, text="Промокод", icon="promo", callback_data="promo")
    kb.adjust(1)
    return kb.as_markup()


def admin_user_menu(telegram_id: int, has_last_payment: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _button(kb, text="+7 дней", icon="renew", callback_data=f"admin_user:add7:{telegram_id}", style=SUCCESS)
    _button(kb, text="+30 дней", icon="renew", callback_data=f"admin_user:add30:{telegram_id}", style=SUCCESS)
    _button(kb, text="Reset traffic", icon="reimport", callback_data=f"admin_user:reset:{telegram_id}")
    _button(kb, text="Disable", icon="close", callback_data=f"admin_user:disable:{telegram_id}", style=DANGER)
    if has_last_payment:
        _button(kb, text="Refund last", icon="close", callback_data=f"admin_user:refund:{telegram_id}", style=DANGER)
    kb.adjust(2, 1, 1, 1)
    return kb.as_markup()


def back_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _button(kb, text="Назад", icon="back", callback_data="back")
    return kb.as_markup()
