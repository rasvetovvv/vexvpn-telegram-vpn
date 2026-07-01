from __future__ import annotations

import asyncio
import unittest

from bot.config import PROMOS, Plan
from bot.services import payments
from bot.services.marzban import MarzbanClient, MarzbanError
from bot.services.promos import parse_promo_create_args
from bot.utils import fmt_size


async def _async_value(value):
    return value


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class PaymentPromoTests(unittest.TestCase):
    def test_invoice_payload_roundtrip_and_sale30_discount(self) -> None:
        plan = Plan("standard_30d", "Standard", 30, 150, 100, 1)
        payload = payments.build_payload(plan, "sale30")

        self.assertEqual(payload, "plan:standard_30d:SALE30")
        self.assertEqual(payments.parse_payload(payload), ("standard_30d", "SALE30"))
        self.assertEqual(payments.price_with_promo(plan, PROMOS["SALE30"]), 105)
        self.assertEqual(payments.build_prices(plan, PROMOS["SALE30"])[0].amount, 105)

    def test_invalid_payload_is_rejected(self) -> None:
        self.assertEqual(payments.parse_payload("bad:standard_30d"), (None, None))
        self.assertEqual(payments.parse_payload(""), (None, None))

    def test_free7_is_configured_as_free_grant_not_percent_coupon(self) -> None:
        promo = PROMOS["FREE7"]
        self.assertTrue(promo.once_per_user)
        self.assertIsNotNone(promo.free_plan_key)
        self.assertFalse(promo.percent)

    def test_promo_create_parses_days_traffic_discount_and_segments(self) -> None:
        code, promo = parse_promo_create_args("/promo_create FREE30 30d")
        self.assertEqual(code, "FREE30")
        self.assertEqual(promo.kind, "days")
        self.assertEqual(promo.value, 30)
        self.assertTrue(promo.once_per_user)

        code, promo = parse_promo_create_args("/promo_create TRAFFIC100 100gb old")
        self.assertEqual(promo.kind, "traffic")
        self.assertEqual(promo.value, 100)
        self.assertTrue(promo.old_only)

        code, promo = parse_promo_create_args("/promo_create SALE50 50% new")
        self.assertEqual(promo.kind, "discount")
        self.assertEqual(promo.percent, 50)
        self.assertTrue(promo.new_only)

        code, promo = parse_promo_create_args("/promo_create TRIAL3 3d trial global")
        self.assertEqual(promo.kind, "trial")
        self.assertEqual(promo.global_limit, 1)

    def test_wheel_pick_segment_is_valid(self) -> None:
        from bot.config import GAMI_WHEEL_SEGMENTS
        from bot.services.gamification import pick_segment

        keys = {s["key"] for s in GAMI_WHEEL_SEGMENTS}
        for _ in range(200):
            seg = pick_segment()
            self.assertIn(seg["key"], keys)
            self.assertIn(seg["kind"], {"none", "days", "traffic", "promo"})

    def test_promo_create_rejects_unsafe_code(self) -> None:
        # Баг 5: код промокода попадает в HTML кабинета — символы разметки запрещены.
        code, err = parse_promo_create_args("/promo_create BAD<IMG> 30d")
        self.assertIsNone(code)
        self.assertIn("промокод", err.lower())
        self.assertIsNone(parse_promo_create_args("/promo_create FREE-30 30d")[0])
        # Валидный код проходит как прежде.
        ok, promo = parse_promo_create_args("/promo_create FREE_30 30d")
        self.assertEqual(ok, "FREE_30")
        self.assertEqual(promo.kind, "days")


class TrafficGrantTests(unittest.IsolatedAsyncioTestCase):
    async def _run_create_or_renew(self, existing: dict | None, plan: Plan) -> dict:
        client = MarzbanClient()
        client.base_url = "https://vpn.example.com"
        username = "tg_12345"

        async def fake_get_user(_username, _http_client):
            self.assertEqual(_username, username)
            return existing

        async def fake_request(method, path, _http_client, **kwargs):
            self.assertIn(method, {"POST", "PUT"})
            payload = kwargs["json"]
            result = {
                "username": username,
                "subscription_url": "/sub/token",
                "expire": payload["expire"],
                "data_limit": payload["data_limit"],
            }
            return _FakeResponse(200, result)

        client.get_user = fake_get_user  # type: ignore[method-assign]
        client._request = fake_request  # type: ignore[method-assign]
        return await client.create_or_renew(12345, plan, username=username)

    async def _capture_payload(self, existing: dict | None, plan: Plan) -> dict:
        client = MarzbanClient()
        client.base_url = "https://vpn.example.com"
        captured: list[dict] = []

        async def fake_get_user(_username, _http_client):
            return existing

        async def fake_request(method, path, _http_client, **kwargs):
            payload = kwargs["json"]
            captured.append(payload)
            return _FakeResponse(200, {
                "username": "tg_12345",
                "subscription_url": "/sub/token",
                "expire": payload["expire"],
                "data_limit": payload["data_limit"],
            })

        client.get_user = fake_get_user  # type: ignore[method-assign]
        client._request = fake_request  # type: ignore[method-assign]
        await client.create_or_renew(12345, plan, username="tg_12345")
        return captured[0]

    async def test_create_sets_device_note(self) -> None:
        plan = Plan("family_30d", "Family", 30, 390, 0, 5, unlimited=True)
        payload = await self._capture_payload(None, plan)
        self.assertIn("devices:5", payload["note"])
        self.assertNotIn("tg:", payload["note"])

    async def test_new_accounts_use_random_marzban_username_when_no_mapping_exists(self) -> None:
        client = MarzbanClient()
        client.base_url = "https://vpn.example.com"
        seen: list[str] = []

        async def fake_stored(_telegram_id):
            return None

        async def fake_get_user(username, _http_client):
            seen.append(username)
            # legacy probe and random collision checks see no existing Marzban user
            return None

        async def fake_request(method, path, _http_client, **kwargs):
            payload = kwargs["json"]
            return _FakeResponse(201, {
                "username": payload["username"],
                "subscription_url": "/sub/random-token",
                "expire": payload["expire"],
                "data_limit": payload["data_limit"],
            })

        client._stored_username = fake_stored  # type: ignore[method-assign]
        client.get_user = fake_get_user  # type: ignore[method-assign]
        client._request = fake_request  # type: ignore[method-assign]
        result = await client.create_or_renew(12345, Plan("lite", "Lite", 1, 1, 1, 1))
        self.assertRegex(result["username"], r"^vxu_[0-9a-f]{24}$")
        self.assertNotEqual(result["username"], "tg_12345")
        self.assertIn("tg_12345", seen)  # legacy probe for backward compatibility

    async def test_existing_mapping_is_preserved_for_renewal(self) -> None:
        client = MarzbanClient()
        client.base_url = "https://vpn.example.com"
        mapped = "vxu_existingabcdef1234567890"

        async def fake_stored(_telegram_id):
            return mapped

        async def fake_get_user(username, _http_client):
            self.assertEqual(username, mapped)
            return {"expire": 2_000_000_000, "data_limit": 10 * 1024**3, "note": "vexvpn | devices:2"}

        async def fake_request(method, path, _http_client, **kwargs):
            self.assertIn(mapped, path)
            payload = kwargs["json"]
            return _FakeResponse(200, {
                "username": mapped,
                "subscription_url": "/sub/existing-token",
                "expire": payload["expire"],
                "data_limit": payload["data_limit"],
            })

        client._stored_username = fake_stored  # type: ignore[method-assign]
        client.get_user = fake_get_user  # type: ignore[method-assign]
        client._request = fake_request  # type: ignore[method-assign]
        result = await client.create_or_renew(12345, Plan("lite", "Lite", 1, 1, 1, 2))
        self.assertEqual(result["username"], mapped)

    async def test_renew_does_not_downgrade_device_note(self) -> None:
        # Был family (5 устройств), пришёл реф-бонус (devices=1) → лимит в note не понижаем.
        plan = Plan("ref_bonus_3d", "Бонус", 3, 0, 0, 1)
        payload = await self._capture_payload(
            {"expire": 2_000_000_000, "data_limit": 0, "note": "tg:12345 | devices:5"},
            plan,
        )
        self.assertIn("devices:5", payload["note"])

    async def test_traffic_addon_does_not_touch_note(self) -> None:
        # Докупка трафика не меняет тариф — note (с лимитом устройств) не трогаем.
        plan = Plan("traffic_30gb", "Traffic", 0, 20, 30, 1, traffic_only=True)
        payload = await self._capture_payload(
            {"expire": 2_000_000_000, "data_limit": 100 * 1024**3, "note": "tg:12345 | devices:5"},
            plan,
        )
        self.assertNotIn("note", payload)

    async def test_renew_preserves_unlimited_traffic(self) -> None:
        plan = Plan("lite_7d", "Lite", 7, 50, 30, 1)
        result = await self._run_create_or_renew(
            {"expire": 2_000_000_000, "data_limit": 0},
            plan,
        )
        self.assertEqual(result["data_limit"], 0)
        self.assertGreater(result["expire"], 2_000_000_000)

    async def test_traffic_addon_adds_to_existing_limit_without_extending_days(self) -> None:
        plan = Plan("traffic_30gb", "Traffic 30GB", 0, 20, 30, 1, traffic_only=True)
        current_expire = 2_000_000_000
        current_limit = 100 * 1024**3
        result = await self._run_create_or_renew(
            {"expire": current_expire, "data_limit": current_limit},
            plan,
        )
        self.assertEqual(result["expire"], current_expire)
        self.assertEqual(result["data_limit"], current_limit + 30 * 1024**3)

    async def test_days_only_topup_preserves_existing_limit(self) -> None:
        # Реф-бонус / админская выдача «только дни» НЕ должны обнулять лимит трафика.
        plan = Plan("ref_bonus_3d", "Бонус", 3, 0, 0, 1)
        current_limit = 30 * 1024**3
        result = await self._run_create_or_renew(
            {"expire": 2_000_000_000, "data_limit": current_limit},
            plan,
        )
        self.assertEqual(result["data_limit"], current_limit)
        self.assertGreater(result["expire"], 2_000_000_000)

    async def test_unlimited_plan_clears_limit(self) -> None:
        # Покупка безлимитного тарифа снимает текущий лимит.
        plan = Plan("unlimited_30d", "Unlimited", 30, 250, 0, 3, unlimited=True)
        result = await self._run_create_or_renew(
            {"expire": 2_000_000_000, "data_limit": 30 * 1024**3},
            plan,
        )
        self.assertEqual(result["data_limit"], 0)

    async def test_traffic_addon_requires_existing_subscription(self) -> None:
        client = MarzbanClient()

        async def fake_get_user(_username, _http_client):
            return None

        client.get_user = fake_get_user  # type: ignore[method-assign]
        plan = Plan("traffic_30gb", "Traffic 30GB", 0, 20, 30, 1, traffic_only=True)
        with self.assertRaises(MarzbanError):
            await client.create_or_renew(12345, plan, username="tg_12345")

    async def test_get_usage_parses_fields(self) -> None:
        client = MarzbanClient()

        async def fake_get_user(_username, _http_client):
            return {"used_traffic": 5 * 1024**3, "data_limit": 30 * 1024**3, "expire": 123, "status": "active"}

        client.get_user = fake_get_user  # type: ignore[method-assign]
        client._stored_username = lambda _telegram_id: _async_value("tg_12345")  # type: ignore[method-assign]
        usage = await client.get_usage(12345)
        self.assertEqual(usage["used_traffic"], 5 * 1024**3)
        self.assertEqual(usage["data_limit"], 30 * 1024**3)
        self.assertEqual(usage["status"], "active")

    async def test_get_usage_handles_missing_user(self) -> None:
        client = MarzbanClient()

        async def fake_get_user(_username, _http_client):
            return None

        client.get_user = fake_get_user  # type: ignore[method-assign]
        client._stored_username = lambda _telegram_id: _async_value("tg_12345")  # type: ignore[method-assign]
        self.assertIsNone(await client.get_usage(12345))

    async def test_revoke_sub_returns_new_full_url(self) -> None:
        client = MarzbanClient()
        client.base_url = "https://vpn.example.com"

        async def fake_request(method, path, _http_client, **kwargs):
            self.assertEqual(method, "POST")
            self.assertTrue(path.endswith("/revoke_sub"))
            return _FakeResponse(200, {"subscription_url": "/sub/new"})

        client._request = fake_request  # type: ignore[method-assign]
        client._stored_username = lambda _telegram_id: _async_value("tg_12345")  # type: ignore[method-assign]
        url = await client.revoke_sub(12345)
        # Public subscription URLs must point to the VexVPN gateway, not directly
        # to Marzban, so browsers get the status/instructions page and clients
        # still receive raw configs.
        self.assertEqual(url, "https://proxy.vexory.xyz/sub/new")

    async def test_get_usage_is_cached_and_invalidated(self) -> None:
        # Баг 4: повторный показ кабинета не должен дёргать панель — отдаём из кэша.
        client = MarzbanClient()
        calls = {"n": 0}

        async def fake_get_user(_username, _http_client):
            calls["n"] += 1
            return {"used_traffic": 1, "data_limit": 2, "expire": 3, "status": "active"}

        client.get_user = fake_get_user  # type: ignore[method-assign]
        client._stored_username = lambda _telegram_id: _async_value("tg_777")  # type: ignore[method-assign]
        first = await client.get_usage(777)
        second = await client.get_usage(777)
        self.assertEqual(first, second)
        self.assertEqual(calls["n"], 1)  # второй вызов взят из кэша
        # После выдачи/сброса кэш инвалидируется — снова идём в панель.
        client._invalidate_usage(777)
        await client.get_usage(777)
        self.assertEqual(calls["n"], 2)


class FormatTests(unittest.TestCase):
    def test_fmt_size(self) -> None:
        self.assertEqual(fmt_size(0), "0 МБ")
        self.assertEqual(fmt_size(30 * 1024**3), "30.0 ГБ")
        self.assertEqual(fmt_size(512 * 1024**2), "512 МБ")


if __name__ == "__main__":
    unittest.main()
