"""Rate limiting for Mini App/API endpoints.

Production uses Redis when REDIS_URL is configured, so limits are shared across
multiple Uvicorn workers or replicated containers. If Redis is unavailable or
not configured, the limiter falls back to an in-memory sliding window. The
fallback is acceptable for single-process development but not sufficient as the
only production control.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimit:
    limit: int
    window_seconds: int


_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_REDIS_CLIENT = None
_REDIS_DISABLED = False


def _too_many(retry_after: int) -> None:
    raise HTTPException(
        status_code=429,
        detail=f"Слишком много запросов. Попробуй через {retry_after} сек.",
        headers={"Retry-After": str(max(1, int(retry_after)))},
    )


def _memory_rate_limit(key: str, rule: RateLimit) -> None:
    now = time.monotonic()
    bucket = _BUCKETS[key]
    cutoff = now - rule.window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.popleft()

    if len(bucket) >= rule.limit:
        retry_after = max(1, int(rule.window_seconds - (now - bucket[0])))
        _too_many(retry_after)

    bucket.append(now)


def _redis_client():
    global _REDIS_CLIENT, _REDIS_DISABLED
    if _REDIS_DISABLED:
        return None
    try:
        from bot.config import settings

        redis_url = (settings.redis_url or "").strip()
        if not redis_url:
            return None
        if _REDIS_CLIENT is None:
            import redis

            _REDIS_CLIENT = redis.Redis.from_url(
                redis_url,
                socket_connect_timeout=0.4,
                socket_timeout=0.4,
                decode_responses=True,
            )
        return _REDIS_CLIENT
    except Exception:
        logger.warning("Redis rate limiter disabled; falling back to memory", exc_info=True)
        _REDIS_DISABLED = True
        return None


def _redis_rate_limit(key: str, rule: RateLimit) -> bool:
    """Return True if handled by Redis; raise HTTPException on limit hit."""
    client = _redis_client()
    if client is None:
        return False
    bucket = f"rl:{rule.window_seconds}:{key}"
    try:
        # Fixed-window shared limiter. This is intentionally simple and atomic
        # enough for production throttling; exact sliding-window precision is
        # less important than using one shared store across workers/replicas.
        pipe = client.pipeline()
        pipe.incr(bucket, 1)
        pipe.ttl(bucket)
        count, ttl = pipe.execute()
        if int(ttl) < 0:
            client.expire(bucket, rule.window_seconds)
            ttl = rule.window_seconds
        if int(count) > rule.limit:
            _too_many(int(ttl) if int(ttl) > 0 else rule.window_seconds)
        return True
    except HTTPException:
        raise
    except Exception:
        logger.warning("Redis rate limiter failed; falling back to memory", exc_info=True)
        return False


def require_rate_limit(key: str, rule: RateLimit) -> None:
    """Raise 429 if the key exceeds its limit.

    REDIS_URL configured: shared fixed-window limit across processes.
    REDIS_URL missing/unavailable: local in-memory sliding window fallback.
    """
    if _redis_rate_limit(key, rule):
        return
    _memory_rate_limit(key, rule)
