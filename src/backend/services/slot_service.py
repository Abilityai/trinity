"""
Slot Service for Trinity platform.

Implements per-agent parallel execution capacity tracking (CAPACITY-001).
Uses Redis ZSET for slot tracking with automatic TTL-based cleanup.

Key Pattern:
- agent:slots:{name} (ZSET) - Active execution IDs with start timestamps
- agent:slot:{name}:{execution_id} (HASH) - Slot metadata (auto-expires)

Slot Rules:
- Each agent has configurable max_parallel_tasks (1..fleet ceiling, default 3;
  ceiling is the admin-set `max_parallel_tasks_ceiling`, default 10 — #506)
- Parallel /task endpoint respects capacity limits
- Return 429 when at capacity
- Slots auto-expire after 30 minutes (safety net)
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional, Dict, List, Any, Tuple
import redis
import time

from dataclasses import dataclass
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# Configuration
SLOT_TTL_BUFFER = 300  # 5 minute buffer added to agent timeout for slot TTL
DEFAULT_SLOT_TTL_SECONDS = 1200  # 20 minutes - fallback if no agent timeout known


@dataclass
class SlotInfo:
    """Information about a single execution slot."""
    execution_id: str
    slot_number: int
    started_at: str
    message_preview: str
    duration_seconds: int


@dataclass
class SlotState:
    """Current slot state for an agent."""
    agent_name: str
    max_parallel_tasks: int
    active_slots: int
    available_slots: int
    slots: List[SlotInfo]


class SlotService:
    """
    Redis-backed slot tracking for parallel execution capacity.

    Uses ZSET with timestamps as scores for automatic cleanup:
    - ZADD agent:slots:{name} {timestamp} {execution_id}
    - ZCARD for counting active slots
    - ZREMRANGEBYSCORE for cleanup
    """

    def __init__(self, redis_url: Optional[str] = None):
        if redis_url is None:
            from config import REDIS_URL
            redis_url = REDIS_URL
        self.redis = redis.from_url(redis_url, decode_responses=True)
        self.slots_prefix = "agent:slots:"
        self.metadata_prefix = "agent:slot:"
        # BACKLOG-001: callbacks fired when a slot is released, typed as
        # async (agent_name) -> None. BacklogService registers drain_next here
        # on startup. Callbacks run via asyncio.create_task so they never block
        # the release path and exceptions are isolated per callback.
        self._on_release_callbacks: List[Callable[[str], Awaitable[None]]] = []

    def register_on_release(
        self, callback: Callable[[str], Awaitable[None]]
    ) -> None:
        """Register an async callback fired after a slot is released.

        Callbacks receive the agent name. They are scheduled via
        asyncio.create_task so the release call returns immediately and a
        failure in one callback does not affect others. The caller is
        responsible for catching and logging its own exceptions — a wrapper
        here logs anything that escapes.
        """
        self._on_release_callbacks.append(callback)

    def _slots_key(self, agent_name: str) -> str:
        """Redis key for agent's slot set."""
        return f"{self.slots_prefix}{agent_name}"

    def _metadata_key(self, agent_name: str, execution_id: str) -> str:
        """Redis key for slot metadata."""
        return f"{self.metadata_prefix}{agent_name}:{execution_id}"

    async def acquire_slot(
        self,
        agent_name: str,
        execution_id: str,
        max_parallel_tasks: int,
        message_preview: str = "",
        timeout_seconds: int = 900
    ) -> bool:
        """
        Try to acquire a slot for an execution.

        Args:
            agent_name: Name of the agent
            execution_id: Unique execution ID
            max_parallel_tasks: Maximum allowed parallel tasks for this agent
            message_preview: First 100 chars of message for display
            timeout_seconds: Agent's execution timeout (TIMEOUT-001). Slot TTL = timeout + 5min buffer.

        Returns:
            True if slot acquired, False if at capacity
        """
        slots_key = self._slots_key(agent_name)
        now = time.time()

        # TIMEOUT-001: Dynamic slot TTL based on agent timeout + buffer
        slot_ttl = timeout_seconds + SLOT_TTL_BUFFER

        # Clean up expired slots first — use this execution's timeout as the
        # default fallback; per-slot metadata TTL takes precedence for each slot.
        await self._cleanup_stale_slots_for_agent(agent_name, default_slot_ttl=slot_ttl)

        # Check current count
        current_count = self.redis.zcard(slots_key)
        if current_count >= max_parallel_tasks:
            logger.info(
                f"[Slots] Agent '{agent_name}' at capacity ({current_count}/{max_parallel_tasks}), "
                f"rejecting execution {execution_id}"
            )
            return False

        # Add slot (ZADD with timestamp score)
        self.redis.zadd(slots_key, {execution_id: now})

        # Store metadata with dynamic TTL
        metadata_key = self._metadata_key(agent_name, execution_id)
        slot_number = current_count + 1  # Assign next available slot number
        self.redis.hset(metadata_key, mapping={
            "started_at": utc_now_iso(),
            "message_preview": message_preview[:100] if message_preview else "",
            "slot_number": str(slot_number),
            "timeout_seconds": str(timeout_seconds)
        })
        self.redis.expire(metadata_key, slot_ttl)

        logger.info(
            f"[Slots] Agent '{agent_name}' acquired slot {slot_number}/{max_parallel_tasks} "
            f"for execution {execution_id} (TTL={slot_ttl}s)"
        )
        return True

    async def release_slot(self, agent_name: str, execution_id: str) -> None:
        """
        Release a slot when execution completes.

        Args:
            agent_name: Name of the agent
            execution_id: Execution ID to release
        """
        slots_key = self._slots_key(agent_name)
        metadata_key = self._metadata_key(agent_name, execution_id)

        # Remove from ZSET
        removed = self.redis.zrem(slots_key, execution_id)

        # Delete metadata
        self.redis.delete(metadata_key)

        if removed:
            remaining = self.redis.zcard(slots_key)
            logger.info(
                f"[Slots] Agent '{agent_name}' released slot for execution {execution_id}, "
                f"{remaining} slots still active"
            )

        # BACKLOG-001: Notify registered drain listeners. Fire-and-forget via
        # create_task so release_slot stays cheap. Wrap in a guard so a crash
        # in one callback doesn't cancel the others.
        #
        # #1083 (Codex #12): gate the drain on `removed`. A release for an
        # execution_id that no longer holds the slot (a replayed/late
        # fire-and-forget callback, a double-release) frees NO capacity, so
        # firing the drain would admit a backlog row past max_parallel_tasks.
        # Every legitimate drain trigger removes a present member (the backlog
        # sentinel swap and capacity.release both release an id they just held),
        # so this only suppresses the spurious replay drain.
        if removed and self._on_release_callbacks:
            for cb in self._on_release_callbacks:
                try:
                    asyncio.create_task(self._safe_invoke(cb, agent_name))
                except RuntimeError:
                    # No running loop (e.g. called from sync cleanup context).
                    # Skipping the callback is safe — the 60s maintenance task
                    # will drain orphans on the next tick.
                    logger.debug(
                        "[Slots] release callback skipped: no running event loop"
                    )

    @staticmethod
    async def _safe_invoke(
        cb: Callable[[str], Awaitable[None]], agent_name: str
    ) -> None:
        """Run a release callback with its exceptions isolated."""
        try:
            await cb(agent_name)
        except Exception as e:  # pragma: no cover - defensive
            logger.error(
                f"[Slots] release callback failed for agent '{agent_name}': {e}",
                exc_info=True,
            )

    async def get_slot_state(self, agent_name: str, max_parallel_tasks: int) -> SlotState:
        """
        Get current slot usage for an agent.

        Args:
            agent_name: Name of the agent
            max_parallel_tasks: Maximum allowed parallel tasks

        Returns:
            SlotState with current usage information
        """
        slots_key = self._slots_key(agent_name)
        now = time.time()

        # Get all active execution IDs with scores (timestamps)
        slot_entries = self.redis.zrangebyscore(
            slots_key, "-inf", "+inf", withscores=True
        )

        slots = []
        for execution_id, start_timestamp in slot_entries:
            metadata_key = self._metadata_key(agent_name, execution_id)
            metadata = self.redis.hgetall(metadata_key)

            duration_seconds = int(now - start_timestamp)
            slot_number = int(metadata.get("slot_number", 0))

            slots.append(SlotInfo(
                execution_id=execution_id,
                slot_number=slot_number,
                started_at=metadata.get("started_at", ""),
                message_preview=metadata.get("message_preview", ""),
                duration_seconds=duration_seconds
            ))

        # Sort by slot number
        slots.sort(key=lambda s: s.slot_number)

        active_count = len(slots)
        return SlotState(
            agent_name=agent_name,
            max_parallel_tasks=max_parallel_tasks,
            active_slots=active_count,
            available_slots=max(0, max_parallel_tasks - active_count),
            slots=slots
        )

    async def get_all_slot_states(self, agent_capacities: Dict[str, int]) -> Dict[str, Dict[str, Any]]:
        """
        Get slot states for multiple agents efficiently.

        Args:
            agent_capacities: Dict mapping agent_name to max_parallel_tasks

        Returns:
            Dict mapping agent_name to {"max": N, "active": M}
        """
        if not agent_capacities:
            return {}

        # #1265: one pipelined round-trip instead of an N+1 ZCARD per agent
        # (Dashboard polls this for the whole fleet every few seconds).
        agent_names = list(agent_capacities.keys())
        try:
            pipe = self.redis.pipeline(transaction=False)
            for agent_name in agent_names:
                pipe.zcard(self._slots_key(agent_name))
            counts = pipe.execute()
        except Exception:  # noqa: BLE001 — fail open to zero active (slot meter is advisory here)
            logger.debug("slots: bulk zcard pipeline failed", exc_info=True)
            counts = [0] * len(agent_names)

        return {
            agent_name: {"max": agent_capacities[agent_name], "active": counts[idx] or 0}
            for idx, agent_name in enumerate(agent_names)
        }

    async def _cleanup_stale_slots_for_agent(
        self, agent_name: str, default_slot_ttl: int = None
    ) -> List[str]:
        """Remove slots whose per-slot TTL has elapsed for a single agent.

        Each slot's effective TTL is read from its stored metadata
        (``timeout_seconds + SLOT_TTL_BUFFER``). Falls back to
        ``default_slot_ttl`` when metadata is absent (e.g. the HASH key
        expired after the ZSET entry was written but before cleanup ran).

        This replaces the former ZREMRANGEBYSCORE approach that used a
        single per-agent cutoff computed from ``agent_ownership.
        execution_timeout_seconds``. That approach incorrectly reclaimed
        slots acquired with a per-schedule ``timeout_seconds`` longer than
        the agent default — killing live executions at ~65 min when the
        configured limit was 120 min (#869).

        Args:
            agent_name: Name of the agent.
            default_slot_ttl: Fallback TTL (seconds) when slot metadata is
                absent. Defaults to DEFAULT_SLOT_TTL_SECONDS.

        Returns:
            List of execution IDs whose slots were reclaimed.
        """
        slots_key = self._slots_key(agent_name)
        fallback_ttl = default_slot_ttl if default_slot_ttl is not None else DEFAULT_SLOT_TTL_SECONDS
        now = time.time()

        slot_entries = self.redis.zrange(slots_key, 0, -1, withscores=True)
        if not slot_entries:
            return []

        stale = []
        for execution_id, score in slot_entries:
            age = now - float(score)
            # Read the timeout stored at acquire time so each slot uses its
            # own deadline, not the agent's current default.
            metadata_key = self._metadata_key(agent_name, execution_id)
            stored_timeout = self.redis.hget(metadata_key, "timeout_seconds")
            if stored_timeout:
                try:
                    effective_ttl = int(stored_timeout) + SLOT_TTL_BUFFER
                except (ValueError, TypeError):
                    effective_ttl = fallback_ttl
            else:
                effective_ttl = fallback_ttl

            if age > effective_ttl:
                stale.append(execution_id)

        if stale:
            self.redis.zrem(slots_key, *stale)
            for execution_id in stale:
                self.redis.delete(self._metadata_key(agent_name, execution_id))
            logger.warning(
                f"[Slots] Cleaned up {len(stale)} stale slots for agent '{agent_name}'"
            )

        return stale

    async def cleanup_stale_slots(
        self, agent_timeouts: Dict[str, int] = None
    ) -> Dict[str, List[str]]:
        """
        Remove slots older than TTL for all agents.

        Args:
            agent_timeouts: Optional dict mapping agent_name to execution_timeout_seconds.
                When provided, uses per-agent TTL (timeout + buffer) instead of the
                fixed default. Fixes #226: prevents premature slot reclamation for
                agents with custom timeouts.

        Returns:
            Dict mapping agent_name to list of reclaimed execution IDs.
            Empty dict if nothing was cleaned.
        """
        reclaimed: Dict[str, List[str]] = {}

        # Find all agent slot keys
        pattern = f"{self.slots_prefix}*"
        cursor = 0
        while True:
            cursor, keys = self.redis.scan(cursor, match=pattern, count=100)
            for key in keys:
                agent_name = key.replace(self.slots_prefix, "")
                # Per-slot TTL from metadata takes precedence; this is only the
                # fallback for slots whose metadata HASH has already expired.
                if agent_timeouts and agent_name in agent_timeouts:
                    default_ttl = agent_timeouts[agent_name] + SLOT_TTL_BUFFER
                else:
                    default_ttl = DEFAULT_SLOT_TTL_SECONDS
                stale_ids = await self._cleanup_stale_slots_for_agent(
                    agent_name, default_slot_ttl=default_ttl
                )
                if stale_ids:
                    reclaimed[agent_name] = stale_ids
            if cursor == 0:
                break

        total = sum(len(ids) for ids in reclaimed.values())
        if total > 0:
            logger.info(f"[Slots] Cleanup removed {total} stale slots total")

        return reclaimed

    async def get_active_count(self, agent_name: str) -> int:
        """Get count of active slots for an agent."""
        slots_key = self._slots_key(agent_name)
        return self.redis.zcard(slots_key)

    async def is_at_capacity(self, agent_name: str, max_parallel_tasks: int) -> bool:
        """Check if agent is at capacity."""
        active = await self.get_active_count(agent_name)
        return active >= max_parallel_tasks

    async def force_clear_slots(self, agent_name: str) -> int:
        """
        Force clear all slots for an agent (emergency use).

        Returns:
            Number of slots cleared
        """
        slots_key = self._slots_key(agent_name)

        # Get all execution IDs first
        entries = self.redis.zrange(slots_key, 0, -1)
        count = len(entries)

        if count > 0:
            # Delete slot set
            self.redis.delete(slots_key)

            # Delete all metadata
            for execution_id in entries:
                metadata_key = self._metadata_key(agent_name, execution_id)
                self.redis.delete(metadata_key)

            logger.warning(f"[Slots] Force cleared {count} slots for agent '{agent_name}'")

        return count


# Global instance
_slot_service: Optional[SlotService] = None


def get_slot_service() -> SlotService:
    """Get the global slot service instance."""
    global _slot_service
    if _slot_service is None:
        _slot_service = SlotService()  # resolves REDIS_URL via config
    return _slot_service
