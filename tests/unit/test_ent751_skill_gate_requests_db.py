"""
Gated skills — the gate-owned durable record (trinity-enterprise#751).

Target: ``src/backend/db/skill_gate_requests.py::SkillGateRequestOperations``
over the real schema (``db_harness`` builds it from ``db/schema.py``; SQLite
always, PostgreSQL when ``TEST_POSTGRES_URL`` is set).

The record is what makes an approval run its request exactly once: the ask
records the decision, this row records the effect. ``ask_service``'s
observers are in-process and at-most-once, so the record is also what the
sweep reconciles against.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar, seed_execution  # noqa: E402,F401

pytestmark = pytest.mark.unit


@pytest.fixture
def ops(db_backend):
    from db.skill_gate_requests import SkillGateRequestOperations
    return SkillGateRequestOperations()


def _record(request_id="gate-1", agent="finance", requester_key="agent:marketing", **over):
    row = {
        "request_id": request_id,
        "agent_name": agent,
        "skills": ["pay-invoice"],
        "request_text": "/pay-invoice 100 EUR to ACME",
        "fingerprints": {"pay-invoice": "sha-1"},
        "requester_kind": "agent",
        "requester_key": requester_key,
        "source_agent": "marketing",
        "requester_email": None,
        "requester_execution_id": "exec-req-1",
        "requester_mcp_key_id": None,
        "origin_execution_id": None,
        "triggered_by": "agent",
        "dispatch": {"model": "sonnet", "timeout_seconds": 900},
    }
    row.update(over)
    return row


def _seed_ask(item_id, agent="finance", request_id="gate-1", status="pending"):
    _hrun(
        "INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, "
        "title, question, created_at, raised_by, channel) VALUES "
        "(:id, :a, :r, 'approval', :s, 'medium', 't', 'q', '2026-10-02T00:00:00Z', 'gate', 'gate')",
        id=item_id, a=agent, r=request_id, s=status,
    )


class TestCreate:
    def test_first_insert_creates_and_a_second_returns_the_original(self, ops):
        row, created = ops.create_gate_request(**_record())
        assert created is True
        assert row["state"] == "pending"
        assert row["skills"] == ["pay-invoice"]
        assert row["dispatch"] == {"model": "sonnet", "timeout_seconds": 900}
        again, created_again = ops.create_gate_request(**_record(request_text="something else"))
        assert created_again is False
        assert again["request_text"] == "/pay-invoice 100 EUR to ACME"

    def test_get_returns_none_for_an_unknown_id(self, ops):
        assert ops.get_gate_request("gate-nope") is None


class TestPendingCounts:
    def test_counts_only_pending_rows_per_agent_and_per_requester(self, ops):
        ops.create_gate_request(**_record("gate-a"))
        ops.create_gate_request(**_record("gate-b"))
        ops.create_gate_request(**_record("gate-c", requester_key="person:p@example.com"))
        ops.create_gate_request(**_record("gate-d", agent="other"))
        ops.transition_gate_request("gate-b", "denied")
        assert ops.count_pending_gate_requests("finance") == 2
        assert ops.count_pending_gate_requests("finance", requester_key="agent:marketing") == 1
        assert ops.count_pending_gate_requests("finance", requester_key="person:p@example.com") == 1
        assert ops.count_pending_gate_requests("other") == 1


class TestClaim:
    def test_a_pending_record_is_claimed_once(self, ops):
        ops.create_gate_request(**_record())
        assert ops.claim_gate_request_for_dispatch("gate-1", "exec-run-1") is True
        assert ops.claim_gate_request_for_dispatch("gate-1", "exec-run-2") is False
        row = ops.get_gate_request("gate-1")
        assert row["state"] == "dispatching"
        assert row["dispatched_execution_id"] == "exec-run-1"
        assert row["decided_at"]
        assert ops.get_gate_request_by_dispatched_execution("exec-run-1")["request_id"] == "gate-1"
        assert ops.get_gate_request_by_dispatched_execution("exec-run-2") is None

    def test_two_records_cannot_hold_the_same_dispatched_execution(self, ops):
        ops.create_gate_request(**_record("gate-a"))
        ops.create_gate_request(**_record("gate-b"))
        assert ops.claim_gate_request_for_dispatch("gate-a", "exec-same") is True
        assert ops.claim_gate_request_for_dispatch("gate-b", "exec-same") is False
        assert ops.get_gate_request("gate-b")["state"] == "pending"

    def test_a_denied_record_cannot_be_claimed(self, ops):
        ops.create_gate_request(**_record())
        assert ops.transition_gate_request("gate-1", "denied") is True
        assert ops.claim_gate_request_for_dispatch("gate-1", "exec-run-1") is False


class TestLattice:
    @pytest.mark.parametrize("path", [
        ["denied"], ["expired"], ["cancelled"], ["refused"],
        ["dispatching", "dispatched"], ["dispatching", "stale"],
        ["dispatching", "not_run"], ["dispatching", "unknown"],
    ])
    def test_allowed_paths(self, ops, path):
        ops.create_gate_request(**_record())
        for state in path:
            if state == "dispatching":
                assert ops.claim_gate_request_for_dispatch("gate-1", "exec-run-1") is True
            else:
                assert ops.transition_gate_request("gate-1", state, detail="why") is True
        row = ops.get_gate_request("gate-1")
        assert row["state"] == path[-1]
        assert row["state_detail"] == ("why" if path[-1] != "dispatching" else None)

    @pytest.mark.parametrize("start, target", [
        (["dispatching", "dispatched"], "dispatched"),   # reflexive: the double-run edge
        (["denied"], "denied"),
        (["denied"], "dispatched"),
        ([], "dispatched"),                              # skipping the claim
        ([], "stale"),
        (["dispatching", "dispatched"], "cancelled"),
        (["expired"], "denied"),
    ])
    def test_refused_transitions_change_nothing(self, ops, start, target):
        ops.create_gate_request(**_record())
        for state in start:
            if state == "dispatching":
                ops.claim_gate_request_for_dispatch("gate-1", "exec-run-1")
            else:
                ops.transition_gate_request("gate-1", state)
        before = ops.get_gate_request("gate-1")["state"]
        assert ops.transition_gate_request("gate-1", target) is False
        assert ops.get_gate_request("gate-1")["state"] == before

    def test_an_unknown_state_is_a_programming_error(self, ops):
        ops.create_gate_request(**_record())
        with pytest.raises(ValueError):
            ops.transition_gate_request("gate-1", "approved")


class TestReconcile:
    def test_a_pending_record_whose_ask_ended_is_listed(self, ops):
        ops.create_gate_request(**_record("gate-ended"))
        ops.attach_gate_ask("gate-ended", "item-ended")
        _seed_ask("item-ended", request_id="gate-ended", status="responded")
        ops.create_gate_request(**_record("gate-open"))
        ops.attach_gate_ask("gate-open", "item-open")
        _seed_ask("item-open", request_id="gate-open", status="pending")
        ops.create_gate_request(**_record("gate-noask"))       # raise never landed
        ids = [r["request_id"] for r in ops.list_gate_requests_with_ended_asks()]
        assert ids == ["gate-ended"]

    def test_a_dispatching_record_with_no_execution_row_after_the_cutoff_is_listed(self, ops):
        ops.create_gate_request(**_record("gate-lost"))
        ops.claim_gate_request_for_dispatch("gate-lost", "exec-lost")
        ops.create_gate_request(**_record("gate-ran"))
        ops.claim_gate_request_for_dispatch("gate-ran", "exec-ran")
        seed_execution("sched", "finance", exec_id="exec-ran", status="running")
        far_future = "2999-01-01T00:00:00Z"
        ids = [r["request_id"] for r in ops.list_gate_requests_lost_in_dispatch(far_future)]
        assert ids == ["gate-lost"]
        assert ops.list_gate_requests_lost_in_dispatch("2000-01-01T00:00:00Z") == []

    def test_pending_records_for_an_agent(self, ops):
        ops.create_gate_request(**_record("gate-a"))
        ops.create_gate_request(**_record("gate-b"))
        ops.transition_gate_request("gate-b", "denied")
        ops.create_gate_request(**_record("gate-c", agent="other"))
        assert [r["request_id"] for r in ops.list_pending_gate_requests("finance")] == ["gate-a"]


def test_notified_is_stamped_once(ops):
    ops.create_gate_request(**_record())
    assert ops.mark_gate_request_notified("gate-1") is True
    assert ops.mark_gate_request_notified("gate-1") is False
    assert ops.get_gate_request("gate-1")["notified_at"]


def test_the_registered_sqlite_migration_creates_the_table_and_is_idempotent(tmp_path):
    """Runs the entry the runner actually registers, twice (learnings
    2026-10-01: schema parity cannot see an unregistered migration)."""
    import sqlite3
    from db import migrations

    entry = dict(migrations.MIGRATIONS)["skill_gate_requests_table"]
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    cur = conn.cursor()
    entry(cur, conn)
    entry(cur, conn)
    cur.execute("PRAGMA table_info(skill_gate_requests)")
    cols = {r[1] for r in cur.fetchall()}
    assert {"request_id", "agent_name", "state", "dispatched_execution_id", "dispatch"} <= cols
    cur.execute("PRAGMA index_list(skill_gate_requests)")
    indexes = {r[1] for r in cur.fetchall()}
    assert {"idx_skill_gate_requests_agent_state", "idx_skill_gate_requests_requester"} <= indexes
    cur.execute("INSERT INTO skill_gate_requests (request_id, agent_name, skills, request_text, "
                "requester_kind, requester_key, dispatch, created_at, dispatched_execution_id) "
                "VALUES ('a','x','[]','t','agent','k','{}','now','run-1')")
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute("INSERT INTO skill_gate_requests (request_id, agent_name, skills, request_text, "
                    "requester_kind, requester_key, dispatch, created_at, dispatched_execution_id) "
                    "VALUES ('b','x','[]','t','agent','k','{}','now','run-1')")
    conn.close()


def test_the_database_facade_mirrors_every_operation_signature():
    """A facade delegation with a stale signature silently drops a new kwarg
    (learnings 2026-09-01); one with no delegation 503s on first real use."""
    import inspect
    import database
    from db.skill_gate_requests import SkillGateRequestOperations

    public = [n for n, _ in inspect.getmembers(SkillGateRequestOperations, inspect.isfunction)
              if not n.startswith("_")]
    assert public, "no public operations found"
    for name in public:
        facade = getattr(database.DatabaseManager, name, None)
        assert facade is not None, f"DatabaseManager has no delegation for {name}"
        ops_params = list(inspect.signature(getattr(SkillGateRequestOperations, name)).parameters.values())[1:]
        facade_params = list(inspect.signature(facade).parameters.values())[1:]
        assert [(p.name, p.kind, p.default) for p in facade_params] == \
               [(p.name, p.kind, p.default) for p in ops_params], name


def test_records_are_found_by_the_row_the_gate_closed(ops):
    ops.create_gate_request(**_record("gate-a", origin_execution_id="exec-tick-1"))
    ops.create_gate_request(**_record("gate-b"))
    found = ops.get_gate_requests_by_origin_executions(["exec-tick-1", "exec-other", None])
    assert list(found) == ["exec-tick-1"]
    assert found["exec-tick-1"]["request_id"] == "gate-a"
    assert ops.get_gate_requests_by_origin_executions([]) == {}
