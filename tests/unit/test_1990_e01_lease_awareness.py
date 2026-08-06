"""#1990 — canary E-01 gives pull-CLAIMED rows a BOUNDED grace, and only those.

E-01 ("no ``running`` row older than ``execution_timeout_seconds + 300s``") had
no lease-awareness. A #1081 Phase-3 pull-claimed row (``lease_expires_at IS NOT
NULL``) is ``running`` but owned **exclusively** by the lease-reaper, which
re-queues or poison-parks it on its own schedule — its age is not evidence of
the stuck-execution class E-01 detects.

The overlap is exact: ``claim_next_queued`` stamps the lease at ``started_at +
(execution_timeout_seconds + SLOT_TTL_BUFFER)`` — the *same* threshold E-01
builds — so ``age > threshold`` became true at the precise instant the reaper's
recovery window opened. E-01's 300s buffer exists to give the owning component
its window; against a leased row that head-room was zero, so E-01 paged
**critical** for every gap between lease expiry and the reaper's next sweep, on
every re-delivery, per execution. That lands on the #1766 soak, whose AC is
"canary green throughout" and whose abort criterion is a critical violation on
the pilot.

## The grace is bounded — silence is not permanent

The skip lasts only while the lease is overdue by at most
``LEASE_REAPER_GRACE_SECONDS`` (2 x ``cleanup_service.CLEANUP_INTERVAL_SECONDS``
— asserted in ``test_grace_is_derived_from_the_cleanup_interval``). Past that
the row fires as a **lease-reaper** failure. Two tests carry that pair and must
be read together:

* ``test_leased_row_within_the_reaper_grace_is_silent`` — a healthy reaper never
  pages. It resolves an overdue lease within one cycle (``requeue_expired_lease``
  clears the lease and resets ``started_at`` in one atomic UPDATE;
  ``park_expired_lease`` goes terminal), so observable overdue-ness tops out at
  one interval and the grace has a whole spare cycle of head-room.
* ``test_leased_row_overdue_past_the_grace_fires`` — a **stopped** reaper does.
  Without this positive case the grace would be decorative: an unconditional
  skip left `PULL_MIGRATION_TESTING.md` §9 M4 ("a ``running`` row past its
  ``lease_expires_at`` that the reaper has not touched is a reaper failure", a
  #1766 abort criterion) with no automated owner at all — E-05 and S-01 exclude
  leased rows too, E-02 only sees terminal→non-terminal reversals, and there is
  no lease-overdue invariant.

The load-bearing test here is ``test_null_lease_control_of_identical_age_fires``
and its twin in ``test_leased_and_control_rows_in_one_snapshot``: the grace must
be keyed on the lease, not a blanket silencing of aged rows. Every other test in
this module would still pass if someone "fixed" E-01 by deleting it.

``test_e02_deliberately_counts_leased_running_rows`` records the AC-5 verdict
executably rather than in prose: E-02 is the fourth reader of
``running_exec_ids`` and must NOT gain this exclusion.

Tests live under ``tests/unit/`` deliberately. The pre-existing E-01 tests are
in ``tests/test_canary_invariants.py``, which **no CI workflow executes** —
``backend-unit-test.yml`` runs ``cd tests && python -m pytest unit/``. A guard
placed beside them would never go red (trinity#2037; the same finding
``test_1880_canary_alert_parity.py`` and ent#336/#337's guards record).

## Clock discipline

Every instant is an integer offset from one literal ``T0``, and E-01 reads its
clock from ``snapshot.snapshot_time`` rather than ``time.time()``. Neither side
samples a real clock, so the threshold boundary is asserted exactly
(``threshold`` → silent, ``threshold + 1`` → fires) instead of with a margin —
see ``docs/memory/learnings.md`` 2026-08-03 (#1909).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pytest


T0 = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)

TIMEOUT = 900
BUFFER = 300
THRESHOLD = TIMEOUT + BUFFER  # 1200s — E-01 fires strictly ABOVE this
GRACE = 600  # LEASE_REAPER_GRACE_SECONDS — also fires strictly ABOVE


def _iso(offset_seconds: int) -> str:
    """T0 + offset as an ISO-Z string. The ONLY source of time in this module."""
    return (
        (T0 + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")
    )


def _snap(rows: Dict[str, Optional[str]], *, age_seconds: int = THRESHOLD + 1):
    """One agent; ``rows`` maps execution_id → lease_expires_at (or None).

    Every row shares one ``started_at`` so age is held constant across a leased
    row and its NULL-lease control — the only variable is the lease.
    """
    from canary.snapshot import AgentSnapshot, Snapshot

    started_at = _iso(-age_seconds)
    return Snapshot(
        snapshot_time=_iso(0),
        agents=[
            AgentSnapshot(
                name="a1",
                is_system=False,
                max_parallel=3,
                execution_timeout_seconds=TIMEOUT,
                running_exec_ids=set(rows),
                running_started_at={eid: started_at for eid in rows},
                running_lease_expires_at=dict(rows),
            )
        ],
    )


def _pull_snap(overdue_seconds: int):
    """One pull-CLAIMED row whose lease is ``overdue_seconds`` past due.

    Faithful to ``claim_next_queued``, which stamps ``lease_expires_at =
    started_at + (execution_timeout_seconds + SLOT_TTL_BUFFER)`` in the SAME
    atomic UPDATE that writes ``started_at`` — so age and lease-overdue-ness
    move together and ``age == THRESHOLD + overdue`` by construction.
    """
    return _snap(
        {"pull-1": _iso(-overdue_seconds)},
        age_seconds=THRESHOLD + overdue_seconds,
    )


def _check(snap):
    from canary.invariants import e01_terminal_state_closure as e01

    return e01.check(snap)


# ---------------------------------------------------------------------------
# The reported bug: a legitimately-running pull turn, within the reaper's window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overdue",
    [
        1,      # the reaper's recovery window has only just opened
        300,    # one full CLEANUP_INTERVAL — a healthy reaper's WORST case
        GRACE,  # the grace itself: fires strictly ABOVE, so still silent
    ],
)
def test_leased_row_within_the_reaper_grace_is_silent(overdue: int):
    """The exact #1990 false positive: a claimed row whose lease has expired.

    Before the fix every one of these paged **critical**, because the lease is
    stamped at exactly ``started_at + THRESHOLD`` — so ``age > threshold``
    became true at the instant the reaper's window opened, with zero head-room.

    A healthy reaper cannot exceed ``overdue == 300``: ``_sweep_expired_leases``
    runs once per 300s ``cleanup_service`` cycle and RESOLVES the row on that
    pass (``requeue_expired_lease`` clears the lease and resets ``started_at``
    in one atomic UPDATE; ``park_expired_lease`` goes terminal, leaving the
    running set). The grace is 2x that, i.e. a whole spare cycle of head-room.
    """
    assert _check(_pull_snap(overdue)) == []


def test_leased_row_with_a_future_lease_is_silent():
    """A live lease (worker still within its deadline) is the common case."""
    assert _check(_snap({"pull-1": _iso(600)})) == []


def test_aged_row_with_a_far_future_lease_is_silent():
    """The LEASE governs, not the age — a not-yet-due lease silences any age.

    Guards the ordering: the lease is read before the age, so a row 10x past
    E-01's threshold but still inside its own lease belongs to the worker, not
    to this invariant.
    """
    snap = _snap({"pull-1": _iso(3600)}, age_seconds=THRESHOLD * 10)
    assert _check(snap) == []


# ---------------------------------------------------------------------------
# The grace is BOUNDED — a stopped lease-reaper must fire
# ---------------------------------------------------------------------------


def test_leased_row_overdue_past_the_grace_fires():
    """The detector this fix restores: a reaper that has stopped.

    ``PULL_MIGRATION_TESTING.md`` §9 **M4** — "a ``running`` row past its
    ``lease_expires_at`` that the reaper has not touched is a reaper failure" —
    is a #1766 soak **abort criterion**. An unconditional lease exclusion left
    it with no automated owner: S-01 and E-05 exclude leased rows too, E-02 only
    catches terminal→non-terminal reversals, and there is no lease-overdue
    invariant. Without this test the grace window is decorative.
    """
    violations = _check(_pull_snap(GRACE + 1))
    assert len(violations) == 1
    v = violations[0]
    assert v.invariant_id == "E-01"
    assert v.severity == "critical"
    assert v.observed_state["execution_id"] == "pull-1"
    # The lease fields are what tell on-call this is the REAPER's failure and
    # not a wedged execution — a different component, a different runbook.
    assert v.observed_state["lease_expires_at"] == _iso(-(GRACE + 1))
    assert v.observed_state["lease_overdue_seconds"] == GRACE + 1
    assert v.observed_state["lease_reaper_grace_seconds"] == GRACE
    assert "LEASE-REAPER failure" in v.signal_query


@pytest.mark.parametrize(
    "overdue,expected",
    [
        (GRACE - 1, 0),
        (GRACE, 0),  # `lease_overdue <= grace` continues — fires strictly above
        (GRACE + 1, 1),
    ],
)
def test_lease_grace_boundary_is_exact(overdue: int, expected: int):
    assert len(_check(_pull_snap(overdue))) == expected


def test_grace_is_derived_from_the_cleanup_interval():
    """The constant is 2x the reaper's own cycle — coupled, not a magic number.

    The lease-reaper runs inside ``cleanup_service``'s loop, so the grace is
    "one worst-case cycle plus one spare". If that interval is ever changed,
    this assertion goes red and the grace has to be re-derived with it, rather
    than a healthy reaper silently starting to page (grace too small) or a dead
    one silently staying invisible for longer (too large).
    """
    from canary.invariants import e01_terminal_state_closure as e01
    from services.cleanup_service import CLEANUP_INTERVAL_SECONDS

    assert e01.LEASE_REAPER_GRACE_SECONDS == 2 * CLEANUP_INTERVAL_SECONDS
    assert GRACE == e01.LEASE_REAPER_GRACE_SECONDS  # this module's local copy


# ---------------------------------------------------------------------------
# The grace is keyed on the LEASE — not a blanket silencing
# ---------------------------------------------------------------------------


def test_null_lease_control_of_identical_age_fires():
    """AC-2. Same agent, same ``started_at``, same age — only the lease differs.

    If this ever goes green, E-01 has been silenced rather than made
    lease-aware, and the stuck-execution class it exists to detect is
    undetected on the entire push fleet.
    """
    violations = _check(_snap({"push-1": None}))
    assert len(violations) == 1
    v = violations[0]
    assert v.invariant_id == "E-01"
    assert v.severity == "critical"
    assert v.observed_state["execution_id"] == "push-1"
    assert v.observed_state["age_seconds"] == THRESHOLD + 1
    # A push violation reports a NULL lease and NO overdue fields — that absence
    # is the discriminator the runbook hint routes on.
    assert v.observed_state["lease_expires_at"] is None
    assert "lease_overdue_seconds" not in v.observed_state
    assert "LEASE-REAPER" not in v.signal_query


def test_leased_and_control_rows_in_one_snapshot():
    """Both rows, one agent, one snapshot: exactly the NULL-lease one fires."""
    violations = _check(_snap({"pull-1": _iso(-1), "push-1": None}))
    assert [v.observed_state["execution_id"] for v in violations] == ["push-1"]


def test_both_fire_and_stay_distinguishable_when_the_reaper_is_also_down():
    """A dead reaper AND a wedged push row in one snapshot: two violations, two
    diagnoses. The lease fields must keep them apart — a single undifferentiated
    "N stuck executions" alert would send on-call to the wrong component for
    half of them."""
    snap = _snap(
        {"pull-1": _iso(-(GRACE + 1)), "push-1": None},
        age_seconds=THRESHOLD + GRACE + 1,
    )
    by_id = {v.observed_state["execution_id"]: v for v in _check(snap)}
    assert set(by_id) == {"pull-1", "push-1"}
    assert by_id["pull-1"].observed_state["lease_overdue_seconds"] == GRACE + 1
    assert by_id["push-1"].observed_state["lease_expires_at"] is None


def test_missing_lease_key_fails_open_and_fires():
    """An absent key is NOT a lease. Older images never populated the column.

    ``running_lease_expires_at`` is read with ``.get(eid)``, so a collector that
    never wrote the field yields ``None`` and the row keeps being checked. A
    fail-CLOSED read here would silently retire E-01 on any instance whose
    snapshot predates #1081.
    """
    from canary.snapshot import AgentSnapshot, Snapshot

    snap = Snapshot(
        snapshot_time=_iso(0),
        agents=[
            AgentSnapshot(
                name="a1",
                is_system=False,
                max_parallel=3,
                execution_timeout_seconds=TIMEOUT,
                running_exec_ids={"legacy-1"},
                running_started_at={"legacy-1": _iso(-(THRESHOLD + 1))},
                # running_lease_expires_at left at its empty default
            )
        ],
    )
    assert len(_check(snap)) == 1


@pytest.mark.parametrize("lease", ["", "not-a-timestamp"])
def test_unusable_lease_value_is_not_a_lease_and_fires(lease: str):
    """An empty or unparseable lease is NOT evidence the reaper owns the row.

    Collector writes go through ``to_utc_iso``, so NULL-or-ISO is what is
    produced today and this is belt-and-braces. But a bare ``is not None``
    would read ``""`` as a LIVE lease and silence the row permanently — the
    exact widening the fail-open on an absent key exists to prevent. Same
    direction for both: not-a-legible-deadline ⇒ keep checking.
    """
    assert len(_check(_snap({"weird-1": lease}))) == 1


# ---------------------------------------------------------------------------
# Boundary — the threshold is untouched for NULL-lease rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "age,expected",
    [
        (THRESHOLD - 1, 0),
        (THRESHOLD, 0),  # `age <= threshold` continues — fires strictly above
        (THRESHOLD + 1, 1),
    ],
)
def test_null_lease_boundary_is_unchanged(age: int, expected: int):
    assert len(_check(_snap({"push-1": None}, age_seconds=age))) == expected


@pytest.mark.parametrize("age", [THRESHOLD - 1, THRESHOLD, THRESHOLD + 1])
def test_within_grace_lease_is_silent_across_the_age_boundary(age: int):
    """A lease inside the grace removes the row at every age, not just past it.

    The age threshold is E-01's push-path predicate; for a leased row inside the
    reaper's window it is not consulted at all.
    """
    assert _check(_snap({"pull-1": _iso(-1)}, age_seconds=age)) == []


# ---------------------------------------------------------------------------
# AC-5 — the E-02 verdict, recorded executably
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal double for E-02's cross-cycle side-table (ZSET + parallel HASH)."""

    def __init__(self, previous_terminal: Dict[str, str]):
        self._z: Dict[str, float] = {eid: 0.0 for eid in previous_terminal}
        self._h: Dict[str, str] = dict(previous_terminal)

    def zrangebyscore(self, key, lo, hi) -> List[str]:
        return []  # nothing is old enough to trim in this test

    def zremrangebyscore(self, key, lo, hi) -> int:
        return 0

    def hdel(self, key, *fields) -> int:
        return 0

    def zrange(self, key, start, end) -> List[str]:
        return sorted(self._z)

    def hmget(self, key, *fields) -> List[Optional[str]]:
        names = fields[0] if len(fields) == 1 and isinstance(fields[0], list) else list(fields)
        return [self._h.get(f) for f in names]

    def zadd(self, key, mapping) -> int:
        self._z.update(mapping)
        return len(mapping)

    def hset(self, key, mapping=None, **kw) -> int:
        self._h.update(mapping or {})
        return len(mapping or {})


def test_e02_deliberately_counts_leased_running_rows(monkeypatch):
    """AC-5 verdict: E-02 reads ``running_exec_ids`` and must NOT be excluded.

    E-02 asks "was this id terminal in a previous cycle and is it non-terminal
    now?" — an answer that does not depend on who owns the row. The reaper
    cannot produce that transition (``requeue_expired_lease`` /
    ``park_expired_lease`` both CAS on ``status='running'`` with a past lease,
    so a terminal row is unreachable to them) and re-delivery *preserves* the
    ``execution_id`` (#1084/#525 are execution_id-scoped), so a terminal id
    reappearing as running is exactly the corruption E-02 exists to catch —
    and pull, where a late worker result races a reaper pass, is MORE exposed
    to it than push.

    This test is the tripwire on someone copying #1990's exclusion into E-02
    "for consistency", which would blind it on the #1081 path.
    """
    from canary.invariants import e02_no_phantom_reversal as e02

    snap = _snap({"pull-1": _iso(-1)})
    monkeypatch.setattr(e02, "_redis", lambda: _FakeRedis({"pull-1": "success"}))

    violations = e02.check(snap)
    assert len(violations) == 1
    assert violations[0].invariant_id == "E-02"
    assert violations[0].observed_state["execution_id"] == "pull-1"
    assert violations[0].observed_state["previous_status"] == "success"
    assert violations[0].observed_state["current_status"] == "running"
