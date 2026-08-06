"""#1990 — canary E-01 skips pull-CLAIMED rows, and ONLY those.

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

The load-bearing test here is ``test_null_lease_control_of_identical_age_fires``
and its twin in ``test_leased_and_control_rows_in_one_snapshot``: the exclusion
must be keyed on the lease, not a blanket silencing of aged rows. Every other
test in this module would still pass if someone "fixed" E-01 by deleting it.

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


def _check(snap):
    from canary.invariants import e01_terminal_state_closure as e01

    return e01.check(snap)


# ---------------------------------------------------------------------------
# The reported bug: a legitimately-running pull turn
# ---------------------------------------------------------------------------


def test_leased_row_past_threshold_is_silent():
    """The exact #1990 false positive: a claimed row whose lease has expired.

    ``lease_expires_at`` is stamped at ``started_at + THRESHOLD``, so at any age
    past the threshold the lease is also past — i.e. the reaper has become
    eligible but has not swept yet. Before the fix this paged critical.
    """
    lease = _iso(-1)  # expired one second ago; reaper's window just opened
    assert _check(_snap({"pull-1": lease})) == []


def test_leased_row_far_past_threshold_is_still_silent():
    """Re-delivery latency is bounded by MAX_REDELIVERY, not by this window.

    A row can sit leased-and-aged across several reaper passes; none of that is
    E-01's business.
    """
    snap = _snap({"pull-1": _iso(-3600)}, age_seconds=THRESHOLD * 10)
    assert _check(snap) == []


def test_leased_row_with_a_future_lease_is_silent():
    """A live lease (worker still within its deadline) is the common case."""
    assert _check(_snap({"pull-1": _iso(600)})) == []


# ---------------------------------------------------------------------------
# The exclusion is keyed on the LEASE — not a blanket silencing
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


def test_leased_and_control_rows_in_one_snapshot():
    """Both rows, one agent, one snapshot: exactly the NULL-lease one fires."""
    violations = _check(_snap({"pull-1": _iso(-1), "push-1": None}))
    assert [v.observed_state["execution_id"] for v in violations] == ["push-1"]


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
def test_leased_boundary_is_silent_throughout(age: int):
    """The lease removes the row from the check at every age, not just past it."""
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
