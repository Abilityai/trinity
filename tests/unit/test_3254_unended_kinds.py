"""
#3254 (S6) — the platform alert kinds the platform never ends, on the seam.

Three emitters move onto `platform_alerts.observe`, none with a clear site:

* the poison-park alert (`lease_reaper_service._create_park_item`, kind
  `poison`) and the portal inbox collision (`client_portal.service.
  _alert_collided_inbox`, kind `portal_inbox_collision`): must-act kinds, no
  lifetime, only a person ends them (AC 4);
* the validation failure (`ValidationService._notify_operator_on_failure`,
  kind `validation_failed`): lifetime-only, nothing observes it clearing.

Pinned here, against the unit island's real SQLite, by reading the rows
(`observe` never raises, so a failure only logs):

* two readings leave ONE pending row carrying the subject `<kind>:<key>`; the
  second bumps `context.seen_count` and moves `last_seen_at`; `expires_at`
  moves for the validation row and stays NULL for the must-act rows;
* the poison outcome mapping (#1081 B3): `failed` → no park, retried next
  pass; `snoozed` → park; a refusal never parks;
* a person's acknowledgement through `ask_service.answer` lands
  `acknowledged` and snoozes the next reading (#2372 / AC 7 on a must-act
  kind);
* D11: a person's ending of a collision is final (it does not re-file at +8
  days), a NEW claimant re-alerts, and the two upgrade sequences stay snoozed
  although legacy rows carry no `claimants_digest`: a material key missing
  from the ended row is not a change;
* the collision is observed at most once per 6 hours per claimant set;
* R1: each kind's new subject equals what the upgrade sweep derives from that
  kind's pre-move rows;
* the other shape, the clock 30 days ahead: poison and collision rows are
  still pending with `expires_at` NULL; the validation row has expired.

The clock is frozen at a pinned date and moved by hand: the seam's
`datetime`, `utils.helpers.utc_now_iso` (the seam's function-local import)
and the `db/operator_queue.py` module globals (R6: the running method's
globals), so every stamp the flow writes and compares is on the one clock.
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

PINNED = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
CLAIMANTS = ["victim+x@example.com", "victim_x@example.com"]
LEGACY_DIR = "victim_x_example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    from database import db
    return db


@pytest.fixture
def pa(monkeypatch):
    from services import operator_resume_service, platform_alerts
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    return platform_alerts


@pytest.fixture
def cps(monkeypatch):
    from client_portal import service
    monkeypatch.setattr(service, "_collision_observed_at", {})
    return service


@pytest.fixture
def clock(monkeypatch, db, pa):
    import utils.helpers as helpers

    state = {"now": PINNED}

    def now_iso():
        return state["now"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["now"] if tz is not None else state["now"].replace(tzinfo=None)

    monkeypatch.setattr(pa, "datetime", _Frozen)
    monkeypatch.setattr(helpers, "utc_now_iso", now_iso)
    monkeypatch.setitem(type(db._operator_queue_ops).create_platform_item.__globals__,
                        "utc_now_iso", now_iso)

    def advance(**delta):
        state["now"] += timedelta(**delta)

    return SimpleNamespace(advance=advance, now=lambda: state["now"], iso=now_iso)


@pytest.fixture
def agent():
    return f"agent-3254u-{uuid.uuid4().hex[:6]}"


def _pending(db, agent):
    return db.list_operator_queue_items(agent_name=agent, status="pending")


def _all(db, agent):
    return db.list_operator_queue_items(agent_name=agent, include_cleared=True, limit=500)


def _acknowledge(row):
    """A person ends the row the way the Operating Room does (#2372)."""
    from services import ask_service
    return ask_service.answer(row, response="acknowledged", response_text=None,
                              actor=ask_service.Actor(email="op@example.com")).rows[0]


def _park(agent, eid):
    from services import lease_reaper_service as lrs
    return lrs._create_park_item(agent, eid, 3, 3)


def _validation_failed(agent, eid, summary="checks failed"):
    from services.validation_service import ValidationResult, ValidationService, ValidationStatus
    svc = ValidationService.__new__(ValidationService)
    result = ValidationResult(status=ValidationStatus.FAIL, summary=summary,
                              items=[{"check": "c", "result": "fail", "evidence": "e"}],
                              recommendation="retry")
    asyncio.run(svc._notify_operator_on_failure(execution_id=eid, agent_name=agent,
                                                validation_result=result))


def _age_collision_memo(cps):
    """Six hours pass for the collision memo (it runs on the monotonic clock)."""
    for key in list(cps._collision_observed_at):
        cps._collision_observed_at[key] -= cps._COLLISION_REOBSERVE_SECONDS + 1


def _sweep(agent, now):
    """The production upgrade sweep, scoped to this test's rows: the unit
    island's database is shared by the whole run."""
    from sqlalchemy import text

    from db.engine import get_engine
    from db.migrations import run_platform_alert_sweep

    def run(sql, params, fetch=False):
        with get_engine().begin() as conn:
            result = conn.execute(text(sql), params)
            if fetch:
                return [r for r in result.fetchall() if r[1] == agent]
        return None

    return run_platform_alert_sweep(run, now=now)


def _legacy_collision(db, agent, clock):
    """The row the pre-move `_alert_collided_inbox` wrote: a deterministic id,
    no context, so no `claimants_digest` and no subject."""
    db.create_operator_queue_item(agent, {
        "id": f"portal-inbox-collision-{LEGACY_DIR}", "type": "alert", "status": "pending",
        "priority": "high", "title": "Two portal clients shared one inbox folder",
        "question": "written to by 2 client addresses", "created_at": clock.iso(),
    })
    (row,) = _pending(db, agent)
    assert row["subject"] is None and not row.get("context")
    return row


# ---------------------------------------------------------------------------
# AC 4 — the registrations
# ---------------------------------------------------------------------------

def test_the_must_act_kinds_never_expire_and_only_the_collision_ending_is_final(pa):
    person = [k for k in pa.KINDS.values() if k.clears == pa.CLEARS_PERSON]
    assert {k.name for k in person} >= {"poison", "portal_inbox_collision"}
    assert all(k.lifetime is None for k in person)
    assert pa.KINDS["validation_failed"].clears == pa.CLEARS_LIFETIME_ONLY
    assert [k.name for k in pa.KINDS.values() if k.person_ending_is_final] == ["portal_inbox_collision"]
    assert pa.KINDS["portal_inbox_collision"].material_keys == ("claimants_digest",)


# ---------------------------------------------------------------------------
# Two readings → one pending row (the seam-property test)
# ---------------------------------------------------------------------------

class TestOneRowPerCondition:
    def test_poison_two_readings_are_one_row_that_never_expires(self, db, pa, clock, agent):
        assert _park(agent, "exec-p1") is True
        (first,) = _pending(db, agent)
        clock.advance(minutes=5)
        assert _park(agent, "exec-p1") is True
        (row,) = _pending(db, agent)
        assert row["id"] == first["id"]
        assert row["subject"] == "poison:exec-p1"
        assert row["request_id"].startswith("poison-exec-p1-")
        assert row["context"]["seen_count"] == 2
        assert row["context"]["execution_id"] == "exec-p1"
        assert row["context"]["reason"] == "poison_lease"
        assert row["last_seen_at"] > first["last_seen_at"]
        assert first["expires_at"] is None and row["expires_at"] is None
        assert row["type"] == "alert" and row["priority"] == "high"

    def test_validation_two_readings_are_one_row_whose_deadline_moves(self, db, pa, clock, agent):
        _validation_failed(agent, "exec-v1", summary="first")
        (first,) = _pending(db, agent)
        clock.advance(hours=1)
        _validation_failed(agent, "exec-v1", summary="second")
        (row,) = _pending(db, agent)
        assert row["id"] == first["id"]
        assert row["subject"] == "validation_failed:exec-v1"
        assert row["request_id"].startswith("val_")
        assert row["question"] == "Execution validation failed: second"
        assert row["context"]["seen_count"] == 2
        assert row["last_seen_at"] > first["last_seen_at"]
        assert row["expires_at"] > first["expires_at"]
        assert row["expires_at"] == (clock.now() + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def test_a_different_execution_is_a_different_row(self, db, pa, clock, agent):
        _validation_failed(agent, "exec-v1")
        _validation_failed(agent, "exec-v2")
        assert sorted(r["subject"] for r in _pending(db, agent)) == [
            "validation_failed:exec-v1", "validation_failed:exec-v2"]

    def test_collision_two_readings_are_one_row_that_never_expires(self, db, pa, cps, clock, agent):
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        (first,) = _pending(db, agent)
        clock.advance(hours=7)
        _age_collision_memo(cps)
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        (row,) = _pending(db, agent)
        assert row["id"] == first["id"]
        assert row["subject"] == f"portal_inbox_collision:{LEGACY_DIR}"
        assert row["context"]["seen_count"] == 2
        assert row["last_seen_at"] > first["last_seen_at"]
        assert first["expires_at"] is None and row["expires_at"] is None
        assert all(addr in row["question"] for addr in CLAIMANTS)
        assert "@" not in json.dumps(row["context"])       # the digest, never an address


# ---------------------------------------------------------------------------
# The poison outcome mapping (#1081 B3)
# ---------------------------------------------------------------------------

class _ReaperDb:
    """The reaper's injected handle: one at-cap candidate. The alert itself is
    written by the seam to the real database."""

    def __init__(self, agent, eid):
        self.candidate = {"id": eid, "agent_name": agent, "redelivery_count": 3}
        self.parked = []

    def find_expired_leases(self, now_iso=None, limit=500):
        return [] if self.parked else [self.candidate]

    def park_expired_lease(self, eid, error, now_iso=None):
        self.parked.append(eid)
        return True


class TestPoisonOutcomes:
    @pytest.mark.parametrize("outcome,parks", [
        ("created", True), ("updated", True), ("snoozed", True),
        ("refused_unregistered", False), ("refused_at_budget", False), ("failed", False),
    ])
    def test_the_park_follows_the_seam_outcome(self, pa, monkeypatch, agent, outcome, parks):
        monkeypatch.setattr(pa, "observe", lambda *a, **k: outcome)
        assert _park(agent, "exec-o") is parks

    def test_a_failed_alert_is_not_parked_and_the_next_pass_retries(self, db, pa, clock, agent, monkeypatch):
        from services import lease_reaper_service as lrs
        reaper = _ReaperDb(agent, "exec-f1")
        original = db.create_platform_operator_queue_item

        def _boom(*_a, **_k):
            raise RuntimeError("operator_queue write failed")

        monkeypatch.setattr(db, "create_platform_operator_queue_item", _boom)
        assert lrs.reap_expired_leases(reaper, max_redelivery=3).parked == 0
        assert reaper.parked == [] and _pending(db, agent) == []

        monkeypatch.setattr(db, "create_platform_operator_queue_item", original)
        assert lrs.reap_expired_leases(reaper, max_redelivery=3).parked == 1
        assert reaper.parked == ["exec-f1"]
        assert [r["subject"] for r in _pending(db, agent)] == ["poison:exec-f1"]

    def test_a_snoozed_alert_still_parks_the_execution(self, db, pa, clock, agent):
        """A person acknowledged the alert, then a crash left the execution
        running: the re-found row files nothing new and is parked."""
        from services import lease_reaper_service as lrs
        assert _park(agent, "exec-s1") is True
        (row,) = _pending(db, agent)
        ended = _acknowledge(row)
        assert (ended["status"], ended["disposed_by"]) == ("acknowledged", "person")
        clock.advance(minutes=10)
        reaper = _ReaperDb(agent, "exec-s1")
        assert lrs.reap_expired_leases(reaper, max_redelivery=3).parked == 1
        assert _pending(db, agent) == []
        assert len(_all(db, agent)) == 1


# ---------------------------------------------------------------------------
# A person's ending is never overwritten (AC 7 on a must-act kind)
# ---------------------------------------------------------------------------

class TestPersonEnding:
    def test_an_acknowledged_poison_alert_stays_acknowledged_and_snoozes(self, db, pa, clock, agent):
        _park(agent, "exec-a1")
        (row,) = _pending(db, agent)
        out = _acknowledge(row)
        assert out["status"] == "acknowledged" and out["acknowledged_at"]
        clock.advance(days=1)
        assert pa.observe(agent, "poison", "exec-a1", title="t", question="q") == pa.OBSERVED_SNOOZED
        assert _park(agent, "exec-a1") is True
        assert _pending(db, agent) == []
        assert db.get_operator_queue_item(row["id"])["status"] == "acknowledged"

    def test_an_acknowledged_validation_alert_snoozes_the_next_failure(self, db, pa, clock, agent):
        _validation_failed(agent, "exec-a2")
        (row,) = _pending(db, agent)
        _acknowledge(row)
        clock.advance(days=1)
        _validation_failed(agent, "exec-a2")
        assert _pending(db, agent) == []
        assert db.get_operator_queue_item(row["id"])["status"] == "acknowledged"


# ---------------------------------------------------------------------------
# D11 — the collision: a person's ending is final; a new claimant re-alerts
# ---------------------------------------------------------------------------

class TestCollisionEndingIsFinal:
    def test_an_ended_collision_does_not_refile_at_eight_days(self, db, pa, cps, clock, agent):
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        (row,) = _pending(db, agent)
        assert _acknowledge(row)["status"] == "acknowledged"
        clock.advance(days=8)
        _age_collision_memo(cps)
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        assert _pending(db, agent) == []
        assert db.get_operator_queue_item(row["id"])["status"] == "acknowledged"

    def test_a_new_claimant_re_alerts_after_an_ending(self, db, pa, cps, clock, agent):
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        (row,) = _pending(db, agent)
        _acknowledge(row)
        clock.advance(days=8)
        third = CLAIMANTS + ["victim!x@example.com"]
        cps._alert_collided_inbox(agent, LEGACY_DIR, third)       # not memoised: a new set
        (fresh,) = _pending(db, agent)
        assert fresh["id"] != row["id"]
        assert fresh["subject"] == f"portal_inbox_collision:{LEGACY_DIR}"
        assert "victim!x@example.com" in fresh["question"]
        assert fresh["context"]["claimants_digest"] != row["context"]["claimants_digest"]

    def test_a_raised_priority_is_still_the_only_other_way_back(self, db, pa, cps, clock, agent):
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        (row,) = _pending(db, agent)
        _acknowledge(row)
        clock.advance(days=40)
        digest = row["context"]["claimants_digest"]
        assert pa.observe(agent, "portal_inbox_collision", LEGACY_DIR, title="t", question="q",
                          context={"claimants_digest": digest}) == pa.OBSERVED_SNOOZED
        assert pa.observe(agent, "portal_inbox_collision", LEGACY_DIR, title="t", question="q",
                          priority="critical",
                          context={"claimants_digest": digest}) == pa.OBSERVED_CREATED


class TestCollisionUpgradeSequences:
    """Both stay snoozed although the legacy rows carry no `claimants_digest`:
    a material key missing from the ended row is not a change (D11)."""

    def test_a_legacy_collision_ended_inside_the_sweeps_window(self, db, pa, cps, clock, agent):
        legacy = _legacy_collision(db, agent, clock)
        clock.advance(days=1)
        _acknowledge(legacy)
        clock.advance(days=2)
        counts = _sweep(agent, clock.now())
        assert counts["snoozed"] == 1
        assert db.get_operator_queue_item(legacy["id"])["subject"] == \
            f"portal_inbox_collision:{LEGACY_DIR}"
        clock.advance(hours=1)
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        assert _pending(db, agent) == []
        clock.advance(days=30)
        _age_collision_memo(cps)
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        assert _pending(db, agent) == []

    def test_a_stamped_legacy_pending_collision_ended_before_its_first_refresh(self, db, pa, cps, clock, agent):
        legacy = _legacy_collision(db, agent, clock)
        clock.advance(hours=1)
        counts = _sweep(agent, clock.now())
        assert counts["survivors"] == 1
        stamped = db.get_operator_queue_item(legacy["id"])
        assert stamped["subject"] == f"portal_inbox_collision:{LEGACY_DIR}"
        assert stamped["expires_at"] is None
        assert "claimants_digest" not in (stamped["context"] or {})
        clock.advance(hours=1)
        _acknowledge(stamped)
        clock.advance(hours=1)
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        assert _pending(db, agent) == []


# ---------------------------------------------------------------------------
# The collision memo: at most one reading per 6 hours per claimant set
# ---------------------------------------------------------------------------

class TestCollisionMemo:
    def test_a_repeat_inside_six_hours_does_not_reach_the_seam(self, db, pa, cps, clock, agent, monkeypatch):
        calls = []
        real = pa.observe
        monkeypatch.setattr(pa, "observe", lambda *a, **k: calls.append(a) or real(*a, **k))
        for _ in range(3):
            cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        assert len(calls) == 1
        _age_collision_memo(cps)
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        assert len(calls) == 2
        (row,) = _pending(db, agent)
        assert row["context"]["seen_count"] == 2

    def test_a_failed_reading_is_retried_on_the_next_read(self, db, pa, cps, clock, agent, monkeypatch):
        original = db.create_platform_operator_queue_item
        monkeypatch.setattr(db, "create_platform_operator_queue_item",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        assert cps._collision_observed_at == {}
        monkeypatch.setattr(db, "create_platform_operator_queue_item", original)
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        assert len(_pending(db, agent)) == 1


# ---------------------------------------------------------------------------
# R1 — the new subject is what the upgrade sweep derives from a pre-move row
# ---------------------------------------------------------------------------

class TestKeyParityWithTheSweep:
    def test_poison(self, db, pa, clock, agent):
        _park(agent, "exec_r-1")
        (row,) = _pending(db, agent)
        old = pa.derive_legacy_subject("poison-exec_r-1", {"execution_id": "exec_r-1"})
        assert old.kind == "poison" and old.subject == row["subject"]

    def test_validation(self, db, pa, clock, agent):
        _validation_failed(agent, "exec_r-2")
        (row,) = _pending(db, agent)
        old = pa.derive_legacy_subject("val_exec_r-2_20261001120000", {"execution_id": "exec_r-2"})
        assert old.kind == "validation_failed" and old.subject == row["subject"]

    def test_collision(self, db, pa, cps, clock, agent):
        cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
        (row,) = _pending(db, agent)
        old = pa.derive_legacy_subject(f"portal-inbox-collision-{LEGACY_DIR}", None)
        assert old.kind == "portal_inbox_collision" and old.subject == row["subject"]


# ---------------------------------------------------------------------------
# The other shape: the clock 30 days ahead
# ---------------------------------------------------------------------------

def test_thirty_days_later_the_must_act_rows_are_still_pending_and_validation_expired(
        db, pa, cps, clock, agent):
    _park(agent, "exec-30")
    cps._alert_collided_inbox(agent, LEGACY_DIR, CLAIMANTS)
    _validation_failed(agent, "exec-30v")
    assert len(_pending(db, agent)) == 3

    clock.advance(days=30)
    for _ in range(50):                      # the island's DB is shared: drain every batch
        if not db.mark_operator_queue_expired():
            break

    rows = {r["subject"].split(":")[0]: r for r in _all(db, agent)}
    for kind in ("poison", "portal_inbox_collision"):
        assert rows[kind]["status"] == "pending", kind
        assert rows[kind]["expires_at"] is None, kind
    assert rows["validation_failed"]["status"] == "expired"
    assert rows["validation_failed"]["disposed_by"] == "timeout"
