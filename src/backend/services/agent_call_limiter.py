"""
Backend agent-call budget limiter (#904 RC-1).

Bounds the fan-out of outbound agent HTTP calls from
`task_execution_service.agent_post_with_retry`. Two layered caps:

  * per-agent: how many concurrent backend coroutines may be mid-call
    to a single agent. Default = the agent's `max_parallel_tasks`,
    fallback 3.
  * global: how many concurrent agent calls the backend will hold
    across all agents. Default = `BACKEND_AGENT_CALL_LIMIT` env (8).

Why this exists: every `await` on `httpx.post(agent_url, ...)` is
intermixed with **synchronous** `sqlite3` calls in the surrounding
`task_execution_service.execute_task` (see `db/connection.py:18`).
Sync DB inside an async coroutine stalls the event loop for the
duration of the call — fine for a single short write, but with N
parallel long-running agent calls each emitting periodic
`mark_execution_dispatched` / `log_activity` / `update_execution_status`
writes, the SQLite writer lock + GIL serialise the writes and
starve unrelated handlers (dashboard, healthcheck). The end state
seen in #904: a single misbehaving agent's 11.5-min HTTP call
drove all backend coroutines into sync-DB contention long enough
that the Docker healthcheck (10s) flipped the container to
`unhealthy` and the dashboard's parallel API fan-out timed out.

This module does NOT fix the underlying sync-DB problem — it bounds
how much concurrent agent work the backend will accept so the
event-loop stalls stay short enough that healthcheck + dashboard
keep responding. The proper sync→async-DB migration is a separate
follow-up.

Public API
----------
* ``acquire_agent_call_slot(agent_name)`` — async context manager
* ``BackendAgentCallBudgetExhausted`` — raised when acquire times out

Tunables (env)
--------------
* ``BACKEND_AGENT_CALL_LIMIT`` — int, default 8
* ``BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S`` — float, default 30
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


# Read once at import. Production never touches them after boot;
# tests use `_reset_for_testing()` to re-create the primitives with
# different bounds.
BACKEND_AGENT_CALL_LIMIT: int = int(os.getenv("BACKEND_AGENT_CALL_LIMIT", "8"))

# Queue-acquire timeout. Default 3600s (1 hour) — matches the
# platform-wide max `execution_timeout_seconds` (TIMEOUT-001 ceiling
# is 7200s, default 3600s, #665) so any task that would have
# eventually succeeded pre-#904 still succeeds: pre-fix the worst
# wall-clock was the agent timeout (max ~610s by default), and the
# queue wait is on top of that, so 3600s leaves a generous margin
# even under sustained backlog.
#
# Why we keep a finite cap instead of "wait forever":
# agent-to-agent chat chains (chat_with_agent MCP tool, X→Y→Z
# collaborations) can deadlock when concurrent chains exceed the
# global semaphore: each chain holds slots for its outer caller
# while waiting on the next-hop call which itself wants a slot.
# A finite timeout surfaces such a deadlock as a 503 within an
# hour, lets the queue drain, and keeps the system unstuck.
# Setting this to 0 disables the cap — explicitly opt-in only.
BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S: float = float(
    os.getenv("BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S", "3600")
)


class BackendAgentCallBudgetExhausted(Exception):
    """Raised when an outbound agent HTTP call can't be admitted within
    ``BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S``. The caller should translate
    to HTTP 503 — the work was rejected at the backend before any
    Claude subprocess started, so retrying after backoff is safe."""

    def __init__(
        self, agent_name: str, agent_cap: int, global_cap: int, wait_ms: int,
    ):
        self.agent_name = agent_name
        self.agent_cap = agent_cap
        self.global_cap = global_cap
        self.wait_ms = wait_ms
        super().__init__(
            f"Backend call budget exhausted for {agent_name} after {wait_ms}ms "
            f"(agent_cap={agent_cap}, global_cap={global_cap})"
        )


_GLOBAL_AGENT_CALL_SEM: Optional[asyncio.Semaphore] = None
_AGENT_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_AGENT_SEMAPHORES_LOCK: Optional[asyncio.Lock] = None


def _ensure_globals() -> None:
    """Lazily create the global primitives on the running event loop.

    Module-import-time instantiation would bind the semaphores to
    whatever loop happened to be current — fine in production
    (uvicorn's single loop) but fragile under pytest's per-test loops.
    """
    global _GLOBAL_AGENT_CALL_SEM, _AGENT_SEMAPHORES_LOCK
    if _GLOBAL_AGENT_CALL_SEM is None:
        _GLOBAL_AGENT_CALL_SEM = asyncio.Semaphore(BACKEND_AGENT_CALL_LIMIT)
    if _AGENT_SEMAPHORES_LOCK is None:
        _AGENT_SEMAPHORES_LOCK = asyncio.Lock()


async def _get_agent_sem(agent_name: str) -> tuple[asyncio.Semaphore, int]:
    """Return ``(per_agent_semaphore, cap)`` — lazily created.

    The cap is read from ``db.get_max_parallel_tasks(agent_name)`` on
    first access. Unknown agents (deleted, or under unit-test stubs)
    fall back to 3.
    """
    _ensure_globals()
    sem = _AGENT_SEMAPHORES.get(agent_name)
    if sem is not None:
        return sem, _AGENT_SEMAPHORE_CAPS.get(agent_name, 3)

    cap = 3
    try:
        # Local import so the limiter is unit-testable without the heavy
        # `database` module init.
        # #506: facade bypass — read the effective cap (stored clamped to the
        # fleet ceiling). NOTE (documented limitation): the cap is frozen in
        # `_AGENT_SEMAPHORE_CAPS` at first access and never re-read, so a live
        # agent's semaphore does not shrink when the ceiling (or its own
        # max_parallel_tasks) drops until process restart. New agents get the
        # clamped cap immediately. Semaphore-resize machinery is out of scope.
        from services.settings_service import (  # noqa: WPS433 — local on purpose
            get_effective_max_parallel_tasks,
        )
        actual = get_effective_max_parallel_tasks(agent_name)
        if isinstance(actual, int) and actual > 0:
            cap = actual
    except Exception:  # pragma: no cover — defensive, unit tests stub `database`
        pass

    assert _AGENT_SEMAPHORES_LOCK is not None
    async with _AGENT_SEMAPHORES_LOCK:
        sem = _AGENT_SEMAPHORES.get(agent_name)
        if sem is None:
            sem = asyncio.Semaphore(cap)
            _AGENT_SEMAPHORES[agent_name] = sem
            _AGENT_SEMAPHORE_CAPS[agent_name] = cap
    return sem, cap


# Companion dict so the cap survives lookups for the log line.
_AGENT_SEMAPHORE_CAPS: dict[str, int] = {}


async def _acquire_with_optional_timeout(
    sem: asyncio.Semaphore,
    agent_name: str,
    where: str,
    agent_cap: int,
    global_cap: int,
    t0: float,
) -> None:
    """Acquire ``sem``. If ``BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S`` is
    > 0, enforce it and raise ``BackendAgentCallBudgetExhausted`` on
    timeout. If 0 (the default), wait indefinitely — preserves the
    pre-#904 semantics that calls which would have eventually
    succeeded still do, just at higher latency under congestion. Also
    surfaces a one-shot "queued > 5s" warning so operators can see
    when the cap is actually biting.

    ``where`` is "per-agent" or "global" for the log line. ``t0`` is
    the monotonic timestamp captured before the first acquire so
    `wait_ms` reflects total queue wait, not just this call.
    """
    timeout = BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S
    if timeout > 0:
        try:
            await asyncio.wait_for(sem.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            wait_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                f"[TaskExecService] Backend call budget exhausted ({where}) for "
                f"{agent_name} after {wait_ms}ms "
                f"(agent_cap={agent_cap}, global_cap={global_cap})"
            )
            raise BackendAgentCallBudgetExhausted(
                agent_name, agent_cap, global_cap, wait_ms,
            )
        return

    # Timeout disabled (default). Wait forever — never fail a caller
    # that would have eventually succeeded pre-fix.
    if not sem.locked():
        # Fast path: a slot is immediately available, skip the
        # monitoring task. `Semaphore.locked()` returns False when
        # the internal counter is > 0, i.e. acquire() would not
        # block. (Don't trust this on its own — race with another
        # awaiter — so we still `acquire()` below; the check just
        # gates the warning task spawn for the hot path.)
        await sem.acquire()
        return

    # Slow path: at least one task is already queued ahead of us.
    # Spawn a one-shot warning timer so a sustained queue surfaces in
    # Vector logs without spamming every wait. Cancelled on acquire.
    warning_task = asyncio.create_task(
        _log_long_queue_wait(agent_name, where, agent_cap, global_cap, t0)
    )
    try:
        await sem.acquire()
    finally:
        warning_task.cancel()


async def _log_long_queue_wait(
    agent_name: str,
    where: str,
    agent_cap: int,
    global_cap: int,
    t0: float,
) -> None:
    """Warn once if a queue wait exceeds 5 seconds. Cancelled by the
    parent acquire when the slot is granted."""
    try:
        await asyncio.sleep(5.0)
        waited_ms = int((time.monotonic() - t0) * 1000)
        logger.warning(
            f"[TaskExecService] Agent-call queue wait > 5s ({where}) for "
            f"{agent_name} (waited={waited_ms}ms, "
            f"agent_cap={agent_cap}, global_cap={global_cap}) — backend "
            f"under sustained pressure"
        )
    except asyncio.CancelledError:
        pass  # acquired in time — no warning needed


@contextlib.asynccontextmanager
async def acquire_agent_call_slot(agent_name: str):
    """Acquire per-agent + global slots for an outbound agent HTTP call.

    Acquire semantics depend on ``BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S``:

    * **= 0** (default): wait indefinitely. The semaphore queues
      callers; the caller's HTTP connection stays open the whole
      time. Behavior matches pre-#904 except that fan-out is
      bounded — any call that would have eventually succeeded
      still does. A one-shot warning fires at 5s queue wait for
      observability.
    * **> 0**: enforce the timeout. Acquire failures raise
      ``BackendAgentCallBudgetExhausted`` (caller translates to
      HTTP 503). Opt-in only — for operators who want a hard
      ceiling on queue wait and accept that some
      previously-successful calls now 503.

    On successful entry, releases both semaphores on context exit
    (including the exception path).
    """
    _ensure_globals()
    agent_sem, agent_cap = await _get_agent_sem(agent_name)
    assert _GLOBAL_AGENT_CALL_SEM is not None
    global_sem = _GLOBAL_AGENT_CALL_SEM
    global_cap = BACKEND_AGENT_CALL_LIMIT

    t0 = time.monotonic()

    # Per-agent first — bounds blast radius per agent before charging
    # against the global pool. Order matters for fairness: a single
    # bursty agent can't acquire the global slot before its per-agent
    # cap rejects it.
    await _acquire_with_optional_timeout(
        agent_sem, agent_name, "per-agent", agent_cap, global_cap, t0,
    )

    try:
        await _acquire_with_optional_timeout(
            global_sem, agent_name, "global", agent_cap, global_cap, t0,
        )

        try:
            wait_ms = int((time.monotonic() - t0) * 1000)
            logger.debug(
                f"[TaskExecService] Acquired agent-call slot for {agent_name} "
                f"(wait={wait_ms}ms, agent_cap={agent_cap}, global_cap={global_cap})"
            )
            yield
        finally:
            global_sem.release()
    finally:
        agent_sem.release()


def _reset_for_testing(
    global_limit: Optional[int] = None,
    queue_timeout_s: Optional[float] = None,
) -> None:
    """Reset module-level state. Test-only — not part of the public API.

    Call from a fixture's setup phase to get a fresh global semaphore
    and per-agent dict bound to the current test's event loop.
    """
    global BACKEND_AGENT_CALL_LIMIT, BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S
    global _GLOBAL_AGENT_CALL_SEM, _AGENT_SEMAPHORES, _AGENT_SEMAPHORES_LOCK
    global _AGENT_SEMAPHORE_CAPS

    if global_limit is not None:
        BACKEND_AGENT_CALL_LIMIT = global_limit
    if queue_timeout_s is not None:
        BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S = queue_timeout_s

    _GLOBAL_AGENT_CALL_SEM = None
    _AGENT_SEMAPHORES_LOCK = None
    _AGENT_SEMAPHORES = {}
    _AGENT_SEMAPHORE_CAPS = {}
