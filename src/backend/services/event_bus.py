"""
Redis Streams event bus for WebSocket delivery (RELIABILITY-003 / #306).

Replaces the in-process ``ConnectionManager.broadcast()`` + ``except: pass`` pattern
with a durable event log. Provides reconnect replay via per-client ``last-event-id``
and graceful degradation when Redis is unavailable.

Design
------
* Producers call ``event_bus.publish(event, scope=...)`` which XADDs to
  ``trinity:events`` with an approximate MAXLEN trim.
* A single ``StreamDispatcher`` coroutine per backend process reads the stream
  with ``XREAD BLOCK`` and fans out in-memory to registered WebSocket clients.
* Each client has a bounded ``asyncio.Queue(256)``. If the queue is full the
  dispatcher drops and schedules a ``resync_required`` message — slow clients
  never block others.
* After 3 consecutive send failures a client is evicted and its socket is closed.
* Reconnect flow: client supplies ``?last-event-id=<id>``; the dispatcher runs
  a one-shot ``XRANGE`` catchup, then joins the live fan-out. If the requested
  id is older than the stream's earliest entry (trimmed) the client receives
  ``{"type": "resync_required"}`` and must fetch current state via REST.

Scope discipline (#306)
-----------------------
This module is the WebSocket delivery layer. Agent-push completion, heartbeat
push, and capacity consolidation (#307 / #428 / #429) will reuse the same
stream primitive in later sprints — see
``docs/archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

try:
    import redis.asyncio as aioredis
    from redis.exceptions import ResponseError as RedisResponseError
except Exception:  # pragma: no cover — redis is a hard dependency
    aioredis = None
    RedisResponseError = Exception

from config import REDIS_URL

logger = logging.getLogger(__name__)

STREAM_KEY = "trinity:events"
STREAM_MAXLEN = int(os.getenv("REDIS_STREAM_MAXLEN", "10000"))
CLIENT_QUEUE_MAXSIZE = 256
EVICT_AFTER_FAILURES = 3
REPLAY_GAP_LIMIT = 5000  # reject replays larger than this; force resync
EID_PATTERN = re.compile(r"^\d+-\d+$")

SCOPE_ALL = "all"
SCOPE_SCOPED = "scoped"

# In-memory fallback used when Redis is unavailable at publish time.
# Capped to avoid unbounded growth in degraded mode.
_FALLBACK_BUFFER_MAX = 1024


def _serialize(event: Any) -> Dict[str, str]:
    """Convert an event (dict or already-json-encoded str) into the flat
    string map Redis XADD expects."""
    if isinstance(event, str):
        try:
            parsed = json.loads(event)
        except (json.JSONDecodeError, TypeError):
            parsed = {"raw": event}
        return {"data": json.dumps(parsed)}
    return {"data": json.dumps(event)}


def _deserialize(fields: Dict[str, str]) -> Dict[str, Any]:
    raw = fields.get("data") if isinstance(fields, dict) else None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"raw": raw}


@dataclass
class _ClientSlot:
    """Per-WebSocket state held by the dispatcher."""

    ws: Any
    scope: str                       # SCOPE_ALL or SCOPE_SCOPED
    send_func: Callable              # async fn(dict) that serializes appropriately
    is_admin: bool = False
    accessible_agents: Set[str] = field(default_factory=set)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE_MAXSIZE))
    last_delivered_id: str = "0-0"
    failure_count: int = 0
    consumer_task: Optional[asyncio.Task] = None
    resync_pending: bool = False


def _event_is_visible(slot: _ClientSlot, event_scope: str, agent_name: Optional[str]) -> bool:
    """Apply the scope/filter contract that replaces FilteredWebSocketManager.

    ``scope=SCOPE_ALL`` events reach every ``/ws`` client. ``scope=SCOPE_SCOPED``
    events reach admin /ws/events listeners and non-admins with access to the
    named agent. Matches the legacy ``broadcast_filtered`` semantics."""
    if slot.scope == SCOPE_ALL:
        return event_scope == SCOPE_ALL
    # slot.scope == SCOPE_SCOPED
    if event_scope != SCOPE_SCOPED:
        return False
    if slot.is_admin:
        return True
    if not agent_name:
        return False
    return agent_name in slot.accessible_agents


class EventBus:
    """Publisher side of the stream.

    One instance per backend process. ``publish`` is fire-and-forget: it enqueues
    to an internal ``asyncio.Queue`` and a background writer drains to Redis, so
    callers never block on Redis latency and a Redis flap doesn't stall broadcast
    sites like chat/activity."""

    def __init__(self) -> None:
        self._outbound: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._fallback: List[Dict[str, Any]] = []
        self._writer_task: Optional[asyncio.Task] = None
        self._redis: Optional["aioredis.Redis"] = None
        self._ready = False
        # Soak-period counters (#306). Monotonic, reset on process restart.
        self._started_at: float = time.time()
        self.events_published: int = 0
        self.publish_failures: int = 0
        self.outbound_overflow: int = 0

    async def start(self) -> None:
        if self._writer_task is not None:
            return
        self._writer_task = asyncio.create_task(self._writer_loop(), name="event_bus_writer")

    async def stop(self, drain_timeout: float = 2.0) -> None:
        """Drain pending publishes, then close Redis. Called from lifespan shutdown."""
        if self._writer_task:
            try:
                await asyncio.wait_for(self._outbound.join(), timeout=drain_timeout)
            except asyncio.TimeoutError:
                logger.warning("event_bus: drain timeout after %.1fs; %d events may be lost",
                               drain_timeout, self._outbound.qsize())
            self._writer_task.cancel()
            try:
                await self._writer_task
            except (asyncio.CancelledError, Exception):
                pass
            self._writer_task = None
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

    async def publish(
        self,
        event: Any,
        scope: str = SCOPE_ALL,
        agent_name: Optional[str] = None,
    ) -> None:
        """Publish an event. Non-blocking; drops oldest on overflow.

        ``event`` may be a dict or a JSON-encoded string (for the legacy
        ``ConnectionManager.broadcast(str)`` call sites). ``scope`` and
        ``agent_name`` are stored alongside the event payload so consumers can
        filter without inspecting the payload."""
        if isinstance(event, str):
            try:
                payload = json.loads(event)
            except (json.JSONDecodeError, TypeError):
                payload = {"raw": event}
        else:
            payload = event

        if not isinstance(payload, dict):
            payload = {"value": payload}

        # Infer agent_name from payload if not provided — matches the legacy
        # FilteredWebSocketManager heuristic.
        if scope == SCOPE_SCOPED and agent_name is None:
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            agent_name = (
                payload.get("agent_name")
                or payload.get("agent")
                or payload.get("name")
                or payload.get("source_agent")
                or details.get("source_agent")
                or details.get("target_agent")
            )

        envelope = {"payload": payload, "scope": scope, "agent_name": agent_name or ""}

        try:
            self._outbound.put_nowait(envelope)
        except asyncio.QueueFull:
            # Evict oldest to make room (favour liveness over completeness under storm).
            try:
                _ = self._outbound.get_nowait()
                self._outbound.task_done()
            except asyncio.QueueEmpty:
                pass
            try:
                self._outbound.put_nowait(envelope)
            except asyncio.QueueFull:
                self.outbound_overflow += 1
                logger.warning("event_bus: outbound queue full, dropping event")

    async def _writer_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                envelope = await self._outbound.get()
                try:
                    await self._xadd(envelope)
                    backoff = 1.0  # reset on success
                finally:
                    self._outbound.task_done()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("event_bus: writer error: %s; backoff=%.1fs", e, backoff)
                await asyncio.sleep(min(backoff, 30.0))
                backoff = min(backoff * 2.0, 30.0)

    async def _get_redis(self) -> Optional["aioredis.Redis"]:
        if self._redis is not None:
            return self._redis
        if aioredis is None:
            return None
        try:
            self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
            await self._redis.ping()
            self._ready = True
            return self._redis
        except Exception as e:
            logger.warning("event_bus: Redis unavailable (%s); publish will degrade", e)
            self._redis = None
            return None

    def stats(self) -> Dict[str, Any]:
        """Snapshot of publisher-side counters for the #306 soak dashboard."""
        return {
            "uptime_seconds": int(time.time() - self._started_at),
            "events_published": self.events_published,
            "publish_failures": self.publish_failures,
            "outbound_overflow": self.outbound_overflow,
            "outbound_queue_depth": self._outbound.qsize(),
            "fallback_buffer_depth": len(self._fallback),
            "redis_ready": self._ready,
        }

    async def _xadd(self, envelope: Dict[str, Any]) -> None:
        redis = await self._get_redis()
        if redis is None:
            # Keep a small rolling buffer so very early events survive a brief
            # Redis outage; drop silently when saturated.
            if len(self._fallback) < _FALLBACK_BUFFER_MAX:
                self._fallback.append(envelope)
            return

        fields = {
            "payload": json.dumps(envelope["payload"]),
            "scope": envelope["scope"],
            "agent_name": envelope["agent_name"] or "",
        }
        try:
            await redis.xadd(STREAM_KEY, fields, maxlen=STREAM_MAXLEN, approximate=True)
            self.events_published += 1
        except Exception as e:
            # Force reconnect on next call.
            self.publish_failures += 1
            logger.warning("event_bus: XADD failed: %s", e)
            try:
                await redis.aclose()
            except Exception:
                pass
            self._redis = None
            raise


class StreamDispatcher:
    """Consumer side of the stream.

    Maintains a ``clients`` map and runs a single ``XREAD BLOCK`` loop per
    backend process. Events are put_nowait'd into each client's bounded queue;
    each client has a consumer task that dequeues and sends.
    """

    def __init__(self) -> None:
        self._clients: Dict[str, _ClientSlot] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._redis: Optional["aioredis.Redis"] = None
        self._last_stream_id: str = "$"   # start at live tip on boot
        self._lock = asyncio.Lock()
        self._shutting_down = False
        # Soak-period counters (#306). Monotonic, reset on process restart.
        self._started_at: float = time.time()
        self.events_delivered: int = 0
        self.send_failures: int = 0
        self.drops_queue_full: int = 0
        self.clients_evicted: int = 0
        self.resyncs_sent: int = 0

    async def start(self) -> None:
        if self._reader_task is not None:
            return
        self._reader_task = asyncio.create_task(self._supervised_reader(), name="stream_dispatcher")

    async def stop(self) -> None:
        self._shutting_down = True
        for slot in list(self._clients.values()):
            if slot.consumer_task:
                slot.consumer_task.cancel()
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

    async def register(
        self,
        ws: Any,
        scope: str,
        send_func: Callable,
        *,
        is_admin: bool = False,
        accessible_agents: Optional[List[str]] = None,
        last_event_id: Optional[str] = None,
    ) -> str:
        """Register a WebSocket client. Returns the client_id used for lookups.

        If ``last_event_id`` is provided and valid, a catch-up XRANGE is queued
        before the live fan-out begins. On failure (malformed id, huge gap, or
        trim race) a ``resync_required`` event is queued and the client resumes
        from the live tip."""
        slot = _ClientSlot(
            ws=ws,
            scope=scope,
            send_func=send_func,
            is_admin=is_admin,
            accessible_agents=set(accessible_agents or []),
        )
        client_id = str(uuid.uuid4())
        slot.consumer_task = asyncio.create_task(
            self._client_consumer(client_id, slot), name=f"ws_consumer_{client_id[:8]}"
        )

        # Snapshot the reader's position BEFORE adding the client to _clients
        # so catchup's upper bound can't overlap with live fan-out. Without this
        # snapshot, ``_catchup`` calling ``XRANGE max="+"`` concurrently with
        # ``_fanout`` could double-deliver events (see #306 review C1).
        catchup_max = self._last_stream_id if self._last_stream_id != "$" else None

        self._clients[client_id] = slot

        if last_event_id:
            asyncio.create_task(
                self._catchup(client_id, slot, last_event_id, catchup_max)
            )

        return client_id

    def unregister(self, client_id: str) -> None:
        slot = self._clients.pop(client_id, None)
        if slot and slot.consumer_task:
            slot.consumer_task.cancel()

    def update_accessible_agents(self, client_id: str, accessible_agents: List[str]) -> None:
        slot = self._clients.get(client_id)
        if slot:
            slot.accessible_agents = set(accessible_agents)

    def client_count(self) -> int:
        return len(self._clients)

    # ------------------------------------------------------------------ reader

    async def _supervised_reader(self) -> None:
        """Restart the XREAD loop with exponential backoff on unexpected errors."""
        backoff = 1.0
        while not self._shutting_down:
            try:
                await self._reader_loop()
                # Normal exit shouldn't happen; treat as error.
                backoff = min(backoff * 2.0, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("stream_dispatcher: reader crashed: %s; restart in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    async def _reader_loop(self) -> None:
        redis = await self._get_redis()
        if redis is None:
            await asyncio.sleep(5.0)
            return

        while not self._shutting_down:
            try:
                response = await redis.xread(
                    {STREAM_KEY: self._last_stream_id}, block=5000, count=100
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("stream_dispatcher: xread error: %s", e)
                try:
                    await redis.aclose()
                except Exception:
                    pass
                self._redis = None
                raise  # let supervisor back off

            if not response:
                continue

            for _stream_name, entries in response:
                for entry_id, fields in entries:
                    self._last_stream_id = entry_id
                    await self._fanout(entry_id, fields)

    async def _fanout(self, entry_id: str, fields: Dict[str, str]) -> None:
        payload = _deserialize({"data": fields.get("payload", "")})
        scope = fields.get("scope", SCOPE_ALL)
        agent_name = fields.get("agent_name") or None

        # Inject stream id into the payload so frontend reconnect logic can
        # persist it. Additive — existing handlers ignore unknown fields.
        payload = dict(payload)
        payload["_eid"] = entry_id

        for client_id, slot in list(self._clients.items()):
            if not _event_is_visible(slot, scope, agent_name):
                continue
            try:
                slot.queue.put_nowait((entry_id, payload))
            except asyncio.QueueFull:
                # Slow client — drop and require resync.
                self.drops_queue_full += 1
                if not slot.resync_pending:
                    slot.resync_pending = True
                    logger.warning("stream_dispatcher: client %s queue full, marking resync",
                                   client_id[:8])
                    try:
                        slot.queue.put_nowait((entry_id, {"type": "resync_required",
                                                          "reason": "slow_consumer",
                                                          "_eid": entry_id}))
                        self.resyncs_sent += 1
                    except asyncio.QueueFull:
                        pass  # will be evicted by consumer on next failure

    # ------------------------------------------------------------------ client

    async def _client_consumer(self, client_id: str, slot: _ClientSlot) -> None:
        """Drain this client's queue, handle send failures and eviction."""
        try:
            while True:
                entry_id, payload = await slot.queue.get()
                try:
                    await slot.send_func(payload)
                    slot.failure_count = 0
                    self.events_delivered += 1
                    # Monotonic guard (#306 review C1): even though catchup's
                    # range is capped to avoid overlap with live fan-out, keep
                    # this defensive check so any future ordering bug can't
                    # regress the client's cursor. Never advances backwards.
                    if payload.get("type") != "resync_required" and _id_greater_than(
                        entry_id, slot.last_delivered_id
                    ):
                        slot.last_delivered_id = entry_id
                    if payload.get("type") == "resync_required":
                        slot.resync_pending = False
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    slot.failure_count += 1
                    self.send_failures += 1
                    logger.info(
                        "stream_dispatcher: send failed for %s (%d/%d): %s",
                        client_id[:8], slot.failure_count, EVICT_AFTER_FAILURES, e,
                    )
                    if slot.failure_count >= EVICT_AFTER_FAILURES:
                        self.clients_evicted += 1
                        logger.warning("stream_dispatcher: evicting client %s after %d failures",
                                       client_id[:8], EVICT_AFTER_FAILURES)
                        try:
                            close = getattr(slot.ws, "close", None)
                            if close:
                                await close(code=1011, reason="broadcast failure eviction")
                        except Exception:
                            pass
                        self._clients.pop(client_id, None)
                        return
        except asyncio.CancelledError:
            pass

    async def _catchup(
        self,
        client_id: str,
        slot: _ClientSlot,
        last_event_id: str,
        catchup_max: Optional[str] = None,
    ) -> None:
        """One-shot replay from (last_event_id, catchup_max].

        ``catchup_max`` is the dispatcher's ``_last_stream_id`` at the moment
        the client was registered. Capping XRANGE at this point prevents
        overlap with live fan-out (see #306 review C1). Falls back to ``"+"``
        only when the reader hasn't read any events yet — in that case fan-out
        itself has delivered nothing, so no overlap is possible."""
        if not EID_PATTERN.match(last_event_id):
            await self._queue_resync(slot, "invalid_last_event_id")
            return

        redis = await self._get_redis()
        if redis is None:
            await self._queue_resync(slot, "redis_unavailable")
            return

        # Exclusive-start form ``(<id>`` avoids re-delivering last_event_id.
        # See https://redis.io/commands/xrange/
        start = f"({last_event_id}"
        end = catchup_max if catchup_max else "+"
        try:
            entries = await redis.xrange(STREAM_KEY, min=start, max=end, count=REPLAY_GAP_LIMIT + 1)
        except Exception as e:
            logger.warning("stream_dispatcher: xrange failed: %s", e)
            await self._queue_resync(slot, "replay_error")
            return

        if len(entries) > REPLAY_GAP_LIMIT:
            await self._queue_resync(slot, "gap_too_large")
            return

        # Check for trim race: if the stream's earliest id is > last_event_id the
        # cursor is stale and events were trimmed.
        if entries:
            # entries sorted ascending; the first id > last_event_id means some
            # entries between last_event_id and the first replayed one may have
            # been trimmed. Detect by comparing against the stream's oldest id.
            try:
                oldest = await redis.xrange(STREAM_KEY, min="-", max="+", count=1)
                if oldest:
                    oldest_id = oldest[0][0]
                    # last_event_id must be >= the oldest-minus-one; if the oldest
                    # id is numerically greater than last_event_id, history has
                    # been trimmed past the cursor.
                    if _id_greater_than(oldest_id, last_event_id):
                        await self._queue_resync(slot, "trimmed")
                        return
            except Exception:
                pass

        for entry_id, fields in entries:
            payload = _deserialize({"data": fields.get("payload", "")})
            scope = fields.get("scope", SCOPE_ALL)
            agent_name = fields.get("agent_name") or None
            if not _event_is_visible(slot, scope, agent_name):
                continue
            payload = dict(payload)
            payload["_eid"] = entry_id
            try:
                slot.queue.put_nowait((entry_id, payload))
            except asyncio.QueueFull:
                await self._queue_resync(slot, "queue_overflow_during_catchup")
                return

    async def _queue_resync(self, slot: _ClientSlot, reason: str) -> None:
        slot.resync_pending = True
        try:
            slot.queue.put_nowait(("0-0", {"type": "resync_required", "reason": reason}))
            self.resyncs_sent += 1
        except asyncio.QueueFull:
            pass

    async def _get_redis(self) -> Optional["aioredis.Redis"]:
        if self._redis is not None:
            return self._redis
        if aioredis is None:
            return None
        try:
            self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
            await self._redis.ping()
            return self._redis
        except Exception as e:
            logger.warning("stream_dispatcher: Redis unavailable (%s)", e)
            self._redis = None
            return None

    def stats(self) -> Dict[str, Any]:
        """Snapshot of consumer-side counters for the #306 soak dashboard."""
        return {
            "uptime_seconds": int(time.time() - self._started_at),
            "client_count": len(self._clients),
            "events_delivered": self.events_delivered,
            "send_failures": self.send_failures,
            "drops_queue_full": self.drops_queue_full,
            "clients_evicted": self.clients_evicted,
            "resyncs_sent": self.resyncs_sent,
            "last_stream_id": self._last_stream_id,
        }


def _id_greater_than(a: str, b: str) -> bool:
    """Compare two Redis stream ids in ``<ms>-<seq>`` form."""
    try:
        a_ms, a_seq = (int(x) for x in a.split("-"))
        b_ms, b_seq = (int(x) for x in b.split("-"))
    except (ValueError, AttributeError):
        return False
    return (a_ms, a_seq) > (b_ms, b_seq)


def validate_last_event_id(raw: Optional[str]) -> Optional[str]:
    """Return ``raw`` if it is a well-formed Redis stream id, else ``None``."""
    if not raw:
        return None
    if not EID_PATTERN.match(raw):
        return None
    return raw


# Module-level singletons used by main.py and 33 legacy broadcast call sites.
event_bus = EventBus()
stream_dispatcher = StreamDispatcher()
