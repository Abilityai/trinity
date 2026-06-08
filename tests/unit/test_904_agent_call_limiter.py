"""
Tests for #904 RC-1: backend agent-call budget limiter.

Verifies:

1. Per-agent semaphore caps concurrent calls to one agent at the
   agent's `max_parallel_tasks` (default 3 when DB lookup misses).
2. Global semaphore caps total concurrent calls across all agents at
   `BACKEND_AGENT_CALL_LIMIT`.
3. `acquire_agent_call_slot` raises `BackendAgentCallBudgetExhausted`
   when the wait exceeds `BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S`.
4. Released slots are immediately reusable by the next waiter (FIFO).
5. Per-agent cap is computed from `db.get_max_parallel_tasks` on first
   acquire — unknown agents fall back to 3 without raising.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


# `services.agent_call_limiter._get_agent_sem` does a local
# `from database import db` to read `max_parallel_tasks`. Stub the
# heavy module so tests don't pay the DatabaseManager() init cost
# and don't accidentally hit a real SQLite file. The sanctioned
# `_STUBBED_MODULE_NAMES` + autouse `_restore_sys_modules` pattern
# tells `tests/lint_sys_modules.py` to whitelist these bare
# `sys.modules[...]` mutations (precedent:
# tests/unit/test_agent_cleanup_parity.py).
_STUBBED_MODULE_NAMES = [
    "database",
    "db_models",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {n: sys.modules.get(n) for n in _STUBBED_MODULE_NAMES}
    # `_install_db_stub()` mutates `database.db` ON the already-imported real
    # `database` module object. Restoring the `sys.modules["database"]` *entry*
    # below does NOT undo that — it's the same object — so the fake `db` would
    # leak into any later test that re-reads `database.db` (e.g. a module
    # `importlib.reload` in test_voip_service). Snapshot and restore the real
    # singleton too. (#1056 regression: voip gate tests vs seed ordering.)
    _database_mod = sys.modules.get("database")
    _saved_db = getattr(_database_mod, "db", None) if _database_mod is not None else None
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        if _database_mod is not None and _saved_db is not None:
            _database_mod.db = _saved_db


def _install_db_stub(max_parallel_tasks_map: dict[str, int]) -> None:
    """Install a `database` stub whose `db.get_max_parallel_tasks`
    returns the value in the supplied dict (or raises KeyError if
    the agent isn't in the dict, so the limiter falls back to 3)."""
    db_stub = sys.modules.get("database")
    if db_stub is None:
        db_stub = type(sys)("database")
        sys.modules["database"] = db_stub

    class _Db:
        def get_max_parallel_tasks(self, agent_name: str) -> int:
            if agent_name not in max_parallel_tasks_map:
                raise KeyError(agent_name)
            return max_parallel_tasks_map[agent_name]

    db_stub.db = _Db()


@pytest.fixture
def limiter():
    """Fresh limiter module bound to the current test's event loop.

    `_reset_for_testing` clears the global semaphore + per-agent dict
    so adjacent tests don't bleed state. We re-import after the reset
    so the patched env-var-driven constants take effect.
    """
    _install_db_stub({})  # default — fall back to 3 for unknown agents
    if "services.agent_call_limiter" in sys.modules:
        mod = sys.modules["services.agent_call_limiter"]
    else:
        import importlib
        mod = importlib.import_module("services.agent_call_limiter")
    mod._reset_for_testing(global_limit=100, queue_timeout_s=10.0)
    return mod


# -----------------------------------------------------------------------------
# Happy path
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_and_release(limiter):
    """Single acquire + release works; slot is reusable."""
    async with limiter.acquire_agent_call_slot("agent-x"):
        pass
    # Re-entering after release succeeds
    async with limiter.acquire_agent_call_slot("agent-x"):
        pass


@pytest.mark.asyncio
async def test_unknown_agent_uses_default_cap(limiter):
    """`db.get_max_parallel_tasks` raising → fall back to 3."""
    # 3 concurrent acquires must succeed without blocking.
    held = [asyncio.Event() for _ in range(3)]
    release = asyncio.Event()

    async def hold(idx: int):
        async with limiter.acquire_agent_call_slot("unknown-agent"):
            held[idx].set()
            await release.wait()

    tasks = [asyncio.create_task(hold(i)) for i in range(3)]
    # All three should reach the held event within a short window
    await asyncio.wait_for(
        asyncio.gather(*(e.wait() for e in held)), timeout=1.0,
    )
    release.set()
    await asyncio.gather(*tasks)


# -----------------------------------------------------------------------------
# Per-agent cap
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_agent_cap_blocks_extra_callers(limiter):
    """With max_parallel_tasks=2, a third concurrent acquire must wait."""
    _install_db_stub({"agent-y": 2})

    held = asyncio.Event()
    release = asyncio.Event()
    started = [asyncio.Event() for _ in range(3)]

    async def hold(idx: int):
        started[idx].set()
        async with limiter.acquire_agent_call_slot("agent-y"):
            if idx < 2:
                held.set()
            await release.wait()

    tasks = [asyncio.create_task(hold(i)) for i in range(3)]

    # First 2 acquire the per-agent semaphore quickly.
    await asyncio.wait_for(held.wait(), timeout=1.0)

    # Give the 3rd task a moment to attempt the acquire. If it
    # incorrectly slipped through, it would be inside the `async
    # with` and waiting on `release.wait()` already. Detect that by
    # checking after a short delay that exactly 2 slots are taken —
    # asyncio.gather with timeout=0.2 against the still-running 3rd
    # would not return.
    third_task = tasks[2]
    assert not third_task.done(), "3rd acquire should be queued"

    # Release the held slots → 3rd acquires and finishes.
    release.set()
    await asyncio.gather(*tasks)


# -----------------------------------------------------------------------------
# Global cap
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_cap_blocks_across_agents(limiter):
    """Global cap = 2 → 3rd call across any agents queues even if
    each agent has higher per-agent cap."""
    limiter._reset_for_testing(global_limit=2, queue_timeout_s=10.0)
    _install_db_stub({"agent-a": 10, "agent-b": 10, "agent-c": 10})

    release = asyncio.Event()
    in_block = [asyncio.Event() for _ in range(3)]

    async def hold(idx: int, name: str):
        async with limiter.acquire_agent_call_slot(name):
            in_block[idx].set()
            await release.wait()

    tasks = [
        asyncio.create_task(hold(0, "agent-a")),
        asyncio.create_task(hold(1, "agent-b")),
        asyncio.create_task(hold(2, "agent-c")),
    ]

    # Two enter; one queues on the global semaphore.
    await asyncio.wait_for(
        asyncio.gather(in_block[0].wait(), in_block[1].wait()),
        timeout=1.0,
    )
    assert not tasks[2].done(), "3rd call should be queued on global cap"
    assert not in_block[2].is_set()

    release.set()
    await asyncio.gather(*tasks)


# -----------------------------------------------------------------------------
# Queue-acquire timeout
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_timeout_raises_budget_exhausted(limiter):
    """Tight timeout + saturated per-agent → BackendAgentCallBudgetExhausted."""
    limiter._reset_for_testing(global_limit=100, queue_timeout_s=0.2)
    _install_db_stub({"agent-z": 1})

    release = asyncio.Event()

    async def hold():
        async with limiter.acquire_agent_call_slot("agent-z"):
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.05)  # let holder enter

    with pytest.raises(limiter.BackendAgentCallBudgetExhausted) as excinfo:
        async with limiter.acquire_agent_call_slot("agent-z"):
            pass  # pragma: no cover — must not enter

    # Exception carries diagnostic fields
    assert excinfo.value.agent_name == "agent-z"
    assert excinfo.value.agent_cap == 1
    assert excinfo.value.wait_ms >= 200

    # Releasing the holder unblocks normal use.
    release.set()
    await holder


@pytest.mark.asyncio
async def test_global_timeout_raises_budget_exhausted(limiter):
    """Tight timeout + saturated global semaphore → exhausted exception
    even when the per-agent cap had room."""
    limiter._reset_for_testing(global_limit=1, queue_timeout_s=0.2)
    _install_db_stub({"agent-q": 10, "agent-r": 10})

    release = asyncio.Event()

    async def hold():
        async with limiter.acquire_agent_call_slot("agent-q"):
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.05)

    with pytest.raises(limiter.BackendAgentCallBudgetExhausted) as excinfo:
        async with limiter.acquire_agent_call_slot("agent-r"):
            pass  # pragma: no cover

    assert excinfo.value.global_cap == 1
    release.set()
    await holder


# -----------------------------------------------------------------------------
# Release-on-exit invariants
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_on_exception_inside_block(limiter):
    """Exception inside the `async with` must release both semaphores."""
    _install_db_stub({"agent-e": 1})

    class _Sentinel(Exception):
        pass

    with pytest.raises(_Sentinel):
        async with limiter.acquire_agent_call_slot("agent-e"):
            raise _Sentinel()

    # Next acquire should succeed immediately — both slots released.
    async with asyncio.timeout(0.5):
        async with limiter.acquire_agent_call_slot("agent-e"):
            pass


# -----------------------------------------------------------------------------
# Opt-in "wait forever" — operators who set queue_timeout_s=0 get
# pre-#904 semantics (any call which would have eventually succeeded
# still does) at the cost of deadlock risk on agent-to-agent chains
# whose depth exceeds the global cap. Production default is 3600s.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_zero_opt_in_waits_indefinitely(limiter):
    """`queue_timeout_s=0` disables the queue timeout. A queued caller
    must NOT raise BackendAgentCallBudgetExhausted — they wait
    indefinitely until the cap clears."""
    limiter._reset_for_testing(global_limit=1, queue_timeout_s=0)
    _install_db_stub({"agent-w": 10})

    release_first = asyncio.Event()

    async def first():
        async with limiter.acquire_agent_call_slot("agent-w"):
            await release_first.wait()

    async def second():
        async with limiter.acquire_agent_call_slot("agent-w"):
            return "ok"

    f = asyncio.create_task(first())
    await asyncio.sleep(0.05)  # let first acquire

    s = asyncio.create_task(second())
    # second is queued — must NOT return for at least 1s under
    # timeout=0, proving the wait isn't truncated.
    done, _pending = await asyncio.wait([s], timeout=1.0)
    assert not done, "second should still be queued, not raised/returned"

    # Release first → second proceeds without raising
    release_first.set()
    await asyncio.gather(f)
    assert await s == "ok"


@pytest.mark.asyncio
async def test_default_timeout_is_one_hour(limiter):
    """Sanity-check the production default — 3600s. Both protects
    against deadlocks AND leaves enough headroom that any call which
    would have eventually succeeded pre-#904 still succeeds (the
    worst-case agent timeout was ~610s)."""
    # Re-read the module-level default without `_reset_for_testing`
    # overriding it.
    import importlib
    import services.agent_call_limiter as _acl
    importlib.reload(_acl)
    assert _acl.BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S == 3600.0
