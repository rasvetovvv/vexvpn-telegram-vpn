"""Конфигурация, тарифы и промокоды проекта."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class Plan:
    """Тариф VPN.

    days — сколько дней добавить. Для пакетов трафика = 0.
    traffic_gb — сколько трафика добавить. 0 = "не добавляет трафик" (см. unlimited).
    traffic_only — пакет трафика, доступен только при активной подписке.
    unlimited — тариф снимает лимит трафика (безлимит). Отличает безлимитный
        тариф от выдачи «только дни» (реф-бонус, админ +дни), где лимит сохраняется.
    """

    key: str
    title: str
    days: int
    stars: int
    traffic_gb: int  # 0 = ничего не добавляет; безлимит задаётся флагом unlimited
    devices: int = 1
    badge: str = ""
    visible: bool = True
    is_trial: bool = False
    traffic_only: bool = False
    unlimited: bool = False

    @property
    def data_limit_bytes(self) -> int:
        """Лимит/добавка трафика в байтах (0 = безлимит)."""
        return self.traffic_gb * 1024**3

    @property
    def traffic_label(self) -> str:
        return "∞ безлимит" if self.traffic_gb == 0 else f"+{self.traffic_gb} ГБ" if self.traffic_only else f"{self.traffic_gb} ГБ"

    @property
    def devices_label(self) -> str:
        if self.devices <= 1:
            return "1 устройство"
        if 2 <= self.devices <= 4:
            return f"{self.devices} устройства"
        return f"{self.devices} устройств"


@dataclass(frozen=True)
class Promo:
    """Промокод.

    kind: discount/days/traffic/trial. Для старых FREE7/SALE30 сохраняются
    percent/free_plan_key, новые промокоды могут жить в БД promo_codes.
    """

    code: str
    title: str
    percent: int = 0
    free_plan_key: str | None = None
    once_per_user: bool = True
    kind: str = "discount"
    value: int = 0
    global_limit: int = 0
    enabled: bool = True
    new_only: bool = False
    old_only: bool = False


# ─── Тарифы ────────────────────────────────────────────────────────
# Старые ключи оставлены скрытыми, чтобы профиль уже купленных подписок
# продолжал нормально отображаться.
PLANS: dict[str, Plan] = {
    "trial_1d": Plan("trial_1d", "Пробный 1 день", 1, 1, 5, 1, "🧪 один раз", True, True),
    "daily_free": Plan("daily_free", "Ежедневный бесплатный VPN", 1, 0, 100, 1, "каждый день", visible=False),
    "lite_7d": Plan("lite_7d", "Lite 7 дней", 7, 50, 30, 1, "для телефона"),
    "standard_30d": Plan("standard_30d", "Standard 30 дней", 30, 150, 100, 2, "популярный"),
    "unlimited_30d": Plan("unlimited_30d", "Unlimited 30 дней", 30, 250, 0, 3, "безлимит", unlimited=True),
    "family_30d": Plan("family_30d", "Family 30 дней", 30, 390, 0, 5, "2–5 устройств", unlimited=True),
    "quarter_90d": Plan("quarter_90d", "Unlimited 90 дней", 90, 650, 0, 3, "выгодно", unlimited=True),
    # дешёвые доп.пакеты трафика — не меняют дату окончания
    "traffic_30gb": Plan("traffic_30gb", "+30 ГБ трафика", 0, 20, 30, 1, "дешевле", True, False, True),
    "traffic_100gb": Plan("traffic_100gb", "+100 ГБ трафика", 0, 55, 100, 1, "пакет", True, False, True),
    "traffic_300gb": Plan("traffic_300gb", "+300 ГБ трафика", 0, 120, 300, 1, "выгодно", True, False, True),
    # legacy aliases
    "week_7d": Plan("week_7d", "7 дней", 7, 50, 30, 1, visible=False),
    "month_30d": Plan("month_30d", "30 дней", 30, 150, 100, 2, visible=False),
}

PROMOS: dict[str, Promo] = {
    "FREE7": Promo("FREE7", "7 дней бесплатно", free_plan_key="lite_7d"),
    "SALE30": Promo("SALE30", "-30% на следующую покупку", percent=30),
    # Награда «колеса фортуны»: многоразовый (once_per_user=False), чтобы его можно
    # было выигрывать повторно. Скидка применяется к следующей покупке.
    "WHEEL15": Promo("WHEEL15", "-15% от колеса фортуны", percent=15, once_per_user=False),
}

# ─── Геймификация ─────────────────────────────────────────────────
# Все награды считаются и выдаются НА СЕРВЕРЕ (клиенту не верим), идемпотентно
# (1 спин + 1 чек-ин в сутки по UTC), и только для пользователей с активной
# подпиской — чтобы нельзя было фармить бесплатный VPN без покупки.
GAMI_DAILY_GOAL = 3              # «зайди N дней подряд — получи бонус»
GAMI_DAILY_REWARD_DAYS = 1       # награда за выполненный стрик
GAMI_WHEEL_PROMO_CODE = "WHEEL15"
# Сегменты колеса: вес определяет вероятность (нормируется по сумме весов).
# kind: none | days | traffic | promo. value — дни или ГБ. Экономику легко крутить здесь.
GAMI_WHEEL_SEGMENTS: tuple[dict, ...] = (
    {"key": "nothing", "label": "Мимо 🙃", "weight": 50, "kind": "none", "value": 0},
    {"key": "traffic_5", "label": "+5 ГБ", "weight": 25, "kind": "traffic", "value": 5},
    {"key": "day_1", "label": "+1 день", "weight": 15, "kind": "days", "value": 1},
    {"key": "promo_15", "label": "−15% промокод", "weight": 10, "kind": "promo", "value": 15},
)

REFERRAL_BONUS_DAYS = 3
REMINDER_HOURS = (72, 24, 3)

# ─── Промо-баннер в кабинете ──────────────────────────────────────
# Витринный баннер акции (редактируется здесь, отдаётся в /api/config).
# until — ISO-время окончания ("2026-07-07T23:59:59Z") для таймера, либо "" без таймера.
PROMO_BANNER = {
    "enabled": True,
    "title": "−30% по промокоду SALE30",
    "subtitle": "Введи SALE30 в разделе промокодов — скидка применится к следующей покупке.",
    "code": "SALE30",
    "until": "",
}

# ─── Удержание и анти-фрод ─────────────────────────────────────────
# Бонус за реферала начисляется только за реальную покупку (не за 1⭐ триал)
REFERRAL_MIN_PAYMENT_STARS = 50
# Максимум начислений бонуса одному рефереру за сутки (защита от мультиаккаунтов)
REFERRAL_MAX_PER_DAY = 5
# Порог уведомления об остатке трафика (0.9 = использовано ≥90% лимита)
TRAFFIC_ALERT_THRESHOLDS = (0.80, 0.95)
TRAFFIC_ALERT_THRESHOLD = 0.9  # legacy alias
# Окно «только что истекла» для уведомления об окончании (минуты)
EXPIRED_NOTICE_WINDOW_MIN = 90


class Settings(BaseSettings):
    """Настройки из переменных окружения / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    bot_token: str
    admin_ids: str = ""  # "123,456"
    super_admin_ids: str = ""  # суперадмины (удаление/рефанд); пусто = все админы super
    sentry_dsn: str = ""  # опционально: трекинг ошибок

    # БД
    database_url: str = "postgresql+asyncpg://vpnbot:vpnbot@db:5432/vpnbot"
    redis_url: str = ""  # shared rate-limit storage; empty = in-memory fallback for single-process dev

    # Marzban
    marzban_base_url: str = "https://vpn.example.com"
    marzban_username: str = "admin"
    marzban_password: str = "admin"
    marzban_proxies: str = "vless"  # "vless,vmess,trojan"
    marzban_flow: str = ""
    marzban_tls_ca_file: str = ""  # optional CA bundle/path for private/self-signed Marzban TLS

    # Web/Mini App security
    trusted_proxy_ips: str = ""  # comma-separated IPs/CIDRs allowed to supply X-Forwarded-For/X-Real-IP
    telegram_init_data_max_age_seconds: int = 21600  # 6h replay window; auth_date is mandatory

    # Прочее
    support_username: str = "support"
    servers_online: int = 12
    bot_username: str = "VexDevVPNbot"
    mini_app_url: str = "https://proxy.vexory.xyz"
    subscription_public_base_url: str = ""  # если пусто, используем mini_app_url для /sub/<token>

    # ── производные ──────────────────────────────────────────────
    @property
    def admin_id_set(self) -> set[int]:
        return {int(x) for x in self.admin_ids.replace(" ", "").split(",") if x}

    @property
    def super_admin_id_set(self) -> set[int]:
        """Суперадмины. Если SUPER_ADMIN_IDS пуст — все админы считаются super."""
        explicit = {int(x) for x in self.super_admin_ids.replace(" ", "").split(",") if x}
        return explicit or self.admin_id_set

    @property
    def proxy_config(self) -> dict[str, dict]:
        """Конфиг proxies для Marzban: {"vless": {"flow": "..."}}."""
        result: dict[str, dict] = {}
        for proto in self.marzban_proxies.split(","):
            proto = proto.strip().lower()
            if not proto:
                continue
            cfg: dict = {}
            if proto == "vless" and self.marzban_flow:
                cfg["flow"] = self.marzban_flow
            result[proto] = cfg
        return result

    @property
    def sub_public_base(self) -> str:
        """Публичный gateway для ссылок подписки: браузеру HTML, VPN-клиентам raw config."""
        return (self.subscription_public_base_url or self.mini_app_url).rstrip("/")

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_id_set

    def is_super_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.super_admin_id_set


settings = Settings()
