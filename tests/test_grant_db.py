"""Интеграционные тесты выдачи на in-memory SQLite (без Postgres/Marzban)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

try:
    import aiosqlite  # noqa: F401

    HAS_SQLITE = True
except Exception:  # pragma: no cover
    HAS_SQLITE = False


@unittest.skipUnless(HAS_SQLITE, "aiosqlite is required for DB integration tests")
class GrantDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        from bot.db.models import Base

        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_add_payment_is_idempotent_by_charge_id(self) -> None:
        from bot.db import repo

        async with self.Session() as s:
            p1 = await repo.add_payment(s, telegram_id=1, plan="lite_7d", stars_amount=50, charge_id="CH1", status="pending")
            self.assertTrue(getattr(p1, "_was_created", False))
        async with self.Session() as s:
            p2 = await repo.add_payment(s, telegram_id=1, plan="lite_7d", stars_amount=50, charge_id="CH1", status="pending")
            self.assertFalse(getattr(p2, "_was_created", True))

    async def test_finalize_grant_writes_everything_atomically(self) -> None:
        from bot.db import repo
        from bot.db.models import Payment

        async with self.Session() as s:
            await repo.ensure_user(s, 2, "u2")
            payment = await repo.add_payment(s, telegram_id=2, plan="trial_1d", stars_amount=1, charge_id="CH2", status="pending")
            payment_id = payment.id

        expire = datetime.now(timezone.utc) + timedelta(days=1)
        async with self.Session() as s:
            await repo.finalize_grant(
                s,
                telegram_id=2,
                marzban_username="tg_2",
                subscription_url="https://vpn.example.com/sub/x",
                plan_key="trial_1d",
                expire_at=expire,
                traffic_limit=5 * 1024**3,
                is_trial=True,
                clear_active_promo=False,
                payment_id=payment_id,
                log_message="test",
                log_paid=True,
            )

        async with self.Session() as s:
            sub = await repo.get_subscription(s, 2)
            user = await repo.get_user(s, 2)
            pay = await s.get(Payment, payment_id)
            self.assertIsNotNone(sub)
            self.assertEqual(sub.plan, "trial_1d")
            self.assertTrue(user.trial_used)
            self.assertEqual(pay.status, "success")

    async def test_validate_accepts_free_promo_and_blocks_traffic_only_without_sub(self) -> None:
        from bot.config import PLANS
        from bot.db import repo
        from bot.handlers.payments import _validate_plan_for_user

        async with self.Session() as s:
            await repo.ensure_user(s, 3, "u3")
            # FREE7 (бесплатный промокод) для обычного тарифа — допустим (баг 3).
            err = await _validate_plan_for_user(s, 3, PLANS["lite_7d"], "FREE7")
            self.assertIsNone(err)
            # Пакет трафика без активной подписки — отклоняем.
            err2 = await _validate_plan_for_user(s, 3, PLANS["traffic_30gb"])
            self.assertIsNotNone(err2)

    async def test_ensure_tariff_settings_preserves_admin_edits(self) -> None:
        # Баг 2: правка is_trial/unlimited в админке не должна откатываться на старте.
        from bot.config import PLANS
        from bot.db import repo
        from bot.db.models import TariffSetting

        # Админ включил unlimited тарифу, у которого в config.py unlimited=False.
        async with self.Session() as s:
            s.add(TariffSetting(key="standard_30d", title="Standard", days=30, stars=150, traffic_gb=100, unlimited=True))
            await s.commit()

        repo._tariff_sync_done = False
        try:
            async with self.Session() as s:
                await repo.ensure_tariff_settings(s, PLANS, force=True)
            async with self.Session() as s:
                edited = await s.get(TariffSetting, "standard_30d")
                seeded = await s.get(TariffSetting, "trial_1d")
                self.assertTrue(edited.unlimited)  # правка админа сохранена, а не сброшена в config
                self.assertIsNotNone(seeded)        # новый тариф засеян
                self.assertTrue(seeded.is_trial)    # с корректными системными флагами из config
        finally:
            repo._tariff_sync_done = False

    async def test_bonus_claim_idempotent_per_day(self) -> None:
        # Анти-фрод геймификации: не больше одного спина/чек-ина в сутки.
        from bot.db import repo

        async with self.Session() as s:
            self.assertIsNotNone(await repo.reserve_bonus_claim(s, 10, "wheel", "2026-06-30"))
        async with self.Session() as s:
            self.assertIsNone(await repo.reserve_bonus_claim(s, 10, "wheel", "2026-06-30"))  # повтор — отклонён
        async with self.Session() as s:
            self.assertIsNotNone(await repo.reserve_bonus_claim(s, 10, "wheel", "2026-07-01"))   # новый день — ок
            self.assertIsNotNone(await repo.reserve_bonus_claim(s, 10, "checkin", "2026-06-30"))  # другой тип — независимо

    async def test_award_achievement_idempotent(self) -> None:
        from bot.db import repo

        async with self.Session() as s:
            self.assertTrue(await repo.award_achievement(s, 10, "unlimited"))
        async with self.Session() as s:
            self.assertFalse(await repo.award_achievement(s, 10, "unlimited"))  # повторно — не выдаём
            self.assertIn("unlimited", await repo.get_user_achievements(s, 10))

    async def test_usage_snapshot_upsert_and_history_order(self) -> None:
        from bot.db import repo

        async with self.Session() as s:
            await repo.record_usage_snapshot(s, 20, "2026-06-28", 1000)
            await repo.record_usage_snapshot(s, 20, "2026-06-29", 3000)
            await repo.record_usage_snapshot(s, 20, "2026-06-29", 5000)  # upsert того же дня
        async with self.Session() as s:
            hist = await repo.get_usage_history(s, 20, days=8)
            self.assertEqual([h.day for h in hist], ["2026-06-28", "2026-06-29"])  # по возрастанию
            self.assertEqual(hist[-1].used_traffic, 5000)  # перезаписано последним значением

    async def test_support_ticket_thread_and_status(self) -> None:
        from bot.db import repo

        async with self.Session() as s:
            t = await repo.create_support_ticket(s, 30, "payment", "не пришла подписка")
            tid = t.id
            self.assertEqual(t.status, "open")
        async with self.Session() as s:
            msgs = await repo.get_ticket_messages(s, tid)
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0].sender, "user")
        # ответ админа → answered
        async with self.Session() as s:
            ticket, _ = await repo.add_support_message(s, tid, "admin", "проверяем", admin_id=999)
            self.assertEqual(ticket.status, "answered")
        # сообщение пользователя → снова open
        async with self.Session() as s:
            ticket, _ = await repo.add_support_message(s, tid, "user", "жду")
            self.assertEqual(ticket.status, "open")
        async with self.Session() as s:
            self.assertEqual(len(await repo.get_ticket_messages(s, tid)), 3)
            self.assertEqual(await repo.count_open_tickets(s), 1)
            self.assertEqual(len(await repo.list_user_tickets(s, 30)), 1)
            self.assertEqual(len(await repo.list_admin_tickets(s, status="active")), 1)
        # закрытие
        async with self.Session() as s:
            await repo.set_ticket_status(s, tid, "closed")
            self.assertEqual(await repo.count_open_tickets(s), 0)
            self.assertEqual(len(await repo.list_admin_tickets(s, status="closed")), 1)


if __name__ == "__main__":
    unittest.main()
