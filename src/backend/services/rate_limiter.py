"""
Unified Redis sliding-window rate limiter (#1023).

One audited implementation that replaces ad-hoc per-endpoint **request-rate**
limiters (starting with the webhook trigger). Uses a Redis sorted set as a
true rolling window — no fixed-window boundary burst, where a flood straddling
a window edge could pass up to 2x the limit.

Fail-open on Redis unavailability, with a bounded per-worker in-process
fallback (lifted from the webhook limiter) so a Redis blip can't either 500
legitimate traffic or remove all backpressure.

SCOPE NOTE: this is a REQUEST-RATE limiter. The auth login/OTP limiters
(`routers/auth.py`) are **failure-counters** — they increment only on failure
and reset on success — which is a different pattern and intentionally does NOT
use this primitive. See #1023.

Algorithm (atomic via pipeline):
    ZREMRANGEBYSCORE key 0 (now-window)   # drop entries outside the window
    ZADD key {member: now}                # record this request
    ZCARD key                             # count requests in window
    EXPIRE key window+1                   # bound key lifetime
If the post-add count exceeds the limit, the just-added member is removed
(so a rejected request doesn't keep the window saturated) and a 429-worthy
result with Retry-After is returned. Mirrors the INCR-then-compare anti-TOCTOU
property of the prior webhook limiter (#644): the count is taken after an
atomic add, never via a read-then-write gap.
"""

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int  # seconds until the caller may retry (0 when allowed)
    limit: int


# ---------------------------------------------------------------------------
# Cached Redis client (mirrors the hardened webhook pattern: cache the client +
# its pool, reset on connection/auth error, ACL-aware logging, fail-open None).
# Re-creating redis.from_url() per call opens a fresh TCP connection per
# request, which under a flood exhausts Redis maxclients and turns the limiter
# into a DoS amplifier.
# ---------------------------------------------------------------------------

_redis_client = None
_redis_client_lock = threading.Lock()


def reset_redis_client() -> None:
    """Drop the cached client so the next call rebuilds it."""
    global _redis_client
    with _redis_client_lock:
        _redis_client = None


def _get_redis():
    """Return a cached Redis client, or None if Redis is unavailable (fail-open)."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    try:
        import redis as _redis
        from config import REDIS_URL
    except Exception as e:  # import-time failure (config.py raises on bad URL)
        logger.error("Rate limiter: cannot import Redis client/config: %s", e)
        return None

    from redis.exceptions import (
        AuthenticationError,
        AuthenticationWrongNumberOfArgsError,
        ConnectionError as RedisConnectionError,
        ResponseError,
        TimeoutError as RedisTimeoutError,
    )

    with _redis_client_lock:
        if _redis_client is not None:  # racy double-check
            return _redis_client
        try:
            r = _redis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
            r.ping()
            _redis_client = r
            return _redis_client
        except (AuthenticationError, AuthenticationWrongNumberOfArgsError) as e:
            logger.error("Rate limiter Redis AUTH failed (%s) — check REDIS_URL/ACL", type(e).__name__)
            return None
        except ResponseError as e:
            msg = str(e).upper()
            if any(s in msg for s in ("NOAUTH", "NOPERM", "WRONGPASS")):
                logger.error("Rate limiter Redis ACL/auth error: %s", e)
            else:
                logger.warning("Rate limiter Redis ResponseError: %s", e)
            return None
        except (RedisConnectionError, RedisTimeoutError) as e:
            logger.warning("Rate limiter Redis transient error: %s", e)
            return None
        except Exception as e:  # last-resort net — still fail-open
            logger.warning("Rate limiter Redis unexpected error: %s", e)
            return None


# ---------------------------------------------------------------------------
# In-process fallback — per-key sliding window of timestamps, per worker.
# Used only when Redis is unreachable. Cardinality is bounded by the number of
# distinct keys the caller passes (DB-resolved tokens etc.), so this can't grow
# from random-key spam. Best-effort: each worker enforces independently, so the
# effective fleet limit during a Redis outage is (workers x limit).
# ---------------------------------------------------------------------------

_inprocess_buckets: Dict[str, Deque[float]] = {}
_inprocess_lock = threading.Lock()


def clear_inprocess() -> None:
    """Reset the in-process fallback buckets (test hook)."""
    with _inprocess_lock:
        _inprocess_buckets.clear()


def _check_inprocess(key: str, limit: int, window_seconds: int) -> RateLimitResult:
    now = time.monotonic()
    cutoff = now - window_seconds
    with _inprocess_lock:
        bucket = _inprocess_buckets.setdefault(key, deque())
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(bucket[0] + window_seconds - now) + 1)
            return RateLimitResult(False, 0, retry_after, limit)
        bucket.append(now)
        return RateLimitResult(True, max(0, limit - len(bucket)), 0, limit)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check(key: str, limit: int, window_seconds: int) -> RateLimitResult:
    """Record one hit against ``key`` and report whether it is within ``limit``
    over the trailing ``window_seconds``. Fail-open via the in-process fallback
    when Redis is unavailable."""
    r = _get_redis()
    if r is None:
        return _check_inprocess(key, limit, window_seconds)

    redis_key = f"ratelimit:{key}"
    try:
        now = time.time()
        member = f"{now:.6f}:{uuid.uuid4().hex}"
        cutoff = now - window_seconds
        pipe = r.pipeline()
        pipe.zremrangebyscore(redis_key, 0, cutoff)
        pipe.zadd(redis_key, {member: now})
        pipe.zcard(redis_key)
        pipe.expire(redis_key, window_seconds + 1)
        results = pipe.execute()
        count = int(results[2])

        if count > limit:
            # Drop our own entry so a rejected request doesn't keep the window
            # saturated, then compute retry_after from the oldest survivor.
            try:
                r.zrem(redis_key, member)
            except Exception:
                pass
            retry_after = window_seconds
            try:
                oldest = r.zrange(redis_key, 0, 0, withscores=True)
                if oldest:
                    retry_after = max(1, int(window_seconds - (now - oldest[0][1])) + 1)
            except Exception:
                pass
            return RateLimitResult(False, 0, retry_after, limit)

        return RateLimitResult(True, max(0, limit - count), 0, limit)
    except Exception as e:
        # Stale cached client (server restart, network blip) or other error —
        # drop it so the next call rebuilds, and fall back in-process.
        logger.warning("Rate limiter primary check failed (%s) — using in-process fallback", e)
        reset_redis_client()
        return _check_inprocess(key, limit, window_seconds)


def enforce(
    key: str,
    limit: int,
    window_seconds: int,
    detail: str = "Rate limit exceeded.",
) -> RateLimitResult:
    """check() + raise HTTP 429 (with Retry-After) when over the limit."""
    result = check(key, limit, window_seconds)
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"{detail} Try again in {result.retry_after} seconds.",
            headers={"Retry-After": str(result.retry_after)},
        )
    return result
