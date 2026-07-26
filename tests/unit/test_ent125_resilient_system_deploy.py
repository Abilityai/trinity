"""Resilient system-manifest deploy — continue-on-error + partial-success report
(trinity-enterprise#125).

POST /api/systems/deploy is best-effort by default: a per-agent create failure
is collected into `failed[]` and the remaining agents still deploy; post-create
configuration is scoped to the created agents and each config phase degrades to
`warnings`. Tri-state `status`: deployed (all created, 200) / partial (some
failed, 200) / failed (none created, 500 with the full report body). `strict:
true` restores abort-on-first-error preserving the original status code.

True unit tests: the router is mounted on a bare FastAPI app. The deploy
orchestration lives in `services.system_service.deploy_manifest` (moved there
by trinity-enterprise#124), so every collaborator is patched on the
`services.system_service` module object: the config functions + db at their
module bindings, and agent creation via the `_default_create_agent_fn` seam
(the real seam lazy-imports the `routers/agents` ws-broadcasting facade at
call time, so patching a from-import binding would be a no-op — learnings
2026-07-11).
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routers.systems as systems
import services.system_service as system_service
from dependencies import get_current_user

pytestmark = pytest.mark.unit


TWO_AGENT_MANIFEST = """
name: test-sys
agents:
  alpha:
    template: local:default
  beta:
    template: local:default
"""

ORCH_MANIFEST = """
name: test-orch
agents:
  orchestrator:
    template: local:default
  worker:
    template: local:default
permissions:
  preset: orchestrator-workers
"""


def _user(role="admin"):
    return types.SimpleNamespace(
        id=1, username=role, role=role, email=f"{role}@example.com",
        agent_name=None, connector_agent=None
    )


@pytest.fixture
def env(monkeypatch):
    """Router mounted alone; every collaborator patched at the module binding."""
    mocks = types.SimpleNamespace(
        create_agent=AsyncMock(return_value={"status": "created"}),
        configure_folders=MagicMock(return_value=1),
        configure_permissions=AsyncMock(return_value=2),
        create_schedules=MagicMock(return_value=0),
        configure_tags=MagicMock(return_value=3),
        create_system_view=MagicMock(return_value=None),
        start_all_agents=AsyncMock(
            side_effect=lambda names: {n: "started" for n in names}
        ),
        db=MagicMock(),
    )
    # ent#124: deploy orchestration lives in services.system_service — patch
    # there. The create seam is `_default_create_agent_fn` (called when no
    # `create_agent_fn` is passed); do NOT patch `systems.db` — the router
    # keeps its own db import for list/get endpoints and a patch there would
    # silently assert nothing.
    monkeypatch.setattr(system_service, "_default_create_agent_fn", lambda: mocks.create_agent)
    monkeypatch.setattr(system_service, "configure_folders", mocks.configure_folders)
    monkeypatch.setattr(system_service, "configure_permissions", mocks.configure_permissions)
    monkeypatch.setattr(system_service, "create_schedules", mocks.create_schedules)
    monkeypatch.setattr(system_service, "configure_tags", mocks.configure_tags)
    monkeypatch.setattr(system_service, "create_system_view", mocks.create_system_view)
    monkeypatch.setattr(system_service, "start_all_agents", mocks.start_all_agents)
    monkeypatch.setattr(system_service, "db", mocks.db)
    # Hermetic name resolution — no shared-SQLite reads.
    monkeypatch.setattr(
        system_service,
        "resolve_agent_names",
        lambda system_name, agents: (
            {s: f"{system_name}-{s}" for s in agents}, []
        ),
    )

    app = FastAPI()
    app.include_router(systems.router)
    app.dependency_overrides[get_current_user] = lambda: _user("admin")
    client = TestClient(app, raise_server_exceptions=False)
    return types.SimpleNamespace(client=client, m=mocks)


def _deploy(env, manifest, **body):
    return env.client.post(
        "/api/systems/deploy", json={"manifest": manifest, **body}
    )


def _fail_agent(env, short_name, exc):
    """Make create_agent_internal fail for `short_name`'s final name only."""
    async def side_effect(config, current_user, request, **kwargs):
        if config.name.endswith(f"-{short_name}"):
            raise exc
        return {"status": "created"}
    env.m.create_agent.side_effect = side_effect


# --- best-effort default ------------------------------------------------------

def test_partial_deploy_continues_and_reports(env):
    _fail_agent(env, "alpha", HTTPException(status_code=400, detail="bad template"))

    resp = _deploy(env, TWO_AGENT_MANIFEST)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "partial"
    assert data["agents_created"] == ["test-sys-beta"]
    assert len(data["failed"]) == 1
    failure = data["failed"][0]
    assert failure["name"] == "test-sys-alpha"
    assert failure["short_name"] == "alpha"
    assert failure["template"] == "local:default"
    assert failure["reason"] == "bad template"
    assert failure["status_code"] == 400
    # Both creates were attempted (no abort on first failure).
    assert env.m.create_agent.await_count == 2
    # Partial responses warn about the _N-suffix duplicate trap on redeploy.
    assert any("duplicates" in w for w in data["warnings"])


def test_absent_local_template_surfaces_per_agent(env):
    """AC#3 of #1759: the new create-time 400 must reach the operator through
    the ent#125 per-agent `failed[]` report, not abort the whole manifest.

    No new plumbing is needed — this asserts that by construction. Note
    `_failure_reason` keeps only `detail["error"]` and DROPS `detail["code"]`,
    which is why the error sentence has to stand alone and name its own
    remedy.
    """
    real_400 = HTTPException(
        status_code=400,
        detail={
            "error": (
                "Local template 'local:typo-template' not found — no "
                "template.yaml for this template. List available templates "
                "with GET /api/templates, or add it under "
                "config/agent-templates/."
            ),
            "code": "LOCAL_TEMPLATE_NOT_FOUND",
        },
    )
    _fail_agent(env, "alpha", real_400)

    resp = _deploy(env, TWO_AGENT_MANIFEST)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "partial"
    assert data["agents_created"] == ["test-sys-beta"]
    failure = data["failed"][0]
    assert failure["name"] == "test-sys-alpha"
    assert failure["status_code"] == 400
    # The sentence survives intact and is self-contained: `code` is dropped by
    # `_failure_reason`, so the operator only ever sees this string.
    assert failure["reason"].startswith("Local template 'local:typo-template' not found")
    assert "GET /api/templates" in failure["reason"]
    assert "LOCAL_TEMPLATE_NOT_FOUND" not in failure["reason"]
    # The sibling agent still deployed — one typo'd template does not sink the
    # manifest (the trinity-enterprise#124 first-run-seed failure mode).
    assert env.m.create_agent.await_count == 2


def test_partial_deploy_scopes_config_to_survivors(env):
    _fail_agent(env, "alpha", HTTPException(status_code=409, detail="conflict"))

    resp = _deploy(env, TWO_AGENT_MANIFEST)

    assert resp.status_code == 200
    survivors = {"beta": "test-sys-beta"}
    assert env.m.configure_folders.call_args.kwargs["agent_names"] == survivors
    assert env.m.create_schedules.call_args.kwargs["agent_names"] == survivors
    assert env.m.configure_tags.call_args.kwargs["agent_names"] == survivors
    env.m.start_all_agents.assert_awaited_once_with(["test-sys-beta"])


def test_all_success_unchanged(env):
    resp = _deploy(env, TWO_AGENT_MANIFEST)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "deployed"
    assert data["failed"] == []
    assert sorted(data["agents_created"]) == ["test-sys-alpha", "test-sys-beta"]


def test_dry_run_unchanged(env):
    resp = _deploy(env, TWO_AGENT_MANIFEST, dry_run=True)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "valid"
    assert data["failed"] == []
    assert len(data["agents_to_create"]) == 2
    env.m.create_agent.assert_not_awaited()


# --- total failure ------------------------------------------------------------

def test_total_failure_returns_500_with_report(env):
    env.m.create_agent.side_effect = HTTPException(status_code=502, detail="clone failed")

    resp = _deploy(env, TWO_AGENT_MANIFEST)

    assert resp.status_code == 500
    data = resp.json()
    assert data["status"] == "failed"
    assert data["agents_created"] == []
    assert len(data["failed"]) == 2
    assert all(f["status_code"] == 502 for f in data["failed"])
    # Nothing runs after a total failure: no config, no start, no prompt write.
    env.m.configure_folders.assert_not_called()
    env.m.configure_permissions.assert_not_called()
    env.m.create_schedules.assert_not_called()
    env.m.configure_tags.assert_not_called()
    env.m.create_system_view.assert_not_called()
    env.m.start_all_agents.assert_not_awaited()
    env.m.db.set_setting.assert_not_called()
    assert data["prompt_updated"] is False


def test_total_failure_never_writes_trinity_prompt(env):
    manifest = TWO_AGENT_MANIFEST + "prompt: system-wide instructions\n"
    env.m.create_agent.side_effect = Exception("docker down")

    resp = _deploy(env, manifest)

    assert resp.status_code == 500
    env.m.db.set_setting.assert_not_called()


def test_partial_success_still_writes_trinity_prompt(env):
    manifest = TWO_AGENT_MANIFEST + "prompt: system-wide instructions\n"
    _fail_agent(env, "alpha", Exception("boom"))

    resp = _deploy(env, manifest)

    assert resp.status_code == 200
    assert resp.json()["prompt_updated"] is True
    env.m.db.set_setting.assert_called_once_with(
        "trinity_prompt", "system-wide instructions"
    )


# --- strict mode --------------------------------------------------------------

def test_strict_aborts_with_original_status_code(env):
    _fail_agent(env, "alpha", HTTPException(
        status_code=429,
        detail={"error": "Agent quota exceeded.", "code": "QUOTA_EXCEEDED"},
    ))

    resp = _deploy(env, TWO_AGENT_MANIFEST, strict=True)

    # Original 429 preserved — not flattened to 500.
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["error"] == "Deployment failed"
    assert detail["failed_at"] == "test-sys-alpha"
    assert detail["created"] == []
    assert detail["reason"] == "Agent quota exceeded."
    # Aborted: the second agent was never attempted, nothing configured.
    assert env.m.create_agent.await_count == 1
    env.m.configure_folders.assert_not_called()


def test_strict_generic_exception_maps_to_500(env):
    _fail_agent(env, "alpha", RuntimeError("docker exploded"))

    resp = _deploy(env, TWO_AGENT_MANIFEST, strict=True)

    assert resp.status_code == 500
    assert resp.json()["detail"]["reason"] == "docker exploded"


# --- reason normalization + sanitization --------------------------------------

def test_dict_detail_normalized_to_error_field(env):
    _fail_agent(env, "alpha", HTTPException(
        status_code=429,
        detail={"error": "Agent quota exceeded.", "code": "QUOTA_EXCEEDED",
                "current": 5, "limit": 5},
    ))

    resp = _deploy(env, TWO_AGENT_MANIFEST)

    failure = resp.json()["failed"][0]
    assert failure["reason"] == "Agent quota exceeded."
    assert failure["status_code"] == 429


def test_reason_redacts_pat_bearing_git_urls(env):
    # learnings 2026-07-14: git prints the PAT-bearing remote URL in errors.
    _fail_agent(env, "alpha", Exception(
        "fatal: unable to access "
        "'https://x-access-token:ghp_0123456789abcdefghijklmnopqrstuvwxyz01@github.com/org/repo/': 403"
    ))

    resp = _deploy(env, TWO_AGENT_MANIFEST)

    reason = resp.json()["failed"][0]["reason"]
    assert "ghp_" not in reason
    assert "://***@github.com" in reason


def test_reason_truncated(env):
    _fail_agent(env, "alpha", Exception("x" * 5000))

    resp = _deploy(env, TWO_AGENT_MANIFEST)

    assert len(resp.json()["failed"][0]["reason"]) <= 500


# --- config-phase degradation -------------------------------------------------

def test_config_phase_failure_degrades_to_warning(env):
    env.m.configure_folders.side_effect = RuntimeError("db locked")

    resp = _deploy(env, TWO_AGENT_MANIFEST)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "deployed"
    assert any("Failed to configure shared folders" in w for w in data["warnings"])
    # Later phases still ran despite the folder failure.
    env.m.create_schedules.assert_called_once()
    env.m.configure_tags.assert_called_once()
    env.m.start_all_agents.assert_awaited_once()


def test_orchestrator_failure_warns_headless_fleet(env):
    _fail_agent(env, "orchestrator", HTTPException(status_code=502, detail="clone failed"))

    resp = _deploy(env, ORCH_MANIFEST)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "partial"
    assert data["agents_created"] == ["test-orch-worker"]
    assert any("non-functional" in w for w in data["warnings"])
    # Permissions ran over the survivor map (no orchestrator inside).
    assert env.m.configure_permissions.call_args.kwargs["agent_names"] == {
        "worker": "test-orch-worker"
    }
