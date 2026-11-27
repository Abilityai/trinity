"""#4778: which silent agent keeps the managed-hooks alert open is read from the
alert's own pending row, not from what this process last saw.

`services/managed_hooks_check_service.py` used to keep the agents its last pass
named in a module global. It is per process, so after a backend restart, or
when the fleet cycle's lease moved to another worker, any running agent that
did not answer `/health` kept the old alert, including one the alert never
named; a worker that regained the lease decided from the set of its old term.

Every test here runs the real pass against a real pending row in the unit
island's SQLite (`_managed_hooks_fleet_world.Fleet`); the row a test starts
from is filed the way another worker's pass files it.
Related flow: docs/memory/feature-flows/agent-lifecycle.md (Managed-hooks report)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `_managed_hooks_fleet_world`, as `_route_census`

from _managed_hooks_fleet_world import Fleet, mhc  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def fleet(monkeypatch):
    return Fleet().install(monkeypatch)


# ---------------------------------------------------------------------------
# After a restart or a lease move: the pending row says who was named
# ---------------------------------------------------------------------------

def test_a_silent_agent_the_pending_alert_does_not_name_does_not_keep_it(fleet):
    fleet.file_alert({"agents": ["alpha"]})
    fleet.agent("alpha", "ok").agent("beta", None)
    out = fleet.run()
    assert out["status"] == "clear" and out["unchecked"] == ["beta"]
    assert fleet.row() is None


def test_a_silent_agent_the_pending_alert_names_keeps_it(fleet):
    fleet.file_alert({"agents": ["alpha"]})
    fleet.agent("alpha", None).agent("beta", "ok")
    out = fleet.run()
    assert (out["status"], out["reason"]) == ("could_not_check", "partial")
    fleet.clear.assert_not_called()
    assert fleet.named() == ["alpha"]


def test_a_silent_agent_the_pending_alert_names_stays_named_while_another_is_skipped(fleet):
    fleet.file_alert({"agents": ["alpha", "beta"]})
    fleet.agent("alpha", None).agent("beta", "skipped")
    assert fleet.run()["status"] == "skipped"
    assert fleet.named() == ["alpha", "beta"]
    assert fleet.row()["title"] == "Guardrails are not running on 2 agents"


def test_a_worker_that_regains_the_lease_decides_from_the_row_not_its_old_term(fleet):
    """This worker named x-agent. Then another worker's pass found x-agent
    clean and y-agent skipped. Back here, x-agent is silent: the alert no
    longer names it, so it does not hold the alert."""
    fleet.agent("x-agent", "skipped").agent("y-agent", "ok")
    fleet.run()
    assert fleet.named() == ["x-agent"]
    fleet.file_alert({"agents": ["y-agent"]})
    fleet.health["x-agent"] = None
    assert fleet.run()["status"] == "clear"
    assert fleet.row() is None


def test_the_pass_reads_back_what_a_pass_wrote(fleet):
    fleet.agent("alpha", "skipped").agent("beta", "ok").agent("gamma", "skipped")
    fleet.run()
    assert mhc._pending_named() == ({"alpha", "gamma"}, True)


# ---------------------------------------------------------------------------
# No pending row, or one a person ended
# ---------------------------------------------------------------------------

def test_after_a_person_ends_the_alert_a_silent_agent_holds_nothing(fleet):
    fleet.agent("alpha", "skipped").agent("beta", "skipped")
    fleet.run()
    fleet.person_ends_the_alert()
    fleet.health.update(alpha=None, beta="ok")
    assert fleet.run()["status"] == "clear"
    assert fleet.row() is None


def test_after_a_person_ends_the_alert_the_same_agents_file_nothing_new(fleet):
    fleet.agent("alpha", "skipped").agent("beta", "skipped")
    fleet.run()
    fleet.person_ends_the_alert()
    fleet.health["alpha"] = None
    assert fleet.run()["status"] == "skipped"
    assert fleet.observe.call_args.kwargs["context"]["agents"] == ["beta"]
    assert fleet.row() is None  # snoozed: no agent the ended alert did not name


# ---------------------------------------------------------------------------
# The named set cannot be read: any silent agent holds, as before
# ---------------------------------------------------------------------------

_UNREADABLE_CONTEXTS = [
    pytest.param({"agents_skipped": 1}, id="no-agents-key"),
    pytest.param({"agents": "alpha"}, id="agents-not-a-list"),
    pytest.param({"agents": None}, id="agents-null"),
]


@pytest.mark.parametrize("context", _UNREADABLE_CONTEXTS)
def test_a_row_without_a_readable_list_lets_any_silent_agent_hold(fleet, context):
    fleet.file_alert(context)
    fleet.agent("alpha", "ok").agent("beta", None)
    out = fleet.run()
    assert (out["status"], out["reason"]) == ("could_not_check", "partial")
    fleet.clear.assert_not_called()
    assert fleet.row() is not None


@pytest.mark.parametrize("context", _UNREADABLE_CONTEXTS)
def test_a_row_without_a_readable_list_clears_once_every_agent_answers(fleet, context):
    fleet.file_alert(context)
    fleet.agent("alpha", "ok").agent("beta", "ok")
    assert fleet.run()["status"] == "clear"
    assert fleet.row() is None


def test_a_row_without_a_readable_list_names_only_the_skipped(fleet):
    fleet.file_alert({"agents_skipped": 1})
    fleet.agent("alpha", "skipped").agent("beta", None)
    assert fleet.run()["status"] == "skipped"
    assert fleet.named() == ["alpha"]


def test_a_failed_read_of_the_row_lets_any_silent_agent_hold(fleet, monkeypatch):
    from database import db
    fleet.file_alert({"agents": ["alpha"]})

    def boom(agent_name, subject):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(db, "find_pending_operator_queue_by_subject", boom)
    fleet.agent("alpha", "ok").agent("beta", None)
    out = fleet.run()
    # `partial`, not the outer handler's `error`: the pass went on and decided.
    assert (out["status"], out["reason"]) == ("could_not_check", "partial")
    fleet.clear.assert_not_called()


@pytest.mark.parametrize("row, expected", [
    (None, (set(), True)),
    ({"context": {"agents": []}}, (set(), True)),
    ({"context": {"agents": ["a", "b"], "seen_count": 3}}, ({"a", "b"}, True)),
    ({"context": {"agents": ["a", 7, None, "b"]}}, ({"a", "b"}, True)),
    ({"context": None}, (set(), False)),
    ({"context": {}}, (set(), False)),
    ({"context": ["a"]}, (set(), False)),
    ({"context": "a"}, (set(), False)),
    ({"context": {"agents": {"a": 1}}}, (set(), False)),
    ({}, (set(), False)),
])
def test_the_named_set_of_each_row_shape(row, expected):
    assert mhc._named_from(row) == expected
