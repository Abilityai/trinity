"""#4105 — `self_task` and `skill_gate` must be pulled on a pilot.

Both triggers dispatch agent work and sat in neither pull trigger set, so on a
pull pilot `pull_owns_dispatch()` answered False and they were PUSHED. A pulled
scheduled run that queued three `self_task` runs through `POST /task` saw three
free Redis slots (a pull claim never enters the slot ZSET) and ran four turns
against `max_parallel_tasks=3`. Neither the soak's M3 probe nor canary S-02 can
see that.

The census test at the bottom is the class guard: every `triggered_by` value a
producer in `src/backend` or `src/scheduler` writes must be in a pull set or
carry a documented exclusion (`pull_pilot.PULL_UNPULLED_TRIGGERS`).

Limits of the census: it reads literal strings and module constants named
`*_TRIGGER` / `TRIGGERED_BY*`. A trigger passed through a runtime variable is
covered by the runtime note (`note_unreachable_pull_trigger`) instead. The
private enterprise submodule is not scanned.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

PILOT = "pilot-a"
NON_PILOT = "bob"
NEW = ("self_task", "skill_gate")


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
    import services.pull_pilot as pp

    pp._UNREACHABLE_NOTED.clear()
    yield pp
    pp._UNREACHABLE_NOTED.clear()


# ---------------------------------------------------------------------------
# 1. Membership
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", NEW)
def test_trigger_is_autonomous_and_pull_reachable(trigger):
    from services.pull_pilot import PULL_REACHABLE_TRIGGERS
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    assert trigger in _AUTONOMOUS_TRIGGERS
    assert trigger in PULL_REACHABLE_TRIGGERS


@pytest.mark.parametrize("trigger", NEW)
def test_a_pilot_owns_dispatch_for_the_trigger(pilot, trigger):
    assert pilot.pull_owns_dispatch(PILOT, trigger) is True
    assert pilot.pull_owns_dispatch(NON_PILOT, trigger) is False


def test_self_task_is_a_valid_executions_filter():
    """`?triggered_by=self_task` must filter, not degrade to "every row"."""
    from routers.executions import _VALID_TRIGGERS

    assert "self_task" in _VALID_TRIGGERS


# ---------------------------------------------------------------------------
# 2. A pilot's self_task never takes a Redis slot (the overbooking)
# ---------------------------------------------------------------------------


def _capacity(monkeypatch):
    from services import capacity_manager as cm_module

    slots = AsyncMock()
    slots.slots_prefix = "agent:slots:"
    slots.acquire_slot = AsyncMock(return_value=True)  # a slot IS free
    slots.register_on_release = lambda cb: None
    backlog = AsyncMock()
    backlog.enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(cm_module.redis, "from_url", lambda *_a, **_kw: MagicMock())
    cap = cm_module.CapacityManager(
        redis_url="redis://test", slot_service=slots, backlog_service=backlog
    )
    return cap, slots, backlog


def _payload(triggered_by):
    from services.capacity_manager import PersistentTaskPayload

    return PersistentTaskPayload(
        request=MagicMock(),
        effective_timeout=900,
        user_id=1,
        user_email="u@example.com",
        subscription_id=None,
        x_source_agent=PILOT,
        triggered_by=triggered_by,
        collaboration_activity_id=None,
        is_self_task=triggered_by == "self_task",
    )


def test_a_self_task_queued_by_a_pulled_turn_goes_to_the_worker_pool(pilot, monkeypatch):
    """The `POST /task` producer (`queue_persistent` + payload), with a free
    slot: a pilot's self_task is queued for its worker and no slot is taken,
    so it shares `max_parallel_tasks` with the pulled parent turn."""
    cap, slots, backlog = _capacity(monkeypatch)
    result = asyncio.run(
        cap.acquire(
            agent_name=PILOT,
            execution_id="exec-self",
            max_concurrent=3,
            overflow_policy="queue_persistent",
            overflow_payload=_payload("self_task"),
        )
    )
    assert result.state == "queued_persistent"
    slots.acquire_slot.assert_not_awaited()
    backlog.enqueue.assert_awaited_once()


def test_a_skill_gate_wake_on_a_pilot_gets_a_queue_payload(pilot):
    """`skill_gate_service` calls `execute_task` with no execution id; the row
    is created first, then this gate decides the policy. Non-None means
    `queue_persistent`, i.e. the wake is claimed by the agent's worker."""
    from services.task_execution_service import build_pull_queue_payload

    kw = dict(
        triggered_by="skill_gate", execution_id="exec-gate", message="decided",
        model=None, allowed_tools=None, system_prompt=None, timeout_seconds=900,
        resume_session_id=None, subscription_id=None, source_user_id=None,
        source_user_email=None, source_agent_name=None, slot_already_held=False,
    )
    assert build_pull_queue_payload(agent_name=PILOT, **kw) is not None
    assert build_pull_queue_payload(agent_name=NON_PILOT, **kw) is None


# ---------------------------------------------------------------------------
# 3. Runtime note: an unclassified trigger pushed on a pilot is logged
# ---------------------------------------------------------------------------


def test_the_note_fires_once_for_an_unclassified_trigger(pilot):
    assert pilot.note_unreachable_pull_trigger(PILOT, "made_up") is True
    assert pilot.note_unreachable_pull_trigger(PILOT, "made_up") is False  # deduped
    assert pilot.note_unreachable_pull_trigger(NON_PILOT, "made_up") is False
    for trigger in (*NEW, "schedule", "manual"):
        assert pilot.note_unreachable_pull_trigger(PILOT, trigger) is False


def test_task_route_push_on_a_pilot_reaches_the_note(pilot, monkeypatch):
    """`POST /task` pre-acquires and then calls `execute_task` with the slot
    held, so `execute_task`'s note never sees it. `acquire` is where an
    unpulled `/task` row on a pilot is pushed, so the note must fire there."""
    import services.pull_pilot as pp

    calls = []
    monkeypatch.setattr(
        pp, "note_unreachable_pull_trigger", lambda a, t: calls.append((a, t)) or True
    )
    cap, slots, _ = _capacity(monkeypatch)
    result = asyncio.run(
        cap.acquire(
            agent_name=PILOT,
            execution_id="exec-x",
            max_concurrent=3,
            overflow_policy="queue_persistent",
            overflow_payload=_payload("made_up"),
        )
    )
    assert result.state == "admitted"
    assert calls == [(PILOT, "made_up")]


# ---------------------------------------------------------------------------
# 4. Census — every trigger a producer writes is classified for pull
# ---------------------------------------------------------------------------


def _is_trigger_const(name: str) -> bool:
    return name.endswith("_TRIGGER") or name.startswith("TRIGGERED_BY")


def _producer_files():
    for top in ("src/backend", "src/scheduler"):
        for path in (_REPO / top).rglob("*.py"):
            rel = path.relative_to(_REPO).parts
            if "tests" in rel or "enterprise" in rel:
                continue
            yield path


def _emitted_triggers() -> dict:
    """`{trigger: [site, ...]}` for every string a producer writes as
    `triggered_by` — a `triggered_by=` keyword or a `triggered_by = ...`
    assignment, both arms of a conditional expression included. Module
    constants named like a trigger are resolved across all scanned modules, so
    an imported constant counts. `track_activity(...)` is skipped: its
    `triggered_by` tags an activity, not an execution row."""
    trees = {p: ast.parse(p.read_text()) for p in _producer_files()}
    consts = {}
    for tree in trees.values():
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name) and _is_trigger_const(target.id):
                        consts[target.id] = node.value.value

    def values(expr):
        out = set()
        for sub in ast.walk(expr):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value:
                out.add(sub.value)
            elif isinstance(sub, ast.Name) and sub.id in consts:
                out.add(consts[sub.id])
            elif isinstance(sub, ast.Attribute) and sub.attr in consts:
                out.add(consts[sub.attr])
        return out

    found: dict = {}
    for path, tree in trees.items():
        skip = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name == "track_activity":
                    skip.update(id(k) for k in node.keywords)
        for node in ast.walk(tree):
            expr = None
            if isinstance(node, ast.keyword) and node.arg == "triggered_by" and id(node) not in skip:
                expr = node.value
            elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "triggered_by" for t in node.targets
            ):
                expr = node.value
            if expr is None:
                continue
            # A pass-through (`triggered_by=row["triggered_by"]`) names the
            # field, not a value.
            for v in values(expr) - {"triggered_by"}:
                found.setdefault(v, []).append(f"{path.relative_to(_REPO)}:{node.lineno}")
    return found


def test_every_emitted_trigger_is_in_a_pull_set_or_documented_as_excluded():
    from services.pull_pilot import (
        PULL_REACHABLE_NON_AUTONOMOUS,
        PULL_REACHABLE_TRIGGERS,
        PULL_UNPULLED_TRIGGERS,
    )

    emitted = _emitted_triggers()
    # Never pass on an empty or truncated scan.
    floor = {"self_task", "skill_gate", "retry", "schedule", "event", "fan_out", "a2a"}
    assert floor <= set(emitted), sorted(floor - set(emitted))

    for trigger, reason in PULL_UNPULLED_TRIGGERS.items():
        assert reason.strip(), f"exclusion of {trigger!r} has no stated reason"

    classified = PULL_REACHABLE_TRIGGERS | PULL_REACHABLE_NON_AUTONOMOUS | set(PULL_UNPULLED_TRIGGERS)
    missing = {t: emitted[t][:3] for t in emitted if t not in classified}
    assert not missing, (
        f"triggers written by a producer but in neither pull set and not in "
        f"PULL_UNPULLED_TRIGGERS: {missing}"
    )
