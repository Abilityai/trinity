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
