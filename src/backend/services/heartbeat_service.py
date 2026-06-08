"""
Agent heartbeat liveness service (RELIABILITY-004 / #307).

Flips agent-liveness detection from backend polling (30s) to agent push (5s).
Each running agent POSTs a lightweight heartbeat; the backend stores it in
Redis with a 15s TTL. This is an *additive*, faster liveness layer — the
existing 30s docker/network/business monitoring stays authoritative.

The module owns all Redis access (Invariant #1: services hold the logic, not
routers). It reuses ``get_redis_client()`` from ``routers/auth.py`` rather than
opening a second client.

Backward compatibility hinges on the persistent ``seen`` marker:
  * marker absent              -> ``unsupported`` (old-image agent; never marked
                                  dead, watch loop ignores it)
  * marker present + TTL key   -> ``alive``
  * marker present, TTL gone   -> ``stale``

The watch loop (``run_heartbeat_watch_loop``) fires a soft, cooldown-debounced
operator alert (via ``services.monitoring_alerts``) on the alive→stale transition
after N consecutive misses — never a hard ``critical`` — because a missed beat can
mean "agent→backend POST failed" while "backend→agent still works". The N-miss
guard plus soft severity keep a false positive recoverable. It writes no
health-check row; the 30s monitoring loop stays authoritative for aggregate
status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Redis TTL on the heartbeat value. Three missed 5s beats before expiry.
HEARTBEAT_TTL_SECONDS = 15
# Consecutive missed beats before the watch loop downgrades health.
HEARTBEAT_MISS_THRESHOLD = 3
# Watch-loop cadence.
HEARTBEAT_WATCH_INTERVAL_SECONDS = 5
# TTL on the per-agent miss counter — long enough to survive a few cycles,
# short enough to self-clean if the watch loop stops touching an agent.
_MISS_COUNTER_TTL_SECONDS = 60

_KEY_PREFIX = "agent:heartbeat:"


def _hb_key(agent_name: str) -> str:
    return f"{_KEY_PREFIX}{agent_name}"


def _seen_key(agent_name: str) -> str:
    return f"{_KEY_PREFIX}seen:{agent_name}"


def _misses_key(agent_name: str) -> str:
    return f"{_KEY_PREFIX}misses:{agent_name}"


def _get_redis():
    """Return the shared Redis client (or None if unavailable).

    Lazy import keeps services/__init__.py off the import path for unit tests
    and avoids a circular import with routers/auth.py.
    """
    try:
        from routers.auth import get_redis_client
        return get_redis_client()
    except Exception:  # noqa: BLE001 — Redis-None fail-soft is the contract
        logger.debug("heartbeat: get_redis_client failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Authorization (Option B — least-privilege)
# ---------------------------------------------------------------------------
def authorize_heartbeat(validation_result: Optional[Dict], agent_name: str) -> bool:
    """True iff the validated MCP key is the agent's OWN agent-scoped key.

    An agent may only heartbeat *itself*. User-, system-, and null-scoped keys
    are rejected, as is an agent-scoped key whose bound agent_name differs from
    the path. Keeping this a pure predicate (no Redis, no FastAPI) makes the
    security decision unit-testable independent of the HTTP glue.
    """
    if not validation_result:
        return False
    return (
        validation_result.get("scope") == "agent"
        and validation_result.get("agent_name") == agent_name
    )


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------
def record_heartbeat(agent_name: str, payload: Dict) -> bool:
    """Persist one heartbeat. Best-effort; returns False when Redis is down.

    Stamps a server-side receive timestamp (``ts``) used to compute
    ``last_heartbeat_age_s`` on read.
    """
    redis = _get_redis()
    if redis is None:
        return False
    try:
        record = dict(payload)
        record["ts"] = time.time()
        redis.setex(_hb_key(agent_name), HEARTBEAT_TTL_SECONDS, json.dumps(record))
        # Persistent backward-compat hinge — set once, idempotent, no TTL.
        # nx=True so a 12x/min beat is a no-op after the first write.
        redis.set(_seen_key(agent_name), "1", nx=True)
        return True
    except Exception:  # noqa: BLE001 — never raise into the request path
        logger.debug("heartbeat: record failed for %s", agent_name, exc_info=True)
        return False


def clear_heartbeat(agent_name: str) -> None:
    """Delete every heartbeat Redis key for an agent (delete / rename cleanup).

    The ``seen`` marker is set with no TTL (it is the backward-compat hinge),
    so without an explicit delete it would outlive the agent forever — one
    leaked key per agent ever created, and a stale key on the old name after a
    rename. The ``hb`` (15s) and ``misses`` (~60s) keys self-expire but are
    cleared here too so a deleted agent leaves nothing behind. Best-effort:
    Redis-None and Redis errors are swallowed so this never blocks the
    delete/rename it hangs off of.
    """
    redis = _get_redis()
    if redis is None:
        return
    try:
        redis.delete(_hb_key(agent_name), _seen_key(agent_name), _misses_key(agent_name))
    except Exception:  # noqa: BLE001 — best-effort, never raise into the caller
        logger.debug("heartbeat: clear failed for %s", agent_name, exc_info=True)


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------
def read_heartbeat(agent_name: str) -> Optional[Dict]:
    """Return the last heartbeat payload, or None if missing/expired/malformed."""
    redis = _get_redis()
    if redis is None:
        return None
    try:
        raw = redis.get(_hb_key(agent_name))
    except Exception:  # noqa: BLE001
        logger.debug("heartbeat: read failed for %s", agent_name, exc_info=True)
        return None
    return _parse_payload(raw)


def _parse_payload(raw) -> Optional[Dict]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def _compute_status(data: Optional[Dict], seen: bool, now: float) -> Dict:
    """Build the status dict from a parsed payload + seen marker.

    Field names match db_models.AgentHealthSummary so callers can merge directly.
    """
    if not seen:
        # Old-image agent (or never heartbeated). Never marked dead.
        return _unsupported_status()
    if data is None:
        # Seen before, but the TTL key has expired -> stale.
        return {
            "heartbeat_state": "stale",
            "heartbeat_alive": False,
            "last_heartbeat_age_s": None,
            "heartbeat_active_executions": None,
            "heartbeat_memory_mb": None,
        }
    ts = data.get("ts")
    age = max(0.0, now - ts) if isinstance(ts, (int, float)) else None
    return {
        "heartbeat_state": "alive",
        "heartbeat_alive": True,
        "last_heartbeat_age_s": age,
        "heartbeat_active_executions": data.get("active_executions"),
        "heartbeat_memory_mb": data.get("memory_mb"),
    }


def _unsupported_status() -> Dict:
    return {
        "heartbeat_state": "unsupported",
        "heartbeat_alive": None,
        "last_heartbeat_age_s": None,
        "heartbeat_active_executions": None,
        "heartbeat_memory_mb": None,
    }


def heartbeat_status(agent_name: str) -> Dict:
    """Single-agent liveness compute. Redis-None fails open to ``unsupported``."""
    redis = _get_redis()
    if redis is None:
        return _unsupported_status()
    try:
        raw = redis.get(_hb_key(agent_name))
        seen = bool(redis.get(_seen_key(agent_name)))
    except Exception:  # noqa: BLE001
        logger.debug("heartbeat: status failed for %s", agent_name, exc_info=True)
        return _unsupported_status()
    return _compute_status(_parse_payload(raw), seen, time.time())


def heartbeat_status_bulk(agent_names: List[str]) -> Dict[str, Dict]:
    """Fleet-wide liveness in a single Redis round-trip (D4).

    Issues one pipeline fetching ``heartbeat:{n}`` and ``seen:{n}`` for every
    agent, rather than 2N sequential GETs. Used by both the status endpoint and
    the watch loop. Redis-None fails open to all-``unsupported``.
    """
    if not agent_names:
        return {}
    redis = _get_redis()
    if redis is None:
        return {n: _unsupported_status() for n in agent_names}
    try:
        pipe = redis.pipeline()
        for name in agent_names:
            pipe.get(_hb_key(name))
            pipe.get(_seen_key(name))
        results = pipe.execute()
    except Exception:  # noqa: BLE001
        logger.debug("heartbeat: bulk status failed", exc_info=True)
        return {n: _unsupported_status() for n in agent_names}

    now = time.time()
    out: Dict[str, Dict] = {}
    for idx, name in enumerate(agent_names):
        raw = results[idx * 2]
        seen = bool(results[idx * 2 + 1])
        out[name] = _compute_status(_parse_payload(raw), seen, now)
    return out


# ---------------------------------------------------------------------------
# Watch loop — active downgrade on missed heartbeats (D2)
# ---------------------------------------------------------------------------
def _list_agent_names() -> List[str]:
    """Authoritative set of live agents (Docker labels are the source of truth).

    Bounding the loop to live agents keeps the health-row write canary-safe
    (L-03 forbids health rows referencing a missing agent) and reuses the same
    source the monitoring endpoint reads. Lazy import keeps Docker off the
    unit-test import path.
    """
    try:
        from services.docker_service import list_all_agents_fast
        return [a.name for a in list_all_agents_fast()]
    except Exception:  # noqa: BLE001 — a Docker blip must not kill the loop
        logger.debug("heartbeat watch: agent listing failed", exc_info=True)
        return []


async def _emit_heartbeat_alert(
    agent_name: str, kind: str, missed_beats: int = HEARTBEAT_MISS_THRESHOLD
) -> None:
    """Surface a heartbeat transition through the existing alert service.

    ``kind`` is ``"degraded"`` (alive→stale) or ``"recovered"`` (stale→alive after
    a prior downgrade). Reuses ``monitoring_alerts``' per-condition cooldown so a
    flapping agent doesn't storm the operator. Lazy import avoids an import cycle.
    Swallows every exception — an alert failure must never kill the watch loop.
    """
    try:
        from services.monitoring_alerts import get_alert_service
        service = get_alert_service()
        if kind == "degraded":
            await service.alert_heartbeat_lost(agent_name, missed_beats)
        elif kind == "recovered":
            await service.alert_heartbeat_recovered(agent_name)
    except Exception:  # noqa: BLE001 — alert failure must not kill the loop
        logger.warning(
            "heartbeat watch: alert dispatch failed for %s (%s)",
            agent_name, kind, exc_info=True,
        )


def process_watch_tick() -> List[Tuple[str, str]]:
    """One watch-loop iteration. Returns the ``(agent_name, kind)`` transitions to
    alert on — ``"degraded"`` on the alive→stale transition (at the miss
    threshold) and ``"recovered"`` on return after a prior downgrade. Both are
    transition-only, so the operator gets exactly one alert per loss episode.

    Per-tick state (the consecutive-miss counter) lives in Redis, never SQLite.
    Keeping this function sync + return-only makes the miss-counting unit-testable
    without awaiting the alert service; the async loop turns transitions into
    alerts.
    """
    transitions: List[Tuple[str, str]] = []
    redis = _get_redis()
    if redis is None:
        return transitions
    names = _list_agent_names()
    if not names:
        return transitions

    statuses = heartbeat_status_bulk(names)
    for name in names:
        state = statuses.get(name, {}).get("heartbeat_state")

        if state == "stale":
            misses_key = _misses_key(name)
            try:
                # The 3-miss threshold assumes a single backend process
                # (confirmed today — docker-compose runs one worker; see
                # docker-compose.yml:292). With multiple workers each tick
                # INCRs, so the threshold is reached in fewer wall-clock ticks,
                # though the atomic INCR still makes exactly one caller observe
                # ``== THRESHOLD``, so the transition stays single-fire.
                misses = redis.incr(misses_key)
                redis.expire(misses_key, _MISS_COUNTER_TTL_SECONDS)
            except Exception:  # noqa: BLE001
                logger.debug("heartbeat watch: incr failed for %s", name, exc_info=True)
                continue
            # Transition-only: emit the degraded signal exactly once, when the
            # counter first reaches the threshold.
            if misses == HEARTBEAT_MISS_THRESHOLD:
                transitions.append((name, "degraded"))

        elif state == "alive":
            misses_key = _misses_key(name)
            try:
                prior = redis.get(misses_key)
            except Exception:  # noqa: BLE001
                logger.debug("heartbeat watch: get failed for %s", name, exc_info=True)
                continue
            if prior is None:
                continue  # healthy + fresh, never counting -> nothing to do (R2)
            redis.delete(misses_key)
            try:
                prior_int = int(prior)
            except (TypeError, ValueError):
                prior_int = 0
            # Only signal recovery if we had actually downgraded.
            if prior_int >= HEARTBEAT_MISS_THRESHOLD:
                transitions.append((name, "recovered"))

        # state == "unsupported" (or unknown) -> ignore entirely.

    return transitions


async def run_heartbeat_watch_loop(interval_seconds: int = HEARTBEAT_WATCH_INTERVAL_SECONDS) -> None:
    """Background loop: actively alert on agents that stop heartbeating.

    Sleeps first (so just-started agents aren't penalized) and swallows every
    exception so a transient Redis/Docker blip can never kill the loop.
    """
    logger.info("heartbeat watch loop started (interval=%ss)", interval_seconds)
    await asyncio.sleep(interval_seconds)
    while True:
        try:
            for name, kind in process_watch_tick():
                await _emit_heartbeat_alert(name, kind)
        except Exception:  # noqa: BLE001 — loop must never die
            logger.exception("heartbeat watch tick raised unexpectedly")
        await asyncio.sleep(interval_seconds)


def schedule_heartbeat_watch() -> "asyncio.Task":
    """Spawn the watch loop as a background task. Called from the backend
    lifespan, mirroring the other staggered maintenance loops in main.py.
    """
    return asyncio.create_task(run_heartbeat_watch_loop())
