"""The per-agent httpx client pool and the connection-drop grace stamps.

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

_client_pool: Dict[str, httpx.AsyncClient] = {}

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


