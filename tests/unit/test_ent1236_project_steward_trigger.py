"""trinity-enterprise#1236 — the `project_steward` trigger is classified everywhere.

The platform now wakes a project's steward agent with a turn of its own,
`triggered_by="project_steward"`. Only the private module writes that value, and
the #4105 census does not scan the private submodule, so nothing else would
notice a set that forgot it. Each set below decides something different about
that turn, so each is asserted here, through its consumer where one is cheap
to drive:

* rated an OWNER turn (`turn_tier_service`) — otherwise it is a visitor's,
  which hides the agent's own execution ledger from it (the ent#1216 class);
* autonomous and pull-reachable — a pilot pulls it like a loop iteration;
* a valid `?triggered_by=` filter on the executions list;
* counted with schedules in the per-agent analytics (it replaces the steward's
  schedule), never in `Other`;
* an operator-side canvas trigger, a personless skill-gate replay, and a
  caller-wake recursion break (a platform turn's end is not a delegation's end).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

TRIGGER = "project_steward"
AGENT = "steward-agent"


def _row(**over):
    row = {"agent_name": AGENT, "source_user_id": None, "source_user_email": None,
           "triggered_by": TRIGGER, "schedule_id": None, "source_channel": None}
    row.update(over)
    return row


def test_a_personless_steward_wake_is_rated_the_owners_turn():
    from services import turn_tier_service as t

    assert t.turn_actor(SimpleNamespace(), AGENT, execution=_row()) == (t.OWNER, None, None)


def test_an_unclassified_personless_trigger_would_be_a_visitor():
    """The control: the rating above comes from the set, not from the row shape."""
    from services import turn_tier_service as t

    assert t.turn_actor(SimpleNamespace(), AGENT, execution=_row(triggered_by="not-a-trigger")) == (
        t.SHARE, t.VISITOR, None)


def test_it_is_autonomous_and_a_pilot_pulls_it(monkeypatch):
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "pilot-a")
    from services import pull_pilot
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    assert TRIGGER in _AUTONOMOUS_TRIGGERS
    assert TRIGGER in pull_pilot.PULL_REACHABLE_TRIGGERS
    assert pull_pilot.pull_owns_dispatch("pilot-a", TRIGGER) is True
    assert pull_pilot.pull_owns_dispatch("bob", TRIGGER) is False


def test_it_is_a_valid_executions_filter():
    from routers.executions import _VALID_TRIGGERS

    assert TRIGGER in _VALID_TRIGGERS


def test_analytics_counts_it_with_schedules():
    from db.schedules.analytics import _bucket_for_trigger

    assert _bucket_for_trigger(TRIGGER) == "Scheduled"


def test_the_canvas_it_writes_is_on_the_operator_side():
    from services.canvas_service import canvas_visibility

    visible, _note = canvas_visibility(audience="owner", triggered_by=TRIGGER)
    assert visible is not None          # a claim, not the "unrecognised label" no-claim


def test_it_is_a_personless_skill_gate_replay():
    from services.skill_gate_service import _PERSONLESS_REPLAY_TRIGGERS

    assert TRIGGER in _PERSONLESS_REPLAY_TRIGGERS


def test_its_end_never_wakes_a_caller(monkeypatch):
    """Even with a `source_agent_name` on the row, the reserved trigger stops
    `maybe_wake_caller` before any policy is read."""
    from services import caller_wake_service as w

    assert TRIGGER in w.RESERVED_TRIGGERS
    row = SimpleNamespace(source_agent_name="other-agent", agent_name=AGENT, fan_out_id=None,
                          triggered_by=TRIGGER, status="success")
    monkeypatch.setattr(w, "_read_row", lambda eid: row)

    def _policy(name):
        raise AssertionError("the policy must not be read for a reserved trigger")
    monkeypatch.setattr(w, "_resolve_policy", _policy)
    assert asyncio.run(w.maybe_wake_caller("ex-1")) is None
