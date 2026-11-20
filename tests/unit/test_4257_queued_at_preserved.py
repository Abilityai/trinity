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


# ---------------------------------------------------------------------------
# #4635: the head read hands back one row per agent, not every queued row.
# `requires_postgres` so the schema-parity PostgreSQL tier runs them too: the
# read is a grouped statement joined back to its table, the shape whose
# GROUP BY strictness and TEXT ordering PostgreSQL enforces and SQLite does not.
# ---------------------------------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _CountingConnection:
    def __init__(self, owner, conn):
        self._owner, self._conn = owner, conn

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def execute(self, *args, **kwargs):
        rows = list(self._conn.execute(*args, **kwargs).mappings())
        self._owner.statements += 1
        self._owner.rows += len(rows)
        return _MappedResult(rows)


class _MappedResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _Rows(self._rows)


class _CountingEngine:
    """Counts what each statement hands back to Python — the per-tick cost
    the issue is about, measured rather than read off the SQL text."""

    def __init__(self, engine):
        self._engine = engine
        self.statements = 0
        self.rows = 0

    def connect(self):
        return _CountingConnection(self, self._engine.connect())

    @property
    def dialect(self):
        return self._engine.dialect


@pytest.mark.requires_postgres
def test_the_head_read_hands_back_one_row_per_agent_not_every_queued_row(ops, monkeypatch):
    for i in range(300):
        _q("alpha", i, f"a-{i:03d}")
    _q("beta", 7, "b-1")
    _q("beta", 9, "b-2")
    queue_mod = sys.modules["db.schedules.queue"]
    counting = _CountingEngine(queue_mod.get_engine())
    monkeypatch.setattr(queue_mod, "get_engine", lambda: counting)

    heads = {h["agent_name"]: h for h in ops.oldest_queued_head_per_agent()}

    assert heads["alpha"]["execution_id"] == "a-000"
    assert heads["alpha"]["queued_count"] == 300
    assert heads["beta"]["execution_id"] == "b-1"
    assert heads["beta"]["queued_count"] == 2
    assert counting.statements == 1
    assert counting.rows == 2, "every queued row was shipped to Python to find two heads"


@pytest.mark.requires_postgres
def test_a_tie_at_the_head_instant_breaks_on_id_and_counts_both(ops):
    qa = _iso(_T0)
    for eid in ("b2", "a1"):
        _hrun(
            "INSERT INTO schedule_executions "
            "(id, schedule_id, agent_name, status, started_at, queued_at, message, "
            " triggered_by) "
            "VALUES (:id, '__manual__', 'alpha', 'queued', :qa, :qa, 'm', 'mcp')",
            id=eid, qa=qa,
        )

    (head,) = ops.oldest_queued_head_per_agent()

    assert head["execution_id"] == "a1"
    assert head["queued_count"] == 2


@pytest.mark.requires_postgres
def test_another_agents_head_instant_never_picks_this_agents_later_row(ops):
    """Fan-out admits several agents in one instant. Alpha's LATER row shares
    beta's head instant and has the smaller id: matched on the instant alone,
    it would be read as alpha's head, carrying beta's count."""
    _q("alpha", 0, "z-head")
    _q("alpha", 10, "a-later")
    _q("beta", 10, "b-head")

    heads = {h["agent_name"]: h for h in ops.oldest_queued_head_per_agent()}

    assert heads["alpha"]["execution_id"] == "z-head"
    assert heads["alpha"]["queued_count"] == 2
    assert heads["beta"]["execution_id"] == "b-head"
    assert heads["beta"]["queued_count"] == 1


@pytest.mark.requires_postgres
def test_a_claimed_row_sharing_the_head_instant_is_neither_head_nor_counted(ops):
    """A claim keeps `queued_at` (#4257), so a running row can carry the very
    instant of the queued head — and a smaller id."""
    qa = _iso(_T0)
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, queued_at, message, "
        " triggered_by) "
        "VALUES ('q1-running', '__manual__', 'alpha', 'running', :qa, :qa, 'm', 'mcp')",
        qa=qa,
    )
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, queued_at, message, "
        " triggered_by) "
        "VALUES ('q2-queued', '__manual__', 'alpha', 'queued', :qa, :qa, 'm', 'mcp')",
        qa=qa,
    )

    (head,) = ops.oldest_queued_head_per_agent()

    assert head["execution_id"] == "q2-queued"
    assert head["queued_count"] == 1


@pytest.mark.requires_postgres
def test_rows_never_admitted_do_not_count_toward_the_depth(ops):
    _q("alpha", 0, "a1")
    _q("alpha", 1, "a2")
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by) "
        "VALUES ('noadmit', '__manual__', 'alpha', 'queued', :sa, 'm', 'mcp')",
        sa=_iso(_T0),
    )

    (head,) = ops.oldest_queued_head_per_agent()

    assert head["execution_id"] == "a1"
    assert head["queued_count"] == 2


@pytest.mark.requires_postgres
def test_the_head_read_keeps_its_shape_and_its_agent_order(ops):
    _q("beta", 0, "b1", trigger="schedule", key="conv-b")
    _q("alpha", 3, "a1")

    heads = ops.oldest_queued_head_per_agent()

    assert [h["agent_name"] for h in heads] == ["alpha", "beta"]
    assert set(heads[1]) == {
        "agent_name", "execution_id", "queued_at", "conversation_key",
        "triggered_by", "redelivery_count", "queued_count",
    }
    assert heads[1]["conversation_key"] == "conv-b"
    assert heads[1]["triggered_by"] == "schedule"
    assert heads[1]["redelivery_count"] == 0
    assert isinstance(heads[1]["redelivery_count"], int)


# ---------------------------------------------------------------------------
# #4635 review (D1): the oldest row a worker could CLAIM. The pull claim skips
# a row whose conversation already has a running turn (#2843), so a held FIFO
# head says nothing about the rows behind it.
# ---------------------------------------------------------------------------


def _running(agent: str, eid: str, key):
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, queued_at, message, "
        " triggered_by, conversation_key) "
        "VALUES (:id, '__manual__', :a, 'running', :sa, :sa, 'm', 'mcp', :k)",
        id=eid, a=agent, sa=_iso(_T0 - timedelta(minutes=30)), k=key,
    )


@pytest.mark.requires_postgres
def test_the_claimable_head_skips_rows_whose_conversation_is_running(ops):
    _running("alpha", "r-1", "conv-1")
    _q("alpha", 0, "held", key="conv-1")
    _q("alpha", 5, "free", key="conv-2")
    _q("alpha", 9, "keyless")

    (head,) = ops.oldest_queued_head_per_agent(agent_names=["alpha"], claimable=True)

    assert head["execution_id"] == "free"
    assert head["queued_count"] == 2, "the held row is not claimable"


@pytest.mark.requires_postgres
def test_a_row_without_a_conversation_is_always_claimable(ops):
    _running("alpha", "r-1", "conv-1")
    _q("alpha", 0, "held", key="conv-1")
    _q("alpha", 4, "keyless")

    (head,) = ops.oldest_queued_head_per_agent(agent_names=["alpha"], claimable=True)

    assert head["execution_id"] == "keyless"


@pytest.mark.requires_postgres
def test_the_claimable_head_is_scoped_to_the_named_agents(ops):
    _running("alpha", "r-1", "conv-1")
    _q("alpha", 0, "held", key="conv-1")
    _q("beta", 1, "beta-free")

    assert ops.oldest_queued_head_per_agent(agent_names=["alpha"], claimable=True) == []
    assert ops.oldest_queued_head_per_agent(agent_names=[], claimable=True) == []


@pytest.mark.requires_postgres
def test_another_agents_running_turn_never_holds_this_agents_conversation(ops):
    """The #2843 guard is per agent: the same key running on beta does not
    hold alpha's row."""
    _running("beta", "r-b", "conv-1")
    _q("alpha", 0, "alpha-row", key="conv-1")

    (head,) = ops.oldest_queued_head_per_agent(agent_names=["alpha"], claimable=True)

    assert head["execution_id"] == "alpha-row"


def test_running_conversation_keys_read_the_2843_guard(ops):
    _q("alpha", 0, "running-one", key="conv-1")
    ops.claim_next_queued("alpha", worker_id="alpha#w0", lease_seconds=900)
    _q("alpha", 1, "waiting", key="conv-1")

    assert ops.running_conversation_keys_by_agent(["alpha", "beta"]) == {"alpha": ["conv-1"]}


# ---------------------------------------------------------------------------
# #4635 G3: the monitor's per-agent reads, batched per tick
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
def test_running_conversation_keys_are_read_for_many_agents_at_once(ops):
    _running("alpha", "r-a1", "conv-1")
    _running("alpha", "r-a2", "conv-2")
    _running("beta", "r-b1", "conv-9")
    _running("gamma", "r-g1", None)          # a keyless turn holds no conversation
    _q("alpha", 0, "queued-a", key="conv-3")  # a queued row holds nothing either

    keys = ops.running_conversation_keys_by_agent(["alpha", "beta", "gamma", "delta"])

    assert {a: sorted(k) for a, k in keys.items()} == {
        "alpha": ["conv-1", "conv-2"],
        "beta": ["conv-9"],
    }
    assert ops.running_conversation_keys_by_agent([]) == {}


def _leased(agent: str, eid: str, lease_delta_minutes: int):
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by, "
        " lease_expires_at, claim_token, claimed_by_worker) "
        "VALUES (:id, '__manual__', :a, 'running', :sa, 'm', 'mcp', :le, 'tok', 'w')",
        id=eid, a=agent, sa=_iso(_T0),
        le=_iso(datetime.now(timezone.utc) + timedelta(minutes=lease_delta_minutes)),
    )


@pytest.mark.requires_postgres
def test_an_expired_lease_is_not_a_busy_worker_when_the_caller_asks(ops):
    """Canary B-02's rule: a running row whose lease has expired has no worker
    behind it until the reaper requeues it, so it must not make a dead pool
    read as full. The meter's own callers keep counting every lease."""
    _leased("alpha", "live-1", 10)
    _leased("alpha", "live-2", 10)
    _leased("alpha", "dead-1", -10)
    _leased("beta", "dead-2", -10)
    now = _iso(datetime.now(timezone.utc))

    assert ops.count_active_leased_by_agent(["alpha", "beta"]) == {"alpha": 3, "beta": 1}
    assert ops.count_active_leased_by_agent(
        ["alpha", "beta"], unexpired_at=now
    ) == {"alpha": 2}
