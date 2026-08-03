"""Edge-case matrix (sub-area A) — `db/schedules/` CAS status writers (#1771 target 3).

Produced by `/edge-cases` v1.0 (understand → enumerate → generate → reflect →
verify). Companion files:

* ``test_1771c_schedules_cas_properties.py``      — Hypothesis properties P-A1…P-A4
* ``test_1771c_schedules_analytics_edges.py``     — sub-area B discrete cases
* ``test_1771c_schedules_analytics_properties.py``— sub-area B properties
* ``.plan/edge-cases-1771c-matrix.md``            — the persisted matrix

Subject under test: the CAS-guarded status writers of the #1082
"status-as-projection" contract — ``db/schedules/executions.py``
(``update_execution_status``) and ``db/schedules/queue.py`` (the backlog +
lease-reaper transitions).

⚠️  READ BEFORE ADDING A TEST HERE — the intuitive contract is WRONG.
At **this** layer ``SUCCESS`` overwrites ``running``, ``queued``,
``pending_retry``, ``skipped``, ``failed`` **and an existing ``success``**.
Only ``CANCELLED`` blocks it (#671). The "an authoritative terminal
short-circuits" behaviour lives UPSTREAM in the #1083 result-callback replay
guard, not in the DB layer. A property asserting "terminals are immutable"
here is a wrong test, not a finding.

Backend honesty (Invariant #3/#9): ``db_backend`` yields **SQLite only** unless
``TEST_POSTGRES_URL`` is set, so by default every case below is exercised on
SQLite. Rows whose behaviour is dialect-sensitive are marked ``[SQLITE-ONLY]``
in the matrix; nothing in this file is dialect-sensitive except where noted
(``expire_stale_queued`` string collation is ASCII-identical on both).

Deliberately NOT duplicated here:
``test_schedule_status_observability.py::TestStatusWriteProjectionGuard`` (the
AST inventory proving every status write carries a precondition) and
``test_cancelled_not_overwritten.py`` (SUCCESS-over-running / over-phantom-stale
-failed). Those already exist and already ``rglob`` the package.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable. pytest auto-adds `tests/` to
# sys.path, which means `tests/utils/` (the API test helpers package) would
# shadow `src/backend/utils/` when backend code does `from utils.helpers ...`.
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

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

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



@pytest.fixture
def ops(db_backend):
    """Composed ``ScheduleOperations`` bound to a fresh production schema.

    Instantiates the **composed** class (Invariant #2) — never a bare mixin,
    whose cross-slice ``self.<method>()`` calls would ``AttributeError``.
    """
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)
    from db.schedules import ScheduleOperations

    yield ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)


def insert_execution(
    exec_id: str,
    *,
    status: str = "running",
    agent_name: str = "agent-1",
    started_at: str = "2026-01-01T00:00:00.000000Z",
    **extra,
) -> str:
    """Insert a ``schedule_executions`` row through the active engine."""
    cols = {
        "id": exec_id,
        "schedule_id": "sched-1",
        "agent_name": agent_name,
        "status": status,
        "started_at": started_at,
        "message": "do the thing",
        "triggered_by": "schedule",
    }
    cols.update(extra)
    names = ", ".join(cols)
    binds = ", ".join(f":{k}" for k in cols)
    _hrun(f"INSERT INTO schedule_executions ({names}) VALUES ({binds})", **cols)
    return exec_id


def status_of(exec_id: str):
    return _hscalar("SELECT status FROM schedule_executions WHERE id = :i", i=exec_id)


def column_of(exec_id: str, col: str):
    return _hscalar(f"SELECT {col} FROM schedule_executions WHERE id = :i", i=exec_id)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ===========================================================================
# A1b — the genuinely untested third of the documented SUCCESS-wins contract
# ===========================================================================


@pytest.mark.parametrize(
    "prior_status",
    ["queued", "pending_retry", "skipped"],
    ids=["A1b-queued", "A1b-pending_retry", "A1b-skipped"],
)
def test_a1b_success_wins_over_non_cancelled_prior(ops, prior_status):
    """A1b — SUCCESS overwrites queued / pending_retry / skipped.

    ``test_cancelled_not_overwritten.py`` already pins SUCCESS-over-``running``
    and SUCCESS-over-phantom-stale-``failed``. These three are the remaining
    members of "everything except CANCELLED", and ``skipped`` is the
    counter-intuitive one: it is an *authoritative* terminal upstream, yet the
    DB layer lets SUCCESS through. Pinning it stops a future refactor from
    "tidying" ``skipped`` into the blocked set and silently dropping real
    completions.
    """
    eid = insert_execution(f"a1b-{prior_status}", status=prior_status)

    assert ops.update_execution_status(eid, "success", response="done") is True
    assert status_of(eid) == "success"
    assert column_of(eid, "response") == "done"


def test_a2_success_blocked_by_cancelled_only(ops):
    """A2 (regression re-pin) — CANCELLED is the ONE status that blocks SUCCESS.

    Covered elsewhere for the positive case; kept here as the paired negative
    so this file states the whole contract in one place.
    """
    eid = insert_execution("a2-cancelled", status="cancelled")

    assert ops.update_execution_status(eid, "success", response="late") is False
    assert status_of(eid) == "cancelled"
    assert column_of(eid, "response") is None


# ===========================================================================
# A3 — UNSPECIFIED: success → success wins twice at this layer
# ===========================================================================


def test_a3_success_over_success_wins_again_unspecified(ops):
    """A3 — a replayed SUCCESS wins the CAS a **second** time. UNSPECIFIED.

    This is a characterization test, not an assertion that the behaviour is
    correct. The DB layer's SUCCESS predicate is ``status != CANCELLED``, which
    an existing ``success`` row satisfies, so the second write lands and
    overwrites ``response`` / ``duration_ms`` / ``cost``.

    That is **not** a bug at this layer: the replay short-circuit is an
    upstream #1083 responsibility (``POST .../executions/{id}/result`` returns
    ``{replayed: true}`` for an authoritative terminal before ever reaching
    here). Pinned so that if the DB predicate is ever tightened, the change is
    visible and deliberate rather than an accidental behaviour break for the
    "late SUCCESS overwrites a reaper LEASE_EXPIRED" guarantee that depends on
    this looseness.
    """
    eid = insert_execution("a3-replay", status="running")

    assert ops.update_execution_status(eid, "success", response="first") is True
    assert ops.update_execution_status(eid, "success", response="second") is True
    assert status_of(eid) == "success"
    assert column_of(eid, "response") == "second"


# ===========================================================================
# A5 / A5b — row absence and malformed `started_at`
# ===========================================================================


def test_a5_missing_row_returns_false_without_raising(ops):
    """A5 — ``update_execution_status`` on an unknown id is a clean ``False``."""
    assert ops.update_execution_status("no-such-execution", "success") is False


def test_a5b_schema_enforces_started_at_not_null(ops):
    """A5b(i) — the live schema enforces ``started_at NOT NULL`` on both backends.

    This is a **schema-invariant guard**, and it is load-bearing for the sibling
    case below. ``update_execution_status`` calls
    ``parse_iso_timestamp(row["started_at"])`` *before* building the CAS WHERE;
    on ``None`` that helper raises ``AttributeError`` ("NoneType has no
    attribute 'endswith'"). The only thing standing between production and that
    crash is this NOT NULL constraint.

    Worth guarding because the Core metadata **disagrees**: ``db/tables.py``
    declares ``Column("started_at", Text)`` — i.e. nullable. That is harmless
    today only because neither backend is built from ``metadata.create_all``
    (SQLite uses ``db.schema.init_schema``; PostgreSQL uses
    ``init_schema_postgres``, which renders the *same* ``TABLES`` DDL). If a
    future migration to metadata-driven DDL (#746) lands, this test fails and
    names the consequence.
    """
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        insert_execution("a5b-null", started_at=None)

    insert_execution("a5b-upd", status="running")
    with pytest.raises(IntegrityError):
        _hrun("UPDATE schedule_executions SET started_at = NULL WHERE id = 'a5b-upd'")


@pytest.mark.parametrize(
    "bad_started_at",
    ["", "not-a-date", "2026-13-45T99:99:99Z"],
    ids=["A5b-empty", "A5b-garbage", "A5b-out-of-range"],
)
@pytest.mark.parametrize(
    "prior_status", ["running", "cancelled"], ids=["would-win", "would-lose"]
)
def test_a5b_malformed_started_at_raises_before_the_cas(
    ops, bad_started_at, prior_status
):
    """A5b(ii) — a malformed (non-NULL) ``started_at`` makes the writer RAISE.

    CHARACTERIZATION TEST — **accepted gap, not a filed bug.** Reason recorded
    in the /edge-cases report: the duration is computed *before* the CAS WHERE
    is built (``executions.py``), so the raise happens even for a write that
    would have **lost** the CAS anyway (the ``would-lose`` parametrization
    proves it — a ``cancelled`` row that should simply return ``False`` raises
    instead).

    Reachable **at this layer**: ``update_execution_to_queued`` stamps
    ``started_at`` from a *caller-supplied* ``queued_at`` string with no
    validation (see ``test_a5b_queued_at_is_unvalidated_at_this_layer``).
    NOT reachable from any current **production** caller: the sole caller,
    ``services/backlog_service.py``, passes ``utc_now_iso()``. So this is
    latent/defence-in-depth, and pinning it makes the contract visible if a
    future caller ever forwards an untrusted timestamp.
    """
    eid = insert_execution(
        f"a5b-{prior_status}-{abs(hash(bad_started_at)) % 10_000}",
        status=prior_status,
        started_at=bad_started_at,
    )

    with pytest.raises(ValueError):
        ops.update_execution_status(eid, "success", response="x")

    # The row is untouched — the raise precedes the UPDATE entirely.
    assert status_of(eid) == prior_status


def test_a5b_queued_at_is_unvalidated_at_this_layer(ops):
    """A5b(iii) — reachability proof for the case above.

    ``update_execution_to_queued`` copies its caller-supplied ``queued_at``
    straight into ``started_at`` with no parse/validation, so the malformed
    state is constructible through the public DB API rather than only by
    hand-writing a row.
    """
    eid = insert_execution("a5b-reach", status="running")

    assert ops.update_execution_to_queued(eid, "{}", "not-a-date") is True
    assert column_of(eid, "started_at") == "not-a-date"


# ===========================================================================
# A6 — REAL BUG: clock-skewed `started_at` persists a negative duration_ms
# ===========================================================================


def test_a6_clock_skew_must_not_persist_negative_duration(ops):
    """A6 — ``duration_ms`` must never be negative.

    Was a ``strict=True`` xfail pinning the unguarded subtraction. The clamp
    landed in #1832, so the marker is gone and this now asserts the contract
    directly, exactly as the xfail reason predicted it would.
    """
    future = _iso(datetime.now(timezone.utc) + timedelta(seconds=300))
    eid = insert_execution("a6-skew", status="running", started_at=future)

    assert ops.update_execution_status(eid, "success", response="ok") is True

    duration = column_of(eid, "duration_ms")
    assert duration >= 0, f"negative duration_ms persisted: {duration}"


def test_a6_skewed_row_clamps_to_zero(ops):
    """A6 (companion) — pins the post-clamp value, not just the sign.

    Replaces the characterization test that asserted ``< 0``. A skewed row
    lands on exactly ``0``, which is what makes it indistinguishable from a
    genuine sub-millisecond execution — a deliberate trade recorded in #1832.
    """
    future = _iso(datetime.now(timezone.utc) + timedelta(seconds=300))
    eid = insert_execution("a6-observed", status="running", started_at=future)

    assert ops.update_execution_status(eid, "success") is True
    assert column_of(eid, "duration_ms") == 0


# ===========================================================================
# A7 — mixed naive / Z / +00:00 `started_at` must not raise
# ===========================================================================


@pytest.mark.parametrize(
    "started_at",
    [
        "2026-01-01T00:00:00",  # naive (scheduler pre-#1474 legacy)
        "2026-01-01T00:00:00.000000Z",  # utc_now_iso() form
        "2026-01-01T00:00:00+00:00",  # explicit UTC offset
        "2026-01-01T05:00:00+05:00",  # non-UTC offset
    ],
    ids=["A7-naive", "A7-zulu", "A7-utc-offset", "A7-plus5-offset"],
)
def test_a7_mixed_timestamp_formats_do_not_raise(ops, started_at):
    """A7 — the ``aware − naive`` TypeError class (#1474) must not reappear.

    ``parse_iso_timestamp`` normalizes naive → UTC-aware before the subtraction,
    so every stored form in the wild (scheduler naive legacy rows, backend
    ``Z`` rows, offset-bearing rows) computes a duration rather than exploding
    mid-terminal-write. A crash here would strand the row ``running`` forever.
    """
    eid = insert_execution(
        f"a7-{started_at[-6:]}", status="running", started_at=started_at
    )

    assert ops.update_execution_status(eid, "success") is True
    assert isinstance(column_of(eid, "duration_ms"), int)


# ===========================================================================
# A8 — retry_count: None preserves, 0/1 write through (#678)
# ===========================================================================


@pytest.mark.parametrize(
    "retry_count, expected",
    [(None, 7), (0, 0), (1, 1), (3, 3)],
    ids=["A8-none-preserves", "A8-zero", "A8-one", "A8-three"],
)
def test_a8_retry_count_none_preserves_prior_value(ops, retry_count, expected):
    """A8 — ``retry_count=None`` must leave the column untouched (#678).

    The prior value is seeded as 7 so a regression that zeroes it (the exact
    failure the ``if retry_count is not None`` guard exists to prevent — cleanup
    and scheduler terminal paths pass ``None``) is unmistakable.
    """
    eid = insert_execution(f"a8-{retry_count}", status="running", retry_count=7)

    assert ops.update_execution_status(eid, "success", retry_count=retry_count) is True
    assert column_of(eid, "retry_count") == expected


# ===========================================================================
# A12 — bulk agent-scoped writers must not cross the tenant boundary
# ===========================================================================


@pytest.mark.parametrize(
    "method_name, terminal_status",
    [
        ("fail_queued_for_agent", "failed"),
        ("cancel_queued_for_agent", "cancelled"),
    ],
    ids=["A12-fail_queued", "A12-cancel_queued"],
)
def test_a12_bulk_writers_are_agent_scoped(ops, method_name, terminal_status):
    """A12 — bulk queue writers touch ONLY the named agent, and only ``queued``.

    ``fail_queued_for_agent`` had no unit-level DB coverage (only integration
    plus ``MagicMock`` wiring assertions in ``test_dispatch_breaker.py``), yet
    it fires on every dispatch-breaker trip (#526) — a cross-agent leak would
    fail an innocent tenant's backlog fleet-wide.

    Also pins the FAILED-vs-CANCELLED split the #526 acceptance criteria
    require: breaker-doomed rows must read as failures, not user cancellations.
    """
    insert_execution("a12-mine-q", status="queued", agent_name="agent-1")
    insert_execution("a12-mine-run", status="running", agent_name="agent-1")
    insert_execution("a12-mine-done", status="success", agent_name="agent-1")
    insert_execution("a12-other-q", status="queued", agent_name="agent-2")

    assert getattr(ops, method_name)("agent-1", "because") == 1

    assert status_of("a12-mine-q") == terminal_status
    assert column_of("a12-mine-q", "error") == "because"
    assert status_of("a12-mine-run") == "running"  # non-queued untouched
    assert status_of("a12-mine-done") == "success"  # terminal untouched
    assert status_of("a12-other-q") == "queued"  # other tenant untouched


@pytest.mark.parametrize(
    "method_name",
    [
        "fail_queued_for_agent",
        "cancel_queued_for_agent",
        "fail_all_nonterminal_for_agent",
    ],
    ids=["A12-fail_queued", "A12-cancel_queued", "A12-fail_nonterminal"],
)
@pytest.mark.parametrize(
    "agent_name", ["", "no-such-agent"], ids=["empty-name", "unknown-name"]
)
def test_a12_bulk_writers_no_match_returns_zero(ops, method_name, agent_name):
    """A12 — an empty or unknown agent name is a clean ``0``, never a wildcard.

    The empty-string case matters: an ``agent_name`` that arrives blank must not
    degrade into "match everything" (which is how a bulk writer becomes a
    fleet-wide outage).
    """
    insert_execution("a12-bystander", status="queued", agent_name="agent-1")

    assert getattr(ops, method_name)(agent_name) == 0
    assert status_of("a12-bystander") == "queued"


def test_a12_fail_all_nonterminal_covers_three_states(ops):
    """A12b — ``fail_all_nonterminal_for_agent`` sweeps queued + running +
    pending_retry and stops at every terminal (trinity-enterprise#69)."""
    for state in ("queued", "running", "pending_retry"):
        insert_execution(f"a12b-{state}", status=state, agent_name="agent-1")
    for state in ("success", "failed", "cancelled", "skipped"):
        insert_execution(f"a12b-{state}", status=state, agent_name="agent-1")

    assert ops.fail_all_nonterminal_for_agent("agent-1", "ghost_discarded") == 3

    for state in ("queued", "running", "pending_retry"):
        assert status_of(f"a12b-{state}") == "failed"
    for state in ("success", "failed", "cancelled", "skipped"):
        assert status_of(f"a12b-{state}") == state


# ===========================================================================
# A13 / A14 — expire_stale_queued boundary + queued_at NULL
# ===========================================================================


def test_a13_exact_cutoff_row_is_not_expired(ops, monkeypatch):
    """A13 — a row queued at *exactly* the cutoff second survives. DOCUMENTED,
    not a bug.

    ``expire_stale_queued`` builds its threshold with
    ``strftime('%Y-%m-%dT%H:%M:%S')`` — no fractional part, no ``Z`` — while
    ``queued_at`` is written by ``utc_now_iso()`` as ``…:SS.ffffffZ``. At an
    equal second-prefix the stored value is the *longer* string, so
    ``queued_at < threshold`` is False and the row is spared.

    Net effect is a ≤1s error band on a 24h window: measured, benign, and worth
    pinning precisely because it is the kind of Invariant-#16 mismatch that
    looks like a bug to the next reader. Backend-agnostic: ASCII ISO-8601
    collation is identical on SQLite and PostgreSQL.

    ⏱  ONE clock sample, deliberately (#1909). This case pins a **zero-width
    boundary**, so it cannot be de-raced by widening a margin the way its
    siblings are (``test_backlog.py`` calls that the "time-mock-free pattern",
    and every other clock site in this file carries a 24-min-to-23-hour cushion).
    Before the fix the test read ``datetime.now()`` here and
    ``expire_stale_queued`` read it *again* at query time: when the wall second
    rolled over between the two, the threshold advanced, ``exact`` became
    strictly older, the row was expired, and the assertions flipped — measured
    at 3.3% on CI-class hardware and 100% when the rollover is forced into the
    window. Freezing the clock the implementation reads makes setup and query
    observe the same instant, so the race is gone *by construction* rather than
    made less likely. Do NOT "fix" a future recurrence by nudging ``exact`` off
    the boundary — that silently deletes the property this test exists to pin.
    """
    import db.schedules.queue as queue_mod

    frozen = datetime.now(timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz else frozen.replace(tzinfo=None)

    monkeypatch.setattr(queue_mod, "datetime", _FrozenDatetime)
    # Patch-took-effect guard: if `queue.py` ever aliases its datetime import,
    # `setattr` silently binds a name nothing reads and the test drifts back to
    # sampling two clocks. That degrades to the #1909 flake, not to a false
    # pass — but a flake is exactly what this asserts we no longer have, so
    # fail loudly here instead of intermittently three months from now.
    assert queue_mod.datetime.now(timezone.utc) == frozen

    exact = (frozen - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S") + ".000000Z"
    older = _iso(frozen - timedelta(hours=24, seconds=5))

    insert_execution("a13-exact", status="queued", queued_at=exact)
    insert_execution("a13-older", status="queued", queued_at=older)

    assert ops.expire_stale_queued(24) == 1
    assert status_of("a13-exact") == "queued"
    assert status_of("a13-older") == "failed"
    assert "24" in (column_of("a13-older", "error") or "")


@pytest.mark.parametrize(
    "max_age_hours, age_hours, should_expire",
    [
        (24, 25, True),
        (24, 1, False),
        (0.5, 1, True),  # fractional window
        (0.5, 0.1, False),
        (1e6, 25, False),  # absurdly wide window expires nothing
    ],
    ids=[
        "A13-24h-old",
        "A13-24h-fresh",
        "A13-frac-old",
        "A13-frac-fresh",
        "A13-huge-window",
    ],
)
def test_a13_expire_window_arithmetic(ops, max_age_hours, age_hours, should_expire):
    """A13 — numeric boundary sweep over ``max_age_hours`` incl. fractional and
    absurdly large windows (no overflow, no raise)."""
    queued_at = _iso(datetime.now(timezone.utc) - timedelta(hours=age_hours))
    insert_execution("a13-row", status="queued", queued_at=queued_at)

    expired = ops.expire_stale_queued(max_age_hours)

    assert expired == (1 if should_expire else 0)
    assert status_of("a13-row") == ("failed" if should_expire else "queued")


def test_a14_null_queued_at_is_excluded_from_expiry(ops):
    """A14 — a ``queued`` row with ``queued_at IS NULL`` is never expired.

    The ``isnot(None)`` filter is what keeps a NULL out of the string
    comparison. Without it the row would be untouchable-but-scanned forever
    (SQL NULL comparison is neither true nor false); with it, the row is
    explicitly out of scope and the FIFO drain owns it.
    """
    insert_execution("a14-null", status="queued", queued_at=None)

    assert ops.expire_stale_queued(0.0) == 0
    assert status_of("a14-null") == "queued"


# ===========================================================================
# A15/A16 — read-side scoping (get_queued_count, find_expired_leases)
# ===========================================================================


def test_a15_get_queued_count_is_agent_scoped(ops):
    """A15 — ``get_queued_count`` counts only this agent's ``queued`` rows.

    It is the number ``CapacityManager`` reports as backlog depth, and canary
    B-01 compares it against an independently-collected id set — a cross-agent
    or cross-status leak would show up as a fleet-wide invariant violation.
    """
    insert_execution("a15-q1", status="queued", agent_name="agent-1")
    insert_execution("a15-q2", status="queued", agent_name="agent-1")
    insert_execution("a15-run", status="running", agent_name="agent-1")
    insert_execution("a15-other", status="queued", agent_name="agent-2")

    assert ops.get_queued_count("agent-1") == 2
    assert ops.get_queued_count("agent-2") == 1
    assert ops.get_queued_count("nobody") == 0
    assert ops.get_queued_count("") == 0


def test_a16_find_expired_leases_excludes_future_and_null_leases(ops):
    """A16 — the lease reaper's candidate query is disjoint from every non-pull
    row (#1081 Phase 3).

    ``lease_expires_at IS NOT NULL`` is the load-bearing clause: push and #1083
    fire-and-forget rows leave it NULL, so a regression that dropped it would
    make the reaper FAIL live async executions that are working perfectly.
    """
    now = datetime.now(timezone.utc)
    past = _iso(now - timedelta(minutes=5))
    future = _iso(now + timedelta(minutes=5))

    insert_execution("a16-expired", status="running", lease_expires_at=past)
    insert_execution("a16-future", status="running", lease_expires_at=future)
    insert_execution("a16-nolease", status="running", lease_expires_at=None)
    insert_execution("a16-terminal", status="failed", lease_expires_at=past)

    found = {row["id"] for row in ops.find_expired_leases(now_iso=_iso(now))}

    assert found == {"a16-expired"}


def test_a16_find_expired_leases_respects_limit_and_ordering(ops):
    """A16b — oldest lease first, capped at ``limit`` (bounded reaper batch)."""
    now = datetime.now(timezone.utc)
    for i in range(5):
        insert_execution(
            f"a16b-{i}",
            status="running",
            lease_expires_at=_iso(now - timedelta(minutes=10 - i)),
        )

    rows = ops.find_expired_leases(now_iso=_iso(now), limit=3)

    assert [r["id"] for r in rows] == ["a16b-0", "a16b-1", "a16b-2"]
    assert ops.find_expired_leases(now_iso=_iso(now), limit=0) == []
