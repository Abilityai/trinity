"""Ephemeral "ghost" agent lifecycle service (trinity-enterprise#69).

Owns the pieces of the ghost lifecycle that don't belong in crud.py:

- the atomic per-owner quota reservation (Redis INCR-with-cap; the
  read-then-insert TOCTOU pattern overshoots under machine-paced concurrent
  fan-out spawns — the feature's core use case), and
- the hard-discard primitive (crash-convergent teardown; see
  ``discard_ephemeral_agent``).

Quota counter semantics: key ``ephemeral:quota:{owner_id}`` (deliberately NOT
``agent:*`` — the #1560 keyspace registry governs name-keyed per-agent state;
this is owner-keyed). Fresh counters (INCR → 1) are reseeded from the DB count
so a Redis restart can't silently reset the quota while ghosts live. Fail-open:
when Redis is unavailable the DB count alone gates (its TOCTOU is accepted only
in that degraded mode).
"""

import logging
import uuid
from typing import Optional

from database import db
from redis_breaker_util import get_breaker_redis

logger = logging.getLogger(__name__)

_DISCARD_LOCK_KEY = "ephemeral:discard:{agent_name}"
_DISCARD_LOCK_TTL_SECONDS = 120

_QUOTA_KEY = "ephemeral:quota:{owner_id}"
# Counter TTL: self-heals drift (a leaked reservation from a crashed create
# expires); comfortably above the 24h default TTL ceiling so a full-lifetime
# ghost never sees its owner counter vanish mid-life. Refreshed on every
# reserve/release.
_QUOTA_KEY_TTL_SECONDS = 3 * 86400


def _quota_key(owner_id: int) -> str:
    return _QUOTA_KEY.format(owner_id=owner_id)


def try_reserve_ephemeral_slot(owner_id: int, cap: int) -> bool:
    """Atomically reserve one ghost slot for ``owner_id`` under ``cap``.

    Returns True when the reservation is held (caller MUST later release it via
    ``release_ephemeral_slot`` on creation failure; a successful discard also
    releases). ``cap <= 0`` means unlimited — no reservation is tracked.
    """
    if cap <= 0:
        return True

    client = get_breaker_redis()
    if client is None:
        # Degraded mode: DB count alone (TOCTOU accepted while Redis is down).
        return db.count_live_ephemeral_agents_for_owner(owner_id) < cap

    key = _quota_key(owner_id)
    try:
        # Fresh-counter reseed BEFORE the INCR, with NX so concurrent
        # creators after a Redis restart can't clobber each other's
        # increments (review M1): exactly one SET NX seeds the DB count,
        # then every INCR stacks on top — no lost updates.
        if not client.exists(key):
            db_count = db.count_live_ephemeral_agents_for_owner(owner_id)
            if db_count > 0:
                client.set(key, db_count, nx=True, ex=_QUOTA_KEY_TTL_SECONDS)
        count = client.incr(key)
        client.expire(key, _QUOTA_KEY_TTL_SECONDS)
        if count > cap:
            client.decr(key)
            return False
        return True
    except Exception as e:  # fail-open to the DB count
        logger.warning(f"[ephemeral] quota reserve via Redis failed: {e}")
        return db.count_live_ephemeral_agents_for_owner(owner_id) < cap


async def discard_ephemeral_agent(agent_name: str, reason: str = "expired") -> bool:
    """Hard-discard an ephemeral agent (trinity-enterprise#69).

    Crash-convergent ordering — every inter-step crash leaves a state the GC
    sweep re-qualifies next cycle:

      0. **Durable intent marker**: ``ephemeral_expires_at = now`` — any crash
         below re-enters via the GC DB pass (no immortal partial state).
      1. Cancel queued/overflow AND CAS-fail every non-terminal row
         (``ghost_discarded``). Terminal-izing FIRST also means a late
         in-flight ``apply_result`` loses its CAS and records no side effects
         (no breaker-key resurrection after the Redis clear). Open
         ``agent_activities`` rows are NOT individually closed — the cascade
         in step 4 deletes them wholesale seconds later.
      2. Remove the container — ``force=True`` (no graceful-stop courtesy for
         ghosts), NotFound tolerated. The writable layer (the ghost's whole
         workspace — ghosts are volume-less) is reclaimed by the removal.
      3. ``clear_agent_runtime_state`` — BEFORE the DB purge, mirroring the
         proven delete-endpoint ordering: the name must never become reusable
         while slots/heartbeat/breaker keys survive (#1560).
      4. Purge the DB rows (cascade over ~40 child tables; KEEP-policy
         ``schedule_executions`` survive and age out via the 90d retention).
      5. Audit ``agent_lifecycle:ephemeral_discard``.

    Serialized per agent by a Redis SETNX lock (hook vs GC vs second worker);
    fail-open when Redis is down (the steps are idempotent, so a rare double
    run is noise, not corruption). Returns True when the DB purge happened in
    this call.
    """
    info = db.get_agent_ephemeral_info(agent_name)
    if not isinstance(info, dict):
        info = None
    if info and not info.get("is_ephemeral"):
        logger.error(
            f"[ephemeral] refusing to discard non-ephemeral agent {agent_name}"
        )
        return False
    owner_id = info.get("owner_id") if info else None

    # --- per-agent discard lock (SETNX + TTL, fail-open) ---
    lock_key = _DISCARD_LOCK_KEY.format(agent_name=agent_name)
    lock_token = uuid.uuid4().hex
    client = get_breaker_redis()
    held = False
    if client is not None:
        try:
            held = bool(
                client.set(lock_key, lock_token, nx=True, ex=_DISCARD_LOCK_TTL_SECONDS)
            )
            if not held:
                logger.info(
                    f"[ephemeral] discard of {agent_name} already in flight — skipping"
                )
                return False
        except Exception:
            client = None  # fail-open: proceed unlocked

    try:
        # 0. Durable intent marker (only meaningful while the row exists).
        if info:
            try:
                db.mark_ephemeral_discard_intent(agent_name)
            except Exception as e:
                logger.warning(f"[ephemeral] intent marker failed for {agent_name}: {e}")

        # 1. Cancel queued/overflow + terminal-ize every non-terminal row.
        try:
            from services.capacity_manager import get_capacity_manager

            await get_capacity_manager().cancel_all_overflow(
                agent_name, reason="ephemeral_discarded"
            )
        except Exception as e:
            logger.warning(f"[ephemeral] overflow cancel failed for {agent_name}: {e}")
        try:
            failed = db.fail_all_nonterminal_for_agent(agent_name, "ghost_discarded")
            if failed:
                logger.info(
                    f"[ephemeral] terminal-ized {failed} in-flight execution(s) for {agent_name}"
                )
        except Exception as e:
            logger.warning(f"[ephemeral] non-terminal fail failed for {agent_name}: {e}")

        # 2. Remove the container (force; absent is fine — half-discarded state).
        try:
            from services.docker_service import get_agent_container
            from services.docker_utils import container_remove

            container = get_agent_container(agent_name)
            if container:
                await container_remove(container, force=True)
        except Exception as e:
            logger.warning(f"[ephemeral] container removal failed for {agent_name}: {e}")

        # 3. Clear per-agent Redis state BEFORE the purge frees the name.
        try:
            from services.agent_runtime_state import clear_agent_runtime_state

            await clear_agent_runtime_state(agent_name)
        except Exception as e:
            logger.warning(f"[ephemeral] Redis state clear failed for {agent_name}: {e}")

        # 4. Hard-purge the DB rows (refuses non-ephemeral rows internally).
        purged = False
        try:
            purged = db.purge_ephemeral_agent_ownership(agent_name)
        except Exception as e:
            logger.warning(f"[ephemeral] DB purge failed for {agent_name}: {e}")
        if purged and owner_id is not None:
            release_ephemeral_slot(owner_id)

        # 5. Audit (best-effort).
        if purged:
            try:
                from services.platform_audit_service import (
                    AuditEventType,
                    platform_audit_service,
                )

                await platform_audit_service.log(
                    event_type=AuditEventType.AGENT_LIFECYCLE,
                    event_action="ephemeral_discard",
                    source="system",
                    target_type="agent",
                    target_id=agent_name,
                    details={"reason": reason},
                )
            except Exception as e:
                logger.warning(f"[ephemeral] audit log failed for {agent_name}: {e}")
            logger.info(f"[ephemeral] discarded ghost agent {agent_name} ({reason})")
        return purged
    finally:
        if held and client is not None:
            try:
                # Release only our own lock (compare-and-delete).
                if client.get(lock_key) == lock_token.encode() or client.get(lock_key) == lock_token:
                    client.delete(lock_key)
            except Exception:
                pass


def release_ephemeral_slot(owner_id: int) -> None:
    """Release one reserved ghost slot (creation rollback or discard).

    Best-effort and floor-clamped at 0 — a double release must never let the
    counter go negative and inflate future capacity.
    """
    client = get_breaker_redis()
    if client is None:
        return
    try:
        new_value = client.decr(_quota_key(owner_id))
        if new_value is not None and int(new_value) < 0:
            client.set(_quota_key(owner_id), 0, ex=_QUOTA_KEY_TTL_SECONDS)
        else:
            client.expire(_quota_key(owner_id), _QUOTA_KEY_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"[ephemeral] quota release via Redis failed: {e}")
