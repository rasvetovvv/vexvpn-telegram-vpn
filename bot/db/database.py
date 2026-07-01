"""Подключение к БД и инициализация схемы."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.config import settings
from bot.db.models import Base

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """Безопасно добавить колонку в существующую БД без Alembic."""
    exists = await conn.scalar(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = :table AND column_name = :column
            """
        ),
        {"table": table, "column": column},
    )
    if not exists:
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


async def _ensure_unique_index(conn, table: str, index_name: str, column: str) -> None:
    """Создать unique index, если его ещё нет."""
    exists = await conn.scalar(
        text(
            """
            SELECT 1
            FROM pg_indexes
            WHERE tablename = :table AND indexname = :index_name
            """
        ),
        {"table": table, "index_name": index_name},
    )
    if not exists:
        await conn.execute(text(f"CREATE UNIQUE INDEX {index_name} ON {table} ({column})"))


async def init_db() -> None:
    """Создать таблицы и мягко обновить старую схему."""
    async with engine.begin() as conn:
        # bot и miniapp стартуют одновременно; PostgreSQL create_all может поймать race
        # при создании новых таблиц. Advisory lock сериализует мягкую миграцию.
        await conn.execute(text("SELECT pg_advisory_xact_lock(7862830247)"))
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_column(conn, "users", "referred_by", "referred_by BIGINT")
        await _ensure_column(conn, "users", "referral_rewarded", "referral_rewarded BOOLEAN DEFAULT FALSE")
        await _ensure_column(conn, "users", "trial_used", "trial_used BOOLEAN DEFAULT FALSE")
        await _ensure_column(conn, "users", "active_promo_code", "active_promo_code VARCHAR(32)")
        await _ensure_column(conn, "users", "support_state", "support_state VARCHAR(32)")
        await _ensure_column(conn, "users", "support_platform", "support_platform VARCHAR(32)")
        await _ensure_column(conn, "users", "miniapp_opened_at", "miniapp_opened_at TIMESTAMP WITH TIME ZONE")
        await _ensure_column(conn, "users", "last_activity_at", "last_activity_at TIMESTAMP WITH TIME ZONE")
        await _ensure_column(conn, "subscriptions", "first_connected_at", "first_connected_at TIMESTAMP WITH TIME ZONE")
        await _ensure_column(conn, "subscriptions", "first_connect_notified_at", "first_connect_notified_at TIMESTAMP WITH TIME ZONE")
        await _ensure_column(conn, "subscriptions", "first_seen_traffic", "first_seen_traffic BIGINT DEFAULT 0")
        await _ensure_column(conn, "subscriptions", "last_usage_bytes", "last_usage_bytes BIGINT DEFAULT 0")
        await _ensure_column(conn, "subscriptions", "last_usage_checked_at", "last_usage_checked_at TIMESTAMP WITH TIME ZONE")
        await _ensure_column(conn, "subscriptions", "security_last_alert_at", "security_last_alert_at TIMESTAMP WITH TIME ZONE")
        await _ensure_column(conn, "subscriptions", "reset_count", "reset_count INTEGER DEFAULT 0")
        await _ensure_column(conn, "subscriptions", "last_reset_at", "last_reset_at TIMESTAMP WITH TIME ZONE")
        await _ensure_column(conn, "payments", "promo_code", "promo_code VARCHAR(32)")
        await _ensure_column(conn, "payments", "status", "status VARCHAR(16) DEFAULT 'success'")
        await _ensure_column(conn, "payments", "error_message", "error_message TEXT")
        await _ensure_column(conn, "tariff_settings", "is_trial", "is_trial BOOLEAN DEFAULT FALSE")
        await _ensure_column(conn, "tariff_settings", "unlimited", "unlimited BOOLEAN DEFAULT FALSE")
        await _ensure_column(conn, "promo_codes", "kind", "kind VARCHAR(16) DEFAULT 'discount'")
        await _ensure_column(conn, "promo_codes", "percent", "percent INTEGER DEFAULT 0")
        await _ensure_column(conn, "promo_codes", "value", "value INTEGER DEFAULT 0")
        await _ensure_column(conn, "promo_codes", "free_plan_key", "free_plan_key VARCHAR(32)")
        await _ensure_column(conn, "promo_codes", "once_per_user", "once_per_user BOOLEAN DEFAULT TRUE")
        await _ensure_column(conn, "promo_codes", "global_limit", "global_limit INTEGER DEFAULT 0")
        await _ensure_column(conn, "promo_codes", "enabled", "enabled BOOLEAN DEFAULT TRUE")
        await _ensure_column(conn, "promo_codes", "new_only", "new_only BOOLEAN DEFAULT FALSE")
        await _ensure_column(conn, "promo_codes", "old_only", "old_only BOOLEAN DEFAULT FALSE")
        await _ensure_column(conn, "promo_codes", "disabled_at", "disabled_at TIMESTAMP WITH TIME ZONE")
        await conn.execute(text("ALTER TABLE reminder_logs ALTER COLUMN marker TYPE VARCHAR(32)"))
        await _ensure_unique_index(conn, "payments", "uq_payments_charge_id", "charge_id")
        await _ensure_column(conn, "grant_queue", "payment_id", "payment_id INTEGER")
        await _ensure_column(conn, "grant_queue", "stars_amount", "stars_amount INTEGER DEFAULT 0")
        await _ensure_column(conn, "grant_queue", "promo_code", "promo_code VARCHAR(32)")
        await _ensure_column(conn, "grant_queue", "is_trial", "is_trial BOOLEAN DEFAULT FALSE")
        await _ensure_column(conn, "grant_queue", "traffic_only", "traffic_only BOOLEAN DEFAULT FALSE")
        await _ensure_column(conn, "grant_queue", "unlimited", "unlimited BOOLEAN DEFAULT FALSE")
        await _ensure_unique_index(conn, "grant_queue", "uq_grant_queue_charge_id", "charge_id")
        await _ensure_column(conn, "support_tickets", "topic", "topic VARCHAR(16) DEFAULT 'other'")
        await _ensure_column(conn, "support_tickets", "updated_at", "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()")
        # старые тикеты со статусом 'new' трактуем как 'open'
        await conn.execute(text("UPDATE support_tickets SET status = 'open' WHERE status = 'new'"))
