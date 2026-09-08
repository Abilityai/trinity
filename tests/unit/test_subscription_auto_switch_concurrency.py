"""Concurrency tests for SUB-003 auto-switch (#799).

Pins the per-agent switch lock + stale-failure guard added in
``services/subscription_auto_switch.py``. These are the race the carve-out
test ``test_subscription_auto_switch_no_cred_import.py`` explicitly left out
of scope ("Explicit non-claim: this is NOT a concurrency test").

Bug being fixed: two subscription failures on the SAME agent (two chat
requests, or a chat overlapping a scheduled task) both ran the
read→decide→assign→restart sequence with no mutual exclusion, so both could
pick the same alternative, both ``assign_subscription_to_agent``, and both
fire ``_restart_agent`` — wedging the container / duplicating the switch.

What's pinned here:
  - Part A: ``handle_subscription_failure`` is serialized per agent by
    ``agent_switch_lock``, so concurrent failures yield exactly ONE switch.
  - Part B (Codex C8): the loser snapshots the subscription at entry and,
    after taking the lock, bails when it changed — so 3+ viable
    subscriptions do NOT cascade A→B→C.

Determinism: rather than rely on asyncio interleaving (``asyncio.Lock.acquire``
does not yield on a free lock, so a naive gather lets the winner switch before
the loser snapshots), the tests PRE-HOLD the agent lock. Both coroutines then
park at ``async with lock`` having already snapshotted the old sub; releasing
the lock lets exactly one win and the other observe the post-switch state.

Modules under test:
    src/backend/services/subscription_auto_switch.py::handle_subscription_failure
    src/backend/services/subscription_auto_switch.py::agent_switch_lock
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest


_BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)

# Slots this file stubs/loads at import time. The autouse snapshot/restore
# fixture (precedent: test_subscription_auto_switch_no_cred_import.py) bounds
# the blast radius so these don't leak into other files sharing the session.
_STUBBED_MODULE_NAMES = [
    "services",
    "database",
    "db_models",
    "services.subscription_auto_switch",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _load_auto_switch(mock_db):
    """Load ``subscription_auto_switch`` in isolation with ``database`` stubbed.

    The module's top-level imports are only ``asyncio``/``logging``/``typing``
    + ``from database import db`` + ``from db_models import NotificationCreate``
    (the ``services.*`` imports are lazy, inside ``_restart_agent`` /
    ``_perform_auto_switch``), so loading it by file location under its real
    dotted name needs no real ``services`` package boot.
    """
    sys.modules.setdefault("services", types.ModuleType("services"))

    db_module = types.ModuleType("database")
    db_module.db = mock_db
    sys.modules["database"] = db_module

    # db_models is pure Pydantic — import the real one rather than stubbing
    # (a bare stub would persist and break unrelated tests).
    if "db_models" not in sys.modules:
        if _BACKEND not in sys.path:
            sys.path.insert(0, _BACKEND)
        import db_models  # noqa: F401 — registers in sys.modules

    sys.modules.pop("services.subscription_auto_switch", None)
    spec = importlib.util.spec_from_file_location(
        "services.subscription_auto_switch",
        os.path.join(_BACKEND, "services", "subscription_auto_switch.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["services.subscription_auto_switch"] = mod
    spec.loader.exec_module(mod)
    return mod


def _sub(sub_id: str) -> MagicMock:
    """A subscription stand-in with ``.id`` / ``.name`` (``.name`` must be set
    after construction — ``MagicMock(name=...)`` is the repr name, not a field)."""
    s = MagicMock()
    s.id = sub_id
    s.name = f"sub-{sub_id}"
    return s


def _build_db(initial_sub: str, alt_map: dict) -> MagicMock:
    """A ``db`` stub whose current subscription is mutable.

    ``alt_map`` maps current-sub-id → the one candidate
    ``list_viable_alternative_subscriptions`` lists for it (or absent → []).
    #2409: the db lists, the service ranks — with ``services`` stubbed as a
    bare module here the ranking import fails open to the db's order, which
    is exactly the old first-match pick.
    """
    state = {"current": initial_sub}
    db = MagicMock()
    db.get_setting_value.return_value = "true"
    db.get_agent_subscription_id.side_effect = lambda _agent: state["current"]
    db.record_rate_limit_event.return_value = 1
    db.get_subscription.side_effect = lambda sub_id: _sub(sub_id)
    db.list_viable_alternative_subscriptions.side_effect = (
        lambda cur: [alt_map[cur]] if alt_map.get(cur) is not None else []
    )
    db._state = state  # expose for assertions / the perform spy
    return db


def _install_perform_spy(mod, db):
    """Replace ``_perform_auto_switch`` with a spy that simulates the assign
    (mutates the stub's current sub) and records each switch."""
    calls: list[str] = []

    async def spy(*, agent_name, old_subscription_name,
                  new_subscription, failure_kind, event_count,
                  destination_headroom=None):
        calls.append(new_subscription.id)
        db._state["current"] = new_subscription.id  # the real assign side effect
        return {
            "switched": True,
            "agent_name": agent_name,
            "old_subscription": old_subscription_name,
            "new_subscription": new_subscription.name,
        }

    mod._perform_auto_switch = spy
    return calls


@pytest.mark.asyncio
async def test_concurrent_failures_switch_exactly_once_two_subs():
    """Two concurrent failures on one agent (A→B viable) → exactly ONE switch;
    the loser returns None."""
    db = _build_db("A", alt_map={"A": _sub("B")})
    mod = _load_auto_switch(db)
    mod._reset_locks_for_test()
    calls = _install_perform_spy(mod, db)

    # Pre-hold the agent lock so both coroutines snapshot "A" and park at
    # `async with lock` before either can switch.
    lock = await mod.agent_switch_lock("burst-agent")
    await lock.acquire()

    t1 = asyncio.create_task(mod.handle_subscription_failure("burst-agent", "429", "rate_limit"))
    t2 = asyncio.create_task(mod.handle_subscription_failure("burst-agent", "429", "rate_limit"))
    await asyncio.sleep(0)  # let both reach `await lock.acquire()`

    lock.release()
    results = await asyncio.gather(t1, t2)

    assert calls == ["B"], f"expected exactly one switch to B, got {calls!r}"
    assert db._state["current"] == "B"
    switched = [r for r in results if r is not None]
    skipped = [r for r in results if r is None]
    assert len(switched) == 1 and len(skipped) == 1, results
    assert db.assign_subscription_to_agent.call_count == 0  # spy stands in


@pytest.mark.asyncio
async def test_concurrent_failures_no_cascade_three_subs():
    """C8 regression (CRITICAL): with A,B,C all viable, two concurrent failures
    that both observed sub-A must NOT cascade A→B→C.

    Without the stale-failure guard, the loser would read current=B (after the
    winner's A→B switch) and switch B→C. The guard makes it a no-op: it never
    even reaches alternative selection for B.
    """
    db = _build_db("A", alt_map={"A": _sub("B"), "B": _sub("C")})
    mod = _load_auto_switch(db)
    mod._reset_locks_for_test()
    calls = _install_perform_spy(mod, db)

    lock = await mod.agent_switch_lock("burst-agent")
    await lock.acquire()

    t1 = asyncio.create_task(mod.handle_subscription_failure("burst-agent", "429", "rate_limit"))
    t2 = asyncio.create_task(mod.handle_subscription_failure("burst-agent", "429", "rate_limit"))
    await asyncio.sleep(0)

    lock.release()
    results = await asyncio.gather(t1, t2)

    assert calls == ["B"], f"expected ONLY A→B, got cascade {calls!r}"
    assert db._state["current"] == "B"
    assert len([r for r in results if r is None]) == 1, "loser must no-op"
    # The guard short-circuits BEFORE alternative selection for the new sub:
    selected = [c.args[0] for c in db.list_viable_alternative_subscriptions.call_args_list]
    assert "B" not in selected, (
        f"loser reached alternative selection for the post-switch sub — the "
        f"stale-failure guard did not fire (selected={selected!r})"
    )


@pytest.mark.asyncio
async def test_different_agents_use_distinct_locks():
    """Distinct agents get distinct lock objects (so they don't serialize);
    the same agent always gets the same lock."""
    db = _build_db("A", alt_map={})
    mod = _load_auto_switch(db)
    mod._reset_locks_for_test()

    lock_a1 = await mod.agent_switch_lock("agent-a")
    lock_a2 = await mod.agent_switch_lock("agent-a")
    lock_b = await mod.agent_switch_lock("agent-b")

    assert lock_a1 is lock_a2, "same agent must reuse its lock"
    assert lock_a1 is not lock_b, "different agents must not share a lock"
