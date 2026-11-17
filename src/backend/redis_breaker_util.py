"""Shared Redis plumbing for per-agent circuit breakers (#526, D4).

Extracted from ``services/agent_client.py`` so the transport-reachability
breaker (``CircuitState``, #631) and the dispatch breaker
(``services/dispatch_breaker.py``, #526) share ONE fail-open Redis client, ONE
Lua script-cache pattern, and the same decode helpers instead of duplicating
them. **Plumbing only** — no breaker *policy* (thresholds, state machine, drain)
lives here; that stays in each breaker module.

Why this is a TOP-LEVEL backend module (sibling of ``config.py`` /
``database.py``) and NOT ``services/redis_breaker_util.py``:

    ``agent_client.py`` is loaded *standalone* via ``importlib`` in both its
    unit suite (``tests/unit/test_circuit_breaker.py``) and its integration
    suite (``tests/integration/test_circuit_breaker.py``) precisely to AVOID
    triggering the heavy ``services/__init__.py`` (which drags in Docker,
    models, FastAPI). A ``from services.redis_breaker_util import ...`` in
    ``agent_client`` would re-trigger that package init and break both suites
    (IRON RULE R1). A top-level leaf module resolves against ``src/backend`` on
    ``sys.path`` in every context — prod, unit, integration — exactly like the
    ``config`` / ``database`` imports ``agent_client`` already relies on.

Keep this module a *leaf*: stdlib + ``redis`` only. ``REDIS_URL`` is imported
lazily inside ``get_breaker_redis`` so merely importing this module never pulls
in ``config``.
"""
from __future__ import annotations

import logging
import threading
import uuid
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

import redis as _redis

logger = logging.getLogger(__name__)


# ----- Shared fail-open Redis client (lazy, cached) -------------------------
#
# One client shared by every Redis-backed breaker. decode_responses=True so all
# breakers do string comparisons on Lua return values. Fail-open: if Redis is
# unreachable we return None and the caller falls through to allowing the
# request — the underlying failure (HTTP error, etc.) surfaces on its own.

_redis_client: Optional[_redis.Redis] = None
_redis_client_lock = threading.Lock()


def get_breaker_redis() -> Optional[_redis.Redis]:
    """Return the shared breaker Redis client, or None if Redis is unreachable.

    Mirrors the original ``agent_client._get_circuit_redis`` behaviour: cached,
    short connect/socket timeouts, fail-open on any error.
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    with _redis_client_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            from config import REDIS_URL
            client = _redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            client.ping()
            _redis_client = client
        except Exception as e:
            logger.warning("Breaker Redis unavailable (%s) — failing open", e)
            return None
    return _redis_client


def reset_breaker_redis_client() -> None:
    """Drop the cached client so the next call rebuilds. For tests + recovery."""
    global _redis_client
    with _redis_client_lock:
        _redis_client = None


# ----- Decode helpers -------------------------------------------------------
#
# decode_responses=True returns str, but older paths / direct byte clients may
# still hand back bytes — defensive normalisation shared by both breakers.

def decode_str(value: Any) -> Any:
    """Decode a single Lua return value to str when it arrives as bytes."""
    if isinstance(value, bytes):
        return value.decode()
    return value


def decode_pair(result: Any) -> Tuple[str, str]:
    """Lua MULTI return → (prior_state, new_state) as strings.

    Falls back to ('closed', 'closed') on a malformed / empty result so the
    caller never raises on a transition decode.
    """
    if not result or len(result) != 2:
        return ("closed", "closed")
    prior, new = result
    return (decode_str(prior), decode_str(new))


def lock_token_matches(current: Any, token: str) -> bool:
    """True when a GET on a SETNX-token lock key returned OUR token (#1919).

    The ownership comparison for the hand-rolled GET-then-DELETE releases that
    sit outside `SingleFlightLock` (whose renew/release compare inside a Lua
    script, trinity#4028). `get_breaker_redis` sets decode_responses=True, so a live
    read is always `str` — the bytes branch is belt-and-braces for a client
    configured without decoding, not a live path.
    """
    return current == token or current == token.encode()


# ----- Single-flight lock (#1920) -------------------------------------------
#
# ONE ownership-checked, fail-open single-flight lock, consolidating the SETNX
# idiom that was hand-rolled in seven sync sites with divergent (and in
# system_seed / cornelius / compat_fix, buggy — a constant-"1" value + a
# tokenless unconditional release could delete a *successor's* lock) behaviour.
#
# Renew and release are each ONE Lua script (trinity#4028): the token compare
# and the mutation run inside Redis, so a holder whose lease lapsed can never
# extend or delete its successor's lease. The GET-then-EXPIRE / GET-then-DELETE
# pair this replaced did exactly that whenever a successor won `SET NX` between
# the two commands. The async `ResumeLock` (`session_turn_service.py`) keeps
# its own Lua and stays a separate primitive: it runs on an async client and
# polls to acquire.


class LeaseState(str, Enum):
    """Outcome of `SingleFlightLock.refresh_if_owned`."""

    OWNED = "owned"            # still ours; lease TTL refreshed
    REACQUIRED = "reacquired"  # lapsed unclaimed and re-took it (same token);
    #                            only with `retake_lapsed=True`
    LOST = "lost"              # another token holds it, or (default) it lapsed — stop
    DEGRADED = "degraded"      # not held / Redis error / unreadable reply — no proof of loss


class SingleFlightLock:
    """A fail-open, ownership-checked single-flight Redis lock (#1920).

    Stateful (the per-acquire token must persist between acquire and release).
    The client is INJECTED — there is deliberately no `client_factory` default:
    every adopting site resolves `get_breaker_redis()` (or its own client) in
    ITS OWN module namespace and passes it here, because every existing lock
    test monkeypatches the SITE binding (`ops.get_breaker_redis`,
    `sss.get_breaker_redis`, `ephemeral_mod.get_breaker_redis`). A helper-owned
    factory would call `redis_breaker_util.get_breaker_redis` and silently
    no-op every one of those patches.

    Fail-open: a None client (Redis unreachable) or any Redis error at acquire
    behaves as the sole worker (`acquire` returns True, `held` stays False, so
    refresh/release become no-ops).

    Single-use state machine (total): `_held` is set exactly once on a real
    SETNX win and NEVER mutated afterwards — `refresh_if_owned` returning
    LOST/DEGRADED must not clear it, or a caller that keeps going after a
    Redis blip would abandon a live lock for the full TTL (the ops
    `ops:fleet_restart` 2100s-wedge class). All mutable state lives on the
    instance — no module-level state (pytest-randomly order-independence).
    The lease scripts are registered on the injected client at each call, not
    cached process-wide, for the same reason (trinity#4028).
    """

    def __init__(self, key: str, ttl_seconds: int, *, client: Optional[_redis.Redis]):
        self._key = key
        self._ttl = ttl_seconds
        self._client = client
        self._token = uuid.uuid4().hex   # unique per acquire — closes constant-"1"
        self._held = False               # True ONLY on a real SETNX win
        self._degraded = False           # True on fail-open (client None / error)

    @property
    def held(self) -> bool:
        """True only when this instance won the SETNX (a real lease is held)."""
        return self._held

    @property
    def token(self) -> str:
        return self._token

    def acquire(self) -> bool:
        """Try to take the lock. Returns True when the caller may proceed
        (real win OR fail-open sole-worker), False only when another holder has
        it (busy). Total: a second call while held/degraded returns True with
        no second SETNX."""
        if self._held or self._degraded:
            return True
        if self._client is None:
            self._degraded = True
            return True
        try:
            won = self._client.set(self._key, self._token, nx=True, ex=self._ttl)
        except Exception as e:  # noqa: BLE001 — fail-open: proceed unlocked
            logger.warning("single-flight lock %s acquire failed-open (%s)", self._key, e)
            self._degraded = True
            return True
        if won:
            self._held = True
            return True
        return False

    def refresh_if_owned(self, *, retake_lapsed: bool = False) -> LeaseState:
        """Renew our lease iff the stored token is still ours, in one script.

        OWNED: renewed. LOST: another token holds the key, or — by default —
        the key is gone (a lapsed lease is never re-taken: another holder may
        have worked inside the lapse, so a caller relying on exclusion stops).
        `retake_lapsed=True` re-takes an ABSENT key with our token in the same
        script (`SET NX`, so never over a successor) and answers REACQUIRED;
        LOST then means another token holds it. DEGRADED: no lease is held
        (`acquire` failed open), Redis errored, or the reply is not one of
        the script's integers. Silent, and never mutates `_held`, so the
        release below still runs.
        """
        if not self._held or self._client is None:
            return LeaseState.DEGRADED
        src = LEASE_REFRESH_OR_RETAKE_LUA if retake_lapsed else LEASE_REFRESH_LUA
        try:
            reply = self._client.register_script(src)(
                keys=[self._key], args=[self._token, self._ttl], client=self._client
            )
        except Exception:  # noqa: BLE001 — a Redis error is not proof of loss
            return LeaseState.DEGRADED
        code = _script_int(reply)
        if code == 1:
            return LeaseState.OWNED
        if code == 0:
            return LeaseState.LOST
        if code == 2 and retake_lapsed:
            return LeaseState.REACQUIRED
        return LeaseState.DEGRADED

    def release_if_owned(self) -> None:
        """Delete our lease iff the stored token is still ours, in one script.

        Gates on the STABLE acquire-time `_held` (never on a refresh result),
        so it runs after a LOST or DEGRADED refresh too, and deletes nothing it
        does not own. Never raises (the TTL expires a lease a failed release
        leaves behind).
        """
        if not self._held or self._client is None:
            return
        try:
            self._client.register_script(LEASE_RELEASE_LUA)(
                keys=[self._key], args=[self._token], client=self._client
            )
        except Exception:  # noqa: BLE001 — TTL will expire it
            pass


def _script_int(reply: Any) -> int | None:
    """A lease script's integer reply, or None when the reply is not one.

    Strict on purpose: `int(MagicMock())` is 1, so a coercing read would turn
    a client that never executed the script into a held lease.
    """
    if isinstance(reply, bool):
        return None
    if isinstance(reply, int):
        return reply
    if isinstance(reply, bytes):
        try:
            reply = reply.decode("ascii")
        except UnicodeDecodeError:
            return None
    # ASCII digits only: `str.isdigit` also accepts '²' and fullwidth digits,
    # which `int()` then rejects or reads, and a refresh must never raise.
    if isinstance(reply, str) and reply.isascii() and reply.isdigit():
        return int(reply)
    return None


# ----- Lease scripts (trinity#4028) ------------------------------------------
#
# The one home of the compare-and-expire / compare-and-delete lease scripts.
# `SingleFlightLock` and the canary leader lease (`canary_service`) both use
# them. KEYS[1] is the lease key, ARGV[1] the holder's token, ARGV[2] the TTL
# in seconds. Register them on the client you call them with
# (`client.register_script(SRC)`); that builds a `Script` locally (a sha1, no
# round-trip) and redis-py re-loads it on NOSCRIPT by itself.

LEASE_REFRESH_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

# Renew, or re-take an absent key with our token: 1 renewed, 2 re-taken,
# 0 another token holds it. `NX` is what keeps the re-take off a successor.
LEASE_REFRESH_OR_RETAKE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
if redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
    return 2
end
return 0
"""

LEASE_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


# ----- Lua script cache -----------------------------------------------------


class ScriptCache:
    """Lazily register a fixed set of Lua scripts on a client and cache the
    registered ``Script`` objects process-wide.

    One instance per breaker module (each breaker has its own Lua). Thread-safe
    double-checked init; ``reset()`` forces re-registration after a Redis
    reconnect (the shared client may have been dropped by
    ``reset_breaker_redis_client``).
    """

    def __init__(self, **sources: str):
        self._sources: Dict[str, str] = sources
        self._scripts: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    def ensure(self, client: _redis.Redis) -> Dict[str, Any]:
        """Return {name: registered Script}, registering on first use."""
        if self._scripts is None:
            with self._lock:
                if self._scripts is None:
                    self._scripts = {
                        name: client.register_script(src)
                        for name, src in self._sources.items()
                    }
        return self._scripts

    def reset(self) -> None:
        with self._lock:
            self._scripts = None


# ----- Fail-open wrapper ----------------------------------------------------


def fail_open(default: Any, op: Callable[[], Any], on_error: Optional[Callable[[], None]] = None) -> Any:
    """Run a Redis ``op``; on any exception log it, optionally run ``on_error``
    (e.g. ``reset_breaker_redis_client`` so the next call rebuilds), and return
    ``default``.

    Keeps breaker methods fail-open and quiet without repeating the same
    try/except at every call site.
    """
    try:
        return op()
    except Exception as e:
        logger.warning("breaker Redis op failed-open (%s)", e)
        if on_error is not None:
            try:
                on_error()
            except Exception:
                pass
        return default
