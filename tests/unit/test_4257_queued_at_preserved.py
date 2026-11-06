"""A claim keeps `queued_at`, so the wait stays measurable (#4257).

trinity#4257: an async `/task` on an idle pull pilot sat `queued` for hours.
Nothing in the row said so afterwards, because `claim_next_queued` wrote
`queued_at = NULL` at the instant the wait became measurable. These tests pin
the pair (`queued_at` = admission, `started_at` = left the queue), the FIFO
place of a released claim, and the two reads the stall monitor makes.

Runs against the real db layer through db_harness (SQLite; Postgres too when
TEST_POSTGRES_URL is set).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _scalar  # noqa: E402,F401

pytestmark = pytest.mark.unit

_STUBBED_MODULE_NAMES = [
    "db.connection",
    "db.schedules",
    "db.agent_settings.resources",
    "database",
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


@pytest.fixture
def ops(db_backend):
    for mod in _STUBBED_MODULE_NAMES:
        sys.modules.pop(mod, None)
    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


# Base the fixtures in the recent PAST: a claim stamps `started_at = now`,
# and the pair must read as a real wait.
_T0 = datetime.now(timezone.utc) - timedelta(hours=6)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _q(agent: str, minute: int, eid: str, trigger: str = "mcp", key=None) -> str:
    qa = _iso(_T0 + timedelta(minutes=minute))
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, queued_at, message, "
        " triggered_by, conversation_key) "
        "VALUES (:id, '__manual__', :a, 'queued', :qa, :qa, 'm', :t, :k)",
        id=eid, a=agent, qa=qa, t=trigger, k=key,
    )
    return eid


def _col(eid: str, col: str):
    return _scalar(f"SELECT {col} FROM schedule_executions WHERE id = :i", i=eid)


# ---------------------------------------------------------------------------
# The claim keeps the admission instant
# ---------------------------------------------------------------------------


def test_worker_claim_keeps_queued_at(ops):
    _q("alpha", 0, "e1")

    row = ops.claim_next_queued("alpha", worker_id="alpha#w0", lease_seconds=900)

    assert row["id"] == "e1"
    assert _col("e1", "status") == "running"
    queued_at, started_at = _col("e1", "queued_at"), _col("e1", "started_at")
    assert queued_at is not None, "the wait was erased at the claim"
    assert started_at >= queued_at


def test_push_claim_keeps_queued_at(ops):
    """The legacy push/backlog-drain claim (no worker) keeps it too."""
    _q("alpha", 0, "e1")

    assert ops.claim_next_queued("alpha")["id"] == "e1"

    assert _col("e1", "queued_at") is not None


def test_expire_stale_queued_ignores_a_claimed_row_with_an_old_queued_at(ops):
    """The wait-preserving claim must not make a running row expirable."""
    _q("alpha", 0, "e1")
    ops.claim_next_queued("alpha", worker_id="alpha#w0", lease_seconds=900)
    # Backdate the preserved admission well past any window.
    _hrun(
        "UPDATE schedule_executions SET queued_at = :qa WHERE id = 'e1'",
        qa=_iso(_T0 - timedelta(days=30)),
    )

    assert ops.expire_stale_queued(max_age_hours=1) == 0
    assert _col("e1", "status") == "running"


def test_release_claim_to_queued_keeps_the_original_fifo_place(ops):
    """A released claim keeps its admission instant, so it stays the head."""
    _q("alpha", 0, "old")
    _q("alpha", 10, "new")
    original = _col("old", "queued_at")
    ops.claim_next_queued("alpha", worker_id="alpha#w0", lease_seconds=900)

    assert ops.release_claim_to_queued("old") is True

    assert _col("old", "queued_at") == original
    assert ops.claim_next_queued("alpha")["id"] == "old"


def test_lease_requeue_restamps_the_wait_per_attempt(ops):
    """Documented: a lease-reaper requeue measures the NEW attempt's wait;
    `redelivery_count` is what says a row has been round before."""
    _q("alpha", 0, "e1")
    original = _col("e1", "queued_at")
    ops.claim_next_queued("alpha", worker_id="alpha#w0", lease_seconds=900)

    # `now_iso` is the reaper's clock; the CAS needs the lease already past.
    assert ops.requeue_expired_lease(
        "e1", _iso(datetime.now(timezone.utc) + timedelta(hours=2))
    ) is True

    assert _col("e1", "status") == "queued"
    assert _col("e1", "queued_at") != original
    assert int(_col("e1", "redelivery_count") or 0) == 1


# ---------------------------------------------------------------------------
# count_queued_ahead
# ---------------------------------------------------------------------------


def test_count_queued_ahead_head_is_zero_and_depth_counts_up(ops):
    _q("alpha", 0, "a")
    _q("alpha", 5, "b")
    _q("alpha", 10, "c")

    def ahead(eid):
        return ops.count_queued_ahead("alpha", _col(eid, "queued_at"), eid)

    assert [ahead("a"), ahead("b"), ahead("c")] == [0, 1, 2]


def test_count_queued_ahead_excludes_other_agents_and_running_rows(ops):
    _q("beta", 0, "other")
    _q("alpha", 1, "mine")
    _q("alpha", 0, "claimed")
    ops.claim_next_queued("alpha", worker_id="alpha#w0", lease_seconds=900)

    assert ops.count_queued_ahead("alpha", _col("mine", "queued_at"), "mine") == 0


def test_count_queued_ahead_breaks_a_queued_at_tie_on_id(ops):
    qa = _iso(_T0)
    for eid in ("b2", "a1"):
        _hrun(
            "INSERT INTO schedule_executions "
            "(id, schedule_id, agent_name, status, started_at, queued_at, message, "
            " triggered_by) "
            "VALUES (:id, '__manual__', 'alpha', 'queued', :qa, :qa, 'm', 'mcp')",
            id=eid, qa=qa,
        )

    assert ops.count_queued_ahead("alpha", qa, "a1") == 0
    assert ops.count_queued_ahead("alpha", qa, "b2") == 1


def test_count_queued_ahead_of_a_row_with_no_admission_is_zero(ops):
    _q("alpha", 0, "a")

    assert ops.count_queued_ahead("alpha", None, "ghost") == 0


# ---------------------------------------------------------------------------
# The monitor's two reads
# ---------------------------------------------------------------------------


def test_oldest_queued_head_per_agent_returns_one_head_each(ops):
    _q("alpha", 5, "a-new")
    _q("alpha", 0, "a-old", key="conv-1")
    _q("beta", 2, "b-only")

    heads = {h["agent_name"]: h for h in ops.oldest_queued_head_per_agent()}

    assert set(heads) == {"alpha", "beta"}
    assert heads["alpha"]["execution_id"] == "a-old"
    assert heads["alpha"]["conversation_key"] == "conv-1"
    assert heads["alpha"]["queued_count"] == 2
    assert heads["beta"]["queued_count"] == 1


def test_oldest_queued_head_per_agent_skips_running_and_nulls(ops):
    _q("alpha", 0, "claimed")
    ops.claim_next_queued("alpha", worker_id="alpha#w0", lease_seconds=900)
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by) "
        "VALUES ('noadmit', '__manual__', 'beta', 'queued', :sa, 'm', 'mcp')",
        sa=_iso(_T0),
    )

    assert ops.oldest_queued_head_per_agent() == []


def test_agents_with_running_conversation_reads_the_2843_guard(ops):
    _q("alpha", 0, "running-one", key="conv-1")
    ops.claim_next_queued("alpha", worker_id="alpha#w0", lease_seconds=900)
    _q("alpha", 1, "waiting", key="conv-1")

    assert ops.agents_with_running_conversation("alpha") == ["conv-1"]
    assert ops.agents_with_running_conversation("beta") == []
