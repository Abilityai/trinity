"""#3254 (S4) — the retention guard's refusal alarm is a condition on the
platform-alert seam, not one row per (setting, window).

Driven against the unit island's real SQLite through the real seam
(`platform_alerts.observe` / `clear`) and, for the long case, through the real
`cleanup_service._guard_allows`:

* the seam property: the alarm carries the subject `retention_refused:<setting>`;
  a second reading bumps `context.seen_count` and moves `last_seen_at` and the
  default-lifetime `expires_at`; an allowed sweep (`note_allowed`) ends it
  `cancelled` / `platform` / `condition_cleared`, also after a restart;
* the #1834 contract on the seam's outcomes: a `failed` reading leaves the
  episode undelivered and is retried every cycle, escalating once; a refused
  reading (`refused_unregistered`) is not delivery either;
* R9: a delivered refusal is re-read at most once per refresh period while it
  persists, and a failed refresh is retried on the next cycle;
* D15a: a reason change rewrites the one row's text in place;
* D15b: after a person's ending (through `ask_service.answer`), the same window
  snoozes, a different window files a fresh row, and the snooze is bounded;
* R1: the legacy `retention-guard-<setting>-<window>` id parses to the subject
  the guard now writes;
* the other shape, the clock 30 days ahead: a prune refused every 5-minute cycle
  for 30 simulated days keeps exactly one pending row whose deadline keeps
  moving, with the real expiry sweep running throughout.

Every clock the path reads is driven from one simulated instant pinned to a
fixed date: the guard's `_clock`, the seam's `datetime`, and `utc_now_iso` as
both the seam and `db/operator_queue.py` bind it (patched through the running
method's globals, R6). No assertion compares a literal to the real `now`.
"""
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

import services.retention_guard as _RG  # noqa: E402
from services import cleanup_service as _CS  # noqa: E402
from services import platform_alerts as _PA  # noqa: E402

_KIND = "retention_refused"
_LABEL = "execution rows"
_CYCLE = 300                       # CleanupService's default poll interval
_START = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def _verdict(candidates=5000, reason="over_threshold"):
    return _RG.GuardVerdict(False, candidates, 1000, reason)


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Clocks:
    """One simulated instant, read by every clock the alarm path consults."""

    def __init__(self):
        self.wall = _START
        self.mono = 50_000.0

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds

    def iso(self):
        return self.wall.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def db():
    from database import db
    return db


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    from services import operator_resume_service
    # The seam schedules a thin broadcast after a changed row; no loop here.
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    _RG.reset_transition_memo()
    yield
    _RG.reset_transition_memo()


@pytest.fixture
def key():
    """A setting key of this test's own: the subject is `retention_refused:<key>`
    on one shared host, so no other test's row can update or snooze this one."""
    k = f"execution_row_retention_days_{uuid.uuid4().hex[:8]}"
    yield k
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM operator_queue WHERE agent_name = :a AND request_id LIKE :p"),
            {"a": _RG.ALARM_AGENT_NAME, "p": f"retention-guard-{k}-%"},
        )


@pytest.fixture
def clocks(monkeypatch, db):
    c = _Clocks()
    monkeypatch.setattr(_RG, "_clock", lambda: c.mono)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return c.wall.astimezone(tz) if tz is not None else c.wall.replace(tzinfo=None)

    monkeypatch.setattr(_PA, "datetime", _Frozen)
    # `observe` imports `utc_now_iso` at call time; the DB layer bound it at import.
    monkeypatch.setattr(sys.modules["utils.helpers"], "utc_now_iso", c.iso)
    ops_globals = type(db._operator_queue_ops).create_platform_item.__globals__
    monkeypatch.setitem(ops_globals, "utc_now_iso", c.iso)
    return c


@pytest.fixture
def seam(db, monkeypatch):
    """The seam's create, observed and switchable: `down` makes it raise, which
    `observe` turns into `failed` (fault injection at the seam, R5)."""
    real = db.create_platform_operator_queue_item
    state = {"down": False, "calls": 0}

    def create(agent_name, item, **kw):
        state["calls"] += 1
        if state["down"]:
            raise RuntimeError("operator queue is down")
        return real(agent_name, item, **kw)

    monkeypatch.setattr(db, "create_platform_operator_queue_item", create)
    return state


def _alarms(db, key, status="pending"):
    rows = db.list_operator_queue_items(
        agent_name=_RG.ALARM_AGENT_NAME, status=status, limit=500)
    return [r for r in rows if (r.get("context") or {}).get("setting_key") == key]


def _one(db, key):
    rows = _alarms(db, key)
    assert len(rows) == 1, f"expected exactly one pending alarm for {key}, got {len(rows)}"
    return rows[0]


def _refuse(key, window=5, **verdict):
    _RG.announce_refusal(key, _LABEL, window, _verdict(**verdict))


def _person_ends(db, row):
    from services import ask_service
    full = db.get_operator_queue_item(row["id"])
    ending = ask_service.answer(full, response="acknowledged", response_text=None,
                                actor=ask_service.Actor(email="op@example.com"))
    return ending.rows[0]


# ---------------------------------------------------------------------------
# The seam property (the universal mutation's target)
# ---------------------------------------------------------------------------


class TestSeamProperty:

    def test_the_alarm_is_one_subject_row_moved_by_each_reading(self, db, key, clocks):
        _refuse(key)
        first = _one(db, key)
        assert first["subject"] == f"{_KIND}:{key}"
        assert first["request_id"].startswith(f"retention-guard-{key}-")
        assert (first["type"], first["priority"]) == ("alert", "critical")
        assert first["context"]["seen_count"] == 1
        assert _parse(first["expires_at"]) == clocks.wall + _PA.lifetime_for(_KIND)

        clocks.advance(_RG.ALARM_REFRESH_SECONDS)
        _refuse(key)

        row = _one(db, key)
        assert row["id"] == first["id"], "a reading updates the one row, never files another"
        assert row["context"]["seen_count"] == 2
        assert row["last_seen_at"] > first["last_seen_at"]
        assert _parse(row["expires_at"]) == clocks.wall + _PA.lifetime_for(_KIND)

    def test_an_allowed_sweep_ends_the_alarm_as_the_platform(self, db, key, clocks):
        _refuse(key)
        row = _one(db, key)
        assert row["subject"] == f"{_KIND}:{key}"

        assert _CS._guard_allows(key, _LABEL, 5, lambda limit: 3) is True

        assert _alarms(db, key) == []
        ended = db.get_operator_queue_item(row["id"])
        assert (ended["status"], ended["disposed_by"], ended["disposition_reason"]) == (
            "cancelled", "platform", "condition_cleared")

    def test_an_allowed_sweep_ends_the_alarm_after_a_restart(self, db, key, clocks):
        """The episode memo is in-process; the alarm row is not. A restart (or the
        worker that never refused) must still end the row from before."""
        _refuse(key)
        row = _one(db, key)
        assert row["subject"] == f"{_KIND}:{key}"

        _RG.reset_transition_memo()                      # a fresh process
        assert _CS._guard_allows(key, _LABEL, 5, lambda limit: 3) is True

        assert _alarms(db, key) == []
        assert db.get_operator_queue_item(row["id"])["disposed_by"] == "platform"

    def test_a_clear_with_nothing_raised_writes_nothing(self, db, key, clocks):
        assert _CS._guard_allows(key, _LABEL, 5, lambda limit: 3) is True
        assert _alarms(db, key, status=None) == []


# ---------------------------------------------------------------------------
# #1834 on the seam: delivery is an outcome, a failure is retried every cycle
# ---------------------------------------------------------------------------


class TestDeliveryContract:

    def test_a_failed_reading_is_not_delivery_and_is_retried_with_escalation(
        self, db, key, clocks, seam, caplog
    ):
        seam["down"] = True
        cycles = int(_RG.ALARM_ESCALATION_AGE_SECONDS / _CYCLE) + 3
        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            for _ in range(cycles):
                _refuse(key)
                clocks.advance(_CYCLE)

        assert seam["calls"] == cycles, "every cycle must re-attempt an undelivered alarm"
        assert _alarms(db, key) == []
        ep = _RG._refusal_episodes[key]
        assert ep.alarm_delivered is False
        escalations = [r for r in caplog.records
                       if "unable to write the refusal alarm" in r.getMessage()]
        assert [r.levelno for r in escalations] == [logging.ERROR]

        seam["down"] = False
        _refuse(key)
        assert _one(db, key)["subject"] == f"{_KIND}:{key}"
        assert ep.alarm_delivered is True

        calls = seam["calls"]
        _refuse(key)
        assert seam["calls"] == calls, "a delivered alarm is not written again every cycle"

    def test_a_refused_reading_is_not_delivery_either(self, db, key, clocks, monkeypatch):
        """`refused_unregistered` is a code defect: loud, and retried — never read
        as an alarm that reached anyone."""
        kind = _PA.KINDS[_KIND]
        monkeypatch.delitem(_PA.KINDS, _KIND)
        _refuse(key)
        assert _RG._refusal_episodes[key].alarm_delivered is False
        assert _alarms(db, key) == []

        monkeypatch.setitem(_PA.KINDS, _KIND, kind)
        clocks.advance(_CYCLE)
        _refuse(key)
        assert _one(db, key)["subject"] == f"{_KIND}:{key}"
        assert _RG._refusal_episodes[key].alarm_delivered is True


# ---------------------------------------------------------------------------
# R9: a persisting refusal keeps being re-read
# ---------------------------------------------------------------------------


class TestRefresh:

    def test_a_delivered_refusal_is_re_read_within_the_refresh_period(self, db, key, clocks):
        _refuse(key)
        first = _one(db, key)
        assert first["subject"] == f"{_KIND}:{key}"
        assert first["context"]["seen_count"] == 1

        clocks.advance(_RG.ALARM_REFRESH_SECONDS - _CYCLE)
        _refuse(key)
        assert _one(db, key)["context"]["seen_count"] == 1, "not yet due: no per-cycle write"

        clocks.advance(_CYCLE)
        _refuse(key)
        row = _one(db, key)
        assert row["context"]["seen_count"] == 2
        assert row["expires_at"] > first["expires_at"]

    def test_a_failed_refresh_is_retried_on_the_next_cycle(self, db, key, clocks, seam):
        _refuse(key)
        assert _one(db, key)["context"]["seen_count"] == 1

        clocks.advance(_RG.ALARM_REFRESH_SECONDS)
        seam["down"] = True
        _refuse(key)
        assert _one(db, key)["context"]["seen_count"] == 1

        seam["down"] = False
        clocks.advance(_CYCLE)
        _refuse(key)
        assert _one(db, key)["context"]["seen_count"] == 2

    def test_the_refresh_is_well_inside_the_alarms_lifetime(self):
        """Direction, not value: a refusal re-read less often than its lifetime
        could expire while it still holds."""
        assert timedelta(seconds=_RG.ALARM_REFRESH_SECONDS) * 2 < _PA.lifetime_for(_KIND)


# ---------------------------------------------------------------------------
# D15a: a reason change rewrites the row
# ---------------------------------------------------------------------------


class TestReasonChange:

    def test_a_reason_change_rewrites_the_one_row_in_both_directions(self, db, key, clocks):
        _refuse(key)                                                  # approvable
        first = _one(db, key)
        assert first["subject"] == f"{_KIND}:{key}"
        assert "/api/settings/retention/acknowledge" in first["question"]

        _refuse(key, candidates=-1, reason="count_uninterpretable")   # undecidable
        row = _one(db, key)
        assert row["id"] == first["id"]
        assert row["context"]["reason"] == "count_uninterpretable"
        assert "will NOT clear this" in row["question"]
        assert "/api/settings/retention/acknowledge" not in row["question"]

        # The direction the old row got wrong: it kept telling the operator an
        # acknowledgement would not help, when by now it is the remedy.
        _refuse(key)
        row = _one(db, key)
        assert row["id"] == first["id"]
        assert row["context"]["reason"] == "over_threshold"
        assert "/api/settings/retention/acknowledge" in row["question"]
        assert "will NOT clear this" not in row["question"]


# ---------------------------------------------------------------------------
# D15b + AC 7: a person's ending, through the real answer path
# ---------------------------------------------------------------------------


class TestPersonEnding:

    def test_after_a_persons_ending_the_same_window_snoozes(self, db, key, clocks):
        _refuse(key, window=5)
        row = _one(db, key)
        assert row["subject"] == f"{_KIND}:{key}"
        assert _person_ends(db, row)["status"] == "acknowledged"

        clocks.advance(_RG.ALARM_REFRESH_SECONDS)
        _refuse(key, window=5)                                 # the R9 refresh reading

        assert _alarms(db, key) == [], "the same window is snoozed: nothing filed"
        assert db.get_operator_queue_item(row["id"])["status"] == "acknowledged"
        assert _RG._refusal_episodes[key].alarm_delivered is True, (
            "a snoozed reading is delivery: the episode must not retry every cycle")

    def test_after_a_persons_ending_a_different_window_files_a_fresh_row(self, db, key, clocks):
        _refuse(key, window=5)
        row = _one(db, key)
        assert row["subject"] == f"{_KIND}:{key}"
        _person_ends(db, row)

        clocks.advance(_CYCLE)
        _refuse(key, window=2)                                 # narrowed again, next cycle

        fresh = _one(db, key)
        assert fresh["id"] != row["id"]
        assert fresh["context"]["window_days"] == 2
        assert db.get_operator_queue_item(row["id"])["status"] == "acknowledged"

    def test_the_snooze_is_bounded_while_the_refusal_persists(self, db, key, clocks):
        _refuse(key, window=5)
        row = _one(db, key)
        assert row["subject"] == f"{_KIND}:{key}"
        _person_ends(db, row)

        clocks.advance(_PA.snooze_window().total_seconds() + _RG.ALARM_REFRESH_SECONDS)
        _refuse(key, window=5)

        assert _one(db, key)["id"] != row["id"], "past the snooze window it files again"


# ---------------------------------------------------------------------------
# R1: key parity with the upgrade sweep
# ---------------------------------------------------------------------------


class TestLegacyParity:

    def test_the_legacy_id_parses_to_the_subject_the_guard_writes(self, db, key, clocks):
        legacy = f"retention-guard-{key}-5"            # the pre-#3254 (setting, window) id
        ctx = {"alert_type": "retention_blast_radius", "setting_key": key,
               "window_days": 5, "reason": "over_threshold"}
        match = _PA.derive_legacy_subject(legacy, ctx)
        assert match.kind == _KIND

        _refuse(key, window=5)

        assert match.subject == _one(db, key)["subject"] == _PA.subject_for(_KIND, key)

    def test_every_guarded_setting_key_parses_back(self):
        from services.settings_service import RETENTION_OPS_KEYS
        for setting in RETENTION_OPS_KEYS:
            for window in (0, 5, 3650):
                match = _PA.derive_legacy_subject(f"retention-guard-{setting}-{window}", {})
                assert match.subject == _PA.subject_for(_KIND, setting), (setting, window)


# ---------------------------------------------------------------------------
# The other shape: the clock 30 days ahead
# ---------------------------------------------------------------------------


class TestThirtyDays:

    def test_a_prune_refused_every_cycle_for_30_days_keeps_one_row_with_a_moving_deadline(
        self, db, key, clocks
    ):
        """Through the real `_guard_allows` (cleanup_service.py:619), the path every
        guarded sweep takes, with the real expiry sweep run every simulated hour.

        Before R9 the row's deadline was set once, so the 14-day lifetime ended a
        refusal that still held; before #3254 the row had no deadline at all."""
        cycles = 30 * 24 * 3600 // _CYCLE
        per_hour = 3600 // _CYCLE
        first_id, deadlines = None, []
        for n in range(cycles):
            assert _CS._guard_allows(key, _LABEL, 5, lambda limit: 10**6) is False
            if n % per_hour == 0:
                db.mark_operator_queue_expired()
                row = _one(db, key)
                first_id = first_id or row["id"]
                assert row["id"] == first_id
                assert row["subject"] == f"{_KIND}:{key}"
                deadlines.append(_parse(row["expires_at"]))
            clocks.advance(_CYCLE)

        db.mark_operator_queue_expired()
        row = _one(db, key)
        assert row["id"] == first_id
        assert deadlines == sorted(deadlines), "the deadline only ever moves forward"
        assert len(set(deadlines)) > 1
        assert _parse(row["expires_at"]) > clocks.wall, "day 30: the alarm is still in force"
        assert _parse(row["expires_at"]) >= (
            clocks.wall + _PA.lifetime_for(_KIND) - timedelta(seconds=_RG.ALARM_REFRESH_SECONDS))
        periods = cycles * _CYCLE // _RG.ALARM_REFRESH_SECONDS
        assert periods <= row["context"]["seen_count"] <= periods + 1, (
            "about one reading per refresh period: neither lapsed nor per-cycle")
