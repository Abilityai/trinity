"""
Agent HTTP Client Service.

Provides a clean interface for communicating with agent containers.
Centralizes URL construction, timeout handling, error handling,
circuit breaking, and retry logic (RELIABILITY-001).

Circuit breaker (#631): state is held in Redis so multiple uvicorn workers
share one source of truth and cannot duplicate-probe a dead agent. State
machine transitions are atomic Lua scripts (no TOCTOU races between workers).
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

_CIRCUIT_HASH_PREFIX = "agent:circuit:"
_CIRCUIT_PROBE_LOCK_SUFFIX = ":probe-lock"

# Tunables — exposed at module level so tests / ops can monkeypatch.
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_BASE_COOLDOWN_SECONDS = 30.0
CIRCUIT_MAX_COOLDOWN_SECONDS = 300.0
CIRCUIT_PROBE_LOCK_TTL_SECONDS = 10
# After this many consecutive open-state probes without recovery, give up
# fast probing and switch to long-cooldown probing. 10 × exponentially-
# growing backoff ≈ 40min before we slow down.
CIRCUIT_DORMANT_AFTER_OPEN_PROBES = 10
# Issue #921: dormant state no longer requires manual recovery — we still
# probe occasionally on a long cooldown so the breaker self-heals when the
# underlying problem clears. Bounds the worst-case false-fail outage to
# ~DORMANT_COOLDOWN instead of "until a human notices". Set above the
# open-state max cooldown so we don't churn probes pointlessly when the
# agent really is down.
CIRCUIT_DORMANT_COOLDOWN_SECONDS = 3600.0  # 1 hour


# ----- Redis client (shared, fail-open) -------------------------------------
#
# The cached fail-open client lives in redis_breaker_util (#526 D4) so this
# transport breaker and the dispatch breaker share one client. These two
# functions stay as module-level wrappers because tests monkeypatch /
# call them by name (agent_client._get_circuit_redis,
# agent_client._reset_circuit_redis_client).


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


# ----- Lua scripts (atomic state machine transitions) -----------------------

# allow_request: returns "allow" | "probe" | "deny".
#   closed         → "allow"
#   open / dormant → if past next_probe_at AND we win SET-NX-EX on probe-lock
#                    → "probe", otherwise → "deny"
# Issue #921: dormant uses the same probe-on-cooldown logic as open, just
# with a much longer next_probe_at interval set in _RECORD_FAILURE_LUA.
# This restores baseline recovery behaviour: even if the breaker has been
# dormant for hours, exactly one request per cooldown window goes through
# to test whether the agent is back.
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

# record_failure: increments failure count, transitions to open / dormant
# with exponential backoff. Returns {prior_state, new_state} so the Python
# layer can log the transition exactly once per cluster (atomic Lua means
# only one worker observes the transition).
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

# record_success: full reset to closed. Returns prior_state for logging.
_RECORD_SUCCESS_LUA = """
local prior_state = redis.call('HGET', KEYS[1], 'state') or 'closed'
redis.call('HSET', KEYS[1], 'state', 'closed', 'failures', 0,
           'probe_count_since_open', 0, 'next_probe_at', 0)
redis.call('DEL', KEYS[2])
return prior_state
"""

# Circuit Lua scripts, cached process-wide via the shared ScriptCache (#526 D4).
_CIRCUIT_SCRIPTS = ScriptCache(
    allow=_ALLOW_REQUEST_LUA,
    record_failure=_RECORD_FAILURE_LUA,
    record_success=_RECORD_SUCCESS_LUA,
)


def _ensure_scripts(client: _redis.Redis):
    """Register + cache the circuit Lua scripts; return (allow, rf, rs) tuple.

    Tuple shape preserved for the existing call sites that unpack it.
    """
    s = _CIRCUIT_SCRIPTS.ensure(client)
    return s["allow"], s["record_failure"], s["record_success"]


# ----- Failure classification (#474) ---------------------------------------
#
# Single source of truth for which exception types should increment the
# circuit-breaker failure counter. Imported by services/monitoring_service.py
# so the /health probe applies the same rule as inline /api/* requests.
#
# Rationale (#474): a dropped MCP sync connection produces a fan-out of
# transient socket teardowns (broken pipe, connection reset, mid-write
# errors) plus read-timeouts from background pollers polling a *busy* (not
# unhealthy) agent. Those signals are noisy — only TCP-level unreachability
# should open the circuit.

CIRCUIT_FAILURE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
)

# Exceptions we should re-raise as AgentNotReachableError (so existing
# callers' `except AgentClientError` blocks keep working) but NOT count
# toward the circuit threshold. httpx.PoolTimeout is included because
# pool exhaustion is a client-side resource issue, not agent unhealth.
#
# httpx.TimeoutException is the parent class of {Connect,Read,Write,Pool}
# Timeout. ConnectTimeout is intentionally in CIRCUIT_FAILURE_EXCEPTIONS
# above and is caught by the *first* `except` arm in _request(), so the
# parent class here only intercepts bare TimeoutException and any future
# subclass we don't enumerate — making the drop-grace reclassification
# total over the timeout hierarchy minus ConnectTimeout.
TRANSIENT_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.WriteError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


def is_circuit_failure(exc: BaseException) -> bool:
    """Return True if `exc` represents a real "agent unreachable" signal.

    Single source of truth shared by AgentClient._request() and
    monitoring_service.check_network_health() so both surfaces apply the
    same rule. See CIRCUIT_FAILURE_EXCEPTIONS for the canonical list.
    """
    return isinstance(exc, CIRCUIT_FAILURE_EXCEPTIONS)


# ----- Dormant-transition notification (#921) -------------------------------
#
# When the breaker enters dormant, surface it in the Operating Room queue so
# operators see "agent silently failing scheduled tasks" without having to
# grep logs. Fire-and-forget: if the DB insert blows up, we still log the
# transition and the breaker is unaffected. Best-effort by design.

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


# ----- Public state object --------------------------------------------------

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


# Shared with the dispatch breaker (#526 D4): (prior_state, new_state) decode.
# Aliased (not re-defined) so agent_client._decode_pair stays importable.
_decode_pair = decode_pair


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
    """Park an agent's circuit in dormant state. Used by autonomy-off (#631 AC#5).

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


# ============================================================================
# Connection Pool (shared httpx.AsyncClient per agent)
# ============================================================================

_client_pool: Dict[str, httpx.AsyncClient] = {}

# Per-base_url "recent transport drop" timestamps. When the first concurrent
# caller catches a transport-level disconnect we stamp this map; siblings whose
# fresh-client retry then dies with ConnectError/Timeout within the grace
# window are classified as collateral drops (no record_failure). Without this
# the eviction-then-fresh-client race makes 9-of-10 concurrent drops still
# trip the breaker — see #474.
#
# Scope: both `_recent_drops` and `_client_pool` are process-local. Under
# multi-uvicorn-worker deployments each worker has its own grace map and its
# own pool, so the burst-neutralization is per-worker. The Redis-backed
# circuit (`CircuitState`) remains the single fleet-wide source of truth, so
# transport drops still never hit `record_failure()` in any worker — only the
# specific in-burst sibling-collapse fix is per-worker, which is acceptable
# because the underlying client pool is also per-worker.
_recent_drops: Dict[str, float] = {}
_DROP_GRACE_SEC = 2.0


def _stamp_drop(base_url: str) -> None:
    _recent_drops[base_url] = time.monotonic()


def _is_within_drop_grace(base_url: str) -> bool:
    ts = _recent_drops.get(base_url)
    if ts is None:
        return False
    if time.monotonic() - ts <= _DROP_GRACE_SEC:
        return True
    _recent_drops.pop(base_url, None)
    return False


def _build_http_client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=30.0,
        ),
    )


def _acquire_client(base_url: str) -> Tuple[httpx.AsyncClient, bool]:
    """Acquire an httpx client and report whether it is pooled.

    Returns `(client, is_pooled)`. Non-pooled clients are single-use and
    MUST be closed by the caller — the in-grace path returns a fresh
    client so a concurrent drop burst can't repopulate the pool with
    transient sockets, but the caller is then responsible for `aclose()`
    to prevent a connection-handle leak.
    """
    if _is_within_drop_grace(base_url):
        return _build_http_client(base_url), False

    client = _client_pool.get(base_url)
    if client is None or client.is_closed:
        client = _build_http_client(base_url)
        _client_pool[base_url] = client
    return client, True


def _get_http_client(base_url: str) -> httpx.AsyncClient:
    """Get or create an httpx client for a base URL.

    Backward-compatible wrapper around `_acquire_client` that discards the
    pooled-ness flag. Callers that need to close non-pooled clients should
    use `_acquire_client` directly. (`_request` does.)
    """
    client, _ = _acquire_client(base_url)
    return client


async def close_all_clients():
    """Close all pooled HTTP clients. Call on app shutdown."""
    for client in _client_pool.values():
        await client.aclose()
    _client_pool.clear()


# ============================================================================
# Response Models
# ============================================================================

@dataclass
class AgentChatMetrics:
    """Observability data extracted from agent chat response."""
    context_used: int
    context_max: int
    context_percent: float
    cost_usd: Optional[float]
    tool_calls_json: Optional[str]
    execution_log_json: Optional[str]


@dataclass
class AgentChatResponse:
    """Parsed response from agent chat endpoint."""
    response_text: str
    metrics: AgentChatMetrics
    raw_response: Dict[str, Any]


@dataclass
class AgentSessionInfo:
    """Agent context/session information."""
    context_tokens: int
    context_window: int
    context_percent: float
    total_cost_usd: Optional[float] = None


# ============================================================================
# Exceptions
# ============================================================================

class AgentClientError(Exception):
    """Base exception for agent client errors."""
    pass


class AgentNotReachableError(AgentClientError):
    """Agent container is not responding."""
    pass


class AgentConnectionDroppedError(AgentNotReachableError):
    """Connection dropped mid-flight (transport-level disconnect).

    Distinguishes "in-flight transport broke" from "agent unreachable from the
    start" so the circuit breaker can stay neutral. Inherits from
    AgentNotReachableError so tenacity retries (`retry_if_exception_type`) and
    callers catching `AgentNotReachableError` handle it correctly. (#474.)
    """
    pass


class AgentCircuitOpenError(AgentClientError):
    """Circuit breaker is open — agent is known to be unhealthy."""
    pass


class AgentRequestError(AgentClientError):
    """Agent returned an error response."""
    def __init__(self, message: str, status_code: int = None):
        super().__init__(message)
        self.status_code = status_code


# ============================================================================
# Agent Client
# ============================================================================

class AgentClient:
    """
    HTTP client for agent container communication.

    Centralizes:
    - URL construction
    - Timeout handling
    - Error handling
    - Response parsing
    """

    # Default timeouts
    CHAT_TIMEOUT = 900.0      # 15 minutes for chat
    SESSION_TIMEOUT = 5.0     # 5 seconds for session info
    DEFAULT_TIMEOUT = 30.0    # 30 seconds default

    def __init__(self, agent_name: str):
        """
        Initialize client for a specific agent.

        Args:
            agent_name: Name of the agent (without 'agent-' prefix)
        """
        self.agent_name = agent_name
        self.base_url = f"http://agent-{agent_name}:8000"
        self._circuit = _get_circuit(agent_name)

    # ========================================================================
    # Core HTTP Methods
    # ========================================================================

    async def _request(
        self,
        method: str,
        path: str,
        timeout: float = None,
        **kwargs
    ) -> httpx.Response:
        """
        Make an HTTP request to the agent.

        Checks circuit breaker before sending. Records success/failure
        to the per-agent circuit state.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: URL path (e.g., "/api/chat")
            timeout: Request timeout in seconds
            **kwargs: Additional arguments for httpx request

        Returns:
            httpx.Response

        Raises:
            AgentCircuitOpenError: If circuit breaker is open
            AgentNotReachableError: If connection fails
            AgentRequestError: If request fails with error status
        """
        if not self._circuit.allow_request():
            raise AgentCircuitOpenError(
                f"Circuit open for agent {self.agent_name} "
                f"(failures={self._circuit.failure_count})"
            )

        timeout = timeout or self.DEFAULT_TIMEOUT
        # #1159: stamp the per-agent auth token. merge_auth_headers overrides any
        # caller-supplied X-Trinity-Agent-Token (case-insensitively) so a stale
        # or forged value can never slip past the agent middleware.
        kwargs["headers"] = merge_auth_headers(self.agent_name, kwargs.get("headers"))
        client, is_pooled = _acquire_client(self.base_url)

        try:
            response = await client.request(
                method, path, timeout=timeout, **kwargs
            )
            self._circuit.record_success()
            return response

        except asyncio.CancelledError:
            # Cancellation (e.g. MCP client drop propagating through FastAPI)
            # is not an agent-health signal. Explicit re-raise so a future
            # maintainer can't shadow it with a broader catch. (#798.)
            raise

        except CIRCUIT_FAILURE_EXCEPTIONS as e:
            # ConnectError / ConnectTimeout — agent is genuinely unreachable.
            # ConnectTimeout is a TimeoutException subclass, so this branch
            # must come before any TimeoutException catch. (#798.)
            #
            # Drop-grace override (#474 sibling-collapse fix): if a peer
            # caller on the same base_url tripped a transport drop within
            # the last _DROP_GRACE_SEC, this ConnectError is collateral —
            # the pool was evicted under us and the fresh client failed
            # to reconnect mid-burst on a still-healthy agent. Raise the
            # AgentNotReachableError subclass instead of record_failure()
            # so siblings of a single drop don't poison the circuit.
            if _is_within_drop_grace(self.base_url):
                raise AgentConnectionDroppedError(
                    f"Connection to agent {self.agent_name} dropped mid-burst "
                    f"(collateral {type(e).__name__}): {e}"
                )
            self._circuit.record_failure()
            raise AgentNotReachableError(
                f"Cannot reach agent {self.agent_name}: "
                f"{type(e).__name__}: {e}"[:200]
            )

        except (
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            BrokenPipeError,
            ConnectionResetError,
        ) as e:
            # Transport drop mid-flight. NOT a circuit-health signal —
            # the agent itself may be fine and the connection just died
            # (upstream MCP-sync cancellation, transient socket reset,
            # broken keepalive). Do NOT record_failure(). (#474.)
            #
            # This branch is layered ABOVE TRANSIENT_TRANSPORT_EXCEPTIONS
            # so the stamp+evict side effects always run on the genuine
            # drop signals (httpx.ReadError / WriteError / RemoteProtocolError
            # — which also appear in the tuple below — plus the raw
            # OSError subclasses, which #798 deliberately let propagate).
            # The tuple-based handler below is then left only with the
            # timeout / pool-exhaustion members of the contract.
            #
            # Pool eviction: a pooled client must be removed so the next
            # call doesn't reuse a broken keepalive socket. The `is
            # client` identity check guarantees only the worker that
            # still owns the pool entry closes it; siblings in a
            # concurrent burst see the pool empty and skip. Non-pooled
            # (fresh-during-grace) clients are not in the pool, so the
            # eviction step is skipped — the outer `finally` closes
            # them either way.
            _stamp_drop(self.base_url)
            if is_pooled:
                evicted = _client_pool.pop(self.base_url, None)
                if evicted is client:
                    try:
                        await client.aclose()
                    except Exception:
                        pass
            raise AgentConnectionDroppedError(
                f"Connection to agent {self.agent_name} dropped mid-flight: "
                f"{type(e).__name__}: {e}"
            )

        except TRANSIENT_TRANSPORT_EXCEPTIONS as e:
            # Read/Write timeouts and pool exhaustion. Surface to the
            # caller as the existing typed error so `except AgentClientError`
            # blocks keep working, but DO NOT count toward the circuit
            # threshold (#474 / #798). The httpx.ReadError / WriteError /
            # RemoteProtocolError members of this tuple are intercepted
            # by the stamp+evict handler above so this branch sees only
            # the *Timeout / PoolTimeout members in practice.
            #
            # Drop-grace override (#474 sibling-collapse fix): if a peer
            # caller on the same base_url tripped a transport drop within
            # the last _DROP_GRACE_SEC, surface as AgentConnectionDroppedError
            # so tenacity / catch chains see a consistent "in-burst" signal
            # rather than a plain transient.
            if _is_within_drop_grace(self.base_url):
                raise AgentConnectionDroppedError(
                    f"Connection to agent {self.agent_name} dropped mid-burst "
                    f"(collateral {type(e).__name__}): {e}"
                )
            raise AgentNotReachableError(
                f"Transient transport error to agent {self.agent_name}: "
                f"{type(e).__name__}: {e}"[:200]
            )

        finally:
            # Non-pooled clients are single-use (returned by `_acquire_client`
            # while a drop-grace window is active so the pool isn't
            # repopulated with transient sockets). The body has already
            # buffered any successful response (default httpx is non-
            # streaming), so closing here is safe on every exit path and
            # prevents the connection-handle leak that would otherwise
            # accumulate during sustained drop bursts. (#474.)
            if not is_pooled:
                try:
                    await client.aclose()
                except Exception:
                    pass

    async def get(self, path: str, timeout: float = None, **kwargs) -> httpx.Response:
        """Make a GET request to the agent."""
        return await self._request("GET", path, timeout, **kwargs)

    async def post(self, path: str, timeout: float = None, **kwargs) -> httpx.Response:
        """Make a POST request to the agent."""
        return await self._request("POST", path, timeout, **kwargs)

    async def put(self, path: str, timeout: float = None, **kwargs) -> httpx.Response:
        """Make a PUT request to the agent."""
        return await self._request("PUT", path, timeout, **kwargs)

    async def delete(self, path: str, timeout: float = None, **kwargs) -> httpx.Response:
        """Make a DELETE request to the agent."""
        return await self._request("DELETE", path, timeout, **kwargs)

    # ========================================================================
    # Chat Operations
    # ========================================================================

    async def chat(
        self,
        message: str,
        stream: bool = False,
        timeout: float = None
    ) -> AgentChatResponse:
        """
        Send a chat message to the agent.

        Args:
            message: Message to send
            stream: Whether to stream the response
            timeout: Request timeout (default: 5 minutes)

        Returns:
            AgentChatResponse with parsed metrics

        Raises:
            AgentNotReachableError: If agent is not reachable
            AgentRequestError: If request fails
        """
        timeout = timeout or self.CHAT_TIMEOUT

        response = await self.post(
            "/api/chat",
            json={"message": message, "stream": stream},
            timeout=timeout
        )

        # Check for error response and extract detailed error message
        if response.status_code >= 400:
            error_msg = self._extract_error_detail(response)
            raise AgentRequestError(error_msg, status_code=response.status_code)

        result = response.json()
        return self._parse_chat_response(result)

    async def task(
        self,
        message: str,
        timeout: float = None,
        execution_id: Optional[str] = None
    ) -> AgentChatResponse:
        """
        Execute a stateless task on the agent (no conversation context).

        Unlike chat(), this endpoint:
        - Does NOT maintain conversation history
        - Each call is independent (no --continue flag)
        - Returns raw Claude Code execution log (full transcript)

        Use this for scheduled executions and independent tasks.

        Args:
            message: Task prompt to execute
            timeout: Request timeout (default: 15 minutes)
            execution_id: Optional execution ID for process registry (enables termination and live streaming)

        Returns:
            AgentChatResponse with parsed metrics and raw execution log

        Raises:
            AgentNotReachableError: If agent is not reachable
            AgentRequestError: If request fails
        """
        timeout = timeout or self.CHAT_TIMEOUT

        payload = {"message": message, "timeout_seconds": int(timeout)}
        if execution_id:
            payload["execution_id"] = execution_id

        response = await self.post(
            "/api/task",
            json=payload,
            timeout=timeout + 10  # Add buffer to agent timeout
        )

        # Check for error response and extract detailed error message
        if response.status_code >= 400:
            error_msg = self._extract_error_detail(response)
            raise AgentRequestError(error_msg, status_code=response.status_code)

        result = response.json()
        return self._parse_task_response(result)

    def _parse_task_response(self, result: Dict[str, Any]) -> AgentChatResponse:
        """
        Parse agent task response into structured data.

        Similar to _parse_chat_response but handles /api/task format
        which returns raw Claude Code execution log.
        """
        # Extract response text
        response_text = result.get("response", str(result))
        if len(response_text) > 10000:
            response_text = response_text[:10000] + "... (truncated)"

        # Extract observability data (task response has metadata but no session)
        metadata = result.get("metadata", {})
        execution_log = result.get("execution_log")

        # Context usage from metadata
        context_used = metadata.get("input_tokens", 0)
        context_max = metadata.get("context_window") or DEFAULT_CONTEXT_WINDOW
        context_percent = round(context_used / max(context_max, 1) * 100, 1)

        # Cost
        cost = metadata.get("cost_usd")

        # Execution log - raw Claude Code transcript
        # Note: Check is not None, not truthiness - empty list [] is valid log
        tool_calls_json = None
        execution_log_json = None
        if execution_log is not None:
            execution_log_json = json.dumps(execution_log)
            tool_calls_json = execution_log_json  # Backwards compatibility

        metrics = AgentChatMetrics(
            context_used=context_used,
            context_max=context_max,
            context_percent=context_percent,
            cost_usd=cost,
            tool_calls_json=tool_calls_json,
            execution_log_json=execution_log_json
        )

        return AgentChatResponse(
            response_text=response_text,
            metrics=metrics,
            raw_response=result
        )

    def _extract_error_detail(self, response: httpx.Response) -> str:
        """Extract detailed error message from agent HTTP response."""
        try:
            error_data = response.json()
            if "detail" in error_data:
                return error_data["detail"]
        except Exception:
            pass
        # Fall back to response text if JSON parsing fails
        if response.text:
            return response.text[:500]
        return f"HTTP {response.status_code} error"

    def _parse_chat_response(self, result: Dict[str, Any]) -> AgentChatResponse:
        """
        Parse agent chat response into structured data.

        Extracts:
        - Response text (truncated if > 10000 chars)
        - Context usage (tokens, window, percentage)
        - Cost
        - Tool calls / execution log
        """
        # Extract response text
        response_text = result.get("response", str(result))
        if len(response_text) > 10000:
            response_text = response_text[:10000] + "... (truncated)"

        # Extract observability data
        session_data = result.get("session", {})
        metadata = result.get("metadata", {})
        execution_log = result.get("execution_log")

        # Context usage
        # NOTE: cache_creation_tokens and cache_read_tokens are SUBSETS of input_tokens
        # for billing purposes, NOT additional tokens. Do NOT sum them.
        context_used = session_data.get("context_tokens") or metadata.get("input_tokens", 0)
        context_max = session_data.get("context_window") or metadata.get("context_window") or DEFAULT_CONTEXT_WINDOW
        context_percent = round(context_used / max(context_max, 1) * 100, 1)

        # Cost
        cost = metadata.get("cost_usd") or session_data.get("total_cost_usd")

        # Tool calls / execution log
        # Note: Check is not None, not truthiness - empty list [] is valid log
        tool_calls_json = None
        execution_log_json = None
        if execution_log is not None:
            execution_log_json = json.dumps(execution_log)
            tool_calls_json = execution_log_json  # Backwards compatibility

        metrics = AgentChatMetrics(
            context_used=context_used,
            context_max=context_max,
            context_percent=context_percent,
            cost_usd=cost,
            tool_calls_json=tool_calls_json,
            execution_log_json=execution_log_json
        )

        return AgentChatResponse(
            response_text=response_text,
            metrics=metrics,
            raw_response=result
        )

    # ========================================================================
    # Session / Context Operations
    # ========================================================================

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(AgentNotReachableError),
        reraise=True,
    )
    async def get_session(self, timeout: float = None) -> Optional[AgentSessionInfo]:
        """
        Get current session/context information.
        Retries up to 3x with exponential backoff on transient errors.

        Returns:
            AgentSessionInfo or None if request fails
        """
        timeout = timeout or self.SESSION_TIMEOUT

        try:
            response = await self.get("/api/chat/session", timeout=timeout)
            if response.status_code == 200:
                session = response.json()
                context_tokens = session.get("context_tokens", 0)
                context_window = session.get("context_window") or DEFAULT_CONTEXT_WINDOW
                return AgentSessionInfo(
                    context_tokens=context_tokens,
                    context_window=context_window,
                    context_percent=round(
                        context_tokens / max(context_window, 1) * 100, 1
                    ),
                    total_cost_usd=session.get("total_cost_usd")
                )
        except AgentClientError:
            pass
        return None

    # ========================================================================
    # File Operations
    # ========================================================================

    async def read_file(
        self,
        path: str,
        timeout: float = 30.0
    ) -> dict:
        """
        Read content from a file in the agent's workspace.

        Args:
            path: File path within /home/developer
            timeout: Request timeout

        Returns:
            dict with success status and content
        """
        try:
            import urllib.parse
            encoded_path = urllib.parse.quote(path, safe='')

            response = await self.get(
                f"/api/files/download?path={encoded_path}",
                timeout=timeout
            )

            if response.status_code == 200:
                return {"success": True, "content": response.text}
            elif response.status_code == 404:
                return {"success": True, "content": None, "not_found": True}
            else:
                return {
                    "success": False,
                    "error": response.text,
                    "status_code": response.status_code
                }

        except AgentClientError as e:
            return {"success": False, "error": str(e)}

    async def write_file(
        self,
        path: str,
        content: str,
        timeout: float = 30.0,
        platform: bool = False
    ) -> dict:
        """
        Write content to a file in the agent's workspace.
        Creates parent directories if they don't exist.

        Args:
            path: File path within /home/developer
            content: File content to write
            timeout: Request timeout
            platform: If True, allows writes to .trinity directory (platform-initiated)

        Returns:
            dict with success status and file info
        """
        try:
            # URL encode the path for query parameter
            import urllib.parse
            encoded_path = urllib.parse.quote(path, safe='')

            # Add platform flag if needed
            query = f"path={encoded_path}"
            if platform:
                query += "&platform=true"

            response = await self.put(
                f"/api/files?{query}",
                json={"content": content},
                timeout=timeout
            )

            if response.status_code == 200:
                return {"success": True, **response.json()}
            else:
                return {
                    "success": False,
                    "error": response.text,
                    "status_code": response.status_code
                }

        except AgentClientError as e:
            return {"success": False, "error": str(e)}

    # ========================================================================
    # Health Check
    # ========================================================================

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(AgentNotReachableError),
        reraise=True,
    )
    async def health_check(self, timeout: float = 5.0) -> bool:
        """
        Check if agent is healthy and responding.
        Retries up to 3x with exponential backoff on transient errors.

        Returns:
            True if agent responds to health check
        """
        try:
            response = await self.get("/api/health", timeout=timeout)
            return response.status_code == 200
        except AgentCircuitOpenError:
            return False
        except AgentClientError:
            return False


# ============================================================================
# Factory Function
# ============================================================================

def get_agent_client(agent_name: str) -> AgentClient:
    """
    Factory function to create an AgentClient.

    Args:
        agent_name: Name of the agent

    Returns:
        AgentClient instance
    """
    return AgentClient(agent_name)
