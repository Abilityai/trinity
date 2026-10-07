"""
#3246 C4 — `platform_alerts.observe / clear / reconcile`, driven at the seam
against the unit island's real SQLite.

* Two readings of one subject are one pending row: the second updates it in
  place (fields, `context.seen_count`, `last_seen_at`, `expires_at`).
* The kind's lifetime becomes `expires_at`; a person-only kind never expires;
  a real `mark_expired` ends a row whose lifetime ran out.
* `clear` ends the pending row as the PLATFORM and never a person's ending.
* T5: after a person ended the subject's row, the same reading files nothing
  for 7 days; a priority increase or a material change files a fresh row;
  a person's ending older than the window does not snooze.
* `observe` never raises and refuses an unregistered kind.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


@pytest.fixture
def db():
    from database import db
    return db


@pytest.fixture
def pa(monkeypatch):
    from services import platform_alerts, operator_resume_service
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    return platform_alerts


@pytest.fixture
def agent():
    return f"agent-3246s-{uuid.uuid4().hex[:6]}"


def _pending(db, agent):
    return db.list_operator_queue_items(agent_name=agent, status="pending")


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _person_end(db, row):
    db.respond_to_operator_queue_item(row["id"], "ack", "seen", None, "person@example.com")


class TestObserve:
    def test_two_readings_are_one_row_updated_in_place(self, db, pa, agent):
        assert pa.observe(agent, "circuit_dormant", agent, title="t1", question="q1",
                          context={"failures": 3}) == pa.OBSERVED_CREATED
        first = _pending(db, agent)[0]
        assert pa.observe(agent, "circuit_dormant", agent, title="t2", question="q2",
                          priority="critical", context={"failures": 9}) == pa.OBSERVED_UPDATED
        rows = _pending(db, agent)
        assert len(rows) == 1 and rows[0]["id"] == first["id"]
        row = rows[0]
        assert row["subject"] == f"circuit_dormant:{agent}"
        assert row["title"] == "t2" and row["priority"] == "critical"
        assert row["context"] == {"failures": 9, "seen_count": 2}
        assert row["last_seen_at"] >= first["last_seen_at"]
        assert row["expires_at"] >= first["expires_at"]

    def test_the_kind_lifetime_is_the_deadline(self, db, pa, agent):
        pa.observe(agent, "circuit_dormant", agent, title="t", question="q")
        pa.observe(agent, "subscription_headroom", "sub1", title="t", question="q",
                   context={"tier": "warn"})
        pa.observe(agent, "poison", "exec-1", title="t", question="q")
        rows = {r["subject"].split(":")[0]: r for r in _pending(db, agent)}
        now = datetime.now(timezone.utc)
        net = _parse(rows["circuit_dormant"]["expires_at"]) - now
        default = _parse(rows["subscription_headroom"]["expires_at"]) - now
        assert timedelta(days=29) < net <= timedelta(days=30)
        assert timedelta(days=13) < default <= timedelta(days=14)
        assert rows["poison"]["expires_at"] is None

    def test_rows_wear_their_kind_prefix_and_are_platform_minted(self, db, pa, agent):
        from services import operator_queue_service as oqs
        pa.observe(agent, "base_image_stale", "trinity-system", title="t", question="q")
        row = _pending(db, agent)[0]
        assert row["request_id"].startswith("base-image-stale-trinity-system-")
        assert oqs.is_platform_minted(row)

    def test_unregistered_kind_is_refused_and_nothing_raises(self, db, pa, agent, monkeypatch):
        assert pa.observe(agent, "no_such_kind", "x", title="t", question="q") == \
            pa.OBSERVED_REFUSED_UNREGISTERED
        monkeypatch.setattr(db, "create_platform_operator_queue_item",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        assert pa.observe(agent, "circuit_dormant", agent, title="t", question="q") == pa.OBSERVED_FAILED
        assert _pending(db, agent) == []

    def test_lifetime_ends_the_row_through_mark_expired(self, db, pa, agent, monkeypatch):
        monkeypatch.setattr(pa, "lifetime_for", lambda kind: timedelta(seconds=-1))
        pa.observe(agent, "circuit_dormant", agent, title="t", question="q")
        db.mark_operator_queue_expired()
        assert _pending(db, agent) == []


class TestClear:
    def test_clear_ends_the_pending_row_as_the_platform(self, db, pa, agent):
        pa.observe(agent, "circuit_dormant", agent, title="t", question="q")
        row = _pending(db, agent)[0]
        assert pa.clear(agent, "circuit_dormant", agent) == 1
        ended = db.get_operator_queue_item(row["id"])
        assert ended["status"] == "cancelled"
        assert ended["disposed_by"] == "platform"
        assert ended["disposition_reason"] == "condition_cleared"
        assert pa.clear(agent, "circuit_dormant", agent) == 0

    def test_clear_with_nothing_pending_is_a_noop(self, pa, agent):
        assert pa.clear(agent, "circuit_dormant", agent) == 0

    def test_reconcile_ends_only_the_subjects_no_longer_live(self, db, pa, agent):
        for sid in ("a", "b"):
            pa.observe(agent, "subscription_headroom", sid, title="t", question="q",
                       context={"tier": "warn"})
        pa.observe(agent, "circuit_dormant", agent, title="t", question="q")
        assert pa.reconcile(agent, "subscription_headroom", ["a"]) == 1
        assert sorted(r["subject"] for r in _pending(db, agent)) == [
            f"circuit_dormant:{agent}", "subscription_headroom:a"]


class TestSnooze:
    @pytest.fixture
    def ended(self, db, pa, agent):
        """A headroom warning a person acknowledged."""
        pa.observe(agent, "subscription_headroom", "sub1", title="75%", question="q",
                   priority="low", context={"tier": "warn"})
        row = _pending(db, agent)[0]
        _person_end(db, row)
        assert _pending(db, agent) == []
        return row

    def test_the_same_reading_files_nothing(self, db, pa, agent, ended):
        assert pa.observe(agent, "subscription_headroom", "sub1", title="80%", question="q",
                          priority="low", context={"tier": "warn"}) == pa.OBSERVED_SNOOZED
        assert _pending(db, agent) == []

    def test_a_priority_increase_files_a_fresh_row(self, db, pa, agent, ended):
        assert pa.observe(agent, "subscription_headroom", "sub1", title="95%", question="q",
                          priority="high", context={"tier": "warn"}) == pa.OBSERVED_CREATED

    def test_a_material_change_files_a_fresh_row(self, db, pa, agent, ended):
        assert pa.observe(agent, "subscription_headroom", "sub1", title="92%", question="q",
                          priority="low", context={"tier": "crit"}) == pa.OBSERVED_CREATED

    def test_an_ending_older_than_seven_days_does_not_snooze(self, db, pa, agent, ended, monkeypatch):
        monkeypatch.setattr(pa, "snooze_window", lambda: timedelta(seconds=0))
        assert pa.observe(agent, "subscription_headroom", "sub1", title="80%", question="q",
                          priority="low", context={"tier": "warn"}) == pa.OBSERVED_CREATED

    def test_the_window_is_seven_days(self, pa, monkeypatch):
        monkeypatch.delenv(pa.SNOOZE_ENV, raising=False)
        assert pa.snooze_window() == timedelta(days=7)

    def test_a_platform_ending_does_not_snooze(self, db, pa, agent):
        pa.observe(agent, "circuit_dormant", agent, title="t", question="q")
        pa.clear(agent, "circuit_dormant", agent)
        assert pa.observe(agent, "circuit_dormant", agent, title="t", question="q") == pa.OBSERVED_CREATED
