"""The queue stall monitor's reads, on the real facade and the real schema (#4635, #4636).

The monitor's own tests (`test_4257_queue_stall_monitor.py`) stub `db`. A stub
proves the caller, never the facade, and never the SQL: `DatabaseManager`
delegates by name, so a missing delegation or a dropped keyword is invisible to
every stubbed test (learnings 2026-07-06, 2026-09-01, 2026-09-06, 2026-10-09).
These run the real `database.db` over the real schema through db_harness —
SQLite always, PostgreSQL when `TEST_POSTGRES_URL` is set; the
`requires_postgres` ones run on the schema-parity PostgreSQL tier.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402,F401

pytestmark = pytest.mark.unit

_PREFIX = "dispatch_queue_stalled:"


def _now() -> datetime:
    """The clock at CALL time. A module-level `now` is taken at collection, and
    in a long CI run a test can execute minutes later: a "live" lease built from
    it had already expired when the read ran (learnings 2026-08-03)."""
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _pending_ask(i: int, *, agent="noisy", priority="critical"):
    """An agent's own ask: pending, no subject."""
    return {
        "id": f"ask-{i:05d}", "agent_name": agent, "request_id": f"ask-{i:05d}",
        "type": "question", "status": "pending", "priority": priority,
        "title": "t", "question": "q",
        "created_at": _iso(_now() - timedelta(days=1, seconds=i)),
    }


def _bulk(rows):
    from sqlalchemy import insert

    from db.engine import get_engine
    from db.tables import operator_queue

    with get_engine().begin() as conn:
        conn.execute(insert(operator_queue), rows)


def _stall_row(agent: str, *, priority="high", status="pending", subject=None,
               context=None, title="Queue head waited", question="q"):
    _hrun(
        "INSERT INTO operator_queue "
        "(id, agent_name, request_id, type, status, priority, title, question, "
        " context, created_at, subject, last_seen_at) "
        "VALUES (:id, :ag, :rid, 'alert', :st, :pr, :ti, :qu, :cx, :ca, :su, :ca)",
        id=f"row-{agent}-{status}", ag=agent, rid=f"queue-stall-{agent}-{status}",
        st=status, pr=priority, ti=title, qu=question,
        cx=json.dumps(context or {"episode_started_at": "e0"}),
        ca=_iso(_now() - timedelta(hours=2)),
        su=subject or f"{_PREFIX}{agent}",
    )


def _status(agent: str, status="pending"):
    return _hscalar(
        "SELECT status FROM operator_queue WHERE id = :i", i=f"row-{agent}-{status}"
    )


# ---------------------------------------------------------------------------
# G1: the pending stall rows, in one filtered read
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
def test_the_pending_stall_rows_are_found_past_a_thousand_other_pending_rows(db_backend):
    """The reconcile used to read 1,000 pending rows of any kind and filter
    them in Python, so past a thousand other asks a finished stall was never
    ended. The read is now the stall rows themselves."""
    _bulk([_pending_ask(i) for i in range(1001)])
    _stall_row("drained")
    _stall_row("busy", priority="medium")
    import database

    rows = database.db.list_pending_operator_queue_by_subject_prefix(_PREFIX)

    assert sorted(r["agent_name"] for r in rows) == ["busy", "drained"]
    by_agent = {r["agent_name"]: r for r in rows}
    assert by_agent["busy"]["priority"] == "medium"
    assert by_agent["drained"]["context"]["episode_started_at"] == "e0"


@pytest.mark.requires_postgres
def test_the_subject_prefix_is_matched_exactly(db_backend):
    """`_` is a LIKE wildcard and SQLite's LIKE ignores case, so a pattern
    match would let these through; an ended row is not pending."""
    _stall_row("real")
    _stall_row("ended", status="cancelled")
    _stall_row("dash", subject="dispatch-queue-stalled:dash")
    _stall_row("upper", subject="DISPATCH_QUEUE_STALLED:upper")
    _stall_row("longer", subject="dispatch_queue_stalledX:longer")
    _stall_row("other", subject="queue_flood:other")
    import database

    rows = database.db.list_pending_operator_queue_by_subject_prefix(_PREFIX)

    assert [r["agent_name"] for r in rows] == ["real"]


# ---------------------------------------------------------------------------
# The facade: every name and every keyword the monitor uses reaches the ops
# ---------------------------------------------------------------------------


def _queued(agent: str, eid: str, minutes_ago: int, key=None):
    qa = _iso(_now() - timedelta(minutes=minutes_ago))
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, queued_at, message, "
        " triggered_by, conversation_key) "
        "VALUES (:id, '__manual__', :a, 'queued', :qa, :qa, 'm', 'mcp', :k)",
        id=eid, a=agent, qa=qa, k=key,
    )


def _running(agent: str, eid: str, *, key=None, lease_minutes=None):
    lease = (None if lease_minutes is None
             else _iso(_now() + timedelta(minutes=lease_minutes)))
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by, "
        " conversation_key, lease_expires_at) "
        "VALUES (:id, '__manual__', :a, 'running', :sa, 'm', 'mcp', :k, :le)",
        id=eid, a=agent, sa=_iso(_now() - timedelta(minutes=50)), k=key, le=lease,
    )


@pytest.mark.requires_postgres
def test_the_facade_passes_every_new_argument_through(db_backend):
    """A delegation that exists but drops a keyword answers the old question
    with no error anywhere (learnings 2026-09-01)."""
    _running("alpha", "r-held", key="conv-1", lease_minutes=10)
    _running("alpha", "r-dead", lease_minutes=-10)
    _queued("alpha", "q-held", 50, key="conv-1")
    _queued("alpha", "q-free", 40)
    import database

    db = database.db
    (claimable,) = db.oldest_queued_head_per_agent(agent_names=["alpha"], claimable=True)
    assert claimable["execution_id"] == "q-free"
    assert db.oldest_queued_head_per_agent(agent_names=["beta"]) == []
    assert db.count_active_leased_by_agent(["alpha"]) == {"alpha": 2}
    assert db.count_active_leased_by_agent(
        ["alpha"], unexpired_at=_iso(datetime.now(timezone.utc))
    ) == {"alpha": 1}
    assert db.running_conversation_keys_by_agent(["alpha", "beta"]) == {"alpha": ["conv-1"]}


def test_every_db_call_the_monitor_makes_exists_on_the_facade():
    """Derived from the monitor's source, so the next new call fails the day
    it is written (learnings 2026-09-06)."""
    import database

    src = (_BACKEND / "services" / "queue_stall_service.py").read_text()
    names = set(re.findall(r"\bdb\.(\w+)\(", src))
    assert names, "expected the monitor to read through the facade"
    missing = sorted(n for n in names if not callable(getattr(database.DatabaseManager, n, None)))
    assert missing == []


# ---------------------------------------------------------------------------
# G1 end to end: a drained agent's alert ends, whatever else is pending
# ---------------------------------------------------------------------------


def _tick():
    from services import queue_stall_service as qss

    with patch("redis_breaker_util.get_breaker_redis", return_value=None), \
            patch("services.slot_service.get_slot_service",
                  side_effect=RuntimeError("no redis in the unit island")), \
            patch.object(qss, "threshold_minutes", return_value=30):
        return qss.run_once()


@pytest.mark.requires_postgres
def test_a_drained_agents_stall_alert_ends_past_a_thousand_pending_rows(db_backend):
    _bulk([_pending_ask(i) for i in range(1001)])
    _stall_row("drained")

    _tick()

    assert _status("drained") != "pending", "the drained agent's alert was never ended"
    assert _hscalar(
        "SELECT disposed_by FROM operator_queue WHERE id = 'row-drained-pending'"
    ) == "platform"
    assert _hscalar(
        "SELECT COUNT(*) FROM operator_queue WHERE agent_name = 'noisy' AND status = 'pending'"
    ) == 1001


@pytest.mark.requires_postgres
def test_a_stalled_agents_row_is_left_pending(db_backend):
    _queued("stuck", "q-1", 45)
    _stall_row("stuck")

    _tick()

    assert _status("stuck") == "pending"


# ---------------------------------------------------------------------------
# #4636 decision 2, at the seam: a higher reading rewrites, a lower one counts
# ---------------------------------------------------------------------------


def _item(priority: str, title: str, marker: str):
    return {
        "id": f"queue-stall-alpha-{title}",
        "type": "alert",
        "status": "pending",
        "priority": priority,
        "title": title,
        "question": f"question {title}",
        "context": {"episode_started_at": marker, "wait_reason": title},
        "created_at": _iso(_now()),
        "expires_at": _iso(_now() + timedelta(days=14)),
    }


@pytest.mark.requires_postgres
def test_a_rewrite_moves_priority_and_text_and_a_counted_reading_does_not(db_backend):
    from db.operator_queue import OperatorQueueOperations

    ops = OperatorQueueOperations()
    subject = f"{_PREFIX}alpha"
    first = ops.create_platform_item("alpha", _item("medium", "capacity", "e1"),
                                     subject=subject, keep_text=True)
    assert first["outcome"] == "created"

    worse = ops.create_platform_item("alpha", _item("high", "worker_claim", "e1"),
                                     subject=subject, keep_text=False)
    assert worse["outcome"] == "updated" and worse["changed"] is True
    row = worse["row"]
    assert (row["priority"], row["title"], row["question"]) == (
        "high", "worker_claim", "question worker_claim")
    assert row["context"]["episode_started_at"] == "e1"
    assert row["id"] == first["row"]["id"], "one row per subject, updated in place"

    better = ops.create_platform_item("alpha", _item("medium", "capacity", "e1"),
                                      subject=subject, keep_text=True)
    assert better["changed"] is False
    assert (better["row"]["priority"], better["row"]["title"]) == ("high", "worker_claim")


# ---------------------------------------------------------------------------
# G3: the effective cap for many agents in one roster read
# ---------------------------------------------------------------------------


def test_the_effective_cap_is_read_once_for_any_number_of_agents():
    import database
    from services import settings_service

    calls = {"roster": 0, "ceiling": 0}

    def roster():
        calls["roster"] += 1
        return {"alpha": 2, "beta": 9}

    def ceiling():
        calls["ceiling"] += 1
        return 6

    with patch.object(database.db, "get_all_agents_parallel_capacity", side_effect=roster), \
            patch.object(settings_service, "get_max_parallel_tasks_ceiling", side_effect=ceiling):
        caps = settings_service.get_effective_max_parallel_tasks_for(["alpha", "beta", "gamma"])

    # beta is clamped to the ceiling; gamma has no roster row and gets the
    # default `get_max_parallel_tasks` answers for one.
    assert caps == {"alpha": 2, "beta": 6, "gamma": 3}
    assert calls == {"roster": 1, "ceiling": 1}


def test_the_effective_cap_for_no_agents_reads_nothing():
    import database
    from services import settings_service

    with patch.object(database.db, "get_all_agents_parallel_capacity",
                      side_effect=AssertionError("read for nobody")):
        assert settings_service.get_effective_max_parallel_tasks_for([]) == {}


# ---------------------------------------------------------------------------
# #4635 review: a person's ending holds while the claimable row is measured
# ---------------------------------------------------------------------------


def _tick_at(now, caps):
    from services import queue_stall_service as qss

    with patch("redis_breaker_util.get_breaker_redis", return_value=None), \
            patch("services.slot_service.get_slot_service",
                  side_effect=RuntimeError("no redis in the unit island")), \
            patch("services.pull_pilot.is_pull_pilot_agent", return_value=True), \
            patch("services.settings_service.get_effective_max_parallel_tasks_for",
                  side_effect=lambda names: {n: caps for n in names}), \
            patch.object(qss, "threshold_minutes", return_value=30):
        return qss.run_once(now=now)


def _stall_rows(agent):
    from sqlalchemy import text

    from db.engine import get_engine

    with get_engine().connect() as conn:
        return conn.execute(text(
            "SELECT status, priority, disposed_by FROM operator_queue "
            "WHERE subject = :s ORDER BY created_at"), {"s": f"{_PREFIX}{agent}"}).fetchall()


@pytest.mark.requires_postgres
def test_ending_a_busy_stall_holds_when_the_row_behind_the_held_head_is_measured(db_backend):
    """A single-slot pilot: one long turn on conversation K holds the head H;
    C (another conversation) waits behind it. The first reading names H, a
    later one names C — the same episode, both busy. A person ends the row;
    the next reading must stay quiet rather than file it again a minute
    later. The episode is the admission of the head that started it."""
    _running("solo", "r-k", key="conv-k", lease_minutes=60)
    _queued("solo", "h", 31, key="conv-k")
    _queued("solo", "c", 29, key="conv-j")
    t0 = datetime.now(timezone.utc)

    _tick_at(t0, caps=1)                              # H: conversation_busy
    _tick_at(t0 + timedelta(minutes=2), caps=1)       # C past too: agent_busy
    (row,) = _stall_rows("solo")
    assert (row[0], row[1]) == ("pending", "medium")
    _hrun(
        "UPDATE operator_queue SET status = 'cancelled', disposition = 'cancelled', "
        "disposed_by = 'person', disposed_at = :t WHERE subject = :s",
        t=_iso(datetime.now(timezone.utc)), s=f"{_PREFIX}solo",
    )

    _tick_at(t0 + timedelta(minutes=3), caps=1)

    assert [r[0] for r in _stall_rows("solo")] == ["cancelled"], (
        "a person's ending was undone by the next reading of the same episode")
