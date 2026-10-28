"""trinity-enterprise#695: `POST /api/systems/apply`, the HTTP surface of apply mode.

The request model, the gates, the status mapping, and the dry run's write
boundary proved through the REAL authenticated path (no dependency override:
a seeded user-scoped MCP key goes through the real `get_current_user`).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from pydantic import ValidationError  # noqa: E402

from models import SystemApplyRequest, SystemDeployRequest  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The request model
# ---------------------------------------------------------------------------

def test_apply_previews_unless_told_otherwise():
    """`dry_run` defaults TRUE on apply (a verb that changes live agents), while
    create keeps its `False` (AC6: `SystemDeployRequest` is untouched)."""
    assert SystemApplyRequest(manifest="x").dry_run is True
    assert SystemDeployRequest(manifest="x").dry_run is False


@pytest.mark.parametrize("stray", [{"mode": "apply"}, {"strict": True}, {"mode": "create"}])
def test_an_unknown_key_is_refused_not_dropped(stray):
    """A silently dropped key is how `mode: "apply"` on an older `/deploy` runs
    CREATE and suffixes the fleet; apply refuses one instead."""
    with pytest.raises(ValidationError):
        SystemApplyRequest(manifest="x", **stray)


# ---------------------------------------------------------------------------
# Over HTTP, through the REAL `get_current_user` (no dependency override)
# ---------------------------------------------------------------------------

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required (system_service chain)")

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `_ent695_world`, as `_route_census`

import _ent695_world as W  # noqa: E402
import dependencies  # noqa: E402
import routers.systems as systems_router  # noqa: E402
import routers.tags as tags_router  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate  # noqa: E402
from services import system_service  # noqa: E402

SYSTEM = W.SYSTEM
APPLY = "/api/systems/apply"


class _Broadcasts:
    def __init__(self) -> None:
        self.messages: list = []

    async def broadcast(self, message: str) -> None:
        self.messages.append(message)


@pytest.fixture
def http(db_backend, monkeypatch, tmp_path):
    world = W.make_world(monkeypatch, tmp_path)
    W.install_executor_seams(world, monkeypatch)
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    monkeypatch.setattr(system_service, "_default_create_agent_fn", lambda: world.create)
    monkeypatch.setattr(system_service, "_default_start_agent_fn", lambda: world.docker.start)
    audits: list = []

    async def _audit(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(systems_router.platform_audit_service, "log", _audit)
    broadcasts = _Broadcasts()
    monkeypatch.setattr(tags_router, "manager", broadcasts)
    app = FastAPI()
    app.include_router(systems_router.router)
    world.client = TestClient(app)
    world.audits = audits
    world.broadcasts = broadcasts
    world.tmp_path = tmp_path
    return world


def _user_key(username: str = W.ALICE) -> dict:
    key = db.create_mcp_api_key(username, McpApiKeyCreate(name=f"{username} key"))
    return {"Authorization": f"Bearer {key.api_key}"}


def _jwt(username: str) -> dict:
    return {"Authorization": f"Bearer {dependencies.create_access_token({'sub': username})}"}


def _agent_key(agent: str, owner: str = W.ALICE) -> dict:
    return {"Authorization": f"Bearer {db.create_agent_mcp_api_key(agent, owner).api_key}"}


def _manifest(*shorts: str, extra: str = "") -> str:
    agents = "\n".join(f"  {s}:\n    template: local:plain" for s in shorts)
    return f"name: {SYSTEM}\nagents:\n{agents}\n{extra}"


def _usage() -> tuple:
    """The one key's usage counter and last-used stamp (the bookkeeping)."""
    from db_harness import scalar
    return (scalar("SELECT SUM(usage_count) FROM mcp_api_keys"),
            scalar("SELECT MAX(last_used_at) FROM mcp_api_keys"))


class TestAC4DryRunOverTheRealAuthPath:
    def test_a_body_without_dry_run_previews_and_writes_only_bookkeeping(self, http):
        http.seed_agent("acme-a")
        http.seed_agent("acme-old")
        headers = _user_key()
        before = _usage()

        with W.Witness(http, http.tmp_path) as w:
            res = http.client.post(APPLY, json={"manifest": _manifest("a", "b")}, headers=headers)

        assert res.status_code == 200, res.text
        body = res.json()
        assert body["dry_run"] is True and body["status"] == "valid" and body["mode"] == "apply"
        assert {m["short_name"]: m["verdict"] for m in body["members"]} == {"a": "unchanged", "b": "create"}
        assert [d["name"] for d in body["drift"]] == ["acme-old"]
        # Fleet state: nothing (SQL, tables, Redis, Docker, files).
        w.assert_no_fleet_write()
        # Bookkeeping: exactly the authenticating key's usage counter, +1.
        assert len(w.bookkeeping_statements) == 1
        after = _usage()
        assert after[0] == before[0] + 1 and after[1] != before[1]
        assert w.changed_tables(ignore=()) == ["mcp_api_keys"]
        assert http.audits == [] and http.broadcasts.messages == []

    @pytest.mark.parametrize("stray", [{"strict": True}, {"mode": "apply"}])
    def test_a_stray_key_is_a_422(self, http, stray):
        res = http.client.post(APPLY, json={"manifest": _manifest("a"), **stray}, headers=_user_key())
        assert res.status_code == 422


class TestAC5OverTheRealAuthPath:
    def test_weak_evidence_is_a_409_carrying_the_report_and_nothing_is_written(self, http):
        http.seed_agent("acme-a", tags=())
        headers = _user_key()
        with W.Witness(http, http.tmp_path) as w:
            res = http.client.post(APPLY, json={"manifest": _manifest("a", "b"), "dry_run": False},
                                   headers=headers)
        assert res.status_code == 409, res.text
        body = res.json()
        assert body["status"] == "refused"
        assert [(b["code"], b["member"]) for b in body["blockers"]] == [
            ("membership_evidence_prefix_only", "acme-a")]
        w.assert_no_fleet_write()  # no lock key either: refused before the lock
        assert http.audits == []

    def test_unreadable_tags_are_a_503(self, http, monkeypatch):
        http.seed_agent("acme-a")

        def _boom(names):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(db, "get_tags_for_agents", _boom)
        res = http.client.post(APPLY, json={"manifest": _manifest("a"), "dry_run": False},
                               headers=_user_key())
        assert res.status_code == 503
        assert [(b["code"], b["retryable"]) for b in res.json()["blockers"]] == [
            ("membership_tags_unreadable", True)]


    def test_unreadable_docker_is_a_503_not_a_409(self, http):
        """An existing member must not pick up a non-retryable
        `member_container_missing` while Docker cannot be asked:
        a caller that retries on 503 and stops on 409 would stop for good."""
        http.seed_agent("acme-a")
        http.docker.readable = False
        res = http.client.post(APPLY, json={"manifest": _manifest("a"), "dry_run": False},
                               headers=_jwt(W.ALICE))
        assert res.status_code == 503, res.text
        assert [b["code"] for b in res.json()["blockers"]] == ["roster_unreadable"]


class TestAgentsTheCallerCannotManage:
    """Fresh review #1 and #5, through the real authenticated path: an agent the
    caller may not manage neither refuses the run nor appears in the body, and
    a DECLARED one is the uniform 409 with nothing written."""

    def test_a_shared_agent_owned_by_another_user_does_not_block_and_is_not_named(self, http):
        http.seed_agent("acme-a")
        http.seed_agent("acme-notes", owner=W.BOB, tags=())  # untagged: name-only evidence
        http.seed_agent("acme-bobs", owner=W.BOB)            # tagged: would be drift
        for name in ("acme-notes", "acme-bobs"):
            db.share_agent(name, W.BOB, f"{W.ALICE}@example.com")
        res = http.client.post(APPLY, json={"manifest": _manifest("a"), "dry_run": False},
                               headers=_user_key())
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "applied" and res.json()["drift"] == []
        assert "acme-notes" not in res.text and "acme-bobs" not in res.text

    def test_a_declared_member_owned_by_another_user_is_a_409_writing_nothing(self, http):
        http.seed_agent("acme-api", owner=W.BOB)
        headers = _user_key()
        with W.Witness(http, http.tmp_path) as w:
            res = http.client.post(APPLY, json={"manifest": _manifest("api", "b"), "dry_run": False},
                                   headers=headers)
        assert res.status_code == 409, res.text
        body = res.json()
        assert body["status"] == "refused"
        assert [(b["code"], b["member"], b["retryable"]) for b in body["blockers"]] == [
            ("member_name_unavailable", "acme-api", False)]
        assert W.BOB not in res.text  # the owner is never named
        w.assert_no_fleet_write()  # and nothing was created for the free `b` either
        assert http.create_calls == [] and http.audits == []


class TestPersonsOnly:
    def test_an_agent_key_is_refused_before_any_read(self, http, monkeypatch):
        http.seed_agent("acme-a")

        async def _never(*a, **k):
            raise AssertionError("apply read the fleet for an agent key")

        monkeypatch.setattr(system_service, "read_apply_snapshot", _never)
        res = http.client.post(APPLY, json={"manifest": _manifest("a")}, headers=_agent_key("acme-a"))
        assert res.status_code == 403
        assert res.json()["detail"]["code"] == "person_required"

    def test_a_user_key_and_a_creator_jwt_pass(self, http):
        for headers in (_user_key(), _jwt(W.ALICE)):
            assert http.client.post(APPLY, json={"manifest": _manifest("a")},
                                    headers=headers).status_code == 200

    def test_a_person_below_creator_is_refused_by_the_creator_gate(self, http):
        res = http.client.post(APPLY, json={"manifest": _manifest("a")}, headers=_jwt(W.CAROL))
        assert res.status_code == 403
        assert "creator" in str(res.json()["detail"])


class TestStatusMapping:
    def test_applied_is_200_with_one_audit_row_and_a_tag_broadcast_per_member(self, http):
        http.seed_agent("acme-a", tags=(SYSTEM,))
        mf = _manifest("a", "b").replace("  a:\n    template: local:plain",
                                         "  a:\n    template: local:plain\n    tags: [team-x]")
        res = http.client.post(APPLY, json={"manifest": mf, "dry_run": False}, headers=_jwt(W.ALICE))
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "applied"
        (audit,) = http.audits
        assert (audit["event_action"], audit["target_type"], audit["target_id"]) == (
            "system_apply", "system", SYSTEM)
        assert audit["details"]["created"] == ["acme-b"]
        import json
        messages = [json.loads(m) for m in http.broadcasts.messages]
        # One thin trigger each, for the existing and the created member;
        # identifiers only, never the tag values (#918).
        assert sorted(m["agent_name"] for m in messages) == ["acme-a", "acme-b"]
        assert all(set(m) == {"type", "agent_name"} and m["type"] == "agent_tags_changed"
                   for m in messages)

    def test_failed_is_500_carrying_the_report(self, http):
        http.fail_create.add("acme-a")
        res = http.client.post(APPLY, json={"manifest": _manifest("a"), "dry_run": False},
                               headers=_jwt(W.ALICE))
        assert res.status_code == 500
        assert res.json()["status"] == "failed" and res.json()["failed"][0]["step"] == "create"
        # The audit row records what happened: nothing.
        (audit,) = http.audits
        assert audit["details"]["created"] == [] and audit["details"]["writes"] == {}

    def test_a_partial_runs_audit_counts_only_the_writes_that_landed(self, http):
        http.fail_create.add("acme-b")
        res = http.client.post(APPLY, json={"manifest": _manifest("a", "b"), "dry_run": False},
                               headers=_jwt(W.ALICE))
        assert res.json()["status"] == "partial"
        (audit,) = http.audits
        assert audit["details"]["created"] == ["acme-a"]
        assert set(audit["details"]["writes"]) == {"acme-a"}  # create, tag, start
        assert audit["details"]["writes"]["acme-a"] == 3

    def test_a_parse_error_is_creates_named_400(self, http):
        res = http.client.post(APPLY, json={"manifest": f"name: {SYSTEM}\nagents: {{}}\n"},
                               headers=_jwt(W.ALICE))
        assert res.status_code == 400
        assert res.json()["detail"] == "Missing required field: agents (must have at least 1)"

    def test_an_audit_failure_after_writes_is_a_warning_never_a_500(self, http, monkeypatch):
        async def _broken(**kwargs):
            raise RuntimeError("audit store down")

        monkeypatch.setattr(systems_router.platform_audit_service, "log", _broken)
        res = http.client.post(APPLY, json={"manifest": _manifest("a"), "dry_run": False},
                               headers=_jwt(W.ALICE))
        assert res.status_code == 200
        assert any("audit record could not be written" in w for w in res.json()["warnings"])

    def test_a_retryable_and_a_non_retryable_blocker_together_are_a_409(self, http):
        """Fresh review #3: 503 only when EVERY blocker is retryable; one that is
        not (here `folders_invalid`) makes it a 409, so a caller that retries on
        503 does not retry a broken manifest forever."""
        http.docker.readable = False
        mf = _manifest("a").replace("template: local:plain", "template: local:plain\n    folders: {expose: null}")
        res = http.client.post(APPLY, json={"manifest": mf, "dry_run": False}, headers=_jwt(W.ALICE))
        assert res.status_code == 409, res.text
        retryable = {b["code"]: b["retryable"] for b in res.json()["blockers"]}
        assert retryable == {"roster_unreadable": True, "folders_invalid": False}

    def test_a_held_lock_is_a_503(self, http):
        http.redis.store[f"system_apply:{SYSTEM}"] = "someone-else"
        res = http.client.post(APPLY, json={"manifest": _manifest("a"), "dry_run": False},
                               headers=_jwt(W.ALICE))
        assert res.status_code == 503
        assert [b["code"] for b in res.json()["blockers"]] == ["apply_in_progress"]


class TestCreateModeUntouched:
    """AC6, locally: the create route's request model and gates are as they
    were (the byte-level golden and the batch run are the other evidence)."""

    def test_deploy_still_ignores_an_unknown_mode_and_keeps_its_gates(self):
        import inspect
        assert SystemDeployRequest(manifest="x", mode="apply").model_dump() == {
            "manifest": "x", "dry_run": False, "strict": False}
        src = inspect.getsource(systems_router.deploy_system)
        assert 'capability_fence("agents.manage")' in inspect.getsource(systems_router) and "deploy_manifest(" in src
