"""trinity-enterprise#984 PR1: apply's dry run says what the real run will do.

Two places the preview and the run used to disagree:

* the schedules a `local:` template brings to a member apply creates (the
  dry run said `pending_template_resolution`; the run then updated, kept
  disabled, or reported the template's rows);
* a leftover `agent-<name>-workspace` volume (#1667): the dry run said
  `create`, and the real create answered 409.

Every case runs over the REAL `db` and the apply test world
(`_ent695_world.py`); Docker is faked at its read seams only, including the
leftover-volume probe.
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

docker = pytest.importorskip("docker", reason="backend venv required (system_service chain)")

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `_ent695_world`

import _ent695_world as W  # noqa: E402
from database import db  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from models import AgentConfig  # noqa: E402
from services import system_service  # noqa: E402
from services.agent_service import crud  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    return W.make_world(monkeypatch, tmp_path)


@pytest.fixture
def ew(world, monkeypatch):
    W.install_executor_seams(world, monkeypatch)
    return world


def _one(template: str, schedules: str = "") -> str:
    return f"name: {SYSTEM}\nagents:\n  a:\n    template: {template}\n{schedules}"


DAILY_OTHER_CRON = "    schedules:\n      - {name: daily, cron: '0 7 * * *', message: /manifest-daily}\n"
WEEKLY = "    schedules:\n      - {name: weekly, cron: '0 9 * * MON', message: /weekly}\n"


def _schedule_changes(response, short="a"):
    member = next(m for m in response.members if m.short_name == short)
    return [(c.field, c.action, c.code, c.current, c.desired, c.note)
            for c in member.changes if c.field.startswith("schedule:")]


def _codes(response, short="a"):
    return [(f, code) for f, _a, code, *_ in _schedule_changes(response, short)]


def _plan(world, manifest_yaml):
    manifest = system_service.parse_manifest(manifest_yaml)
    system_service.validate_manifest(manifest)
    return system_service.resolve_apply_plan(manifest, world.snapshot(manifest_yaml))


def _plan_codes(mp):
    return [(c.field, c.code) for c in mp.changes if c.field.startswith("schedule:")]


# ===========================================================================
# Item 3: a `local:` template's schedules, predicted
# ===========================================================================

class TestTemplateSchedulesPredicted:
    def test_a_collision_previews_the_update_and_the_kept_disabled_state(self, ew):
        """`sched` declares `daily` (enabled: false by default). The manifest's
        `daily` has another cron: the template's row is updated in place and
        stays disabled. The dry run says so, and says exactly what the run does."""
        mf = _one("local:sched", DAILY_OTHER_CRON)
        dry = W.apply(ew, mf, dry_run=True)
        assert dry.status == "valid", dry.blockers
        assert _codes(dry) == [("schedule:daily", "schedule_update"),
                               ("schedule:daily", "enabled_differs")]

        real = W.apply(ew, mf, dry_run=False)
        assert real.status == "applied", real.failed
        assert _schedule_changes(real) == _schedule_changes(dry)  # parity, field for field
        (row,) = db.list_agent_schedules("acme-a")
        assert (row.cron_expression, row.enabled) == ("0 7 * * *", False)

    def test_no_collision_is_a_create_and_a_template_only_row_is_reported(self, ew):
        mf = _one("local:sched", WEEKLY)
        dry = W.apply(ew, mf, dry_run=True)
        assert _codes(dry) == [("schedule:weekly", "schedule_create"),
                               ("schedule:daily", "schedule_undeclared")]
        real = W.apply(ew, mf, dry_run=False)
        assert real.status == "applied", real.failed
        assert _schedule_changes(real) == _schedule_changes(dry)

    def test_a_github_template_is_not_probed_and_stays_pending(self, world):
        mp = _plan(world, _one("github:org/repo", DAILY_OTHER_CRON)).members["a"]
        assert _plan_codes(mp) == [("schedule:daily", "pending_template_resolution")]

    def test_the_template_read_per_source(self, world):
        assert system_service._template_declared_schedules("acme-a", None) == []
        assert system_service._template_declared_schedules("acme-a", "github:org/repo") is None
        assert system_service._template_declared_schedules("acme-a", "local:nope") is None
        assert [e["name"] for e in system_service._template_declared_schedules(
            "acme-a", "local:sched")] == ["daily"]

    def test_a_template_read_error_is_a_retryable_read_failure(self, world, monkeypatch):
        def broken(config):
            raise OSError("disk went away")

        monkeypatch.setattr(crud, "_resolve_local_template", broken)
        snap = world.snapshot(_one("local:sched"))
        assert [(b.code, b.member, b.retryable) for b in snap.blockers] == [
            ("member_reads_unavailable", "acme-a", True)]

    def test_a_predicted_update_error_is_a_blocker_and_the_run_writes_nothing(
            self, ew, monkeypatch, tmp_path):
        """An update is validated over the MERGED cron/timezone (the template
        row's timezone when the entry names none). The template normalizer and
        the update check share today's parser, so the case is reached through
        the check's seam: the dry run refuses what the executor would fail."""
        (ew.templates_root / "tokyo").mkdir()
        (ew.templates_root / "tokyo" / "template.yaml").write_text(
            "name: tokyo\nschedules:\n  - {name: daily, cron: '0 9 * * *', message: /t, "
            "timezone: Asia/Tokyo}\n")
        real_check = system_service._schedule_entry_error

        def check(entry, *, cron=None, timezone=None):
            if timezone == "Asia/Tokyo":
                return "schedule_timezone_invalid", "not on this install"
            return real_check(entry, cron=cron, timezone=timezone)

        monkeypatch.setattr(system_service, "_schedule_entry_error", check)
        mf = _one("local:tokyo", DAILY_OTHER_CRON)
        dry = W.apply(ew, mf, dry_run=True)
        assert dry.status == "invalid"
        assert [(b.code, b.member, b.retryable) for b in dry.blockers] == [
            ("schedule_timezone_invalid", "acme-a", False)]

        with W.Witness(ew, tmp_path) as w:
            real = W.apply(ew, mf, dry_run=False)
        assert real.status == "refused"
        w.assert_no_fleet_write()


class TestTemplateOnlySchedules:
    """D6: the template brings rows and the manifest names none."""

    @pytest.mark.parametrize("schedules", ["", "    schedules: []\n"])
    def test_both_runs_report_the_template_row(self, ew, schedules):
        mf = _one("local:sched", schedules)
        assert [op.args for op in _plan(ew, mf).members["a"].ops
                if op.kind == "created_schedules"] == [{"entries": []}]
        dry = W.apply(ew, mf, dry_run=True)
        assert _codes(dry) == [("schedule:daily", "schedule_undeclared")]
        real = W.apply(ew, mf, dry_run=False)
        assert real.status == "applied", real.failed
        assert _schedule_changes(real) == _schedule_changes(dry)

    def test_a_github_template_re_reads_too(self, world):
        assert [op.args for op in _plan(world, _one("github:org/repo")).members["a"].ops
                if op.kind == "created_schedules"] == [{"entries": []}]

    def test_a_template_without_schedules_adds_no_re_read(self, world):
        assert [op for op in _plan(world, _one("local:plain")).members["a"].ops
                if op.kind == "created_schedules"] == []

    def test_a_failed_post_create_read_is_a_failure_never_an_unfiltered_create(
            self, ew, monkeypatch):
        real_list = db.list_agent_schedules
        calls = {"n": 0}

        def flaky(agent_name):
            if agent_name == "acme-a":
                calls["n"] += 1
                if calls["n"] >= 2:  # the template's own read succeeds; apply's fails
                    raise RuntimeError("read failed")
            return real_list(agent_name)

        monkeypatch.setattr(db, "list_agent_schedules", flaky)
        result = W.apply(ew, _one("local:sched"), dry_run=False)
        monkeypatch.setattr(db, "list_agent_schedules", real_list)
        assert result.status == "partial"
        assert [(f.name, f.step) for f in result.failed] == [("acme-a", "schedules")]
        assert "the template's schedules" in result.failed[0].reason
        assert [s.name for s in db.list_agent_schedules("acme-a")] == ["daily"]

    def test_a_failed_create_reports_no_skipped_re_read(self, ew):
        """A re-read with no manifest entry plans no write, so a failed create
        has nothing of it to report as skipped."""
        ew.fail_create.add("acme-a")
        result = W.apply(ew, _one("local:sched"), dry_run=False)
        assert [(f.name, f.step) for f in result.failed] == [("acme-a", "create")]


class TestEachScheduleListedOnce:
    def test_the_real_run_report_lists_each_created_member_schedule_once(self, ew):
        mf = _one("local:sched", "    schedules:\n"
                                 "      - {name: daily, cron: '0 7 * * *', message: /d}\n"
                                 "      - {name: weekly, cron: '0 9 * * MON', message: /w}\n")
        dry = W.apply(ew, mf, dry_run=True)
        real = W.apply(ew, mf, dry_run=False)
        assert real.status == "applied", real.failed
        counts = Counter(_codes(real))
        assert counts and max(counts.values()) == 1, counts
        assert _schedule_changes(real) == _schedule_changes(dry)


# ===========================================================================
# Create mode: the collision deploy skips, named in the preview
# ===========================================================================

def _deploy_preview(world, manifest_yaml):
    return asyncio.run(system_service.deploy_manifest(
        manifest_yaml, world.user(W.ALICE), dry_run=True))


def _template_warnings(response):
    return [w for w in response.warnings if "declares schedule(s)" in w]


class TestCreateModeCollisionWarning:
    def test_a_collision_is_exactly_one_warning(self, world):
        result = _deploy_preview(world, _one("local:sched", DAILY_OTHER_CRON))
        assert _template_warnings(result) == [
            "'acme-a': template 'local:sched' declares schedule(s) 'daily' (enabled: false), "
            "so deploy keeps the template's row and skips the manifest's fields for it."]
        # The preview entry itself is unchanged.
        assert [(p.name, p.cron, p.enabled) for p in result.schedules_preview] == [
            ("daily", "0 7 * * *", True)]

    def test_no_collision_and_a_github_member_add_no_warning(self, world):
        for mf in (_one("local:sched", WEEKLY), _one("github:org/repo", DAILY_OTHER_CRON)):
            assert _template_warnings(_deploy_preview(world, mf)) == []


# ===========================================================================
# Item 4: a leftover workspace volume, seen by the dry run
# ===========================================================================

class TestLeftoverWorkspaceVolume:
    def test_the_dry_run_refuses_with_the_create_paths_text(self, ew):
        ew.docker.volumes.add("agent-acme-a-workspace")
        dry = W.apply(ew, _one("local:plain"), dry_run=True)
        assert dry.status == "invalid"
        (blocker,) = dry.blockers
        assert (blocker.code, blocker.member, blocker.retryable) == (
            "member_workspace_volume_leftover", "acme-a", False)
        assert blocker.detail == crud.leftover_workspace_volume_detail("acme-a")
        assert "docker volume rm agent-acme-a-workspace" in blocker.remedy
        assert {m.short_name: m.verdict for m in dry.members} == {"a": "blocked"}

    def test_the_real_run_is_refused_before_any_write_or_lock_key(self, ew, tmp_path):
        ew.docker.volumes.add("agent-acme-a-workspace")
        with W.Witness(ew, tmp_path) as w:
            real = W.apply(ew, _one("local:plain"), dry_run=False)
        assert real.status == "refused"
        assert [b.code for b in real.blockers] == ["member_workspace_volume_leftover"]
        w.assert_no_fleet_write()  # includes the Redis log: no lock key

    def test_a_probe_that_errors_is_a_retryable_read_failure_never_free(self, ew):
        ew.docker.volume_probe_error = RuntimeError("docker timed out")
        dry = W.apply(ew, _one("local:plain"), dry_run=True)
        assert [(b.code, b.member, b.retryable) for b in dry.blockers] == [
            ("member_reads_unavailable", "acme-a", True)]
        assert {m.short_name: m.verdict for m in dry.members} == {"a": "blocked"}

    def test_only_a_free_deployable_name_is_probed(self, world):
        world.seed_agent("acme-a")
        mf = (f"name: {SYSTEM}\nagents:\n  a:\n    template: local:plain\n"
              "  b:\n    template: local:plain\n    deploy: false\n"
              "  c:\n    template: local:plain\n")
        world.snapshot(mf)
        assert world.docker.volume_probes == ["acme-c"]

    def test_no_probe_while_docker_is_unreadable(self, world):
        world.docker.readable = False
        world.snapshot(_one("local:plain"))
        assert world.docker.volume_probes == []


class TestOverHttp:
    """The router's status split for the two new outcomes."""

    @pytest.fixture
    def client(self, ew, monkeypatch):
        import dependencies
        import routers.systems as systems_router
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
        monkeypatch.setattr(system_service, "_default_create_agent_fn", lambda: ew.create)
        monkeypatch.setattr(system_service, "_default_start_agent_fn", lambda: ew.docker.start)
        app = FastAPI()
        app.include_router(systems_router.router)
        token = dependencies.create_access_token({"sub": W.ALICE})
        return TestClient(app), {"Authorization": f"Bearer {token}"}

    def test_a_leftover_volume_is_a_409(self, ew, client):
        http, headers = client
        ew.docker.volumes.add("agent-acme-a-workspace")
        res = http.post("/api/systems/apply", headers=headers,
                        json={"manifest": _one("local:plain"), "dry_run": False})
        assert res.status_code == 409, res.text
        assert [b["code"] for b in res.json()["blockers"]] == ["member_workspace_volume_leftover"]

    def test_a_failed_probe_is_a_503(self, ew, client):
        http, headers = client
        ew.docker.volume_probe_error = RuntimeError("docker timed out")
        res = http.post("/api/systems/apply", headers=headers,
                        json={"manifest": _one("local:plain"), "dry_run": False})
        assert res.status_code == 503, res.text


class TestTheCreatePathKeepsItsPolicy:
    """The predicate and the message moved; create's #1667 behaviour did not."""

    @pytest.fixture
    def probe(self, monkeypatch):
        monkeypatch.setattr(crud, "docker_client", object())
        state = {}

        async def volume_get(name):
            state["asked"] = name
            outcome = state["outcome"]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(crud, "volume_get", volume_get)
        return state

    @staticmethod
    def _guard(name="acme-a"):
        return asyncio.run(crud._guard_leftover_workspace_volume(
            AgentConfig(name=name, template="local:plain"), False))

    def test_an_existing_volume_is_creates_409_with_the_shared_text(self, probe):
        probe["outcome"] = object()
        with pytest.raises(HTTPException) as exc:
            self._guard()
        assert exc.value.status_code == 409
        assert exc.value.detail == crud.leftover_workspace_volume_detail("acme-a")
        assert probe["asked"] == "agent-acme-a-workspace"

    def test_a_probe_error_still_proceeds(self, probe):
        probe["outcome"] = RuntimeError("docker timed out")
        assert self._guard() is None

    def test_no_volume_proceeds(self, probe):
        probe["outcome"] = docker.errors.NotFound("no such volume")
        assert self._guard() is None

    def test_the_predicate_answers_or_raises(self, probe, monkeypatch):
        probe["outcome"] = docker.errors.NotFound("no such volume")
        assert asyncio.run(crud.workspace_volume_exists("acme-a")) is False
        probe["outcome"] = object()
        assert asyncio.run(crud.workspace_volume_exists("acme-a")) is True
        probe["outcome"] = RuntimeError("docker timed out")
        with pytest.raises(RuntimeError):
            asyncio.run(crud.workspace_volume_exists("acme-a"))
        monkeypatch.setattr(crud, "docker_client", None)
        with pytest.raises(RuntimeError):
            asyncio.run(crud.workspace_volume_exists("acme-a"))
