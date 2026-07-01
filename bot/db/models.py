"""ORM-модели."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    referral_rewarded: Mapped[bool] = mapped_column(Boolean, default=False)
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    active_promo_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    support_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    support_platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    miniapp_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MiniAppDevice(Base):
    """Хешированный fingerprint MiniApp-устройства/IP для антиабуза бесплатного VPN."""

    __tablename__ = "miniapp_devices"
    __table_args__ = (UniqueConstraint("telegram_id", "fingerprint_hash", name="uq_miniapp_user_fingerprint"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    fingerprint_hash: Mapped[str] = mapped_column(String(64), index=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True)


class DailyFreeClaim(Base):
    """Идемпотентная запись бесплатной ежедневной выдачи с антиабуз-следами."""

    __tablename__ = "daily_free_claims"
    __table_args__ = (UniqueConstraint("telegram_id", "day", name="uq_daily_free_user_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    fingerprint_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/success/blocked/error
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AbuseFlag(Base):
    """Флаги подозрительных паттернов для отображения в админке."""

    __tablename__ = "abuse_flags"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(12), default="warn", index=True)  # info/warn/block
    fingerprint_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Subscription(Base):
    """Активная подписка пользователя (одна на пользователя, продлевается)."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    marzban_username: Mapped[str] = mapped_column(String(64))
    subscription_url: Mapped[str] = mapped_column(String(512))
    plan: Mapped[str] = mapped_column(String(32))
    expire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    traffic_limit: Mapped[int] = mapped_column(BigInteger, default=0)
    first_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_connect_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_traffic: Mapped[int] = mapped_column(BigInteger, default=0)
    last_usage_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    last_usage_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    security_last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reset_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SecurityEvent(Base):
    """User-visible security events without raw IPs or destinations."""

    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(12), default="info", index=True)
    title: Mapped[str] = mapped_column(String(160))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("charge_id", name="uq_payments_charge_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    plan: Mapped[str] = mapped_column(String(32))
    stars_amount: Mapped[int] = mapped_column(Integer)
    charge_id: Mapped[str] = mapped_column(String(128))
    promo_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="success")  # pending/success/marzban_error/manual
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GrantQueue(Base):
    __tablename__ = "grant_queue"
    __table_args__ = (UniqueConstraint("charge_id", name="uq_grant_queue_charge_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    payment_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    charge_id: Mapped[str] = mapped_column(String(128), index=True)
    plan_key: Mapped[str] = mapped_column(String(32))
    plan_title: Mapped[str] = mapped_column(String(128))
    days: Mapped[int] = mapped_column(Integer, default=0)
    traffic_gb: Mapped[int] = mapped_column(Integer, default=0)
    devices: Mapped[int] = mapped_column(Integer, default=1)
    stars_amount: Mapped[int] = mapped_column(Integer, default=0)
    promo_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)
    traffic_only: Mapped[bool] = mapped_column(Boolean, default=False)
    unlimited: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AlertLog(Base):
    __tablename__ = "alert_logs"
    __table_args__ = (UniqueConstraint("key", "bucket", name="uq_alert_key_bucket"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), index=True)
    bucket: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="sent")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), default="discount")  # discount/days/traffic/trial
    percent: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[int] = mapped_column(Integer, default=0)  # days or GB
    free_plan_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    once_per_user: Mapped[bool] = mapped_column(Boolean, default=True)
    global_limit: Mapped[int] = mapped_column(Integer, default=0)  # 0 = unlimited
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    new_only: Mapped[bool] = mapped_column(Boolean, default=False)
    old_only: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PromoUse(Base):
    __tablename__ = "promo_uses"
    __table_args__ = (UniqueConstraint("telegram_id", "code", name="uq_promo_user_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReminderLog(Base):
    __tablename__ = "reminder_logs"
    __table_args__ = (UniqueConstraint("telegram_id", "marker", "expire_at", name="uq_reminder_user_marker_expire"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    marker: Mapped[str] = mapped_column(String(32))
    expire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    topic: Mapped[str] = mapped_column(String(16), default="other")  # payment/vpn/promo/other
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str | None] = mapped_column(String(2000), nullable=True)  # первое сообщение (превью)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open/answered/closed
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class SupportMessage(Base):
    """Сообщение переписки тикета (двусторонний тред user ↔ admin)."""

    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(Integer, index=True)
    sender: Mapped[str] = mapped_column(String(8))  # "user" | "admin"
    admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text: Mapped[str] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class MarzbanLog(Base):
    __tablename__ = "marzban_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class BotEvent(Base):
    __tablename__ = "bot_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class UsageSnapshot(Base):
    """Суточный снимок израсходованного трафика для графика по дням (спарклайн)."""

    __tablename__ = "usage_snapshots"
    __table_args__ = (UniqueConstraint("telegram_id", "day", name="uq_usage_snapshot"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)  # "YYYY-MM-DD" UTC
    used_traffic: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GamificationState(Base):
    """Состояние геймификации пользователя: стрик ежедневного захода + счётчики."""

    __tablename__ = "gamification_state"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    streak: Mapped[int] = mapped_column(Integer, default=0)
    last_checkin_day: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "YYYY-MM-DD" UTC
    total_checkins: Mapped[int] = mapped_column(Integer, default=0)
    total_spins: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BonusClaim(Base):
    """Идемпотентная запись разовых действий: 1 строка на (юзер, тип, день UTC).

    kind: "checkin" | "wheel". Уникальный ключ гарантирует не больше одного
    ежедневного бонуса / спина в сутки, даже при гонке запросов.
    """

    __tablename__ = "bonus_claims"
    __table_args__ = (UniqueConstraint("telegram_id", "kind", "day", name="uq_bonus_claim"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)  # "YYYY-MM-DD" UTC
    reward_kind: Mapped[str] = mapped_column(String(16), default="none")  # none/days/traffic/promo
    reward_value: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class UserAchievement(Base):
    """Полученные достижения (бейджи). Один раз на (юзер, код)."""

    __tablename__ = "user_achievements"
    __table_args__ = (UniqueConstraint("telegram_id", "code", name="uq_user_achievement"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TariffSetting(Base):
    __tablename__ = "tariff_settings"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    days: Mapped[int] = mapped_column(Integer)
    stars: Mapped[int] = mapped_column(Integer)
    traffic_gb: Mapped[int] = mapped_column(Integer)
    devices: Mapped[int] = mapped_column(Integer, default=1)
    badge: Mapped[str] = mapped_column(String(64), default="")
    visible: Mapped[bool] = mapped_column(Boolean, default=True)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)
    traffic_only: Mapped[bool] = mapped_column(Boolean, default=False)
    unlimited: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
