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

    The single ownership comparison for hand-rolled single-flight locks
    (refresh and compare-and-delete release must use the same predicate or
    they drift). `get_breaker_redis` sets decode_responses=True, so a live
    read is always `str` — the bytes branch is belt-and-braces for a client
    configured without decoding, not a live path.
    """
    return current == token or current == token.encode()


# ----- Single-flight lock (#1920) -------------------------------------------
#
# ONE ownership-checked, fail-open single-flight lock, consolidating the SETNX
# idiom that was hand-rolled in five sync sites with divergent (and in
# system_seed's case, buggy — a tokenless unconditional release could delete a
# *successor's* lock) behaviour. Reuses `lock_token_matches` as the ownership
# predicate. Release is GET-then-DELETE compare-and-delete — deliberately NOT
# atomic-Lua: it stays fakeredis-unit-testable and ships the same accepted
# GET→DELETE TOCTOU window `ops.py` already carried. The async `ResumeLock`
# (`session_turn_service.py`) keeps its atomic Lua compare-and-delete and is a
# deliberately-separate, stronger primitive — do NOT merge ResumeLock onto this.


class LeaseState(str, Enum):
    """Outcome of `SingleFlightLock.refresh_if_owned` (only ops.py refreshes)."""

    OWNED = "owned"           # still ours; lease TTL refreshed
    REACQUIRED = "reacquired"  # lease had lapsed unclaimed; re-took it (same token)
    LOST = "lost"             # foreign holder OR lost the re-acquire race — stop
    DEGRADED = "degraded"     # Redis error / not held — fail open, proceed


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
    LOST/DEGRADED must not clear it, or a caller that reclaims its own lock
    after a stale-replica false-foreign read would abandon a live lock for the
    full TTL (the ops `ops:fleet_restart` 2100s-wedge class). All mutable state
    lives on the instance — no module-level state (pytest-randomly order-
    independence).
    """

    def __init__(self, key: str, ttl_seconds: int, *, client: Optional[_redis.Redis]):
        self._key = key
        self._ttl = ttl_seconds
        self._client = client
        self._token = uuid.uuid4().hex   # unique per acquire — closes constant-"1"
        self._held = False               # True ONLY on a real SETNX win
        self._degraded = False           # True on fail-open (client None / error)
        self._last_current = None        # last GET value → caller's foreign/absent WORD

    @property
    def held(self) -> bool:
        """True only when this instance won the SETNX (a real lease is held)."""
        return self._held

    @property
    def token(self) -> str:
        return self._token

    @property
    def last_current(self):
        """The last value a refresh GET saw — for the caller's foreign (not
        None) vs absent (None) log WORD. Never the raw token value itself."""
        return self._last_current

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

    def refresh_if_owned(self) -> LeaseState:
        """Refresh our own lease before the next protected action. Silent (no
        logging) — the sole caller (ops.py) owns its own warn-once. NEVER
        mutates `_held`."""
        if not self._held or self._client is None:
            return LeaseState.DEGRADED
        try:
            current = self._client.get(self._key)
            self._last_current = current
            owned = lock_token_matches(current, self._token)
            if owned and self._client.expire(self._key, self._ttl):
                return LeaseState.OWNED
            # Not owned, OR owned-but-the-key-vanished-in-the-GET→EXPIRE-sliver
            # (EXPIRE falsy creates nothing): a vanished key is 'absent' and the
            # same-token SETNX can re-take it; a live foreign token is real loss.
            if owned:
                current = None
                self._last_current = None
            if current is None:
                if self._client.set(self._key, self._token, nx=True, ex=self._ttl):
                    return LeaseState.REACQUIRED
                return LeaseState.LOST
            return LeaseState.LOST
        except Exception:  # noqa: BLE001 — a Redis blip is NOT lease loss
            return LeaseState.DEGRADED

    def release_if_owned(self) -> None:
        """Compare-and-delete release. Gates on the STABLE acquire-time `_held`
        (never on a refresh result), so it runs even after a detected LOST and
        is foreign-safe by construction (GET-compare, deletes only on a token
        match). Never raises."""
        if not self._held or self._client is None:
            return
        try:
            if lock_token_matches(self._client.get(self._key), self._token):
                self._client.delete(self._key)
        except Exception:  # noqa: BLE001 — TTL will expire it
            pass


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
