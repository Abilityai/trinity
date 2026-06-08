"""Per-agent dispatch circuit breaker (#526, RELIABILITY-007).

A **producer-side** breaker at the dispatch layer. When an agent is *auth-dead*
(reachable, but answers HTTP 503 → execution ``error_code == AUTH``), this
breaker fast-fails NEW executions instead of poisoning the persistent backlog
with doomed tasks, and self-heals via a half-open probe.

Distinct from the transport-reachability breaker (``services/agent_client.py``
``CircuitState``, #631):

    transport breaker  — owns *unreachable* (ConnectError / ConnectTimeout).
    dispatch breaker   — owns *auth-dead*   (execution error_code == AUTH).
    #307 heartbeat     — owns *wedged*      (calls record_failure() seam).

The two breakers share Redis plumbing (``redis_breaker_util``, D4) but have
**separate namespaces** and **separate Lua**, so neither contaminates the
other's counter (autoplan-Q1): ``AgentClient.record_success()`` (HTTP-200)
never touches this breaker, and this breaker's AUTH counting never touches the
transport one.

Model (D9): **consecutive-failure** ``closed → open → half-open(probe) →
closed``. A key-dead agent fails back-to-back, so a consecutive count trips
reliably without the moving parts (ordered success/failure log + probe
generation token) a rolling window would need. Any **success resets** the
counter.

Dependency-free by design (D3): ``record_outcome`` only *returns* the
transition; the CALLER (``task_execution_service``) backgrounds the backlog
drain + audit on ``→open``. No ``capacity``/``db`` import here → no circular
import. Fail-open on Redis down; never raises.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, cast

import redis as _redis

from redis_breaker_util import (
    ScriptCache,
    decode_pair,
    decode_str,
    fail_open,
    get_breaker_redis,
    reset_breaker_redis_client,
)

logger = logging.getLogger(__name__)


# ----- Redis layout / tunables ----------------------------------------------
#
#     agent:dispatch:{name}             HASH  state, failures, last_failure_ts,
#                                            next_probe_at, probe_count_since_open
#     agent:dispatch:{name}:probe-lock  STRING (NX EX 10) — half-open probe permit
#
# Tunables at module level so tests / ops can monkeypatch.

_DISPATCH_HASH_PREFIX = "agent:dispatch:"
_DISPATCH_PROBE_LOCK_SUFFIX = ":probe-lock"

DISPATCH_FAILURE_THRESHOLD = 3
DISPATCH_BASE_COOLDOWN_SECONDS = 30.0
DISPATCH_MAX_COOLDOWN_SECONDS = 300.0
DISPATCH_PROBE_LOCK_TTL_SECONDS = 10

# The one outcome that counts (D10). Compared by .value so this module never
# imports TaskExecutionErrorCode (which lives in task_execution_service, the
# consumer — importing it here would be a circular import). Mirrors
# TaskExecutionErrorCode.AUTH.value.
AUTH_ERROR_CODE = "auth"


# ----- Lua scripts (atomic state-machine transitions) -----------------------
#
# Structurally identical to the transport CircuitState Lua (proven half-open /
# probe-lock semantics) minus the dormant tier — the half-open probe alone
# self-heals, and the max-cooldown cap bounds the worst-case false-fail window.

_ALLOW_DISPATCH_LUA = """
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

local failures = redis.call('HINCRBY', KEYS[1], 'failures', 1)
redis.call('HSET', KEYS[1], 'last_failure_ts', ARGV[1])

-- Below threshold from a clean closed state: stay closed, no backoff yet.
if prior_state == 'closed' and failures < threshold then
    return {'closed', 'closed'}
end

-- Transitioning to (or staying in) open. Tick probe counter for backoff.
local probe_count = redis.call('HINCRBY', KEYS[1], 'probe_count_since_open', 1)
local exp = probe_count - 1
if exp > 20 then exp = 20 end
local cooldown = base * math.pow(2, exp)
if cooldown > max_cd then cooldown = max_cd end
local next_probe_at = now + cooldown

redis.call('HSET', KEYS[1], 'state', 'open', 'next_probe_at', next_probe_at)
-- Release the probe-lock so the next eligible probe races fairly after cooldown.
redis.call('DEL', KEYS[2])

return {prior_state, 'open'}
"""

_RECORD_SUCCESS_LUA = """
local prior_state = redis.call('HGET', KEYS[1], 'state') or 'closed'
local failures = redis.call('HGET', KEYS[1], 'failures')
-- Already clean (closed + zero failures): nothing to reset, skip the write to
-- avoid HSET/DEL amplification on every success of a healthy agent. A closed/0
-- breaker holds no probe-lock, so skipping the DEL is safe.
if prior_state == 'closed' and (not failures or failures == '0') then
    return prior_state
end
redis.call('HSET', KEYS[1], 'state', 'closed', 'failures', 0,
           'probe_count_since_open', 0, 'next_probe_at', 0)
redis.call('DEL', KEYS[2])
return prior_state
"""

_DISPATCH_SCRIPTS = ScriptCache(
    allow=_ALLOW_DISPATCH_LUA,
    record_failure=_RECORD_FAILURE_LUA,
    record_success=_RECORD_SUCCESS_LUA,
)


def _reset_dispatch_redis() -> None:
    """Drop the shared client + dispatch Lua cache so the next call rebuilds."""
    reset_breaker_redis_client()
    _DISPATCH_SCRIPTS.reset()


# ----- Transition value object ----------------------------------------------


@dataclass(frozen=True)
class Transition:
    """The (prior, new) state pair returned by record_* methods.

    The caller inspects ``.opened`` to fire the backlog drain + audit, and
    ``.closed`` to audit recovery. A no-op (ignored outcome / fail-open) is a
    closed→closed transition that triggers neither.
    """

    prior: str
    new: str

    @property
    def opened(self) -> bool:
        """True on a fresh transition INTO open (not open→open probe re-fail)."""
        return self.new == "open" and self.prior != "open"

    @property
    def closed(self) -> bool:
        """True on recovery: a probe / success closed a previously-open breaker."""
        return self.new == "closed" and self.prior == "open"

    @property
    def changed(self) -> bool:
        return self.prior != self.new

    @classmethod
    def noop(cls) -> "Transition":
        return cls("closed", "closed")


def _dispatch_state_dict(data: Dict[str, Any]) -> dict:
    """Translate a raw HGETALL result into the public state shape."""
    state = data.get("state") or "closed"
    try:
        failures = int(data.get("failures") or 0)
    except (TypeError, ValueError):
        failures = 0
    try:
        next_probe_at = float(data.get("next_probe_at") or 0)
    except (TypeError, ValueError):
        next_probe_at = 0.0
    retry_after = (
        max(0, int(math.ceil(next_probe_at - time.time()))) if state == "open" else 0
    )
    return {
        "state": state,
        "failure_count": failures,
        "retry_after_seconds": retry_after,
    }


# ----- Public breaker --------------------------------------------------------


class DispatchBreaker:
    """Per-agent dispatch breaker, Redis-backed. Thin facade over Redis ops;
    construction is cheap (no I/O). Fail-open and non-raising throughout."""

    def __init__(self, agent_name: str, redis_client: Optional[_redis.Redis] = None):
        self.agent_name = agent_name
        self._key = f"{_DISPATCH_HASH_PREFIX}{agent_name}"
        self._lock_key = f"{self._key}{_DISPATCH_PROBE_LOCK_SUFFIX}"
        self._redis = redis_client  # None → resolve lazily, supports per-call swap

    def _client(self) -> Optional[_redis.Redis]:
        return self._redis if self._redis is not None else get_breaker_redis()

    # --- dispatch gate (hot path) ---

    def allow_dispatch(self) -> bool:
        """Decide whether a new dispatch may proceed. Fail-open."""
        client = self._client()
        if client is None:
            return True

        def _op() -> bool:
            scripts = _DISPATCH_SCRIPTS.ensure(client)
            verdict = scripts["allow"](
                keys=[self._key, self._lock_key],
                args=[time.time(), DISPATCH_PROBE_LOCK_TTL_SECONDS],
                client=client,
            )
            return decode_str(verdict) in ("allow", "probe")

        return fail_open(True, _op, on_error=_reset_dispatch_redis)

    def retry_after_seconds(self) -> int:
        """Seconds until the next half-open probe is allowed (0 when not open)."""
        client = self._client()
        if client is None:
            return 0

        def _op() -> int:
            data = cast(Dict[str, Any], client.hgetall(self._key) or {})
            return _dispatch_state_dict(data)["retry_after_seconds"]

        return fail_open(0, _op, on_error=_reset_dispatch_redis)

    # --- outcome recording (terminal points; off the hot path) ---

    def record_failure(self, reason: str = "missed_heartbeat") -> Transition:
        """Record one failure. Returns the (prior, new) transition.

        ``reason`` is a log label only: ``"auth"`` from ``record_outcome``,
        ``"missed_heartbeat"`` from the #307 seam.
        """
        client = self._client()
        if client is None:
            return Transition.noop()

        def _op() -> Transition:
            scripts = _DISPATCH_SCRIPTS.ensure(client)
            result = scripts["record_failure"](
                keys=[self._key, self._lock_key],
                args=[
                    time.time(),
                    DISPATCH_FAILURE_THRESHOLD,
                    DISPATCH_BASE_COOLDOWN_SECONDS,
                    DISPATCH_MAX_COOLDOWN_SECONDS,
                ],
                client=client,
            )
            prior, new = decode_pair(result)
            t = Transition(prior, new)
            if t.opened:
                logger.warning(
                    "[DispatchBreaker] OPEN for %s (reason=%s, threshold=%d)",
                    self.agent_name,
                    reason,
                    DISPATCH_FAILURE_THRESHOLD,
                )
            return t

        return fail_open(Transition.noop(), _op, on_error=_reset_dispatch_redis)

    def record_success(self) -> Transition:
        """Record one success → reset to closed. Returns the transition."""
        client = self._client()
        if client is None:
            return Transition.noop()

        def _op() -> Transition:
            scripts = _DISPATCH_SCRIPTS.ensure(client)
            prior = decode_str(
                scripts["record_success"](
                    keys=[self._key, self._lock_key], args=[], client=client
                )
            )
            t = Transition(prior or "closed", "closed")
            if t.closed:
                logger.info(
                    "[DispatchBreaker] CLOSED for %s (recovered from %s)",
                    self.agent_name,
                    prior,
                )
            return t

        return fail_open(Transition.noop(), _op, on_error=_reset_dispatch_redis)

    def record_outcome(self, error_code: Any) -> Transition:
        """Record an execution outcome (D10 — AUTH-only counting).

        - ``None``  (success terminal)        → ``record_success``
        - ``AUTH``                            → ``record_failure(reason="auth")``
        - everything else (TIMEOUT, CAPACITY, NETWORK, BILLING, AGENT_ERROR,
          CIRCUIT_OPEN)                       → ignored (no-op transition)

        ``error_code`` may be a ``TaskExecutionErrorCode`` member or its
        ``.value`` string — compared by value so this module never imports the
        enum (circular-import-free).

        CALLER CONTRACT: pass ``None`` ONLY at a genuine success terminal. At a
        failure terminal, gate the call on ``error_code == AUTH`` and pass the
        code — never pass a failure's ``None`` here, which would read as a
        success and reset the counter.
        """
        if error_code is None:
            return self.record_success()
        code = getattr(error_code, "value", error_code)
        if isinstance(code, str) and code.lower() == AUTH_ERROR_CODE:
            return self.record_failure(reason="auth")
        return Transition.noop()

    # --- introspection ---

    def to_dict(self) -> dict:
        client = self._client()
        if client is None:
            return {"state": "closed", "failure_count": 0, "retry_after_seconds": 0}

        def _op() -> dict:
            data = cast(Dict[str, Any], client.hgetall(self._key) or {})
            return _dispatch_state_dict(data)

        return fail_open(
            {"state": "closed", "failure_count": 0, "retry_after_seconds": 0},
            _op,
            on_error=_reset_dispatch_redis,
        )


# ----- Module-level operator hooks ------------------------------------------


def get_all_dispatch_states() -> Dict[str, dict]:
    """Return the state dict for every agent that has dispatch-breaker history.

    Bounded SCAN (count=200), skips probe-lock keys. Fail-open → {}.
    """
    client = get_breaker_redis()
    if client is None:
        return {}

    def _op() -> Dict[str, dict]:
        result: Dict[str, dict] = {}
        for key in client.scan_iter(match=f"{_DISPATCH_HASH_PREFIX}*", count=200):
            if key.endswith(_DISPATCH_PROBE_LOCK_SUFFIX):
                continue
            agent_name = key[len(_DISPATCH_HASH_PREFIX):]
            data = cast(Dict[str, Any], client.hgetall(key) or {})
            result[agent_name] = _dispatch_state_dict(data)
        return result

    return fail_open({}, _op, on_error=_reset_dispatch_redis)


def get_dispatch_states_for(agent_names: list) -> Dict[str, dict]:
    """Bulk dispatch state for a KNOWN set of agents via one pipelined HGETALL
    round-trip — no keyspace SCAN (#526 D7, slots-dashboard poll path).

    Agents with no breaker history resolve to the closed default. Fail-open →
    all-closed dict.
    """
    closed_default = {
        n: {"state": "closed", "failure_count": 0, "retry_after_seconds": 0}
        for n in agent_names
    }
    if not agent_names:
        return {}
    client = get_breaker_redis()
    if client is None:
        return closed_default

    def _op() -> Dict[str, dict]:
        pipe = client.pipeline()
        for n in agent_names:
            pipe.hgetall(f"{_DISPATCH_HASH_PREFIX}{n}")
        rows = pipe.execute()
        return {
            n: _dispatch_state_dict(cast(Dict[str, Any], rows[i] or {}))
            for i, n in enumerate(agent_names)
        }

    return fail_open(closed_default, _op, on_error=_reset_dispatch_redis)


def reset_dispatch(agent_name: str) -> None:
    """Reset an agent's dispatch breaker to closed. Used by the unified reset
    endpoint (clears both breakers) and operator recovery."""
    client = get_breaker_redis()
    if client is None:
        return
    key = f"{_DISPATCH_HASH_PREFIX}{agent_name}"
    fail_open(
        None,
        lambda: client.delete(key, f"{key}{_DISPATCH_PROBE_LOCK_SUFFIX}"),
        on_error=_reset_dispatch_redis,
    )
    logger.info("Dispatch breaker reset to CLOSED for %s", agent_name)
