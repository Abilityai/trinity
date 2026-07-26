"""Single Redis target resolver for ``tests/integration`` (#1775).

Before this file existed, five integration modules each resolved Redis for
themselves at **module import** time — i.e. during pytest *collection*, before
any test ran. One of them (``test_monitoring_service.py``) wrote
``os.environ["REDIS_URL"]`` **unconditionally**, so collecting the directory
overwrote whatever target the harness had supplied, for the whole session. The
other four then found an unreachable Redis and *skipped* — and pytest still
exited 0, so a caller scoring the suite on its exit code read green while ~7% of
it ran. That is the bug.

Two mechanisms had to be handled, not one:

**M1 — the clobber.** The unconditional write, now deleted. Guarding it is not
enough: a module that ERRORs later in its import has already executed the write,
so the write must not exist at import time at all.

**M2 — the root-conftest sentinels.** ``tests/conftest.py`` (#589) installs
``REDIS_URL=redis://test:test@redis:6379`` plus ``"test"`` passwords at global
import, then preloads backend modules — and ``src/backend/config.py`` snapshots
``REDIS_URL = os.getenv("REDIS_URL", "")`` at *module scope*. Three consequences
this resolver has to defeat:

1. ``"REDIS_URL" in os.environ`` is **always** true by the time any child
   conftest runs, so it cannot discriminate "the harness told us" from "the root
   conftest defaulted". ``conftest.CALLER_SUPPLIED_REDIS_URL`` — captured before
   the setdefault — is the honest signal.
2. ``setdefault`` on the passwords is a no-op once ``"test"`` is in place; the
   sentinel must be **deleted** before real ``.env`` values can be overlaid.
   ``tests/security/conftest.py`` (#804) already solved exactly this — this file
   mirrors that pattern rather than inventing a second one.
3. ``config.REDIS_URL`` is already frozen to the dummy, so re-pointing
   ``os.environ`` alone does nothing. We re-point the imported module object and
   reset the cached breaker Redis client so it rebuilds against the real target.

**Reachability policy (the durability item, reporter's suggested fix #3).**

    If the harness told us where Redis is, unreachable = FAIL.
    If we had to derive the target ourselves, unreachable = SKIP.

A silent skip is lost coverage reported as success; a hard failure on a bare
developer machine with no Redis is a false red. The discriminator above is what
lets us have both.

Environment:
- ``REDIS_URL``  — export it and an unreachable Redis becomes a hard failure.
  This is what ``verify-local`` and ``tests/run-integration.sh`` do.
- ``TRINITY_REDIS_REQUIRED`` — ``1``/``true``/``yes``/``on`` forces the same
  fail-don't-skip behaviour without pinning a URL (useful in CI that starts
  Redis on the documented default port). This is the ONLY knob this file adds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import redis as _redis

# Captured by the ROOT conftest *before* it installs its sentinels — the only
# place in the session that can distinguish caller intent from the dummy.
from conftest import (  # type: ignore[import-not-found]
    CALLER_SUPPLIED_REDIS_URL,
    ROOT_REDIS_PASSWORD_SENTINEL,
)

from integration.redis_target import (  # type: ignore[import-not-found]
    LOCAL_REDIS_ENDPOINT,
    build_redis_url,
    mask_redis_url,
)

try:
    from dotenv import dotenv_values  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — python-dotenv is in requirements-test
    dotenv_values = None  # type: ignore[assignment]

_REPO = Path(__file__).resolve().parents[2]
_DOTENV = _REPO / ".env"

# Accepted spellings for TRINITY_REDIS_REQUIRED, matching the idiom the root
# conftest already uses for TRINITY_TEST_CLEANUP_SWEEP.
_TRUTHY = ("1", "true", "yes", "on")


# ── resolution ──────────────────────────────────────────────────────────────


def _overlay_real_redis_passwords() -> None:
    """Clear the root ``"test"`` sentinels, then overlay real ``.env`` values.

    Mirrors ``tests/security/conftest.py`` (#804). One deliberate difference: we
    only fill a key that is *absent* after the sentinel sweep, so an explicit
    caller export (``tests/setup-env.sh`` exports ``REDIS_BACKEND_PASSWORD``)
    still wins over ``.env``.
    """
    for key in ("REDIS_PASSWORD", "REDIS_BACKEND_PASSWORD"):
        if os.environ.get(key) == ROOT_REDIS_PASSWORD_SENTINEL:
            del os.environ[key]

    if dotenv_values is None or not _DOTENV.exists():
        return
    values = dotenv_values(_DOTENV)
    for key in ("REDIS_PASSWORD", "REDIS_BACKEND_PASSWORD"):
        value = values.get(key)
        if value and key not in os.environ:
            os.environ[key] = value


def _resolve_redis_target() -> tuple[str | None, bool, str]:
    """Return ``(url_or_None, required, derivation)``."""
    forced = os.environ.get("TRINITY_REDIS_REQUIRED", "").strip().lower() in _TRUTHY

    if CALLER_SUPPLIED_REDIS_URL:
        # The caller pinned a target before pytest started, so the root
        # conftest's setdefault was a no-op and this value is theirs.
        return os.environ["REDIS_URL"], True, "caller-supplied REDIS_URL"

    _overlay_real_redis_passwords()
    password = os.environ.get("REDIS_BACKEND_PASSWORD")
    if not password or password == ROOT_REDIS_PASSWORD_SENTINEL:
        return (
            None,
            forced,
            "no REDIS_BACKEND_PASSWORD in the environment or .env",
        )
    url = build_redis_url(password)
    return url, forced, f"derived from .env + {LOCAL_REDIS_ENDPOINT}"


def _apply_resolved_target(url: str) -> None:
    """Make the resolved URL the one truth for env, ``config``, and the breakers.

    Re-pointing ``sys.modules["config"].REDIS_URL`` is unavoidable: the root
    conftest imports ``config`` (via ``services.agent_client``) while the dummy
    is still in force, and ``config.py:85`` snapshots the value at module scope.
    ``redis_breaker_util.get_breaker_redis`` re-reads ``config.REDIS_URL`` on
    each rebuild, so re-point + reset is sufficient — and it centralises the
    ``_reset_circuit_redis_client()`` call the five modules were each making for
    this same reason.
    """
    os.environ["REDIS_URL"] = url

    config_module = sys.modules.get("config")
    if config_module is not None:
        config_module.REDIS_URL = url

    # Drop any client cached against the stale URL. Deliberately not wrapped in
    # try/except: a failure here would silently leave every breaker assertion
    # reading a fail-open zero, which is the exact class of bug #1775 is about.
    breaker_util = sys.modules.get("redis_breaker_util")
    if breaker_util is not None:
        breaker_util.reset_breaker_redis_client()


REDIS_URL, REDIS_REQUIRED, REDIS_DERIVATION = _resolve_redis_target()
if REDIS_URL:
    _apply_resolved_target(REDIS_URL)


# ── fixtures ────────────────────────────────────────────────────────────────


def _unreachable(reason: str) -> None:
    """FAIL when a target was declared, SKIP when we had to guess one."""
    if REDIS_REQUIRED:
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def redis_required() -> bool:
    """True when an unreachable Redis must fail the run rather than skip it."""
    return REDIS_REQUIRED


@pytest.fixture(scope="session")
def redis_url() -> str:
    """The resolved Redis URL. Skips/fails when no target could be resolved."""
    if not REDIS_URL:
        _unreachable(f"No Redis target: {REDIS_DERIVATION}")
    return REDIS_URL


@pytest.fixture(scope="session")
def redis_client(redis_url: str):
    """A live Redis client, shared by every integration module.

    Replaces five per-module clients that each re-derived the target. On a
    connection failure the reachability policy above decides fail vs skip.

    Two deliberate shapes here, both about keeping the URL out of the report:

    1. ``from_url`` is INSIDE the ``try``. It can raise on its own (bad scheme,
       unparseable port), and outside the block that error would escape both
       guarantees this file provides — it would bypass ``mask_redis_url`` and
       bypass the fail-vs-skip policy, erroring a bare developer run the derived
       path is supposed to skip.
    2. ``_unreachable`` is called AFTER the ``except`` block, not inside it.
       ``pytest.fail(..., pytrace=False)`` suppresses its own traceback but NOT
       an implicitly chained one: raising inside ``except`` makes pytest render
       "During handling of the above exception..." plus the original frames —
       and for a URL *parse* error those frames hold the unmasked URL as a local
       (``ParseResult(netloc='backend:<password>@...')``). Deferring the raise
       until the handler has exited clears the chain, so only the masked message
       is reported. Verified: a malformed ``REDIS_URL`` leaked the password into
       pytest output before this, and does not now.
    """
    client = None
    failure: str | None = None
    try:
        client = _redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
    except Exception as e:  # noqa: BLE001 — construct/connect/auth all mean "unusable"
        # Only the exception TYPE and TEXT survive; neither carries the password
        # for any error redis-py or urllib raises here.
        failure = f"{type(e).__name__}: {e}"
        if client is not None:
            client.close()
            client = None

    if failure is not None:
        _unreachable(
            f"Redis unusable at {mask_redis_url(redis_url)} "
            f"({REDIS_DERIVATION}): {failure}"
        )
    yield client
    client.close()
