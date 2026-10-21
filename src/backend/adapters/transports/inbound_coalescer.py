"""
Cross-worker inbound coalescer — several channel events, one agent turn
(abilityai/trinity-enterprise#934).

A channel can split one user action across several webhook deliveries:
Telegram sends an album as one update per item, linked by `media_group_id`.
This module buffers the items of a group and hands them to `on_flush` once,
`window_s` after the LAST item arrived (every arrival re-arms the window), or
at once when the group reaches `max_items`.

Why Redis and not an in-process dict: production runs `uvicorn --workers 2`
(docker-compose.prod.yml, docker-compose.hosted.yml) and the items of one group
are separate HTTP requests that land on either worker. An in-process buffer
splits the group about half the time, and a single-process test cannot see it.

How exactly-once works without a leader lease:
- every arrival INCRs the group's `seq` and arms a timer on the worker that
  received it, remembering its own seq;
- a timer drains only if `seq` is still its own (WATCH + MULTI), so only the
  LAST item's timer drains, and an item that lands between the check and the
  drain aborts the drain (WatchError) — its own timer takes over;
- a drain also writes a short tombstone, so an item that arrives after its
  group was flushed is handed back to the caller (one extra turn, logged)
  instead of starting a second, silent buffer.

Orphans: a restart (or a dead worker) leaves items in Redis with no timer.
`sweep_orphans()` runs at transport start and, throttled, after any `add`; an
orphan younger than `recover_max_age_ms` is delivered (`recovered=True`), an
older one is dropped with a WARN naming the group and the item count.

Fail-open: any Redis error on `add` returns False — the caller processes the
item itself, exactly as it would without this module — after a best-effort
forced flush of whatever that group had already buffered. Nothing is dropped
silently.

Telegram-free on purpose: the merge of a flushed group lives with the channel
adapter. `scope` (the bot binding) must not contain ':'.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

from redis.exceptions import WatchError

logger = logging.getLogger(__name__)

# Group TTL in Redis: long enough for the sweep to find an orphan, short enough
# that an abandoned group cannot accumulate.
DEFAULT_KEY_TTL_MS = 10 * 60 * 1000
# How long "this group was flushed" is remembered, for late items.
DEFAULT_TOMBSTONE_MS = 60 * 1000
# A group whose last item is older than this has no live timer anywhere.
# Generous on purpose: the sweep must never steal a group whose timer is still
# pending on the other worker.
DEFAULT_ORPHAN_AFTER_MS = 10 * 1000
# An orphan younger than this is delivered; older ones are dropped with a WARN.
DEFAULT_RECOVER_MAX_AGE_MS = 60 * 1000
# Bounds on what one scope (bot binding) can hold in Redis.
DEFAULT_MAX_OPEN_PER_SCOPE = 50
DEFAULT_MAX_ITEM_BYTES = 64 * 1024
DEFAULT_FLUSH_RETRIES = 3
# At most one opportunistic sweep per process per this interval.
_SWEEP_INTERVAL_MS = 5 * 1000

OnFlush = Callable[..., Awaitable[None]]

_async_redis = None


def get_async_redis():
    """The shared asyncio Redis client (lazy; short timeouts, fail fast)."""
    global _async_redis
    if _async_redis is None:
        import redis.asyncio as aioredis
        from config import REDIS_URL

        _async_redis = aioredis.from_url(
            REDIS_URL, decode_responses=True, socket_connect_timeout=1, socket_timeout=2,
        )
    return _async_redis


class InboundCoalescer:
    def __init__(
        self,
        *,
        prefix: str,
        window_s: float,
        max_items: int,
        on_flush: OnFlush,
        redis=None,
        clock_ms: Optional[Callable[[], int]] = None,
        key_ttl_ms: int = DEFAULT_KEY_TTL_MS,
        tombstone_ms: int = DEFAULT_TOMBSTONE_MS,
        orphan_after_ms: int = DEFAULT_ORPHAN_AFTER_MS,
        recover_max_age_ms: int = DEFAULT_RECOVER_MAX_AGE_MS,
        max_open_per_scope: int = DEFAULT_MAX_OPEN_PER_SCOPE,
        max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
        flush_retries: int = DEFAULT_FLUSH_RETRIES,
    ):
        self._prefix = prefix
        self._window_s = window_s
        self._max_items = max_items
        self._on_flush = on_flush
        self._redis = redis
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._key_ttl_ms = key_ttl_ms
        self._tombstone_ms = tombstone_ms
        self._orphan_after_ms = orphan_after_ms
        self._recover_max_age_ms = recover_max_age_ms
        self._max_open_per_scope = max_open_per_scope
        self._max_item_bytes = max_item_bytes
        self._flush_retries = flush_retries
        # Strong references: a bare create_task can be garbage-collected mid-flight,
        # and a flush runs a whole agent turn.
        self._tasks: set = set()
        self._last_sweep_ms = 0

    # ------------------------------------------------------------------ keys

    def _r(self):
        if self._redis is None:
            self._redis = get_async_redis()
        return self._redis

    def _key(self, scope: str, group_id: str, part: str) -> str:
        return f"{self._prefix}:{scope}:{group_id}:{part}"

    @property
    def _open_key(self) -> str:
        return f"{self._prefix}:open"

    def _open_scope_key(self, scope: str) -> str:
        return f"{self._prefix}:open:{scope}"

    # ------------------------------------------------------------------- add

    async def add(self, scope: str, group_id: str, item: dict) -> bool:
        """Buffer `item`. False means "not buffered — process it yourself"."""
        payload = json.dumps(item)
        if len(payload) > self._max_item_bytes:
            logger.warning(
                "[COALESCE] item for group %s is %d bytes (cap %d) — processed on its own",
                group_id, len(payload), self._max_item_bytes,
            )
            return False
        try:
            seq, count = await self._buffer(scope, group_id, payload)
        except Exception as e:  # Redis down / blip: never drop, never block
            logger.warning(
                "[COALESCE] buffer unavailable for group %s (%s) — item processed on its own",
                group_id, e,
            )
            self._spawn(self._drain_and_flush(scope, group_id, expected_seq=None, recovered=False))
            return False
        if seq is None:
            return False

        if count >= self._max_items:
            self._spawn(self._drain_and_flush(scope, group_id, expected_seq=None, recovered=False))
        else:
            self._spawn(self._flush_after_window(scope, group_id, seq))
        self._maybe_sweep()
        return True

    async def _buffer(self, scope: str, group_id: str, payload: str):
        """Returns (seq, item_count), or (None, 0) when the item must not be buffered."""
        r = self._r()
        member = f"{scope}:{group_id}"
        if await r.exists(self._key(scope, group_id, "flushed")):
            logger.info(
                "[COALESCE] item for group %s arrived after its group was flushed — "
                "processed on its own", group_id,
            )
            return None, 0
        if await r.zscore(self._open_scope_key(scope), member) is None:
            if await r.zcard(self._open_scope_key(scope)) >= self._max_open_per_scope:
                logger.warning(
                    "[COALESCE] scope %s has %d open groups (cap) — group %s not buffered",
                    scope, self._max_open_per_scope, group_id,
                )
                return None, 0
        now = self._clock_ms()
        items, seq_key = self._key(scope, group_id, "items"), self._key(scope, group_id, "seq")
        async with r.pipeline(transaction=True) as p:
            p.rpush(items, payload)
            p.incr(seq_key)
            p.pexpire(items, self._key_ttl_ms)
            p.pexpire(seq_key, self._key_ttl_ms)
            p.zadd(self._open_key, {member: now})
            p.zadd(self._open_scope_key(scope), {member: now})
            count, seq, *_ = await p.execute()
        return int(seq), int(count)

    # ----------------------------------------------------------------- flush

    async def _flush_after_window(self, scope: str, group_id: str, seq: int) -> None:
        await asyncio.sleep(self._window_s)
        await self._drain_and_flush(scope, group_id, expected_seq=seq, recovered=False)

    async def _drain_and_flush(self, scope, group_id, *, expected_seq, recovered) -> None:
        items = None
        for attempt in range(self._flush_retries):
            try:
                items = await self._drain(scope, group_id, expected_seq)
                break
            except WatchError:
                return  # a newer item arrived; its timer owns the flush
            except Exception as e:
                if attempt + 1 == self._flush_retries:
                    logger.error(
                        "[COALESCE] could not drain group %s after %d attempts (%s) — "
                        "left for the orphan sweep", group_id, self._flush_retries, e,
                    )
                    return
                await asyncio.sleep(0.2 * (2 ** attempt))
        if not items:
            return
        logger.info("[COALESCE] flushing group %s: %d item(s)%s",
                    group_id, len(items), " (recovered)" if recovered else "")
        try:
            await self._on_flush(items, recovered=recovered)
        except Exception as e:
            logger.error("[COALESCE] flush handler failed for group %s: %s", group_id, e, exc_info=True)

    async def _drain(self, scope: str, group_id: str, expected_seq: Optional[int]):
        """Atomically take every item of the group. With `expected_seq`, only if no
        item arrived after the caller's — otherwise WatchError / None."""
        r = self._r()
        items_key, seq_key = self._key(scope, group_id, "items"), self._key(scope, group_id, "seq")
        member = f"{scope}:{group_id}"
        async with r.pipeline(transaction=True) as p:
            await p.watch(seq_key)
            if expected_seq is not None:
                current = await p.get(seq_key)
                if current is None or int(current) != expected_seq:
                    await p.unwatch()
                    return None
            p.multi()
            p.lrange(items_key, 0, -1)
            p.delete(items_key, seq_key)
            p.set(self._key(scope, group_id, "flushed"), "1", px=self._tombstone_ms)
            p.zrem(self._open_key, member)
            p.zrem(self._open_scope_key(scope), member)
            raw, *_ = await p.execute()
        return [json.loads(x) for x in raw]

    # ----------------------------------------------------------------- sweep

    async def sweep_orphans(self) -> int:
        """Drain every group with no live timer. Returns how many were found."""
        now = self._clock_ms()
        self._last_sweep_ms = now
        r = self._r()
        stale = await r.zrangebyscore(self._open_key, "-inf", now - self._orphan_after_ms, withscores=True)
        for member, last_ms in stale:
            scope, _, group_id = member.partition(":")
            age_ms = now - int(last_ms)
            if age_ms <= self._recover_max_age_ms:
                logger.warning(
                    "[COALESCE] group %s was buffered with no live timer (restart?) — "
                    "delivering it now (%d s old)", group_id, age_ms // 1000,
                )
                self._spawn(self._drain_and_flush(scope, group_id, expected_seq=None, recovered=True))
            else:
                items = await self._drain(scope, group_id, None) or []
                logger.warning(
                    "[COALESCE] dropped group %s: %d item(s) buffered %d s ago with no live "
                    "timer (restart?) — too old to deliver", group_id, len(items), age_ms // 1000,
                )
        return len(stale)

    def _maybe_sweep(self) -> None:
        if self._clock_ms() - self._last_sweep_ms >= _SWEEP_INTERVAL_MS:
            self._last_sweep_ms = self._clock_ms()
            self._spawn(self._safe_sweep())

    async def _safe_sweep(self) -> None:
        try:
            await self.sweep_orphans()
        except Exception as e:
            logger.warning("[COALESCE] orphan sweep failed: %s", e)

    # ------------------------------------------------------------- lifecycle

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        """Cancel pending timers. Their groups stay in Redis for the next sweep."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
