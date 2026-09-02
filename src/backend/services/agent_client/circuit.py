"""The transport circuit breaker (#631/#526) — Lua state machine, dormant alerting, and the admin read/reset surface.

Carved out of the 1,294-line `services/agent_client.py` (#1028); the package
`__init__` re-exports the public surface unchanged. Cross-module calls go
through the sibling module object so a patch on the owning module reaches
every caller (the git_service rule).
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional, Any, Dict, Tuple

import httpx
import redis as _redis
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

# Shared Redis plumbing (#526 D4). Top-level module (NOT services/) so this
# import stays clean when agent_client is loaded standalone in its unit +
# integration suites — see redis_breaker_util's module docstring.
from redis_breaker_util import (
    ScriptCache,
    decode_pair,
    get_breaker_redis,
    reset_breaker_redis_client,
)
from services.agent_auth import merge_auth_headers
from services.model_context import DEFAULT_CONTEXT_WINDOW

logger = logging.getLogger(__name__)


# ============================================================================
# Circuit Breaker (per-agent, Redis-backed for cross-worker coordination, #631)
# ============================================================================
#
# Why Redis: backend runs with N uvicorn workers. Per-process state means
# each worker probed independently, doubled DB writes, doubled log noise.
# Single Redis hash + Lua scripts give atomic state machine transitions and
# the "only one worker probes at a time" semantics for free.
#
# Redis layout (per agent):
#     agent:circuit:{name}             HASH  state, failures, last_failure_ts,
#                                            next_probe_at, probe_count_since_open
#     agent:circuit:{name}:probe-lock  STRING (NX EX 10) — short-lived probe permit
#
# State machine:
#     closed                    — happy path; every request goes through.
#     open                      — failure_threshold hit; only one half-open probe
#                                 per cooldown window (per cluster, not per worker).
#     dormant                   — too many consecutive failed probes; stops probing
#                                 entirely until the agent container restarts or an
#                                 operator manually triggers a health check.



logger = logging.getLogger(__name__)

_CIRCUIT_HASH_PREFIX = "agent:circuit:"

_CIRCUIT_PROBE_LOCK_SUFFIX = ":probe-lock"

CIRCUIT_FAILURE_THRESHOLD = 3

CIRCUIT_BASE_COOLDOWN_SECONDS = 30.0

CIRCUIT_MAX_COOLDOWN_SECONDS = 300.0

CIRCUIT_PROBE_LOCK_TTL_SECONDS = 10

CIRCUIT_DORMANT_AFTER_OPEN_PROBES = 10

CIRCUIT_DORMANT_COOLDOWN_SECONDS = 3600.0  # 1 hour

_ALLOW_REQUEST_LUA = """
local state = redis.call('HGET', KEYS[1], 'state')
if not state or state == 'closed' then
    return 'allow'
end
local now = tonumber(ARGV[1])
local next_probe_at = tonumber(redis.call('HGET', KEYS[1], 'next_probe_at') or '0')
if now < next_probe_at then
    return 'deny'
end
local lock_ttl = tonumber(ARGV[2])
local locked = redis.call('SET', KEYS[2], '1', 'NX', 'EX', lock_ttl)
if locked then
    return 'probe'
else
    return 'deny'
end
"""

_RECORD_FAILURE_LUA = """
local prior_state = redis.call('HGET', KEYS[1], 'state') or 'closed'
local now = tonumber(ARGV[1])
local threshold = tonumber(ARGV[2])
local base = tonumber(ARGV[3])
local max_cd = tonumber(ARGV[4])
local dormant_threshold = tonumber(ARGV[5])
local dormant_cooldown = tonumber(ARGV[6])

local failures = redis.call('HINCRBY', KEYS[1], 'failures', 1)
redis.call('HSET', KEYS[1], 'last_failure_ts', ARGV[1])

-- Below threshold from a clean closed state: stay closed, no backoff yet.
if prior_state == 'closed' and failures < threshold then
    return {'closed', 'closed'}
end

-- We're transitioning to (or staying in) open. Tick probe counter.
local probe_count = redis.call('HINCRBY', KEYS[1], 'probe_count_since_open', 1)
local new_state = 'open'
if probe_count >= dormant_threshold then
    new_state = 'dormant'
end

-- Cooldown selection (#921):
--   open:     exponential backoff capped at max_cd (~5min default)
--   dormant:  long cooldown (~1h default) so we self-heal slowly instead
--             of falling silent forever.
local cooldown
if new_state == 'dormant' then
    cooldown = dormant_cooldown
else
    local exp = probe_count - 1
    if exp > 20 then exp = 20 end
    cooldown = base * math.pow(2, exp)
    if cooldown > max_cd then cooldown = max_cd end
end
local next_probe_at = now + cooldown

redis.call('HSET', KEYS[1], 'state', new_state, 'next_probe_at', next_probe_at)
-- Release the probe-lock — whoever called us holds it; clearing here lets
-- the next eligible probe race fairly after the cooldown.
redis.call('DEL', KEYS[2])

return {prior_state, new_state}
"""

_RECORD_SUCCESS_LUA = """
local prior_state = redis.call('HGET', KEYS[1], 'state') or 'closed'
redis.call('HSET', KEYS[1], 'state', 'closed', 'failures', 0,
           'probe_count_since_open', 0, 'next_probe_at', 0)
redis.call('DEL', KEYS[2])
return prior_state
"""

_CIRCUIT_SCRIPTS = ScriptCache(
    allow=_ALLOW_REQUEST_LUA,
    record_failure=_RECORD_FAILURE_LUA,
    record_success=_RECORD_SUCCESS_LUA,
)

CIRCUIT_FAILURE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
)

TRANSIENT_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.WriteError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)

_decode_pair = decode_pair

def _get_circuit_redis() -> Optional[_redis.Redis]:
    """Return the shared breaker Redis client, or None if unreachable.

    Thin delegate to ``redis_breaker_util.get_breaker_redis`` — kept as a
    module function so existing tests can monkeypatch
    ``agent_client._get_circuit_redis``.
    """
    return get_breaker_redis()


def _reset_circuit_redis_client() -> None:
    """Drop the cached client + circuit Lua cache so the next call rebuilds.

    For tests + recovery. Resets both the shared client (redis_breaker_util)
    and this module's circuit script cache.
    """
    reset_breaker_redis_client()
    _CIRCUIT_SCRIPTS.reset()


def _ensure_scripts(client: _redis.Redis):
    """Register + cache the circuit Lua scripts; return (allow, rf, rs) tuple.

    Tuple shape preserved for the existing call sites that unpack it.
    """
    s = _CIRCUIT_SCRIPTS.ensure(client)
    return s["allow"], s["record_failure"], s["record_success"]


def is_circuit_failure(exc: BaseException) -> bool:
    """Return True if `exc` represents a real "agent unreachable" signal.

    Single source of truth shared by AgentClient._request() and
    monitoring_service.check_network_health() so both surfaces apply the
    same rule. See CIRCUIT_FAILURE_EXCEPTIONS for the canonical list.
    """
    return isinstance(exc, CIRCUIT_FAILURE_EXCEPTIONS)


def _emit_dormant_alert(agent_name: str) -> None:
    """Insert a circuit_breaker_dormant operator-queue entry.

    Called from CircuitState.record_failure on the closed/open → dormant
    transition. The Lua atomicity of _RECORD_FAILURE_LUA guarantees exactly
    one worker observes the transition, so this fires at most once per
    distinct dormant entry — no de-dupe layer required.
    """
    try:
        from database import db
        from utils.helpers import utc_now_iso
        now = utc_now_iso()
        # Use the generic 'alert' type so the existing Operating Room UI
        # (QueueCard.vue / QueueItemDetail.vue branch on 'approval|question|alert')
        # renders an Acknowledge control. The narrower discriminator goes in
        # context.alert_type for callers that want to filter — same pattern
        # the existing `sync_failing` work should adopt when its UI is touched.
        item = {
            "id": f"cb-dormant-{agent_name}-{now}",
            "agent_name": agent_name,
            "type": "alert",
            "status": "pending",
            "priority": "high",
            "title": "Agent circuit breaker DORMANT",
            "question": (
                f"{agent_name}'s circuit breaker entered DORMANT after "
                f"{CIRCUIT_DORMANT_AFTER_OPEN_PROBES} consecutive failed probes. "
                f"Scheduled tasks fast-fail until the agent recovers via the "
                f"~{int(CIRCUIT_DORMANT_COOLDOWN_SECONDS / 60)} min cooldown "
                f"probe or an admin reset."
            ),
            "context": {
                "agent_name": agent_name,
                "alert_type": "circuit_breaker_dormant",
                "transition": "dormant",
                "dormant_after_open_probes": CIRCUIT_DORMANT_AFTER_OPEN_PROBES,
                "dormant_cooldown_seconds": CIRCUIT_DORMANT_COOLDOWN_SECONDS,
            },
            "created_at": now,
        }
        db.create_operator_queue_item(agent_name, item)
        logger.warning(
            "[CB] circuit_breaker_dormant operator-queue entry emitted for %s",
            agent_name,
        )
    except Exception:
        # Don't let alert-delivery failure mask the breaker transition.
        logger.exception("[CB] failed to emit dormant alert for %s", agent_name)


class CircuitState:
    """Per-agent circuit breaker, Redis-backed (#631).

    The class is a thin facade over Redis ops — no in-process state to drift
    between workers. Construction is cheap (no DB / network I/O); state is
    fetched per call. Callers should still cache the instance per request
    rather than re-constructing for each method call.
    """

    def __init__(self, agent_name: str, redis_client: Optional[_redis.Redis] = None):
        self.agent_name = agent_name
        self._key = f"{_CIRCUIT_HASH_PREFIX}{agent_name}"
        self._lock_key = f"{self._key}{_CIRCUIT_PROBE_LOCK_SUFFIX}"
        self._redis = redis_client  # None → resolve lazily, supports per-call swap

    def _client(self) -> Optional[_redis.Redis]:
        return self._redis if self._redis is not None else _get_circuit_redis()

    def allow_request(self) -> bool:
        """Decide whether the caller may issue an HTTP request to the agent."""
        client = self._client()
        if client is None:
            return True  # Fail-open when Redis is unreachable
        try:
            allow, _, _ = _ensure_scripts(client)
            verdict = allow(
                keys=[self._key, self._lock_key],
                args=[time.time(), CIRCUIT_PROBE_LOCK_TTL_SECONDS],
                client=client,
            )
            # decode_responses=True returns str; older paths may still hand
            # back bytes (defensive).
            if isinstance(verdict, bytes):
                verdict = verdict.decode()
            return verdict in ("allow", "probe")
        except Exception as e:
            logger.warning("Circuit allow_request fell back to allow (%s)", e)
            _reset_circuit_redis_client()
            return True

    def record_failure(self) -> str:
        """Record a failure. Returns the new state ('closed'|'open'|'dormant')."""
        client = self._client()
        if client is None:
            return "closed"  # Fail-open: pretend nothing changed
        try:
            _, record_failure, _ = _ensure_scripts(client)
            result = record_failure(
                keys=[self._key, self._lock_key],
                args=[
                    time.time(),
                    CIRCUIT_FAILURE_THRESHOLD,
                    CIRCUIT_BASE_COOLDOWN_SECONDS,
                    CIRCUIT_MAX_COOLDOWN_SECONDS,
                    CIRCUIT_DORMANT_AFTER_OPEN_PROBES,
                    CIRCUIT_DORMANT_COOLDOWN_SECONDS,
                ],
                client=client,
            )
            prior_state, new_state = _decode_pair(result)
            if prior_state != new_state:
                if new_state == "open":
                    failures = self._read_int("failures")
                    logger.warning(
                        "Circuit OPENED for agent %s after %d failures",
                        self.agent_name, failures,
                    )
                elif new_state == "dormant":
                    logger.warning(
                        "Circuit DORMANT for agent %s after %d consecutive open-probe "
                        "failures — switching to %.0fs cooldown probing (#921)",
                        self.agent_name,
                        CIRCUIT_DORMANT_AFTER_OPEN_PROBES,
                        CIRCUIT_DORMANT_COOLDOWN_SECONDS,
                    )
                    # #921: surface the transition in the Operating Room so
                    # operators see it without having to grep logs. The Lua
                    # atomicity guarantees exactly one worker observes the
                    # transition, so this fires once per dormant entry.
                    _emit_dormant_alert(self.agent_name)
            return new_state
        except Exception as e:
            logger.warning("Circuit record_failure swallowed (%s)", e)
            _reset_circuit_redis_client()
            return "closed"

    def record_success(self) -> None:
        client = self._client()
        if client is None:
            return
        try:
            _, _, record_success = _ensure_scripts(client)
            prior = record_success(
                keys=[self._key, self._lock_key],
                args=[],
                client=client,
            )
            if isinstance(prior, bytes):
                prior = prior.decode()
            if prior and prior != "closed":
                logger.info(
                    "Circuit CLOSED for agent %s (recovered from %s)",
                    self.agent_name, prior,
                )
        except Exception as e:
            logger.warning("Circuit record_success swallowed (%s)", e)
            _reset_circuit_redis_client()

    def _read_int(self, field_name: str) -> int:
        client = self._client()
        if client is None:
            return 0
        raw = client.hget(self._key, field_name)
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    def to_dict(self) -> dict:
        client = self._client()
        if client is None:
            return {"state": "closed", "failure_count": 0, "cooldown_remaining": 0.0}
        data = client.hgetall(self._key) or {}
        return _state_dict(data)

    # --- Compatibility shims so callers that read .state / .failure_count
    # directly continue to work. Each property does a Redis read; callers in
    # hot paths should prefer to_dict() to bundle them into one HGETALL.

    @property
    def state(self) -> str:
        client = self._client()
        if client is None:
            return "closed"
        return client.hget(self._key, "state") or "closed"

    @property
    def failure_count(self) -> int:
        return self._read_int("failures")


def _state_dict(data: Dict[str, Any]) -> dict:
    """Translate a raw HGETALL result into the public to_dict shape."""
    state = data.get("state") or "closed"
    try:
        failures = int(data.get("failures") or 0)
    except (TypeError, ValueError):
        failures = 0
    try:
        next_probe_at = float(data.get("next_probe_at") or 0)
    except (TypeError, ValueError):
        next_probe_at = 0.0
    cooldown_remaining = max(0.0, next_probe_at - time.time()) if state == "open" else 0.0
    return {
        "state": state,
        "failure_count": failures,
        "cooldown_remaining": cooldown_remaining,
    }


def _get_circuit(agent_name: str) -> CircuitState:
    """Construct a fresh CircuitState facade for the agent.

    No registry — state lives in Redis. Construction is cheap.
    """
    return CircuitState(agent_name=agent_name)


def get_all_circuit_states() -> Dict[str, dict]:
    """Return the state dict for every agent that has any circuit history."""
    client = _get_circuit_redis()
    if client is None:
        return {}
    result: Dict[str, dict] = {}
    try:
        for key in client.scan_iter(match=f"{_CIRCUIT_HASH_PREFIX}*", count=200):
            if key.endswith(_CIRCUIT_PROBE_LOCK_SUFFIX):
                continue
            agent_name = key[len(_CIRCUIT_HASH_PREFIX):]
            data = client.hgetall(key)
            result[agent_name] = _state_dict(data or {})
    except Exception as e:
        logger.warning("Circuit get_all_states failed: %s", e)
        _reset_circuit_redis_client()
    return result


def force_circuit_dormant(agent_name: str, *, reason: str = "manual") -> None:
    """Park an agent's circuit in dormant state.

    Test-only / operator helper — no production caller since #1557 removed the
    autonomy-off hook (it conflated "administratively paused" with "transport
    unhealthy" and fast-failed inbound chat). Kept because it pairs with
    ``reset_circuit`` as a manual breaker primitive and is used by the breaker
    tests to stage dormant state. Do NOT wire this into a lifecycle path.

    Idempotent. Safe to call from any worker.
    """
    client = _get_circuit_redis()
    if client is None:
        return
    try:
        client.hset(
            f"{_CIRCUIT_HASH_PREFIX}{agent_name}",
            mapping={
                "state": "dormant",
                "next_probe_at": time.time() + CIRCUIT_MAX_COOLDOWN_SECONDS,
            },
        )
        client.delete(f"{_CIRCUIT_HASH_PREFIX}{agent_name}{_CIRCUIT_PROBE_LOCK_SUFFIX}")
        logger.info("Circuit forced DORMANT for %s (reason=%s)", agent_name, reason)
    except Exception as e:
        logger.warning("force_circuit_dormant(%s) swallowed: %s", agent_name, e)


def reset_circuit(agent_name: str) -> None:
    """Reset an agent's circuit to closed. Used by autonomy-on / manual recovery."""
    client = _get_circuit_redis()
    if client is None:
        return
    try:
        client.delete(
            f"{_CIRCUIT_HASH_PREFIX}{agent_name}",
            f"{_CIRCUIT_HASH_PREFIX}{agent_name}{_CIRCUIT_PROBE_LOCK_SUFFIX}",
        )
        logger.info("Circuit reset to CLOSED for %s", agent_name)
    except Exception as e:
        logger.warning("reset_circuit(%s) swallowed: %s", agent_name, e)


