"""trinity-enterprise#871 — using a grant leaves an audit row, and grant changes
share one verb.

An agent key holding a capability that clears a fence writes one
`capability_used` row: the agent as actor, its key id, the capability, the
target, the method and the endpoint. The row means "the grant admitted this
call". It records the attempt, so a handler that refuses afterwards does not
remove it.

Everything else stays out of that row: a refusal writes only
`capability_refused`, a person or the system key writes nothing because no
grant decided their call, and an exemption writes nothing because no grant was
used.

Rows are read back from the real `audit_log`. The route tests go through the
real routers with real keys presented as bearers, because the decorators and
`get_current_user` are the wiring under test (`test_2996_owner_config_person_only.py`
is the same harness). `projects.manage` is covered by a direct call to
`enforce_agent_capability`.

Related: docs/memory/requirements/auth.md §2.9, feature-flows/self-change-grants.md.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_UNIT = Path(__file__).resolve().parent
_BACKEND = _UNIT.parents[1] / "src" / "backend"
for _p in (str(_BACKEND), str(_UNIT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

from db_harness import db_backend  # noqa: E402,F401 — the fixture, used by name
from db_harness import run as _hrun  # noqa: E402

pytest.importorskip("docker", reason="backend venv required (routers.agents chain)")

import _ent695_world as W  # noqa: E402
import routers.agent_config as _CFG  # noqa: E402
import routers.agent_files as _FILES  # noqa: E402
import routers.agents as _AGENTS  # noqa: E402
import routers.schedules as _SCHED  # noqa: E402
import routers.skills as _SKILLS  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, ScheduleCreate, UserCreate  # noqa: E402
from models import User  # noqa: E402
from services import metric_declaration_service as metric_svc  # noqa: E402
from services import metric_declaration_editor as metric_editor  # noqa: E402
from services import metric_references, metric_registry  # noqa: E402
from services import skill_set_service, system_service  # noqa: E402
from services.agent_service import files as files_svc  # noqa: E402
from services.system_apply import gates as apply_gates  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "ent871-owner"    # an admin: the default admin-owned install
OTHER = "ent871-other"
ORCH = "ent871-orch"      # the agent making the calls
AGENT = "ent871-agent"    # a sibling its owner owns
FOREIGN = "ent871-foreign"  # an agent another user owns
SYSTEM = "ent871-system"
NOWHERE = "ent871-no-such-agent"

USED = "capability_used"
REFUSED = "capability_refused"
NOT_OWNED = "capability_target_not_owned"

TEMPLATE = """\
name: declare-agent
metrics:
  - name: close_rate
    type: percentage
    label: Close rate
    cadence: 1w
"""
NEW_METRIC = {"name": "proposals_sent", "type": "counter", "label": "Proposals sent",
              "cadence": "1w", "direction": "up_good", "recorded_by": "the weekly skill"}


def deps(name: str):
    """A name in the `dependencies` module the routers hold. The unit conftest
    evicts that module after every test, so a module-level import here could be
    a different object from the one the routes were built with."""
    return _SCHED.capability_fence.__globals__[name]


def _schedule(agent: str, owner: str) -> str:
    return db.create_schedule(agent, owner, ScheduleCreate(
        name=f"nightly-{secrets.token_hex(3)}", cron_expression="0 3 * * *",
        message="run", enabled=False)).id


@pytest.fixture
def world(db_backend, monkeypatch):  # noqa: F811 — the db_harness fixture
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    monkeypatch.setitem(_SCHED.capability_fence.__globals__, "get_breaker_redis", lambda: None)
    db.create_user(UserCreate(username=OWNER, role="admin", email="ent871-owner@example.com"))
    db.create_user(UserCreate(username=OTHER, role="operator", email="ent871-other@example.com"))
    for name in (ORCH, AGENT):
        db.register_agent_owner(name, OWNER)
    db.register_agent_owner(FOREIGN, OTHER)

    stopped = SimpleNamespace(status="exited", attrs={"Config": {"Labels": {}}})
    monkeypatch.setattr(_CFG, "get_agent_container", lambda _n: stopped)

    app = FastAPI()

    @app.middleware("http")
    async def add_request_id(request, call_next):  # main.add_request_id's one job here
        request.state.request_id = "ent871-request"
        return await call_next(request)

    for router in (_SCHED.router, _CFG.router, _SKILLS.router, _FILES.router, _AGENTS.router):
        app.include_router(router)
    key = db.create_agent_mcp_api_key(ORCH, OWNER)
    yield SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        key_id=key.id,
        headers={"Authorization": f"Bearer {key.api_key}"},
        schedule=_schedule(AGENT, OWNER),
        foreign_schedule=_schedule(FOREIGN, OTHER),
    )


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(username: str = OWNER) -> dict:
    return _bearer(deps("create_access_token")({"sub": username}))


def _user_key() -> dict:
    return _bearer(db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="owner key")).api_key)


def _system_key() -> dict:
    key = db.create_agent_mcp_api_key(SYSTEM, OWNER)
    _hrun("UPDATE mcp_api_keys SET scope = 'system' WHERE id = :i", i=key.id)
    return _bearer(key.api_key)


def _grant(*capabilities: str, agent: str = ORCH) -> None:
    for capability in capabilities:
        db.grant_agent_capability(agent, capability, OWNER)


def _rows(action: str) -> list:
    return [r for r in db.get_audit_entries(limit=500) if r["event_action"] == action]


def _request(path="/api/test", method="POST", params=None) -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": [],
                    "query_string": b"", "client": ("198.51.100.7", 4321),
                    "path_params": params or {}})


def _agent_principal(agent: str = ORCH) -> User:
    row = db.get_user_by_username(OWNER)
    return User(id=row["id"], username=OWNER, email=row["email"], role="admin",
                mcp_scope="agent", agent_name=agent, mcp_key_id="key-871",
                mcp_key_name="agent key")


# One call per way a grant is checked: (method, url, kwargs, capability, target).
def _calls(w) -> dict:
    return {
        "capability_fence": ("DELETE", f"/api/agents/{AGENT}/schedules/{w.schedule}", {},
                             "schedules.manage", AGENT),
        "person_or_grant": ("GET", f"/api/agents/{AGENT}/schedules/{w.schedule}/webhook", {},
                            "schedules.manage", AGENT),
        "reconfigure": ("PUT", f"/api/agents/{AGENT}/timeout",
                        {"json": {"execution_timeout_seconds": 1800}}, "agents.manage", AGENT),
        "skills_fence": ("PUT", f"/api/agents/{AGENT}/skills", {"json": {"skills": []}},
                         "skills.manage", AGENT),
        "file_instructions": ("PUT", f"/api/agents/{AGENT}/files",
                              {"params": {"path": "CLAUDE.md"}, "json": {"content": "x"}},
                              "instructions.manage", AGENT),
        "file_own_instructions": ("PUT", f"/api/agents/{ORCH}/files",
                                  {"params": {"path": "CLAUDE.md"}, "json": {"content": "x"}},
                                  "instructions.manage", ORCH),
        "file_skills": ("PUT", f"/api/agents/{AGENT}/files",
                        {"params": {"path": ".claude/skills/x/SKILL.md"}, "json": {"content": "x"}},
                        "skills.manage", AGENT),
        "create": ("POST", "/api/agents", {"json": {"name": "ent871-new"}}, "agents.manage",
                   "ent871-new"),
    }


CALL_IDS = ["capability_fence", "person_or_grant", "reconfigure", "skills_fence",
            "file_instructions", "file_own_instructions", "file_skills", "create"]


@pytest.fixture
def fake_create(monkeypatch):
    async def create(config, *_a, **_k):
        return {"name": config.name}
    monkeypatch.setattr(_AGENTS, "create_agent_internal", create)


# --------------------------------------------------------------------------- #
# A holder's call
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("call", CALL_IDS)
def test_a_holder_call_writes_one_row_naming_agent_key_capability_and_target(
        world, fake_create, call):
    method, url, kwargs, capability, target = _calls(world)[call]
    _grant(capability)
    res = world.client.request(method, url, headers=world.headers, **kwargs)
    assert res.status_code not in (401, 403), res.text
    if not call.startswith("file_"):
        # Without the reach the fence earns, the handler's own gate answers 404.
        assert res.status_code != 404, res.text
    rows = _rows(USED)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["event_type"] == "authorization"
    assert (row["actor_type"], row["actor_id"]) == ("agent", ORCH)
    assert row["actor_email"] == "ent871-owner@example.com"
    assert (row["mcp_key_id"], row["mcp_scope"]) == (world.key_id, "agent")
    assert (row["target_type"], row["target_id"]) == ("agent", target)
    assert row["details"] == {"capability": capability, "method": method}
    assert row["endpoint"] == url.split("?")[0]
    assert row["request_id"] == "ent871-request"
    assert _rows(REFUSED) == []


def test_a_call_that_names_no_target_writes_a_row_with_none(world, monkeypatch):
    """`/deploy-local` and `/systems/deploy` have no agent in the path."""
    fence = deps("capability_fence")("agents.manage")
    _grant("agents.manage")
    asyncio.run(fence(_request("/api/agents/deploy-local"), _agent_principal()))
    rows = _rows(USED)
    assert [(r["target_id"], r["details"]["capability"]) for r in rows] == [(None, "agents.manage")]


def test_a_direct_enforce_call_writes_the_row_for_projects_manage(world):
    """The fifth capability, through the function every caller shares: a
    direct call with the agent itself as the target and no owner bound."""
    _grant("projects.manage")
    asyncio.run(deps("enforce_agent_capability")(
        _request(), _agent_principal(), "projects.manage", target=ORCH))
    rows = _rows(USED)
    assert [(r["actor_id"], r["target_id"], r["details"]["capability"]) for r in rows] == [
        (ORCH, ORCH, "projects.manage")]


def test_the_row_is_the_attempt_a_handler_refusal_does_not_remove_it(world):
    """A stated `agents.manage` bound lives in the handler, after the fence: an
    agent deletes only agents it spawned."""
    _grant("agents.manage")
    res = world.client.delete(f"/api/agents/{AGENT}", headers=world.headers)
    assert res.status_code == 403, res.text
    assert [r["target_id"] for r in _rows(USED)] == [AGENT]
    assert db.get_agent_owner(AGENT) is not None


def test_the_delete_row_shares_the_use_rows_request_id(world, monkeypatch):
    """A row does not say the delete landed; this join is how a reader finds out."""
    monkeypatch.setattr(_AGENTS, "get_agent_container", lambda _n: None)
    res = world.client.delete(f"/api/agents/{AGENT}", headers=_jwt())
    assert res.status_code == 200, res.text
    assert [(r["target_id"], r["request_id"]) for r in _rows("delete")] == [(AGENT, "ent871-request")]


def test_a_failed_audit_write_never_fails_the_call(world, monkeypatch):
    mod = importlib.import_module("services.platform_audit_service")
    monkeypatch.setattr(mod.platform_audit_service, "log", AsyncMock(side_effect=RuntimeError("db down")))
    _grant("schedules.manage")
    res = world.client.delete(f"/api/agents/{AGENT}/schedules/{world.schedule}", headers=world.headers)
    assert res.status_code == 204, res.text
    assert db.get_schedule(world.schedule) is None


# --------------------------------------------------------------------------- #
# One call, one row per capability
# --------------------------------------------------------------------------- #


@pytest.fixture
def metric_template(monkeypatch):
    async def read(_agent):
        return TEMPLATE
    monkeypatch.setattr(metric_registry, "_read_template_from_container", read)
    monkeypatch.setattr(metric_svc, "_objectives", AsyncMock(return_value=[]))
    monkeypatch.setattr(metric_svc, "_widgets", AsyncMock(return_value=[]))
    metric_references.clear_providers()
    yield
    metric_references.clear_providers()


@pytest.mark.parametrize("method,suffix,kwargs", [
    ("POST", "", {"json": NEW_METRIC}),
    ("PUT", "/close_rate", {"json": {"name": "close_rate", "type": "percentage",
                                     "label": "Win rate", "cadence": "1w"}}),
    ("DELETE", "/close_rate", {"params": {"acknowledge_references": "true"}}),
], ids=["declare", "update", "retire"])
def test_a_metric_declaration_write_is_one_row_though_it_passes_the_grant_twice(
        world, metric_template, method, suffix, kwargs):
    """The route fence takes `instructions.manage`, then the Files write it
    delegates to takes it again for `template.yaml`."""
    _grant("instructions.manage")
    base = metric_editor.template_hash(TEMPLATE)
    kwargs = dict(kwargs)
    if method == "DELETE":
        kwargs["params"] = {**kwargs["params"], "base_hash": base}
    else:
        kwargs["json"] = {**kwargs["json"], "base_hash": base}
    res = world.client.request(method, f"/api/agents/{AGENT}/metrics/declarations{suffix}",
                               headers=world.headers, **kwargs)
    assert res.status_code not in (403, 409, 422), res.text
    assert [(r["target_id"], r["details"]["capability"]) for r in _rows(USED)] == [
        (AGENT, "instructions.manage")]


def test_a_path_that_takes_two_capabilities_writes_one_row_for_each(world):
    _grant("skills.manage", "instructions.manage")
    world.client.delete(f"/api/agents/{AGENT}/files", params={"path": ".claude"}, headers=world.headers)
    assert sorted(r["details"]["capability"] for r in _rows(USED)) == [
        "instructions.manage", "skills.manage"]
    assert _rows(REFUSED) == []


@pytest.mark.parametrize("held,missing", [("skills.manage", "instructions.manage"),
                                          ("instructions.manage", "skills.manage")])
def test_holding_one_of_two_capabilities_writes_only_the_refusal(world, held, missing):
    """Reach comes from the sibling call edge here, so the file route's capability
    gate is what decides."""
    db.add_agent_permission(ORCH, AGENT, OWNER)
    _grant(held)
    res = world.client.delete(f"/api/agents/{AGENT}/files", params={"path": ".claude"}, headers=world.headers)
    assert res.status_code == 403, res.text
    assert _rows(USED) == []
    assert [r["details"]["capability"] for r in _rows(REFUSED)] == [missing]


def test_the_memo_is_per_request_a_second_call_writes_a_second_row(world):
    _grant("schedules.manage")
    url = f"/api/agents/{AGENT}/schedules/{world.schedule}/webhook"
    for _ in range(2):
        assert world.client.get(url, headers=world.headers).status_code == 200
    assert len(_rows(USED)) == 2


# --------------------------------------------------------------------------- #
# Refused calls
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("call", CALL_IDS)
def test_a_non_holder_writes_only_the_refusal(world, call):
    if call == "file_own_instructions":
        pass  # its own files: the capability gate is what refuses
    elif call.startswith("file_"):
        db.add_agent_permission(ORCH, AGENT, OWNER)  # reach, so the gate decides
    method, url, kwargs, capability, target = _calls(world)[call]
    res = world.client.request(method, url, headers=world.headers, **kwargs)
    assert res.status_code == 403, res.text
    assert _rows(USED) == []
    assert [(r["target_id"], r["details"]["capability"]) for r in _rows(REFUSED)] == [
        (target, capability)]


def _out_of_reach(w, target: str) -> dict:
    schedule = w.foreign_schedule if target == FOREIGN else "no-such-schedule"
    return {
        "capability_fence": ("DELETE", f"/api/agents/{target}/schedules/{schedule}", {}, "schedules.manage"),
        "person_or_grant": ("GET", f"/api/agents/{target}/schedules/{schedule}/webhook", {}, "schedules.manage"),
        "skills_fence": ("PUT", f"/api/agents/{target}/skills", {"json": {"skills": []}}, "skills.manage"),
    }


@pytest.mark.parametrize("fence", ["capability_fence", "person_or_grant", "skills_fence"])
def test_a_holder_outside_its_owner_gets_the_same_404_and_a_refusal_row(world, fence):
    """The grant admitted nothing here, so it is not a use. The row is the only
    trace of a holder probing past its owner's agents."""
    capability = _out_of_reach(world, FOREIGN)[fence][3]
    _grant(capability)
    bodies = []
    for target in (FOREIGN, NOWHERE):
        method, url, kwargs, _ = _out_of_reach(world, target)[fence]
        res = world.client.request(method, url, headers=world.headers, **kwargs)
        assert res.status_code == 404, res.text
        bodies.append(res.json())
    assert bodies[0] == bodies[1] == {"detail": "Agent not found"}
    assert _rows(USED) == []
    rows = _rows(REFUSED)
    assert sorted(r["target_id"] for r in rows) == sorted((FOREIGN, NOWHERE))
    for row in rows:
        assert row["details"]["code"] == NOT_OWNED
        assert row["details"]["capability"] == capability
        assert (row["actor_type"], row["actor_id"]) == ("agent", ORCH)
    assert db.get_schedule(world.foreign_schedule) is not None


def test_a_holder_on_itself_on_a_self_person_only_route_writes_nothing(world):
    """Refused by the person rule before the grant is read: nothing to record."""
    _grant("agents.manage")
    res = world.client.put(f"/api/agents/{ORCH}/read-only", json={"enabled": True}, headers=world.headers)
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["code"] == "person_required"
    assert _rows(USED) == [] and _rows(REFUSED) == []


# --------------------------------------------------------------------------- #
# No grant decided
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("headers", [
    pytest.param(lambda: _jwt(), id="signed-in-person"),
    pytest.param(lambda: _user_key(), id="user-key"),
    pytest.param(lambda: _system_key(), id="system-key"),
])
def test_a_person_or_the_system_agent_writes_no_row(world, headers):
    db.grant_agent_capability(SYSTEM, "schedules.manage", OWNER)
    res = world.client.delete(f"/api/agents/{AGENT}/schedules/{world.schedule}", headers=headers())
    assert res.status_code == 204, res.text
    assert _rows(USED) == [] and _rows(REFUSED) == []


@pytest.mark.parametrize("principal", [
    pytest.param({"vouched_source_agent": ORCH, "is_event_loopback": True}, id="loopback-jwt"),
    pytest.param({"mcp_scope": "user", "agent_name": ORCH}, id="user-scope-carrying-an-agent"),
])
def test_a_principal_that_is_not_an_agent_key_writes_no_row(world, principal):
    user = User(id=1, username=OWNER, role="admin", **principal)
    _grant("schedules.manage")
    asyncio.run(deps("enforce_agent_capability")(_request(), user, "schedules.manage", target=AGENT))
    assert _rows(USED) == []


@pytest.mark.parametrize("held", [False, True], ids=["without-the-grant", "holding-the-grant"])
def test_an_agent_on_its_own_schedule_uses_no_grant(world, held):
    if held:
        _grant("schedules.manage")
    schedule = _schedule(ORCH, OWNER)
    res = world.client.delete(f"/api/agents/{ORCH}/schedules/{schedule}", headers=world.headers)
    assert res.status_code == 204, res.text
    assert _rows(USED) == []


def test_discarding_a_ghost_uses_no_grant(world, monkeypatch):
    monkeypatch.setattr(db, "get_agent_ephemeral_info", lambda _n: {"is_ephemeral": True})
    _grant("agents.manage")
    fence = deps("capability_fence")("agents.manage", ephemeral_target_exempt=True)
    asyncio.run(fence(_request(params={"agent_name": AGENT}), _agent_principal()))
    assert _rows(USED) == []


def test_a_question_about_what_the_key_may_do_writes_no_row(world):
    """Predicates answer a read or a response field; none of them is the use."""
    _grant("skills.manage", "schedules.manage", "instructions.manage")
    user = _agent_principal()
    assert deps("can_manage_agent_skills")(user, AGENT) is True
    assert deps("person_or_capability_admits")(user, "schedules.manage", AGENT) is True
    assert deps("assert_skill_gates_readable")(AGENT, user) == AGENT
    assert files_svc._may_reach_for_path(user, AGENT, "CLAUDE.md") is True
    assert _rows(USED) == [] and _rows(REFUSED) == []


# --------------------------------------------------------------------------- #
# A write that judges the grant itself
# --------------------------------------------------------------------------- #


@pytest.fixture
def sets(monkeypatch):
    status = AsyncMock(return_value=[])
    monkeypatch.setattr(skill_set_service, "agent_set_status", status)
    return status


def test_a_holders_honoured_probe_is_a_use(world, sets):
    """`probe=true` runs a command inside the target, on `skills.manage` alone."""
    _grant("skills.manage")
    db.add_agent_permission(ORCH, AGENT, OWNER)  # the read needs reach of its own
    res = world.client.get(f"/api/agents/{AGENT}/skill-sets", params={"probe": "true"}, headers=world.headers)
    assert res.status_code == 200, res.text
    assert sets.await_args.kwargs["probe"] is True
    assert [(r["target_id"], r["details"]["capability"]) for r in _rows(USED)] == [(AGENT, "skills.manage")]


@pytest.mark.parametrize("params,grant", [
    pytest.param({}, True, id="no-probe-asked"),
    pytest.param({"probe": "true"}, False, id="probe-downgraded-for-a-non-holder"),
])
def test_a_read_with_no_honoured_probe_writes_no_row(world, sets, params, grant):
    if grant:
        _grant("skills.manage")
    db.add_agent_permission(ORCH, AGENT, OWNER)
    res = world.client.get(f"/api/agents/{AGENT}/skill-sets", params=params, headers=world.headers)
    assert res.status_code == 200, res.text
    assert sets.await_args.kwargs["probe"] is False
    assert _rows(USED) == [] and _rows(REFUSED) == []


def test_a_persons_probe_writes_no_row(world, sets):
    res = world.client.get(f"/api/agents/{AGENT}/skill-sets", params={"probe": "true"}, headers=_jwt())
    assert res.status_code == 200, res.text
    assert sets.await_args.kwargs["probe"] is True
    assert _rows(USED) == []


def test_record_capability_use_never_raises_past_a_failed_read(world, monkeypatch):
    """It runs after a deploy's agents exist: a raise would skip the rest."""
    def boom(*_a):
        raise RuntimeError("db locked")
    monkeypatch.setattr(db, "agent_has_capability", boom)
    asyncio.run(deps("record_capability_use")(_request(), _agent_principal(), "skills.manage"))
    assert _rows(USED) == []


class TestManifestDeploy:
    SYSTEM_NAME = W.SYSTEM
    LEAD = f"{W.SYSTEM}-lead"
    PLAIN = f"{W.SYSTEM}-plain"

    @pytest.fixture
    def mw(self, db_backend, monkeypatch, tmp_path):  # noqa: F811
        w = W.make_world(monkeypatch, tmp_path)
        W.install_executor_seams(w, monkeypatch)
        w.audit = []

        async def log(**kw):
            w.audit.append(kw)
        audit_mod = importlib.import_module("services.platform_audit_service")
        monkeypatch.setattr(audit_mod.platform_audit_service, "log", log)
        # The two phases' own writes are other suites' subject (test_ent1030_gates).
        monkeypatch.setattr(apply_gates, "deploy", AsyncMock(return_value=[]))
        monkeypatch.setattr(system_service, "assign_manifest_skills", AsyncMock(return_value=[]))
        # Left real, the deploy reaches `start_agent_internal` and the host's Docker.
        monkeypatch.setattr(system_service, "start_all_agents", AsyncMock(return_value={}))
        db.register_agent_owner("orchestrator", W.ALICE)
        return w

    @staticmethod
    def _key(w):
        return w.user(W.ALICE).model_copy(update={
            "mcp_scope": "agent", "agent_name": "orchestrator", "mcp_key_id": "key-871"})

    def _manifest(self, field: str) -> str:
        return (f"name: {self.SYSTEM_NAME}\nagents:\n  lead:\n    template: local:plain\n"
                f"    {field}\n  plain:\n    template: local:plain\n")

    def _deploy(self, w, text, user, *, dry_run=False):
        return asyncio.run(system_service.deploy_manifest(
            text, user, _request("/api/systems/deploy"), dry_run=dry_run, create_agent_fn=w.create))

    @staticmethod
    def _used(w):
        return [(a["target_id"], a["details"]["capability"]) for a in w.audit
                if a["event_action"] == USED]

    @pytest.mark.parametrize("field", ["gates: {deploy: {}}", "skills: [research]", "skill_sets: [ops]"],
                             ids=["gates", "skills", "skill_sets"])
    def test_a_holder_writes_one_row_per_member_the_grant_decided(self, mw, field):
        db.grant_agent_capability("orchestrator", "skills.manage", W.ADMIN)
        res = self._deploy(mw, self._manifest(field), self._key(mw))
        assert res.status == "deployed", (res.failed, res.warnings)
        assert self._used(mw) == [(self.LEAD, "skills.manage")]

    def test_a_key_without_the_grant_is_held_back_and_writes_no_row(self, mw):
        res = self._deploy(mw, self._manifest("skills: [research]"), self._key(mw))
        assert res.status == "deployed", res.failed
        assert self._used(mw) == []

    def test_a_dry_run_writes_no_row(self, mw):
        db.grant_agent_capability("orchestrator", "skills.manage", W.ADMIN)
        self._deploy(mw, self._manifest("skills: [research]"), self._key(mw), dry_run=True)
        assert self._used(mw) == []

    def test_a_member_that_was_not_created_is_not_a_use(self, mw):
        db.grant_agent_capability("orchestrator", "skills.manage", W.ADMIN)
        mw.fail_create.add(self.LEAD)
        res = self._deploy(mw, self._manifest("skills: [research]"), self._key(mw))
        assert res.status == "partial", (res.status, res.failed)
        assert self._used(mw) == []

    def test_a_person_writes_no_row(self, mw):
        res = self._deploy(mw, self._manifest("skills: [research]"), mw.user(W.ALICE))
        assert res.status == "deployed", res.failed
        assert self._used(mw) == []


# --------------------------------------------------------------------------- #
# Grant changes share one verb
# --------------------------------------------------------------------------- #


GRANT_ROUTES = [
    pytest.param(f"/api/agents/{AGENT}/skill-manager", id="skill-manager-route"),
    pytest.param(f"/api/agents/{AGENT}/capability-grants/skills.manage", id="capability-grants-route"),
]


@pytest.mark.parametrize("url", GRANT_ROUTES)
def test_skills_manage_logs_the_shared_verbs_through_either_route(world, url):
    for granted in (True, False):
        res = world.client.put(url, json={"granted": granted}, headers=_jwt())
        assert res.status_code == 200 and res.json()["changed"] is True, res.text
    rows = _rows("capability_grant") + _rows("capability_revoke")
    assert sorted((r["event_action"], r["details"]["capability"], r["details"]["granted"]) for r in rows) == [
        ("capability_grant", "skills.manage", True), ("capability_revoke", "skills.manage", False)]
    assert all(r["target_id"] == AGENT for r in rows)
    assert _rows("skill_manager_grant") == [] and _rows("skill_manager_revoke") == []
    assert _rows(USED) == []
