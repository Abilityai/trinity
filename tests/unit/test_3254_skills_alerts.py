"""#3254 S1 — the two `_skills-sync` host alarms report through the platform
alert seam (#3246 part 2).

Before: the reconcile refusal filed `skills-reconcile-{agent}-{orphan_count}`
(a new row whenever the count moved) and the fleet sweep filed
`skills-fleet-reinject-{finished_at}` (a new row per bad run), each with
`expires_at = NULL`, and nothing ever ended either. After: one pending row per
subject — `skills_reconcile_refused:<agent>` / `skills_fleet_reinject:fleet` —
updated in place by every reading, ended by the platform only on POSITIVE
evidence of recovery (R10), and never over a person's ending.

Against a fresh full production schema per test (`db_harness.db_backend`:
SQLite always, PostgreSQL when TEST_POSTGRES_URL is set) and the real seam,
with the clock pinned (never compared to the wall clock). The emitters are
driven through the code paths that call them in production:
`SkillService.reconcile_agent_skills` and
`SkillsLibrarySyncService.run_fleet_reinject`.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, scalar  # noqa: E402,F401  (db_backend is a fixture)

pytestmark = pytest.mark.unit

HOST = "_skills-sync"
T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
NET = timedelta(days=30)


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class _Clock:
    """One pinned 'now' for the seam (`platform_alerts.datetime`), the ids it
    mints (`utils.helpers.utc_now_iso`) and the DB layer that stamps
    `last_seen_at` / `disposed_at` (the running method's own globals, R6)."""

    def __init__(self, at):
        self.at = at

    def advance(self, **delta):
        self.at = self.at + timedelta(**delta)

    def iso(self):
        return self.at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def clock(monkeypatch):
    import utils.helpers as helpers
    from database import db
    from services import platform_alerts as pa

    c = _Clock(T0)

    class _Pinned(datetime):
        @classmethod
        def now(cls, tz=None):
            return c.at if tz is not None else c.at.replace(tzinfo=None)

    monkeypatch.setattr(pa, "datetime", _Pinned)
    monkeypatch.setattr(helpers, "utc_now_iso", c.iso)
    monkeypatch.setitem(type(db._operator_queue_ops).create_platform_item.__globals__,
                        "utc_now_iso", c.iso)
    return c


@pytest.fixture
def triggers(monkeypatch):
    """Record (never run) every follow-up the seam and the ending sink schedule."""
    from services import operator_resume_service
    scheduled = []
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", scheduled.append)
    return scheduled


@pytest.fixture
def db(db_backend, clock, triggers):  # noqa: F811 — the db_harness fixture, by name
    from database import db as real_db
    return real_db


def _rows(db, subject, status=None):
    rows = db.list_operator_queue_items(agent_name=HOST, status=status, limit=1000)
    return [r for r in rows if r.get("subject") == subject]


def _pending(db, subject):
    return _rows(db, subject, status="pending")


def _answer(db, row):
    from services import ask_service
    return ask_service.answer(db.get_operator_queue_item(row["id"]), response="acknowledged",
                              response_text=None,
                              actor=ask_service.Actor(email="op@example.com")).rows[0]


def _assert_ended_by_platform(db, row):
    ended = db.get_operator_queue_item(row["id"])
    assert ended["status"] == "cancelled"
    assert ended["disposed_by"] == "platform"
    assert ended["disposition_reason"] == "condition_cleared"


def _queue_row_count():
    return scalar("SELECT COUNT(*) FROM operator_queue")


# ---------------------------------------------------------------------------
# The reconcile refusal — `skills_reconcile_refused:<agent>`, 30-day net
# ---------------------------------------------------------------------------

class TestReconcileRefusal:
    AGENT = "worker-3"   # ends in `-<digits>`: the legacy parser must not eat it
    SUBJECT = "skills_reconcile_refused:worker-3"

    @pytest.fixture
    def svc(self):
        from services.skill_service import SkillService
        svc = SkillService.__new__(SkillService)
        svc.remove_skills = AsyncMock(
            return_value={"skills_removed": 3, "skills_failed": 0, "results": {}})
        svc._audit_removal = AsyncMock()
        return svc

    def _reconcile(self, svc, orphans=0, *, managed=None, error=None):
        if error is not None:
            svc._list_managed_skills = AsyncMock(side_effect=error)
        else:
            svc._list_managed_skills = AsyncMock(
                return_value=managed if managed is not None
                else [f"orphan-{i:02d}" for i in range(orphans)])
        return asyncio.run(svc.reconcile_agent_skills(self.AGENT, []))

    def test_two_refusals_are_one_row_carrying_the_seam_properties(self, db, svc, clock):
        """The seam property (plan §14): the subject, the seen count, the moved
        `last_seen_at`, and the 30-day net deadline moved by the second reading.
        The second refusal has a DIFFERENT orphan count, which the old id
        (`skills-reconcile-{agent}-{count}`) filed as a second row."""
        assert self._reconcile(svc, 25)["status"] == "refused"
        clock.advance(hours=3)
        assert self._reconcile(svc, 31)["status"] == "refused"

        [row] = _pending(db, self.SUBJECT)
        assert len(db.list_operator_queue_items(agent_name=HOST, status="pending")) == 1
        assert row["request_id"].startswith(f"skills-reconcile-{self.AGENT}-")
        assert row["type"] == "alert" and row["priority"] == "high"
        assert row["context"]["seen_count"] == 2
        assert row["context"]["orphan_count"] == 31          # the latest reading
        assert "31 orphaned" in row["question"]
        assert _parse(row["last_seen_at"]) == T0 + timedelta(hours=3)
        assert _parse(row["expires_at"]) == T0 + timedelta(hours=3) + NET

    def test_the_alarm_carries_counts_never_skill_names(self, db, svc):
        self._reconcile(svc, managed=[f"secret-skill-{i:02d}" for i in range(12)])
        [row] = _pending(db, self.SUBJECT)
        blob = repr(row)
        assert "secret-skill" not in blob
        assert row["context"]["orphan_count"] == 12

    def test_a_clean_pass_ends_the_refusal_as_the_platform(self, db, svc):
        self._reconcile(svc, 25)
        [row] = _pending(db, self.SUBJECT)
        assert self._reconcile(svc, 0)["status"] == "clean"
        assert _pending(db, self.SUBJECT) == []
        _assert_ended_by_platform(db, row)

    def test_a_reconciled_pass_ends_the_refusal_as_the_platform(self, db, svc):
        """Back under the cap: the refusal no longer holds, whatever the prune
        then removed."""
        self._reconcile(svc, 25)
        [row] = _pending(db, self.SUBJECT)
        assert self._reconcile(svc, 3)["status"] == "reconciled"
        assert _pending(db, self.SUBJECT) == []
        _assert_ended_by_platform(db, row)

    @pytest.mark.parametrize("case", ["inventory_unavailable", "busy", "error"])
    def test_an_inconclusive_pass_leaves_the_refusal_alone(self, db, svc, case):
        """R10: a pass that could not look is not evidence of recovery."""
        from services.skill_service import SkillInjectionBusy
        self._reconcile(svc, 25)
        if case == "inventory_unavailable":
            svc._list_managed_skills = AsyncMock(return_value=None)
            result = asyncio.run(svc.reconcile_agent_skills(self.AGENT, []))
        elif case == "busy":
            svc.remove_skills = AsyncMock(side_effect=SkillInjectionBusy("busy"))
            result = self._reconcile(svc, 3)
        else:
            result = self._reconcile(svc, error=RuntimeError("boom"))
        assert result["status"] == "skipped"
        [row] = _pending(db, self.SUBJECT)
        assert row["context"]["seen_count"] == 1

    def test_a_persons_ending_stands_and_snoozes_the_next_refusal(self, db, svc, clock):
        """AC 7 (#2372) on this kind: the answer lands `acknowledged` as the
        person; a clean pass does not re-end it; the next refusal inside the
        snooze window files nothing."""
        self._reconcile(svc, 25)
        [row] = _pending(db, self.SUBJECT)
        out = _answer(db, row)
        assert out["status"] == "acknowledged" and out["disposed_by"] == "person"

        clock.advance(hours=1)
        assert self._reconcile(svc, 0)["status"] == "clean"
        after = db.get_operator_queue_item(row["id"])
        assert after["status"] == "acknowledged" and after["disposed_by"] == "person"

        clock.advance(days=1)
        assert self._reconcile(svc, 25)["status"] == "refused"
        assert _pending(db, self.SUBJECT) == []

    def test_r1_the_legacy_row_derives_the_subject_the_seam_files(self, db, svc):
        """R1: the second upgrade sweep stamps a pre-move row with exactly the
        subject `observe` now uses for the same agent."""
        from services import platform_alerts as pa
        legacy = pa.derive_legacy_subject(f"skills-reconcile-{self.AGENT}-25",
                                          {"alert_type": "skills_reconcile_blast_radius"})
        self._reconcile(svc, 25)
        [row] = _pending(db, self.SUBJECT)
        assert legacy == pa.LegacyMatch("skills_reconcile_refused", row["subject"])
        assert legacy.subject == pa.subject_for("skills_reconcile_refused", self.AGENT)
