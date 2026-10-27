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
import os
import shutil
import subprocess
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

        pending = db.list_operator_queue_items(agent_name=HOST, status="pending")
        assert [(r["subject"], r["context"]["orphan_count"]) for r in pending] == [
            (self.SUBJECT, 31)]                               # one row, on the subject
        [row] = pending
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
        [first] = _pending(db, self.SUBJECT)
        if case == "inventory_unavailable":
            svc._list_managed_skills = AsyncMock(return_value=None)
            result = asyncio.run(svc.reconcile_agent_skills(self.AGENT, []))
        elif case == "busy":
            svc.remove_skills = AsyncMock(side_effect=SkillInjectionBusy("busy"))
            result = self._reconcile(svc, 3)
        else:
            result = self._reconcile(svc, error=RuntimeError("boom"))
        assert result["status"] == "skipped"
        assert [(r["id"], r["context"]["seen_count"]) for r in _pending(db, self.SUBJECT)] == [
            (first["id"], 1)]                                 # untouched, not ended

    # -- the REAL agent-side inventory (`_list_managed_skills`'s script) -------

    @pytest.fixture
    def agent_home(self, tmp_path, monkeypatch):
        """Run the agent-side inventory script on this host instead of in a
        container: the container exec is replaced by a local shell with HOME at
        a temp dir, so `_list_managed_skills` and `_run_agent_python` run
        unmodified against a real `~/.claude/skills`."""
        import services.docker_service as docker_service
        home = tmp_path / "home"
        home.mkdir()

        async def _exec(container_name, command, timeout=None, **_kw):
            proc = subprocess.run(command, shell=True, capture_output=True, text=True,
                                  env={**os.environ, "HOME": str(home)}, timeout=120)
            return {"exit_code": proc.returncode, "output": proc.stdout}

        monkeypatch.setattr(docker_service, "execute_command_in_container", _exec)
        return home

    @staticmethod
    def _managed(home, count):
        from services import skill_packaging as pkg
        skills = home / ".claude" / "skills"
        for i in range(count):
            d = skills / f"orphan-{i:02d}"
            d.mkdir(parents=True)
            (d / pkg.META_FILENAME).write_text("{}")
        return skills

    @pytest.mark.parametrize("damage", ["not_a_directory", "unreadable"])
    def test_an_unreadable_skills_dir_leaves_the_refusal_alone(
            self, db, svc, agent_home, clock, damage):
        """R10 through the real inventory: an enumeration that FAILS is "could
        not look", never "nothing is managed". It used to print `[]`, which read
        as a clean pass and would have ended the refusal."""
        if damage == "unreadable" and os.geteuid() == 0:
            pytest.skip("root can list a mode-000 directory")
        skills = self._managed(agent_home, 12)
        assert asyncio.run(svc.reconcile_agent_skills(self.AGENT, []))["status"] == "refused"
        [first] = _pending(db, self.SUBJECT)

        if damage == "not_a_directory":
            shutil.rmtree(skills)
            skills.write_text("")
        else:
            skills.chmod(0)
        clock.advance(minutes=1)
        try:
            result = asyncio.run(svc.reconcile_agent_skills(self.AGENT, []))
        finally:
            if damage == "unreadable":
                skills.chmod(0o755)
        assert result == {"status": "skipped", "reason": "inventory_unavailable"}
        assert [r["id"] for r in _pending(db, self.SUBJECT)] == [first["id"]]   # not ended

    def test_an_absent_skills_dir_is_a_clean_pass_that_ends_the_refusal(
            self, db, svc, agent_home, clock):
        """No `~/.claude/skills` at all is positive evidence: nothing is managed,
        so nothing is over the cap."""
        skills = self._managed(agent_home, 12)
        assert asyncio.run(svc.reconcile_agent_skills(self.AGENT, []))["status"] == "refused"
        [row] = _pending(db, self.SUBJECT)
        shutil.rmtree(skills)
        clock.advance(minutes=1)
        assert asyncio.run(svc.reconcile_agent_skills(self.AGENT, [])) == {
            "status": "clean", "removed": 0}
        assert _pending(db, self.SUBJECT) == []
        _assert_ended_by_platform(db, row)

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


# ---------------------------------------------------------------------------
# The fleet sweep — `skills_fleet_reinject:fleet`, 14-day default lifetime
# ---------------------------------------------------------------------------

class TestFleetReinject:
    SUBJECT = "skills_fleet_reinject:fleet"

    @pytest.fixture
    def sweep(self, monkeypatch):
        from services.skills_sync_service import SkillsLibrarySyncService as S
        monkeypatch.setattr(S, "_audit_fleet", staticmethod(AsyncMock()))

        def _run(outcomes, *, agents=None, commit="abc123"):
            names = list(outcomes) if agents is None else agents
            monkeypatch.setattr(S, "_eligible_agents", staticmethod(lambda: names))
            monkeypatch.setattr(S, "_reinject_agent", staticmethod(
                AsyncMock(side_effect=lambda n, sets_lib=None: outcomes[n])))
            return asyncio.run(S().run_fleet_reinject(commit_sha=commit))
        return _run

    OK = {"status": "injected", "injected": 1, "unchanged": 0}
    BAD = {"status": "failed", "error": "1 skill(s) failed"}
    BUSY = {"status": "skipped", "reason": "busy"}
    NO_SKILLS = {"status": "skipped", "reason": "no_skills"}

    def test_two_failed_sweeps_are_one_row_carrying_the_seam_properties(
            self, db, sweep, clock):
        """The old id (`skills-fleet-reinject-{finished_at}`) filed every bad
        run as its own row; now the latest run updates the one condition."""
        from services import platform_alerts as pa
        sweep({"a1": self.BAD, "a2": self.OK}, commit="c1")
        clock.advance(hours=6)
        sweep({"a1": self.OK, "a2": self.BAD, "a3": self.BAD}, commit="c2")

        pending = db.list_operator_queue_items(agent_name=HOST, status="pending")
        assert [(r["subject"], r["context"]["commit_sha"]) for r in pending] == [
            (self.SUBJECT, "c2")]                             # one row, on the subject
        [row] = pending
        assert row["request_id"].startswith("skills-fleet-reinject-fleet-")
        assert row["priority"] == "medium"
        assert row["context"]["seen_count"] == 2
        assert row["context"]["agents"] == ["a2", "a3"]    # the latest reading
        assert row["context"]["commit_sha"] == "c2"
        assert "a2, a3" in row["question"]
        assert _parse(row["last_seen_at"]) == T0 + timedelta(hours=6)
        lifetime = pa.lifetime_for("skills_fleet_reinject")
        assert _parse(row["expires_at"]) == T0 + timedelta(hours=6) + lifetime

    @pytest.mark.parametrize("outcomes", [
        {"a1": OK, "a2": OK},
        {"a1": OK, "a2": NO_SKILLS},
    ], ids=["all-injected", "injected-and-no-skills"])
    def test_a_sweep_that_reached_every_agent_ends_the_row_as_the_platform(
            self, db, sweep, outcomes):
        sweep({"a1": self.BAD, "a2": self.OK})
        [row] = _pending(db, self.SUBJECT)
        report = sweep(outcomes)
        assert report["agents_total"] == 2 and report["agents_failed"] == 0
        assert _pending(db, self.SUBJECT) == []
        _assert_ended_by_platform(db, row)

    def test_a_docker_error_enumerates_nothing_and_ends_nothing(
            self, db, sweep, monkeypatch):
        """R10 (the #2196 class): `_eligible_agents` returns [] when Docker is
        unreadable — an empty sweep is not a recovered fleet."""
        import services.docker_service as docker_service
        from services.skills_sync_service import SkillsLibrarySyncService as S
        real_enumeration = S.__dict__["_eligible_agents"]
        sweep({"a1": self.BAD})
        [row] = _pending(db, self.SUBJECT)

        def _unreadable():
            raise RuntimeError("docker daemon unreachable")

        monkeypatch.setattr(S, "_eligible_agents", real_enumeration)
        monkeypatch.setattr(docker_service, "list_all_agents_fast", _unreadable)
        assert S._eligible_agents() == []
        report = asyncio.run(S().run_fleet_reinject(commit_sha="abc123"))
        assert report["agents_total"] == 0 and report["agents_failed"] == 0
        assert [r["id"] for r in _pending(db, self.SUBJECT)] == [row["id"]]   # not ended

    def test_a_busy_agent_leaves_the_row_alone(self, db, sweep):
        """A busy agent was never re-injected: its earlier failure is unknown."""
        sweep({"a1": self.BAD, "a2": self.OK})
        [row] = _pending(db, self.SUBJECT)
        report = sweep({"a1": self.BUSY, "a2": self.OK})
        assert report["agents_failed"] == 0
        assert [r["id"] for r in _pending(db, self.SUBJECT)] == [row["id"]]   # not ended

    def test_an_agent_with_no_result_leaves_the_row_alone(self, db, sweep):
        """Every enumerated agent must have reported; a missing result (the
        gather swallowed an exception) is not evidence."""
        sweep({"a1": self.BAD})
        [row] = _pending(db, self.SUBJECT)

        class _Partial(dict):
            def __getitem__(self, name):
                if name == "a2":
                    raise RuntimeError("lost")
                return super().__getitem__(name)

        report = sweep(_Partial({"a1": self.OK}), agents=["a1", "a2"])
        assert report["agents_failed"] == 0
        assert [r["id"] for r in _pending(db, self.SUBJECT)] == [row["id"]]   # not ended

    def test_a_persons_ending_stands_and_snoozes_the_next_failure(self, db, sweep, clock):
        sweep({"a1": self.BAD})
        [row] = _pending(db, self.SUBJECT)
        out = _answer(db, row)
        assert out["status"] == "acknowledged" and out["disposed_by"] == "person"

        clock.advance(hours=1)
        sweep({"a1": self.OK})
        after = db.get_operator_queue_item(row["id"])
        assert after["status"] == "acknowledged" and after["disposed_by"] == "person"

        clock.advance(days=1)
        sweep({"a1": self.BAD})
        assert _pending(db, self.SUBJECT) == []

    def test_r1_the_legacy_row_derives_the_subject_the_seam_files(self, db, sweep):
        from services import platform_alerts as pa
        legacy = pa.derive_legacy_subject("skills-fleet-reinject-2026-09-20T00:00:00.000000Z",
                                          {"alert_type": "skills_fleet_reinject_failed"})
        sweep({"a1": self.BAD})
        [row] = _pending(db, self.SUBJECT)
        assert legacy == pa.LegacyMatch("skills_fleet_reinject", row["subject"])


# ---------------------------------------------------------------------------
# Other shape — a fresh install that never raised either alarm
# ---------------------------------------------------------------------------

class TestFreshInstall:
    def test_a_clear_on_a_fresh_install_ends_nothing_and_writes_nothing(
            self, db, triggers, monkeypatch):
        """The clear sites run on every agent start and every clean sweep, so
        on an install that never raised the alarm they must be pure reads: no
        row, no platform ending, no audit or broadcast scheduled. The first
        refusal after that files exactly one row."""
        from services import ask_service
        from services.skill_service import SkillService
        from services.skills_sync_service import SkillsLibrarySyncService as S

        endings = []
        real_clear_platform = ask_service.clear_platform
        monkeypatch.setattr(ask_service, "clear_platform",
                            lambda *a, **kw: endings.append(a) or real_clear_platform(*a, **kw))
        monkeypatch.setattr(S, "_audit_fleet", staticmethod(AsyncMock()))
        monkeypatch.setattr(S, "_eligible_agents", staticmethod(lambda: ["a1"]))
        monkeypatch.setattr(S, "_reinject_agent", staticmethod(
            AsyncMock(return_value={"status": "injected", "injected": 0, "unchanged": 2})))
        svc = SkillService.__new__(SkillService)
        svc._list_managed_skills = AsyncMock(return_value=[])

        assert _queue_row_count() == 0
        assert asyncio.run(svc.reconcile_agent_skills("fresh-agent", []))["status"] == "clean"
        assert asyncio.run(S().run_fleet_reinject(commit_sha="abc"))["agents_failed"] == 0
        assert _queue_row_count() == 0
        assert endings == [] and triggers == []

        svc._list_managed_skills = AsyncMock(return_value=[f"o{i:02d}" for i in range(11)])
        assert asyncio.run(svc.reconcile_agent_skills("fresh-agent", []))["status"] == "refused"
        assert _queue_row_count() == 1
        [row] = _pending(db, "skills_reconcile_refused:fresh-agent")
        assert row["context"]["seen_count"] == 1
