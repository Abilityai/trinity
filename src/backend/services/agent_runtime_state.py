"""Single enumeration point for an agent's per-agent Redis runtime state (#1560).

Several Redis structures are keyed by **agent name**, not by container identity.
Nothing about a recreate, rename, delete, or purge invalidates them, so state
written for one incarnation of a name silently becomes the starting state of the
next. The transport circuit breaker is the case that bit us: a dead agent's
``dormant`` verdict fast-failed a fresh, healthy container under the same name
with *"Agent circuit breaker open — agent is unhealthy"* — without the backend
ever contacting it (#1560). The keys carry no TTL, and while a background poller
keeps touching a removed agent (#1561) its ``next_probe_at`` is pushed forward
indefinitely, so the state never decays into something harmless.

This module is the Redis-side twin of ``db/agent_cleanup.py``'s ``AGENT_REFS``
registry: every keyspace keyed by agent name is enumerated here exactly once, and
``tests/unit/test_1560_agent_redis_key_parity.py`` fails CI when a new ``agent:*``
keyspace appears in the backend without being registered or explicitly exempted.

Two entry points, because the safe blast radius depends on whether a container is
running:

``clear_agent_breakers``
    Backend-owned verdict state only — heartbeat markers and both circuit
    breakers. Safe against a *running* container, so the start and create paths
    call this one.

``clear_agent_runtime_state``
    The above **plus** execution-slot bookkeeping. Only safe where the container
    is provably gone or stopped (delete, rename, purge): ``force_clear_slots``
    wholesale-``DEL``s ``agent:slots:{name}``, which on a live agent would drop
    capacity accounting for an in-flight fire-and-forget execution (#1083).

Every underlying helper is fail-open, so neither function raises into the
lifecycle operation it hangs off of. A one-shot clear is therefore never retried
— which is precisely why the start path (idempotent, runs on every start) is the
load-bearing call site rather than delete.

Imports are function-local: it keeps this module importable with only the stdlib
(the parity test relies on that) and avoids an import cycle with
``dispatch_breaker``, which deliberately imports neither ``capacity`` nor ``db``.
"""

from __future__ import annotations

import logging
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


# Keyspaces that are deliberately NOT agent-keyed and therefore outside this
# registry by construction (the `session_inflight:{session_id}` precedent):
#   execution:inflight:{execution_id}  — #2433 live-dispatcher marker, 60s TTL,
#                                        refreshed by agent_call_limiter's
#                                        refresher; liveness, not state.
#   execution:cancel:{execution_id}    — #2433 cross-worker cancel flag for a
#                                        parked dispatch, TTL-bounded.
# Clearing either on an agent lifecycle event would strand (or un-cancel) a
# call that is genuinely in flight; both expire on their own.

# Per-agent Redis keyspaces this module clears. Written exactly as the prefix
# literals appear in the backend source — the parity test greps for them.
CLEARED_KEYSPACES: Tuple[str, ...] = (
    "agent:heartbeat:",  # + seen:/misses: variants — heartbeat_service.clear_heartbeat
    "agent:circuit:",    # + :probe-lock            — agent_client.reset_circuit
    "agent:dispatch:",   # + :probe-lock            — dispatch_breaker.reset_dispatch
    "agent:slots:",      # ZSET of execution ids    — slot_service.force_clear_slots
    "agent:slot:",       # per-execution metadata   — ditto
    # ent#337: canary R-01's zombie-dwell marker HASH ({pid: first_seen}).
    # Carries a TTL of its own, but must ALSO be cleared on the lifecycle
    # events here: a recycled agent name would otherwise inherit its
    # predecessor's dwell and page critical on the fresh container's first
    # transient zombie — #1560 verbatim. Clearing it is always safe: a new
    # container has a new process table, so no dwell can legitimately span
    # the boundary.
    "agent:canary_zombie:",
)

# Deliberately NOT cleared here, each with the reason. Registered so the parity
# test stays green while keeping every omission a conscious decision rather than
# an oversight.
EXEMPT_KEYSPACES: Dict[str, str] = {
    "agent:queue:": (
        "Overflow backlog. Draining it is a business operation with side effects "
        "(marks executions cancelled, writes the operator queue), so it runs via "
        "capacity_manager.cancel_all_overflow on delete only — never on the "
        "create/start paths this module also serves."
    ),
    "agent:data_op:": (
        "Short-lived SETNX export/import lock carrying its own TTL. Deleting it "
        "would let a second data operation run concurrently with an in-flight one."
    ),
    "agent:bind_op:": (
        "Short-lived SETNX repo-binding lock (ent#109) carrying its own TTL, "
        "guarding double-submit on ONE agent. Same reasoning as agent:data_op: "
        "— clearing it mid-operation would admit a second binder. It is also "
        "held ACROSS the container recreate this module's start-path caller "
        "triggers, so clearing there would drop the lock protecting the very "
        "operation that asked for the recreate."
    ),
    "agent:bind_dest:": (
        "Repo-binding DESTINATION lock (ent#109), keyed by sha256 of the "
        "destination repo — NOT by agent name at all, so the name-keyed "
        "lifecycle this module implements cannot address it. It exists "
        "precisely because the collision is between two DIFFERENT agents "
        "targeting one repo; clearing it on any single agent's lifecycle event "
        "would unserialize the other. Registered here so the parity test stays "
        "green and the omission stays a decision."
    ),
    "agent:mcp_key_regen:": (
        "Short-lived SETNX MCP-key rotation lock carrying its own TTL (#1854). "
        "Same shape as agent:data_op: above, and clearing it would be actively "
        "harmful: this lock is deliberately FAIL-CLOSED because two interleaved "
        "rotations end at 'the container holds K1 while the only active row is "
        "K2', permanently 401-ing the heartbeat, the result callback, the pull "
        "worker and the MCP client — with the surviving plaintext unrecoverable. "
        "Deleting it mid-rotation would manufacture exactly that interleave, and "
        "the rotation itself replaces the container, so a lifecycle clear on the "
        "start path could fire against its own in-flight operation."
    ),
}


def clear_agent_breakers(agent_name: str) -> None:
    """Clear the name-keyed verdict state a previous incarnation of the name left.

    Safe while a container is running: every key touched here is backend-owned
    bookkeeping *about* the agent, never the agent's own work. Idempotent, so a
    transient Redis failure is corrected by the next start.

    Each helper is already fail-open; the try/except pairs guarantee one failing
    subsystem cannot stop the others from being cleared.
    """
    try:
        from services import heartbeat_service

        heartbeat_service.clear_heartbeat(agent_name)
    except Exception as e:  # noqa: BLE001 — best-effort, never raise into lifecycle
        logger.warning("clear_agent_breakers: heartbeat clear failed for %s: %s", agent_name, e)

    try:
        from services.agent_client import reset_circuit

        reset_circuit(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("clear_agent_breakers: transport circuit reset failed for %s: %s", agent_name, e)

    try:
        from services.dispatch_breaker import reset_dispatch

        reset_dispatch(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("clear_agent_breakers: dispatch reset failed for %s: %s", agent_name, e)

    # ent#337: canary R-01's zombie-dwell marker. A fresh container has a fresh
    # process table, so no dwell can legitimately span the boundary — and left
    # behind, a recycled agent name inherits the previous incarnation's
    # `first_seen` and pages critical on its first transient zombie (#1560).
    # Imported from the canary package (a leaf) rather than reaching into Redis
    # here, so the key name has exactly one definition.
    try:
        from canary.invariants.r01_no_zombie_claude import REDIS_KEY_PREFIX
        from services.slot_service import get_slot_service

        get_slot_service().redis.delete(f"{REDIS_KEY_PREFIX}{agent_name}")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "clear_agent_breakers: canary zombie-marker clear failed for %s: %s",
            agent_name,
            e,
        )


async def clear_agent_runtime_state(agent_name: str) -> None:
    """``clear_agent_breakers`` plus the execution-slot bookkeeping.

    Call ONLY where the container is provably gone or stopped — agent delete,
    rename, and the retention purge. On a running agent this would wipe the slot
    ZSET out from under an in-flight execution; use ``clear_agent_breakers`` there.
    """
    clear_agent_breakers(agent_name)

    try:
        from services.slot_service import get_slot_service

        await get_slot_service().force_clear_slots(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("clear_agent_runtime_state: slot clear failed for %s: %s", agent_name, e)
