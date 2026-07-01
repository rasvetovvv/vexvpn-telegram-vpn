"""Telegram Mini App initData validation."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from bot.config import settings


def _secret_key() -> bytes:
    return hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()


def validate_init_data(init_data: str, max_age: int = 86400) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Telegram initData is missing")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram initData hash is missing")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    calculated = hmac.new(_secret_key(), data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(status_code=401, detail="Telegram initData is invalid")

    auth_date = int(pairs.get("auth_date", "0") or 0)
    if auth_date and time.time() - auth_date > max_age:
        raise HTTPException(status_code=401, detail="Telegram initData is expired")

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="Telegram user payload is invalid") from exc
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user id is missing")
    return user


async def telegram_user(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    return validate_init_data(x_telegram_init_data or "")
