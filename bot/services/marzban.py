"""Клиент Marzban API: создание и продление подписок."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from urllib.parse import urlparse

import httpx

from bot.config import Plan, settings

logger = logging.getLogger(__name__)


class MarzbanError(Exception):
    pass


def _parse_note_devices(note: str | None) -> int:
    """Вытащить лимит устройств из note вида 'tg:123 | devices:5'."""
    if not note:
        return 0
    match = re.search(r"devices:(\d+)", note)
    return int(match.group(1)) if match else 0


def _device_note(telegram_id: int, devices: int) -> str:
    """Device-limit note for Marzban without exposing Telegram ID.

    Older accounts may still have legacy notes like `tg:<id> | devices:N`.
    New/updated accounts use only a coarse internal marker plus the device limit;
    the Telegram↔Marzban mapping lives in the local subscriptions table.
    """
    return f"vexvpn | devices:{devices}"


def _legacy_username(telegram_id: int) -> str:
    return f"tg_{telegram_id}"


def _random_username() -> str:
    return f"vxu_{secrets.token_hex(12)}"


class MarzbanClient:
    """Тонкий асинхронный клиент Marzban с авто-обновлением токена."""

    def __init__(self) -> None:
        self.base_url = settings.marzban_base_url.rstrip("/")
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._servers_cache: int | None = None
        self._servers_exp: float = 0.0
        # Кэш потребления по telegram_id: {id: (timestamp, usage|None)}.
        # Кабинет авто-рефрешится раз в минуту у каждого юзера — без кэша это N запросов
        # к панели в минуту. Кэш на ~45с снимает нагрузку, оставаясь «почти live».
        self._usage_cache: dict[int, tuple[float, dict | None]] = {}
        self._auth_lock = asyncio.Lock()
        self._usage_cache_lock = asyncio.Lock()
        self._servers_lock = asyncio.Lock()

    # ── авторизация ──────────────────────────────────────────────
    async def _authenticate(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            f"{self.base_url}/api/admin/token",
            data={
                "username": settings.marzban_username,
                "password": settings.marzban_password,
            },
        )
        if resp.status_code != 200:
            raise MarzbanError(f"Авторизация не удалась: {resp.status_code} {resp.text}")
        self._token = resp.json()["access_token"]
        # токен Marzban живёт ~24ч, обновляем с запасом раз в час
        self._token_exp = time.time() + 3600

    async def _auth_header(self, client: httpx.AsyncClient) -> dict[str, str]:
        if not self._token or time.time() > self._token_exp:
            async with self._auth_lock:
                if not self._token or time.time() > self._token_exp:
                    await self._authenticate(client)
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self, method: str, path: str, client: httpx.AsyncClient, **kwargs
    ) -> httpx.Response:
        headers = await self._auth_header(client)
        resp = await client.request(
            method, f"{self.base_url}{path}", headers=headers, **kwargs
        )
        if resp.status_code == 401:  # токен протух — переавторизуемся и повторяем
            async with self._auth_lock:
                self._token = None
                self._token_exp = 0.0
                await self._authenticate(client)
            headers = {"Authorization": f"Bearer {self._token}"}
            resp = await client.request(
                method, f"{self.base_url}{path}", headers=headers, **kwargs
            )
        return resp

    # ── операции ─────────────────────────────────────────────────
    async def get_user(
        self, username: str, client: httpx.AsyncClient
    ) -> dict | None:
        resp = await self._request("GET", f"/api/user/{username}", client)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise MarzbanError(f"get_user: {resp.status_code} {resp.text}")
        return resp.json()

    def _full_sub_url(self, sub_url: str) -> str:
        if sub_url.startswith("http"):
            return sub_url
        return f"{self.base_url}{sub_url}"

    def _public_sub_url(self, sub_url: str) -> str:
        """Gateway URL: browser получает красивую страницу, VPN-клиент — raw config."""
        full = self._full_sub_url(sub_url)
        token = urlparse(full).path.rstrip('/').split('/')[-1]
        return f"{settings.sub_public_base}/sub/{token}" if token else full

    def _invalidate_usage(self, telegram_id: int) -> None:
        """Сбросить кэш потребления после изменения юзера (выдача/сброс/статус)."""
        self._usage_cache.pop(telegram_id, None)

    async def _stored_username(self, telegram_id: int) -> str | None:
        """Return local Telegram→Marzban mapping, if it exists.

        Fail closed for privacy only when creating new accounts; for operational
        actions on legacy accounts we fall back to tg_<id> so existing users are
        not broken by the migration.
        """
        try:
            from sqlalchemy import select
            from bot.db.database import session_maker
            from bot.db.models import Subscription

            async with session_maker() as session:
                return await session.scalar(select(Subscription.marzban_username).where(Subscription.telegram_id == telegram_id))
        except Exception:
            logger.warning("Could not resolve stored Marzban username for %s; using legacy fallback", telegram_id, exc_info=True)
            return None

    async def _username_for_existing(self, telegram_id: int) -> str:
        return await self._stored_username(telegram_id) or _legacy_username(telegram_id)

    async def _username_for_create_or_renew(self, telegram_id: int, client: httpx.AsyncClient, explicit_username: str | None = None) -> str:
        if explicit_username:
            return explicit_username
        stored = await self._stored_username(telegram_id)
        if stored:
            return stored
        legacy = _legacy_username(telegram_id)
        try:
            if await self.get_user(legacy, client):
                return legacy
        except Exception:
            logger.warning("Could not probe legacy Marzban username for %s", telegram_id, exc_info=True)
        for _ in range(8):
            candidate = _random_username()
            if not await self.get_user(candidate, client):
                return candidate
        raise MarzbanError("Не удалось сгенерировать уникальный Marzban username")

    async def reset_traffic(self, telegram_id: int) -> None:
        """Сбросить израсходованный трафик пользователя (used_traffic → 0)."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            username = await self._username_for_existing(telegram_id)
            resp = await self._request("POST", f"/api/user/{username}/reset", client)
        if resp.status_code not in (200, 204):
            raise MarzbanError(f"reset: {resp.status_code} {resp.text}")
        self._invalidate_usage(telegram_id)

    async def set_status(self, telegram_id: int, status: str) -> None:
        """Включить/выключить пользователя (status: active|disabled)."""
        if status not in ("active", "disabled"):
            raise MarzbanError(f"Недопустимый статус: {status}")
        async with httpx.AsyncClient(timeout=15.0) as client:
            username = await self._username_for_existing(telegram_id)
            resp = await self._request("PUT", f"/api/user/{username}", client, json={"status": status})
        if resp.status_code != 200:
            raise MarzbanError(f"set_status: {resp.status_code} {resp.text}")
        self._invalidate_usage(telegram_id)

    async def revoke_sub(self, telegram_id: int) -> str:
        """Перевыпустить ссылку подписки (revoke_sub).

        Сбрасывает старый токен подписки — конфиги на прежних устройствах
        перестают работать. Используется для self-service «сменить устройство».
        Возвращает новую полную ссылку подписки.
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            username = await self._username_for_existing(telegram_id)
            resp = await self._request("POST", f"/api/user/{username}/revoke_sub", client)
        if resp.status_code != 200:
            raise MarzbanError(f"revoke_sub: {resp.status_code} {resp.text}")
        self._invalidate_usage(telegram_id)
        return self._public_sub_url(resp.json().get("subscription_url", ""))

    async def delete_user(self, telegram_id: int) -> None:
        """Удалить пользователя в Marzban (идемпотентно: 404 считаем успехом)."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            username = await self._username_for_existing(telegram_id)
            resp = await self._request("DELETE", f"/api/user/{username}", client)
        if resp.status_code not in (200, 204, 404):
            raise MarzbanError(f"delete: {resp.status_code} {resp.text}")
        self._invalidate_usage(telegram_id)

    async def servers_online(self) -> int | None:
        """Кол-во подключённых нод Marzban (best-effort, кэш 5 минут)."""
        now = time.time()
        async with self._servers_lock:
            if now < self._servers_exp:
                return self._servers_cache
            value: int | None = None
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await self._request("GET", "/api/nodes", client)
                if resp.status_code == 200:
                    nodes = resp.json()
                    if isinstance(nodes, list):
                        connected = sum(1 for n in nodes if str(n.get("status")).lower() == "connected")
                        # У одно-нодовых установок список нод пуст — считаем 1 активный сервер.
                        value = connected if nodes else 1
            except Exception:
                logger.warning("Не удалось получить статус нод Marzban", exc_info=True)
                value = None
            self._servers_cache = value
            self._servers_exp = now + 300  # не дёргаем панель чаще раза в 5 минут
            return value

    async def get_usage(self, telegram_id: int, *, max_age: float = 45.0) -> dict | None:
        """Текущее потребление пользователя (best-effort, не бросает наружу).

        Возвращает dict: used_traffic, data_limit (0 = безлимит), expire, status.
        Результат кэшируется на max_age секунд по telegram_id, чтобы авто-рефреш
        кабинета и профиль бота не дёргали панель на каждый показ. Кэш сбрасывается
        при выдаче/сбросе/смене статуса (см. _invalidate_usage). Ошибки сети НЕ
        кэшируются — чтобы быстро восстановиться после недоступности панели.
        """
        now = time.time()
        async with self._usage_cache_lock:
            cached = self._usage_cache.get(telegram_id)
            if cached and now - cached[0] < max_age:
                return cached[1]

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                username = await self._username_for_existing(telegram_id)
                data = await self.get_user(username, client)
        except Exception:
            logger.warning("Не удалось получить usage для %s", telegram_id, exc_info=True)
            return None
        if not data:
            result: dict | None = None
        else:
            result = {
                "used_traffic": int(data.get("used_traffic") or 0),
                "data_limit": int(data.get("data_limit") or 0),
                "expire": int(data.get("expire") or 0),
                "status": data.get("status"),
                "online_at": data.get("online_at") or data.get("last_online") or data.get("last_online_at"),
            }
        async with self._usage_cache_lock:
            self._usage_cache[telegram_id] = (now, result)
        return result

    async def create_or_renew(self, telegram_id: int, plan: Plan, *, username: str | None = None) -> dict:
        """Создать пользователя в Marzban или продлить существующего.

        New accounts use random Marzban usernames (`vxu_<random>`) to avoid
        exposing Telegram IDs. Existing subscriptions keep their stored mapping;
        legacy `tg_<telegram_id>` accounts continue to work.
        Возвращает dict: username, subscription_url, expire (unix), data_limit.
        """
        now = int(time.time())

        async with httpx.AsyncClient(timeout=20.0) as client:
            username = await self._username_for_create_or_renew(telegram_id, client, username)
            existing = await self.get_user(username, client)

            added_seconds = plan.days * 86400
            added_traffic = plan.data_limit_bytes

            if existing:
                current_expire = existing.get("expire") or 0
                current_limit = existing.get("data_limit") or 0  # 0 = безлимит
                base = max(now, current_expire)
                new_expire = base + added_seconds if added_seconds else current_expire

                # Важно: покупка/промокод НЕ должны ухудшать текущий лимит.
                # - уже безлимит или покупается безлимитный тариф → остаётся безлимит
                # - выдача только дней (реф-бонус, админ +дни) → текущий лимит сохраняется
                # - иначе докупленный трафик прибавляется к текущему лимиту
                if current_limit == 0 or plan.unlimited:
                    new_data_limit = 0
                elif added_traffic == 0:
                    new_data_limit = current_limit
                else:
                    new_data_limit = current_limit + added_traffic

                payload = {
                    "expire": new_expire,
                    "data_limit": new_data_limit,
                    "data_limit_reset_strategy": "no_reset",
                    "status": "active",
                }
                # Лимит устройств в note. Докупка трафика (traffic_only) не меняет тариф —
                # note не трогаем; иначе берём максимум, чтобы не понизить тариф при +днях/бонусе.
                if not plan.traffic_only:
                    devices = max(plan.devices, _parse_note_devices(existing.get("note")))
                    payload["note"] = _device_note(telegram_id, devices)
                resp = await self._request(
                    "PUT", f"/api/user/{username}", client, json=payload
                )
                if resp.status_code != 200:
                    raise MarzbanError(f"modify: {resp.status_code} {resp.text}")
            else:
                if plan.traffic_only:
                    raise MarzbanError("Пакет трафика можно купить только при активной подписке")
                new_expire = now + added_seconds
                new_data_limit = added_traffic
                payload = {
                    "username": username,
                    "proxies": settings.proxy_config or {"vless": {}},
                    "inbounds": {},
                    "expire": new_expire,
                    "data_limit": new_data_limit,
                    "data_limit_reset_strategy": "no_reset",
                    "status": "active",
                    "note": _device_note(telegram_id, plan.devices),
                }
                resp = await self._request("POST", "/api/user", client, json=payload)
                if resp.status_code not in (200, 201):
                    raise MarzbanError(f"create: {resp.status_code} {resp.text}")

            data = resp.json()
            sub_url = self._public_sub_url(data.get("subscription_url", ""))
            self._invalidate_usage(telegram_id)
            return {
                "username": username,
                "subscription_url": sub_url,
                "expire": new_expire,
                "data_limit": new_data_limit,
            }


# единый инстанс на всё приложение
marzban = MarzbanClient()
