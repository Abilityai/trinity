"""#1771 target 1 — property tests for the retention blast-radius guard (P-38).

Companion to `test_1771a_retention_edges.py` (parametrized examples). Where that
file pins named boundaries, this one asserts the invariants that must hold across
the whole input space.

TWO TIERS, because the fixture cost differs by two orders of magnitude:

* **Tier 1 — pure** (`max_examples=200`). No DB. `evaluate` is near-100% branching
  over an injected `count_fn` plus four thin `db` calls, so its whole decision
  table is exercisable in-memory.
* **Tier 2 — real DB** (`max_examples=30`). Predicate parity and full drain, which
  need actual SQL because the property under test IS the SQL predicate.

THE ORACLE, NOT A MEMBERSHIP CHECK
----------------------------------
`test_decision_table_matches_an_independent_oracle` recomputes the expected
`(allowed, candidates, threshold, reason)` from the spec and asserts EQUALITY.
It deliberately does not assert "the result is one of the five legal verdicts":
`evaluate` has exactly five `return` statements, so a membership check is a
tautology that survives `<=` -> `<`, `if acked` -> `if not acked`, and
`count_fn(threshold + 1)` -> `count_fn(threshold)` — the three mutants that matter
most. All three are verified to break this oracle (see its docstring).

TWO TRAPS THIS FILE HANDLES EXPLICITLY (both produce silent FALSE GREENS)
------------------------------------------------------------------------
1. **A fixture is NOT reset between Hypothesis examples.** A function-scoped
   fixture is built once per test FUNCTION, and `@given` runs many examples inside
   it. Hypothesis raises `FailedHealthCheck` to say so. Rather than suppress that,
   Tier 1 uses a per-example CONTEXT MANAGER (`isolated_guard`) — the fix
   Hypothesis itself recommends — and Tier 2 keeps its engine module-scoped and
   TRUNCATES at the top of every example. Suppressing the health check here would
   have shipped two real bugs: an alarm-count property that accumulates across
   examples, and row sets that leak from one example into the next.
2. **`TRINITY_DB_PATH` alone is not isolation.** `db/engine.py:34-41`
   `resolve_database_url()` reads **`DATABASE_URL` first**. With that exported —
   routine in this repo — a per-example `DELETE FROM schedule_executions` and a
   real `prune_execution_rows` would run against THAT database. In a task about
   destructive-path safety that is the one unacceptable failure mode, so the
   fixture unsets both env vars and ASSERTS the resolved URL points at its own
   temp file before any test issues a DELETE.

Invocation: `cd tests && python -m pytest unit/test_1771a_retention_properties.py`.
"""

import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.retention_guard as _RG  # noqa: E402
from services import cleanup_service as _CS  # noqa: E402

# CI-bounded (AC#2): pure properties are cheap, DB properties are not.
PURE_EXAMPLES = 200
DB_EXAMPLES = 30


# ---------------------------------------------------------------------------
# Doubles + per-example isolation
# ---------------------------------------------------------------------------


class _DbDouble:
    """In-memory stand-in for the four `db` methods this subsystem touches.

    Verified signatures: `get_setting_value(key, default=None)`,
    `set_setting(key, value)`, `delete_setting(key)` (database.py:1705-1711),
    `create_operator_queue_item(agent_name, item)` (database.py:2513).
    """

    def __init__(self):
        self.settings = {}
        self.queue_items = []
        self.writes = 0

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.writes += 1
        self.settings[key] = value

    def delete_setting(self, key):
        self.writes += 1
        self.settings.pop(key, None)

    def create_operator_queue_item(self, agent_name, item):
        self.writes += 1
        self.queue_items.append((agent_name, item))


@contextmanager
def _null_patch():
    """A do-nothing `with` target, so the error-path branch of the oracle property
    stays a single code path instead of forking into two near-identical bodies."""
    yield


@contextmanager
def isolated_guard():
    """Fresh `db` double + empty transition memo, scoped to ONE Hypothesis example.

    Deliberately a context manager rather than a pytest fixture. A function-scoped
    fixture is built once per test FUNCTION and is NOT reset between the inputs
    `@given` generates; Hypothesis raises `FailedHealthCheck` to say exactly that,
    and this is the fix it recommends. Suppressing the health check instead would
    have left real shared state in place:

      * `_RG.db` — the double accumulates `queue_items` across examples, so
        "exactly one alarm" silently becomes "one alarm, eventually";
      * `_RG._last_refused` — module state that outlives the whole session
        (`test_1644:177` already writes to it, and CI shuffles order under
        `pytest-randomly`), so a "first refusal" example degrades into a "repeat
        refusal" one depending on what ran before.

    Both patches are BY OBJECT: `retention_guard.py:56` does
    `from database import db`, binding its own module-global, so patching
    `database.db` would be inert (learnings.md:99).
    """
    double = _DbDouble()
    with patch.object(_RG, "db", double), patch.object(_RG, "_last_refused", {}):
        yield double


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Floors bounded well below MAX_ROWS_PER_SWEEP so BOTH sides of every threshold
# are reachable by random search, not just the "far under" case.
_floors = st.one_of(st.none(), st.integers(min_value=-2, max_value=200))
_available = st.integers(min_value=0, max_value=400)
_windows = st.integers(min_value=0, max_value=3650)
_keys = st.sampled_from(
    [
        "execution_log_retention_days",
        "execution_row_retention_days",
        "health_check_retention_days",
        "agent_soft_delete_retention_days",
        "schedule_soft_delete_retention_days",
        "agent_reports_retention_days",
        "operator_queue_retention_days",
        "agent_reminders_retention_days",
    ]
)


def _expected_verdict(available, floor, acked, count_raises=False, ack_raises=False):
    """Independent reimplementation of `evaluate`'s decision table.

    Written from the SPEC (the ':153' docstring + the module design notes), not by
    copying the implementation, so it is a real oracle. `min(available, limit)`
    mirrors what `_bounded_count` actually does — which is what makes this
    property sensitive to the `threshold + 1` bound.

    The two error branches are modelled here rather than left to the example
    tests, so this one property covers the FAIL-CLOSED guarantee too. That matters:
    a hand-applied mutation check showed that flipping `count_failed`'s verdict
    from False to True — inverting the whole fail-closed design (module docstring
    note 3: "a guard that fails open is worse than no guard, because it
    manufactures confidence") — was caught only by `test_1644`, not by this file.
    A property file that cannot detect the single most dangerous mutation in its
    own subject is not carrying its weight.
    """
    threshold = _RG.MAX_ROWS_PER_SWEEP if floor is None else floor
    if count_raises:
        return False, -1, threshold, "count_failed"
    candidates = min(available, threshold + 1)
    if candidates <= threshold:
        return True, candidates, threshold, "under_threshold"
    if ack_raises:
        return False, candidates, threshold, "ack_lookup_failed"
    if acked:
        return True, candidates, threshold, "acknowledged"
    return False, candidates, threshold, "over_threshold"


# ---------------------------------------------------------------------------
# Tier 1 — pure properties
# ---------------------------------------------------------------------------


class TestDecisionTable:

    @given(
        available=_available,
        floor=_floors,
        acked=st.booleans(),
        window=_windows,
        key=_keys,
        count_raises=st.booleans(),
        ack_raises=st.booleans(),
    )
    @settings(max_examples=PURE_EXAMPLES, deadline=None)
    # A5 — exactly at the global threshold.
    @example(
        available=_RG.MAX_ROWS_PER_SWEEP,
        floor=None,
        acked=False,
        window=90,
        key="execution_row_retention_days",
        count_raises=False,
        ack_raises=False,
    )
    # A4 — one over the global threshold, unacknowledged (the #1638 shape).
    @example(
        available=_RG.MAX_ROWS_PER_SWEEP + 1,
        floor=None,
        acked=False,
        window=5,
        key="execution_row_retention_days",
        count_raises=False,
        ack_raises=False,
    )
    # A13 — the same, acknowledged.
    @example(
        available=_RG.MAX_ROWS_PER_SWEEP + 1,
        floor=None,
        acked=True,
        window=5,
        key="execution_row_retention_days",
        count_raises=False,
        ack_raises=False,
    )
    # A2 — floor=0 must NOT fall back to the global default (`is None`, not falsy).
    @example(
        available=1,
        floor=0,
        acked=False,
        window=180,
        key="agent_soft_delete_retention_days",
        count_raises=False,
        ack_raises=False,
    )
    @example(
        available=0,
        floor=0,
        acked=False,
        window=180,
        key="agent_soft_delete_retention_days",
        count_raises=False,
        ack_raises=False,
    )
    # A3 — exactly at the schedules floor.
    @example(
        available=100,
        floor=100,
        acked=False,
        window=30,
        key="schedule_soft_delete_retention_days",
        count_raises=False,
        ack_raises=False,
    )
    # A17 — a negative floor refuses even on an empty table.
    @example(
        available=0,
        floor=-1,
        acked=False,
        window=90,
        key="execution_row_retention_days",
        count_raises=False,
        ack_raises=False,
    )
    def test_decision_table_matches_an_independent_oracle(
        self, available, floor, acked, window, key, count_raises, ack_raises
    ):
        """A16 + A14. Equality on the whole verdict, against a spec-derived oracle.

        Covers all FIVE exits, including both fail-closed error branches, so the
        whole decision table is one property.

        Anti-tautology — why this is an oracle and not a shape check. `evaluate`
        has exactly five `return` statements, so "the verdict is one of the five
        legal pairs" would be trivially true. Each of these mutants survives that
        check and is killed by this one (all verified by hand-applying them to
        `retention_guard.py` and re-running this file):
          * `<=` -> `<` (:183) flips `available == threshold` to refused while the
            oracle still says allowed;
          * `if acked` -> `if not acked` (:195) inverts every over-threshold row;
          * `count_fn(threshold + 1)` -> `count_fn(threshold)` (:175) makes an
            over-threshold set count as exactly `threshold`, which the mutant then
            reads as `under_threshold`, while the oracle still computes
            `min(available, threshold + 1) > threshold`;
          * dropping `floor` (:170) silently promotes the agent sweep's floor of 0
            to the 1000-row default;
          * `GuardVerdict(False, ...)` -> `GuardVerdict(True, ...)` on either error
            branch — the fail-OPEN inversion, and the most dangerous single edit
            possible in this file.
        """
        with isolated_guard():
            if acked:
                _RG.record_acknowledgement(key, window)

            seen_limits = []

            def count_fn(limit):
                seen_limits.append(limit)
                if count_raises:
                    raise RuntimeError("db is down")
                return min(available, limit)

            expected = _expected_verdict(
                available, floor, acked, count_raises, ack_raises
            )

            # `is_acknowledged` is only consulted on the over-threshold path, so an
            # ack-lookup failure can only be observed there.
            ack_patch = (
                patch.object(
                    _RG,
                    "is_acknowledged",
                    side_effect=RuntimeError("ack lookup exploded"),
                )
                if ack_raises
                else _null_patch()
            )
            with ack_patch:
                verdict = _RG.evaluate(key, window, count_fn, floor=floor)

            assert (
                verdict.allowed,
                verdict.candidates,
                verdict.threshold,
                verdict.reason,
            ) == expected, (
                f"available={available} floor={floor} acked={acked} "
                f"window={window} key={key} count_raises={count_raises} "
                f"ack_raises={ack_raises}"
            )
            # A14: the bound must be `threshold + 1`. With `threshold`, a
            # candidate set of exactly `threshold + 1` counts as `threshold` and
            # slips through the `<=` as "under threshold".
            assert seen_limits == [expected[2] + 1], (
                f"count_fn must be called exactly once with threshold+1; got "
                f"{seen_limits} for floor={floor}"
            )

    @given(
        available=_available,
        floor=_floors,
        acked=st.booleans(),
        window=_windows,
        key=_keys,
    )
    @settings(max_examples=PURE_EXAMPLES, deadline=None)
    def test_evaluate_never_writes_to_the_database(
        self, available, floor, acked, window, key
    ):
        """A15. `evaluate` is a DECISION, not an action: it must never record an
        ack, clear one, or raise an alarm.

        Scoped to `evaluate`'s own DIRECT writes — "never mutates anything" is
        unprovable for an arbitrary generated `count_fn`, which may itself write,
        so the `count_fn` here is pure.
        """
        with isolated_guard() as db:
            if acked:
                _RG.record_acknowledgement(key, window)
            baseline = db.writes

            _RG.evaluate(key, window, lambda limit: min(available, limit), floor=floor)

            assert db.writes == baseline
            assert db.queue_items == []

    @given(
        value=st.one_of(
            st.integers(min_value=-(10**18), max_value=10**18),
            st.floats(allow_nan=True, allow_infinity=True),
        ),
        floor=_floors,
    )
    @settings(max_examples=PURE_EXAMPLES, deadline=None)
    def test_never_raises_for_any_numeric_count(self, value, floor):
        """The half of the ':153' "NEVER raises" contract that is actually true.

        The docstring is false for a non-NUMERIC return, because
        `candidates <= threshold` (:183) sits outside the try that wraps
        `count_fn`. That case is production-unreachable (all 8 `count_fn`s are
        `db.count_*` accessors returning `int(...)`) and is pinned as OBSERVED
        behaviour in `test_1771a_retention_edges.py`; the docstring correction is
        a flagged follow-up. This property asserts the guarantee that does hold.
        """
        with isolated_guard():
            _RG.evaluate(
                "execution_row_retention_days", 90, lambda limit: value, floor=floor
            )


class TestAckRoundTrip:

    @given(window=st.integers(), key=_keys)
    @settings(max_examples=PURE_EXAMPLES, deadline=None)
    @example(window=0, key="execution_row_retention_days")
    @example(window=-1, key="execution_row_retention_days")
    @example(window=5, key="execution_row_retention_days")
    @example(window=30, key="execution_row_retention_days")
    def test_ack_round_trips_at_any_window(self, window, key):
        """B1. Existing coverage is only w in {5, 30}, both hardcoded."""
        with isolated_guard():
            assert _RG.is_acknowledged(key, window) is False
            _RG.record_acknowledgement(key, window)
            assert _RG.is_acknowledged(key, window) is True
            _RG.consume_acknowledgement(key)
            assert _RG.is_acknowledged(key, window) is False

    @given(recorded=st.integers(), probed=st.integers(), key=_keys)
    @settings(max_examples=PURE_EXAMPLES, deadline=None)
    @example(recorded=30, probed=5, key="execution_row_retention_days")
    def test_an_ack_authorizes_exactly_one_window(self, recorded, probed, key):
        """B9. Narrowing the window invalidates the old approval: approving "prune
        at 30 days" is not approving "prune at 1 day" — and that narrowing IS the
        #1638 event, so the ack must not survive it."""
        assume(recorded != probed)
        with isolated_guard():
            _RG.record_acknowledgement(key, recorded)
            assert _RG.is_acknowledged(key, probed) is False
            assert _RG.is_acknowledged(key, recorded) is True

    @given(recorded_key=_keys, probed_key=_keys, window=st.integers())
    @settings(max_examples=PURE_EXAMPLES, deadline=None)
    def test_an_ack_authorizes_exactly_one_setting(
        self, recorded_key, probed_key, window
    ):
        """B3, generalized: an ack for one sweep must never license another."""
        assume(recorded_key != probed_key)
        with isolated_guard():
            _RG.record_acknowledgement(recorded_key, window)
            assert _RG.is_acknowledged(probed_key, window) is False


class TestAlarmTransitions:

    @given(
        ops=st.lists(
            st.tuples(
                st.sampled_from(["refuse", "allow"]),
                st.integers(min_value=1, max_value=4),
            ),
            min_size=1,
            max_size=12,
        )
    )
    @settings(max_examples=PURE_EXAMPLES, deadline=None)
    @example(ops=[("refuse", 1)])  # C1
    @example(ops=[("refuse", 1), ("refuse", 1)])  # C2 repeat
    @example(ops=[("refuse", 1), ("refuse", 2)])  # C3 window change
    @example(ops=[("refuse", 1), ("allow", 1), ("refuse", 1)])  # C4 re-arm
    def test_alarm_count_equals_the_number_of_transitions(self, ops):
        """C1-C4 as a state machine.

        The alarm must fire on the green->red TRANSITION, not per cycle: 288
        identical ERRORs a day is how an alert gets muted, and a muted alert is
        the #1638 failure mode repeated. It must equally not UNDER-fire — a
        narrowed window is a new blast radius and has to re-alarm.

        The expected count is recomputed from the memo's contract (fresh iff
        `_last_refused[key] != window`), never read back from the implementation's
        own bookkeeping.
        """
        key = "execution_row_retention_days"
        with isolated_guard() as db:
            memo = {}
            expected = 0
            for op, window in ops:
                if op == "allow":
                    memo.pop(key, None)
                    _RG.note_allowed(key)
                    continue
                if memo.get(key) != window:
                    expected += 1
                memo[key] = window
                _RG.announce_refusal(
                    key,
                    "rows",
                    window,
                    _RG.GuardVerdict(False, 5000, 1000, "over_threshold"),
                )

            assert len(db.queue_items) == expected, (
                f"ops={ops} produced {len(db.queue_items)} alarms, expected "
                f"{expected} transitions"
            )

    @given(
        window=st.integers(min_value=0, max_value=3650),
        key=_keys,
        candidates=st.integers(min_value=0, max_value=10**7),
        threshold=st.integers(min_value=0, max_value=10**4),
    )
    @settings(max_examples=PURE_EXAMPLES, deadline=None)
    def test_alarm_payload_never_grows_new_fields(
        self, window, key, candidates, threshold
    ):
        """C8 — SECURITY, as an invariant over the whole input space.

        The queue row is durable and operator-visible. `schedule_executions`
        `message`/`response`/`error`/`backlog_metadata` carry user content and
        credential-bearing agent output; canary G-04 exists because that blob
        leaked secrets into exactly this kind of durable state. The "show the
        operator what would be deleted" instinct is the bug.

        Pins the key set of the WHOLE item (any future `sample_rows`/`examples`
        fails) and that no `context` value is a container — a row payload could
        only arrive as a list or dict of rows.
        """
        with isolated_guard() as db:
            _RG.announce_refusal(
                key,
                "label",
                window,
                _RG.GuardVerdict(False, candidates, threshold, "over_threshold"),
            )
            _agent, item = db.queue_items[0]

            assert set(item) == {
                "id",
                "type",
                "priority",
                "title",
                "question",
                "context",
                "expires_at",
            }
            assert set(item["context"]) == {
                "alert_type",
                "setting_key",
                "window_days",
                "window_source",
                "candidate_count",
                "threshold",
                "reason",
            }
            for name, value in item["context"].items():
                assert isinstance(value, (str, int, type(None))), name
            # `expires_at` must stay NULL: `mark_operator_queue_expired` flips any
            # pending row past it to `expired` fleet-wide every 5s.
            assert item["expires_at"] is None
            assert item["id"] == _RG._alarm_id(key, window)


class TestRetentionSettingsTotality:

    @given(
        raw=st.one_of(
            st.none(),
            st.text(max_size=40),
            st.integers(min_value=-(10**9), max_value=10**9),
            st.sampled_from(["0", "30", " 7 ", "-5", "abc", "", "1e5", "0x10", "1.5"]),
        )
    )
    @settings(max_examples=PURE_EXAMPLES, deadline=None)
    @example(raw=None)
    @example(raw="")
    @example(raw="-5")
    @example(raw="abc")
    def test_reader_is_total_and_never_negative(self, raw):
        """D8. `_read_retention_settings` must be TOTAL: any stored value, however
        malformed, yields four non-negative ints and never raises.

        The failure DIRECTION is the point — an unreadable setting coerces to 0
        ("sweep disabled", keep everything), never to a small positive number,
        which is the catastrophic input for a destructive window (#1638).
        """

        class _Raw:
            def get_setting_value(self, key, default=None):
                return raw

        with patch.object(_CS, "db", _Raw()):
            result = _CS._read_retention_settings()

        assert isinstance(result, tuple) and len(result) == 4
        assert all(isinstance(v, int) and v >= 0 for v in result), f"raw={raw!r}"


# ---------------------------------------------------------------------------
# Tier 2 — real DB (predicate parity + full drain)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def retention_db(tmp_path_factory):
    """A throwaway SQLite file with ONLY `schedule_executions`.

    MODULE-scoped on purpose. A function-scoped fixture is built once per test
    FUNCTION and `@given` runs many examples inside it, so per-example isolation
    cannot come from the fixture either way — it comes from the explicit
    `truncate()` each example calls first. Making it module-scoped additionally
    keeps Hypothesis's `function_scoped_fixture` health check meaningful for this
    file instead of blanket-suppressed.

    Building the full `db_harness` schema (67 tables + 149 indexes) and letting it
    auto-parametrize onto PostgreSQL when `TEST_POSTGRES_URL` is set buys nothing
    here: `db/tables.py` declares zero ForeignKey/UniqueConstraint, so the single
    table builds standalone.

    SAFETY (the reason for the assert): `resolve_database_url()` reads
    `DATABASE_URL` FIRST and only then falls back to `TRINITY_DB_PATH`. If
    `DATABASE_URL` is exported in the shell, every DELETE below would run against
    THAT database. Both env vars are unset here, and the resolved URL is asserted
    to be this fixture's own temp file before any test deletes anything.
    """
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()  # `monkeypatch` itself is function-scoped
    db_file = tmp_path_factory.mktemp("retention-props") / "trinity.db"

    mp.delenv("DATABASE_URL", raising=False)
    mp.delenv("TEST_POSTGRES_URL", raising=False)
    mp.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod

    mp.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine, resolve_database_url

    resolved = resolve_database_url()
    assert resolved == f"sqlite:///{db_file}", (
        "REFUSING to run destructive property tests: the engine resolves to "
        f"{resolved!r}, not this test's temp database. Something re-set "
        "DATABASE_URL / TRINITY_DB_PATH — these tests DELETE rows."
    )

    from db.tables import metadata, schedule_executions

    engine = get_engine()
    metadata.create_all(engine, tables=[schedule_executions])

    from db.schedules import ScheduleOperations
    from sqlalchemy import delete as sa_delete

    def truncate():
        """Per-EXAMPLE isolation. Without this, rows accumulate across the
        examples `@given` generates and every property passes on the wrong data —
        the single most likely way to ship a false green here."""
        with engine.begin() as conn:
            conn.execute(sa_delete(schedule_executions))

    try:
        yield ScheduleOperations(None, None), engine, schedule_executions, truncate
    finally:
        mp.undo()


_TERMINAL = ("success", "failed", "cancelled", "skipped")
_NON_TERMINAL = ("running", "queued")

# (status, age_in_days | None -> completed_at IS NULL, has_execution_log)
_row = st.tuples(
    st.sampled_from(_TERMINAL + _NON_TERMINAL),
    st.one_of(st.none(), st.integers(min_value=0, max_value=400)),
    st.booleans(),
)


def _seed(conn, table, rows, cutoff):
    """Insert generated rows; return the ids a correct row-prune must remove.

    The expectation is computed in PYTHON from the generated data, never by
    re-running the accessor — otherwise the parity assertion is a tautology.
    """
    from sqlalchemy import insert
    from utils.helpers import iso_cutoff

    expected_rows, expected_logs = [], []
    for i, (status, age_days, has_log) in enumerate(rows):
        completed = None if age_days is None else iso_cutoff(hours=age_days * 24)
        conn.execute(
            insert(table).values(
                id=f"r{i}",
                schedule_id="s1",
                agent_name="a1",
                status=status,
                started_at=cutoff,
                completed_at=completed,
                message="m",
                triggered_by="schedule",
                execution_log="transcript" if has_log else None,
            )
        )
        if status in _TERMINAL and completed is not None and completed < cutoff:
            expected_rows.append(f"r{i}")
            if has_log:
                expected_logs.append(f"r{i}")
    return expected_rows, expected_logs


@contextmanager
def _frozen_cutoff(cutoff):
    """Freeze `iso_cutoff` for BOTH the count and the prune.

    `iso_cutoff` is evaluated INSIDE each accessor at call time
    (retention.py:121,134), so the count and the prune each compute their own
    "now". Without freezing, a row seeded exactly at the cutoff is strictly older
    by the elapsed microseconds by the time the second call runs — the property
    would fail on CORRECT code.
    """
    import db.schedules.retention as _RET

    with patch.object(_RET, "iso_cutoff", lambda **kwargs: cutoff):
        yield


class TestPredicateParityProperty:

    @given(
        rows=st.lists(_row, max_size=25),
        window=st.integers(min_value=1, max_value=300),
        chunk=st.integers(min_value=1, max_value=8),
    )
    @settings(max_examples=DB_EXAMPLES, deadline=None)
    @example(rows=[], window=90, chunk=5)
    # F4: every terminal state, jointly counted and pruned.
    @example(rows=[(s, 100, True) for s in _TERMINAL], window=90, chunk=5)
    # F5: a terminal row with no completion has no age.
    @example(rows=[("success", None, True), ("success", 100, True)], window=90, chunk=5)
    # F7: non-terminal rows are never touched.
    @example(rows=[(s, 100, True) for s in _NON_TERMINAL], window=90, chunk=5)
    # F11: full drain when the candidate set far exceeds chunk_size.
    @example(rows=[("success", 100, True)] * 25, window=90, chunk=1)
    def test_row_count_equals_what_the_prune_deletes(
        self, rows, window, chunk, retention_db
    ):
        """F3 + F11 — the headline property, over arbitrary generated row sets.

        Two invariants at once:
          * PARITY — the guard's count and the prune's DELETE describe the same
            set. They share `_execution_row_prune_predicate` by construction; this
            asserts that construction actually holds. A guard that counts a
            different set than the prune deletes reports a blast radius that is
            not the one about to happen.
          * FULL DRAIN — `chunk_size` bounds each TRANSACTION, not the call
            (`cleanup_service.py:84-96`, "READ THIS, THE NAME LIES"). #1638
            destroyed 5352 rows in one sweep, which a real per-call cap could not
            have produced. `chunk` is drawn 1-8 against up to 25 rows, so
            multi-chunk drains are the common case here, not an edge one.

        Also asserts the SET of survivors, not just the counts — equal counts over
        different row sets would still be a broken predicate.
        """
        from sqlalchemy import select
        from utils.helpers import iso_cutoff

        sched, engine, table, truncate = retention_db
        truncate()  # per-example isolation — see the fixture docstring

        cutoff = iso_cutoff(hours=window * 24)
        with engine.begin() as conn:
            expected_ids, _ = _seed(conn, table, rows, cutoff)

        with _frozen_cutoff(cutoff):
            counted = sched.count_execution_row_candidates(window, limit=len(rows) + 10)
            pruned = sched.prune_execution_rows(window, chunk_size=chunk)

        # `hide_parameters=True` (db/engine.py:67) keeps bound values out of
        # SQLAlchemy errors, so the shrunk input has to be in the message.
        detail = f"rows={rows} window={window} chunk={chunk}"
        assert counted == len(expected_ids), f"count drifted: {detail}"
        assert pruned == len(expected_ids), f"prune drifted (drain?): {detail}"

        with engine.connect() as conn:
            remaining = {r[0] for r in conn.execute(select(table.c.id))}
        assert remaining == {f"r{i}" for i in range(len(rows))} - set(
            expected_ids
        ), f"the prune deleted a different set than it counted: {detail}"

    @given(
        rows=st.lists(_row, max_size=25),
        window=st.integers(min_value=1, max_value=300),
        chunk=st.integers(min_value=1, max_value=8),
    )
    @settings(max_examples=DB_EXAMPLES, deadline=None)
    @example(rows=[("success", 100, False), ("success", 100, True)], window=90, chunk=5)
    def test_log_count_equals_what_the_log_prune_nulls(
        self, rows, window, chunk, retention_db
    ):
        """F3 for the sibling `execution_log` sweep, whose predicate adds
        `execution_log IS NOT NULL` — the term that makes this predicate falsify
        its own denominator, and precisely why the guard uses absolute counts with
        no percentage (`retention_guard.py:27-35`)."""
        from sqlalchemy import select
        from utils.helpers import iso_cutoff

        sched, engine, table, truncate = retention_db
        truncate()

        cutoff = iso_cutoff(hours=window * 24)
        with engine.begin() as conn:
            _, expected_logs = _seed(conn, table, rows, cutoff)

        with _frozen_cutoff(cutoff):
            counted = sched.count_execution_log_candidates(window, limit=len(rows) + 10)
            pruned = sched.prune_execution_logs(window, chunk_size=chunk)

        detail = f"rows={rows} window={window} chunk={chunk}"
        assert counted == len(expected_logs) == pruned, f"log parity broken: {detail}"

        # The null-out PRESERVES the row — that is the whole point of the #772 log
        # sweep versus the row sweep (~150-190 KB/row reclaimed, metadata kept).
        with engine.connect() as conn:
            assert len(list(conn.execute(select(table.c.id)))) == len(rows), detail

    @given(
        available=st.integers(min_value=0, max_value=30),
        limit=st.integers(min_value=1, max_value=40),
    )
    @settings(max_examples=DB_EXAMPLES, deadline=None)
    @example(available=20, limit=6)
    # `limit=1` is the boundary of the LIMIT subquery and the only input that
    # separates `limit <= 0` from `limit <= 1` in the accessor's disabled-sweep
    # guard. Random search usually finds it (the strategy's min_value is 1), but
    # "usually" is not a gate: pinned so that mutant is killed deterministically
    # rather than whenever Hypothesis happens to draw the boundary.
    @example(available=5, limit=1)
    def test_bounded_count_returns_min_of_candidates_and_limit(
        self, available, limit, retention_db
    ):
        """F9 as a property. The count answers "is this more than N?", not "how
        many?" — `_bounded_count`'s LIMIT subquery is what makes the guard
        O(limit) rather than O(candidates) on a loop that runs every 5 minutes. A
        regression to an unbounded COUNT would be invisible to a correctness test
        while putting a full-table scan on the cleanup path."""
        from sqlalchemy import insert
        from utils.helpers import iso_cutoff

        sched, engine, table, truncate = retention_db
        truncate()

        old = iso_cutoff(hours=200 * 24)
        with engine.begin() as conn:
            for i in range(available):
                conn.execute(
                    insert(table).values(
                        id=f"r{i}",
                        schedule_id="s1",
                        agent_name="a1",
                        status="success",
                        started_at=old,
                        completed_at=old,
                        message="m",
                        triggered_by="schedule",
                        execution_log="x",
                    )
                )

        counted = sched.count_execution_row_candidates(90, limit=limit)
        assert counted == min(available, limit), f"available={available} limit={limit}"
