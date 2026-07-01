"""Простой in-memory rate limit для Mini App API.

Это защита от частого клика/спама по дорогим endpoint'ам. Для одного
uvicorn-процесса достаточно; при масштабировании на несколько процессов лучше
перенести счётчики в Redis.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException


@dataclass(frozen=True)
class RateLimit:
    limit: int
    window_seconds: int


_BUCKETS: dict[str, deque[float]] = defaultdict(deque)


def require_rate_limit(key: str, rule: RateLimit) -> None:
    """Бросить 429, если по ключу слишком много запросов за окно."""
    now = time.monotonic()
    bucket = _BUCKETS[key]
    cutoff = now - rule.window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.popleft()

    if len(bucket) >= rule.limit:
        retry_after = max(1, int(rule.window_seconds - (now - bucket[0])))
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много запросов. Попробуй через {retry_after} сек.",
            headers={"Retry-After": str(retry_after)},
        )

    bucket.append(now)
