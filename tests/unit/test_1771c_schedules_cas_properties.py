"""Hypothesis properties (sub-area A) — CAS status writers (#1771 target 3).

Properties P-A1…P-A4 from the `/edge-cases` matrix
(``.plan/edge-cases-1771c-matrix.md``). The discrete companion cases live in
``test_1771c_schedules_cas_edges.py``.

Why properties and not more examples
------------------------------------
Every pre-existing CAS test is a **single** transition. The whole #1082 bug
class ("status-as-projection") is about *sequences* — a late worker result, a
second reaper pass, a replayed callback. P-A1 is a bounded stateful machine
because that is the smallest construct which explores orderings a parametrized
table cannot.

⚠️  THE TRAP THIS FILE DELIBERATELY AVOIDS
The tempting property "an authoritative terminal is never overwritten" is
**FALSE at this layer** — SUCCESS legitimately upgrades ``failed`` (phantom-
stale recovery, #378), ``skipped``, and even ``success``. Only ``CANCELLED``
blocks it (#671). P-A1 therefore states the invariant that IS true and IS
load-bearing: **terminal is absorbing with respect to non-terminal** — no
writer here can resurrect a terminal row back into ``queued`` / ``running`` /
``pending_retry`` (the canary E-02 phantom-reversal class).

CI bounds: ``max_examples`` 100 (DB-touching) / 200 (pure), ``deadline=None``.

Hypothesis ↔ function-scoped fixture: ``@given`` + ``db_backend`` raises
``FailedHealthCheck``. Suppressing it is necessary but **NOT sufficient** — the
DB is then built once and shared by every example. Correctness comes from each
example drawing a **unique** execution id / agent name, so cross-example state
is harmless by construction.

Backend honesty (Invariant #3/#9): SQLite only unless ``TEST_POSTGRES_URL`` is
set. Nothing asserted here is dialect-sensitive: P-A1/P-A2/P-A3 are pure CAS
row-count semantics, and P-A4 is ASCII string collation (identical on both).
Concurrency semantics ARE dialect-specific (PostgreSQL ``FOR UPDATE SKIP
LOCKED`` vs SQLite writer serialisation) and are deliberately **out of scope**
here — ``test_1081_pull_endpoints.py::TestClaimConcurrencyC1`` owns them.
"""

from __future__ import annotations

import itertools
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

# ---------------------------------------------------------------------------
# Bootstrap (see the sibling edges file for the tests/utils shadowing rationale)
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402,F401

_DB_MODULES = ("db.connection", "db.schedules", "db.activities", "database")

# ---------------------------------------------------------------------------
# sys.modules hygiene (#762 lint). Both evictions in this file are outside
# monkeypatch's reach, or actively wrong for it:
#   * the `utils*` shadow clear above runs at IMPORT time, before any fixture
#     exists, so monkeypatch structurally cannot reach it;
#   * the `ops` fixture must evict `db.*` so `from db.schedules import ...`
#     re-imports against the harness-bound engine. `monkeypatch.delitem`
#     records NO undo for a key that was absent on entry, so the freshly
#     imported, harness-bound module would stay resident for later files —
#     strictly worse isolation than the explicit pop it would replace.
# Snapshot/restore covers both the present-on-entry and absent-on-entry cases,
# which is the leak the lint actually guards against.
# Precedent: tests/unit/test_telegram_webhook_backfill.py.
# ---------------------------------------------------------------------------
_STUBBED_MODULE_NAMES = [
    "utils",
    "utils.api_client",
    "utils.assertions",
    "utils.cleanup",
    *_DB_MODULES,
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot the evicted modules before each test and restore after.

    Purely a teardown-time guarantee: the snapshot is taken before the test
    body, so no in-test behaviour (and no assertion) changes.
    """
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


TERMINAL = frozenset({"success", "failed", "cancelled", "skipped"})
NON_TERMINAL = frozenset({"queued", "running", "pending_retry"})
ALL_STATUSES = sorted(TERMINAL | NON_TERMINAL)

# Per-example uniqueness. Hypothesis shares one DB across examples (the
# function-scoped-fixture health check is suppressed), so every example MUST
# key its rows on a fresh id/agent or examples would contaminate each other and
# manufacture false greens.
_counter = itertools.count()


def _uid(prefix: str) -> str:
    return f"{prefix}-{next(_counter)}"


@pytest.fixture
def ops(db_backend):
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)
    from db.schedules import ScheduleOperations

    yield ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _insert(exec_id, *, status="running", agent_name="agent-1", **extra):
    cols = {
        "id": exec_id,
        "schedule_id": "sched-1",
        "agent_name": agent_name,
        "status": status,
        "started_at": _iso(datetime.now(timezone.utc) - timedelta(seconds=1)),
        "message": "m",
        "triggered_by": "schedule",
    }
    cols.update(extra)
    names = ", ".join(cols)
    binds = ", ".join(f":{k}" for k in cols)
    _hrun(f"INSERT INTO schedule_executions ({names}) VALUES ({binds})", **cols)
    return exec_id


def _status(exec_id):
    return _hscalar("SELECT status FROM schedule_executions WHERE id = :i", i=exec_id)


# ===========================================================================
# P-A1 — Invariant preservation (stateful): terminal is absorbing
# ===========================================================================

_MACHINE_OPS: list = []


class CasWriterMachine(RuleBasedStateMachine):
    """Drive one execution row through arbitrary interleavings of CAS writers.

    The invariant checked after every step is the true #1082 / canary-E-02
    statement: **once observed terminal, never observed non-terminal again.**

    Bounded on purpose (``stateful_step_count=8``, ``max_examples=30`` ⇒ a few
    hundred SQL statements). If this ever turns slow or flaky the documented
    downgrade is a parametrized 3-step sequence table — do NOT paper over it
    with a suppressed health check.
    """

    def __init__(self):
        super().__init__()
        self.ops = _MACHINE_OPS[0]
        self.agent = _uid("m-agent")
        self.eid = _insert(_uid("m-exec"), status="running", agent_name=self.agent)
        self.seen_terminal = False

    # --- rules: every CAS writer that can touch a single row ---------------

    @rule()
    def to_queued(self):
        self.ops.update_execution_to_queued(
            self.eid, "{}", _iso(datetime.now(timezone.utc) - timedelta(hours=48))
        )

    @rule()
    def claim(self):
        self.ops.claim_next_queued(self.agent)

    @rule()
    def claim_with_lease(self):
        self.ops.claim_next_queued(self.agent, worker_id="w1", lease_seconds=-60)

    @rule()
    def release(self):
        self.ops.release_claim_to_queued(self.eid)

    @rule()
    def requeue_lease(self):
        self.ops.requeue_expired_lease(self.eid)

    @rule()
    def park_lease(self):
        self.ops.park_expired_lease(self.eid, "poison")

    @rule()
    def cancel(self):
        self.ops.cancel_queued_execution(self.eid)

    @rule(status=st.sampled_from(sorted(TERMINAL | {"running"})))
    def finalize(self, status):
        self.ops.update_execution_status(self.eid, status, response="r")

    @rule()
    def bulk_fail_queued(self):
        self.ops.fail_queued_for_agent(self.agent)

    @rule()
    def bulk_fail_nonterminal(self):
        self.ops.fail_all_nonterminal_for_agent(self.agent)

    @rule()
    def expire(self):
        self.ops.expire_stale_queued(24)

    @rule()
    def dispatch_marker(self):
        self.ops.mark_execution_dispatched(self.eid)

    # --- the invariant (read-only by construction) -------------------------

    @invariant()
    def terminal_is_absorbing(self):
        current = _status(self.eid)
        assert current in TERMINAL | NON_TERMINAL, f"unknown status {current!r}"
        if current in TERMINAL:
            self.seen_terminal = True
        elif self.seen_terminal:
            raise AssertionError(
                f"E-02 phantom reversal: row {self.eid} returned to "
                f"non-terminal {current!r} after reaching a terminal state"
            )


def test_p_a1_terminal_is_absorbing(ops):
    """P-A1 — across ANY legal interleaving of the CAS writers, a terminal row
    is never resurrected into a non-terminal state.

    This is the mechanised form of canary **E-02** at the DB layer, and it is
    the property the single-transition tests structurally cannot express.
    """
    _MACHINE_OPS.clear()
    _MACHINE_OPS.append(ops)
    run_state_machine_as_test(
        CasWriterMachine,
        settings=settings(
            max_examples=30,
            stateful_step_count=8,
            deadline=None,
            suppress_health_check=[
                HealthCheck.function_scoped_fixture,
                HealthCheck.too_slow,
            ],
        ),
    )


class _ResurrectingMachine(CasWriterMachine):
    """Sabotaged machine used ONLY by the meta-test below.

    Adds one rule that does what no production writer can: a raw, precondition-
    free UPDATE that drags the row back to ``queued``.
    """

    @rule()
    def illegally_resurrect(self):
        _hrun(
            "UPDATE schedule_executions SET status = 'queued' WHERE id = :i",
            i=self.eid,
        )


def test_p_a1_meta_the_invariant_can_actually_fail(ops):
    """META — proves P-A1 is load-bearing, not vacuously green.

    ``run_state_machine_as_test`` reports no Hypothesis statistics through the
    pytest plugin, so "it passed" is by itself weak evidence: a machine whose
    rules all no-op, or whose invariant never observes a terminal, would pass
    identically. This test injects a deliberate E-02 phantom reversal and
    asserts the machine **finds and reports it** — the same meta-test discipline
    ``test_schedule_status_observability.py::TestStatusWriteProjectionGuard``
    already applies to the AST guard.
    """
    _MACHINE_OPS.clear()
    _MACHINE_OPS.append(ops)
    with pytest.raises(AssertionError, match="phantom reversal"):
        run_state_machine_as_test(
            _ResurrectingMachine,
            settings=settings(
                max_examples=50,
                stateful_step_count=8,
                deadline=None,
                suppress_health_check=[
                    HealthCheck.function_scoped_fixture,
                    HealthCheck.too_slow,
                ],
            ),
        )


# ===========================================================================
# P-A2 — No-crash-total, asserted PER RETURN CLASS
# ===========================================================================
#
# The writers do NOT share a return type. Asserting "returns a bool" globally
# would be a wrong test that passes for the wrong reason (`bool` is a subclass
# of `int`, so an int-returning bulk writer would sail through `isinstance(x,
# int)` and a rowcount of 0/1 would even satisfy `in (True, False)`).
#
#   bool          -> single-row CAS writers
#   int           -> agent-scoped bulk writers (rowcount)
#   Optional[Dict]-> claim_next_queued
#
# `started_at` is held well-formed here on purpose: the malformed case is a
# KNOWN pre-CAS raise (matrix row A5b) and folding it in would make this
# property assert something false.

_BOOL_WRITERS = [
    ("update_execution_status", lambda o, e, a: o.update_execution_status(e, "failed")),
    ("mark_execution_dispatched", lambda o, e, a: o.mark_execution_dispatched(e)),
    (
        "update_execution_to_queued",
        lambda o, e, a: o.update_execution_to_queued(
            e, "{}", _iso(datetime.now(timezone.utc))
        ),
    ),
    ("release_claim_to_queued", lambda o, e, a: o.release_claim_to_queued(e)),
    ("requeue_expired_lease", lambda o, e, a: o.requeue_expired_lease(e)),
    ("park_expired_lease", lambda o, e, a: o.park_expired_lease(e, "poison")),
    ("cancel_queued_execution", lambda o, e, a: o.cancel_queued_execution(e)),
]

_INT_WRITERS = [
    ("cancel_queued_for_agent", lambda o, e, a: o.cancel_queued_for_agent(a)),
    ("fail_queued_for_agent", lambda o, e, a: o.fail_queued_for_agent(a)),
    (
        "fail_all_nonterminal_for_agent",
        lambda o, e, a: o.fail_all_nonterminal_for_agent(a),
    ),
    ("expire_stale_queued", lambda o, e, a: o.expire_stale_queued(0.0)),
]


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    prior_status=st.sampled_from(ALL_STATUSES),
    writer_index=st.integers(min_value=0, max_value=len(_BOOL_WRITERS) - 1),
)
def test_p_a2_bool_writers_never_raise_and_return_bool(ops, prior_status, writer_index):
    """P-A2a — every single-row CAS writer returns a **strict bool** and never
    raises, for any prior status in the full domain."""
    name, invoke = _BOOL_WRITERS[writer_index]
    agent = _uid("p2-agent")
    eid = _insert(_uid("p2"), status=prior_status, agent_name=agent)

    result = invoke(ops, eid, agent)

    assert (
        result is True or result is False
    ), f"{name} returned {result!r} ({type(result).__name__}), expected a bool"


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    prior_status=st.sampled_from(ALL_STATUSES),
    writer_index=st.integers(min_value=0, max_value=len(_INT_WRITERS) - 1),
)
def test_p_a2_bulk_writers_never_raise_and_return_nonneg_int(
    ops, prior_status, writer_index
):
    """P-A2b — every bulk writer returns a non-negative rowcount ``int`` (NOT a
    bool) and never raises, for any prior status."""
    name, invoke = _INT_WRITERS[writer_index]
    agent = _uid("p2b-agent")
    _insert(
        _uid("p2b"),
        status=prior_status,
        agent_name=agent,
        queued_at=_iso(datetime.now(timezone.utc) - timedelta(hours=48)),
    )

    result = invoke(ops, None, agent)

    assert isinstance(result, int) and not isinstance(
        result, bool
    ), f"{name} returned {result!r} ({type(result).__name__}), expected int"
    assert result >= 0


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(prior_status=st.sampled_from(ALL_STATUSES))
def test_p_a2_claim_returns_dict_or_none(ops, prior_status):
    """P-A2c — ``claim_next_queued`` returns a dict (only from a ``queued`` row)
    or ``None``; never raises, never returns a bool."""
    agent = _uid("p2c-agent")
    _insert(
        _uid("p2c"),
        status=prior_status,
        agent_name=agent,
        queued_at=_iso(datetime.now(timezone.utc)),
    )

    result = ops.claim_next_queued(agent)

    if prior_status == "queued":
        assert isinstance(result, dict)
        assert result["agent_name"] == agent
    else:
        assert result is None


# ===========================================================================
# P-A3 — Idempotence, again PER RETURN CLASS
# ===========================================================================


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    prior_status=st.sampled_from(ALL_STATUSES),
    writer_index=st.integers(min_value=0, max_value=len(_BOOL_WRITERS) - 1),
)
def test_p_a3_bool_writer_second_call_is_a_no_op(ops, prior_status, writer_index):
    """P-A3a — a second identical single-row call is a genuine no-op.

    Every one of these writers is a CAS whose precondition its own success
    invalidates, so a replay must return ``False`` and leave ``status``
    untouched. The one documented exception is carved out below rather than
    weakened away: ``update_execution_status(SUCCESS)`` is intentionally
    re-winnable (matrix row A3 / the "late SUCCESS overwrites a reaper
    LEASE_EXPIRED" guarantee), and this property invokes it with ``failed`` so
    the carve-out is not silently in play.
    """
    name, invoke = _BOOL_WRITERS[writer_index]
    agent = _uid("p3-agent")
    eid = _insert(_uid("p3"), status=prior_status, agent_name=agent)

    first = invoke(ops, eid, agent)
    after_first = _status(eid)
    second = invoke(ops, eid, agent)
    after_second = _status(eid)

    assume(first is True)  # only replays of a winning call are interesting
    assert second is False, f"{name} won its CAS twice"
    assert (
        after_second == after_first
    ), f"{name} changed status on replay: {after_first!r} -> {after_second!r}"


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    prior_status=st.sampled_from(ALL_STATUSES),
    writer_index=st.integers(min_value=0, max_value=len(_INT_WRITERS) - 1),
)
def test_p_a3_bulk_writer_second_call_returns_zero(ops, prior_status, writer_index):
    """P-A3b — a bulk writer's replay reports ``0`` rows and mutates nothing."""
    name, invoke = _INT_WRITERS[writer_index]
    agent = _uid("p3b-agent")
    eid = _insert(
        _uid("p3b"),
        status=prior_status,
        agent_name=agent,
        queued_at=_iso(datetime.now(timezone.utc) - timedelta(hours=48)),
    )

    first = invoke(ops, None, agent)
    after_first = _status(eid)
    second = invoke(ops, None, agent)

    assume(first > 0)
    assert second == 0, f"{name} affected rows twice ({first} then {second})"
    assert _status(eid) == after_first


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(queued_rows=st.integers(min_value=1, max_value=4))
def test_p_a3_claim_drains_then_returns_none(ops, queued_rows):
    """P-A3c — ``claim_next_queued`` hands out each queued row exactly once and
    then returns ``None`` (the backlog is drained, not re-served).

    Exactly-once is the property BACKLOG-001 rests on: a re-served row is a
    duplicated agent execution (double spend, double side-effect).
    """
    agent = _uid("p3c-agent")
    base = datetime.now(timezone.utc)
    expected = [
        _insert(
            _uid("p3c"),
            status="queued",
            agent_name=agent,
            queued_at=_iso(base + timedelta(seconds=i)),
        )
        for i in range(queued_rows)
    ]

    claimed = []
    for _ in range(queued_rows):
        row = ops.claim_next_queued(agent)
        assert row is not None
        claimed.append(row["id"])

    assert claimed == expected, "FIFO order violated"
    assert len(set(claimed)) == queued_rows, "a row was claimed twice"
    assert ops.claim_next_queued(agent) is None


# ===========================================================================
# P-A4 — Oracle: lexicographic ISO order ≡ chronological order
# ===========================================================================
#
# Pure function property: no fixture, no DB, no health-check suppression.

_UTC_DATETIMES = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2099, 12, 31),
).map(lambda d: d.replace(tzinfo=timezone.utc))


def _z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@settings(max_examples=200, deadline=None)
@given(a=_UTC_DATETIMES, b=_UTC_DATETIMES)
def test_p_a4_zulu_iso_sorts_chronologically(a, b):
    """P-A4 — for ``utc_now_iso()``-formatted strings, string ``<`` **is**
    chronological ``<``.

    This is the assumption ``expire_stale_queued`` and ``find_expired_leases``
    rest on: both compare ``queued_at`` / ``lease_expires_at`` as **strings** in
    SQL rather than parsing them. The format's fixed-width zero-padded fields
    are what make that sound. Backend-agnostic — ASCII collation is identical on
    SQLite and PostgreSQL.
    """
    assert (_z(a) < _z(b)) == (a < b)
    assert (_z(a) == _z(b)) == (a == b)


@settings(max_examples=200, deadline=None)
@given(dt=_UTC_DATETIMES, offset_hours=st.integers(min_value=1, max_value=14))
def test_p_a4_mixed_offset_formats_break_lexicographic_order(dt, offset_hours):
    """P-A4b — the SAME instant written with a non-UTC offset does NOT compare
    correctly as a string. This is WHY format discipline is load-bearing.

    Documents the failure mode Invariant #16 exists to prevent: mixing
    ``+05:00``-style timestamps into a column that SQL compares lexicographically
    silently corrupts every window query over it. Asserted as "the naive string
    comparison disagrees with the true chronology", so the test is a live
    explanation rather than a comment.
    """
    aware = dt.astimezone(timezone(timedelta(hours=offset_hours)))
    offset_form = aware.isoformat()  # e.g. 2026-01-01T05:00:00+05:00
    zulu_form = _z(dt)  # same instant, Z form

    assume(offset_form[:4] == zulu_form[:4])  # ignore year-rollover pairs
    # Same instant...
    assert datetime.fromisoformat(offset_form) == dt
    # ...yet the strings are not equal, so a lexicographic comparison against a
    # Z-form threshold cannot be trusted.
    assert offset_form != zulu_form
