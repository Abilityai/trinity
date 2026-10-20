"""#3814 — the system agent can be a pull pilot.

`trinity-system`'s MCP key is `scope == "system"` and bound to its own name
(`system_agent_service._create_system_agent` mints it for `trinity-system`, then
`_set_system_scope` flips the scope). The pull seams and the heartbeat accepted
only `scope == "agent"`, so a piloted system agent had every poll refused (eu2,
build 984f8f4c8). Push is being retired (#1989, Phase 5 #429), so the system
agent needs its own claim path; exempting it from pull is not an option.

The boundary this file pins: a system-scoped key acts for `trinity-system` only,
and only when the key row itself is bound to that name. It never acts for any
other agent, on any of the seams the key now passes:

  * GET  /api/internal/next-task
  * POST /api/internal/tasks/{id}/result
  * POST /api/agents/{name}/heartbeat
  * POST /api/agents/{name}/executions/{id}/result   (#1083, until push is deleted)

Related flow: docs/memory/feature-flows/agent-lifecycle.md
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

SYS = "trinity-system"
SYSTEM_KEY = {"scope": "system", "agent_name": SYS, "key_id": "k-sys", "user_id": "admin"}


def _key(scope, agent_name):
    return {"scope": scope, "agent_name": agent_name, "key_id": "k1", "user_id": "u"}


# ---------------------------------------------------------------------------
# The predicate every seam routes through
# ---------------------------------------------------------------------------
class TestPredicate:
    def test_system_key_acts_for_the_system_agent(self):
        from services.heartbeat_service import authorize_heartbeat

        assert authorize_heartbeat(SYSTEM_KEY, SYS) is True

    def test_system_key_never_acts_for_another_agent(self):
        from services.heartbeat_service import authorize_heartbeat

        assert authorize_heartbeat(SYSTEM_KEY, "alpha") is False

    def test_system_scope_bound_to_another_name_is_refused(self):
        """The name must come from the key row AND be the system agent: a
        system-scoped row bound to a regular agent grants nothing."""
        from services.heartbeat_service import authorize_heartbeat

        assert authorize_heartbeat(_key("system", "alpha"), "alpha") is False

    def test_system_scope_with_no_bound_name_is_refused(self):
        from services.heartbeat_service import authorize_heartbeat

        assert authorize_heartbeat(_key("system", None), SYS) is False

    @pytest.mark.parametrize("scope", ["user", "connector", "portal_delegate", None])
    def test_other_scopes_bound_to_the_system_agent_are_refused(self, scope):
        from services.heartbeat_service import authorize_heartbeat

        assert authorize_heartbeat(_key(scope, SYS), SYS) is False

    def test_agent_key_unchanged(self):
        from services.heartbeat_service import authorize_heartbeat

        assert authorize_heartbeat(_key("agent", "alpha"), "alpha") is True
        assert authorize_heartbeat(_key("agent", "beta"), "alpha") is False


# ---------------------------------------------------------------------------
# Pull seams (routers/internal.py, real router)
# ---------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "unit-secret")
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", f"{SYS},alpha")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.internal import pull_router

    app = FastAPI()
    app.include_router(pull_router)
    return TestClient(app, raise_server_exceptions=True)


_BEARER = {"Authorization": "Bearer trinity_mcp_sys"}


class TestPullSeams:
    def test_next_task_system_key_claims_its_own_queue(self, client):
        fake = {"envelope": {"id": "m1"}, "execution_id": "exec-1", "claim_token": "t"}
        with patch("routers.internal.db.validate_mcp_api_key", return_value=SYSTEM_KEY), \
             patch("services.pull_coordination_service.claim_next_task", return_value=fake):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": SYS, "worker_id": f"{SYS}#w1"},
                headers=_BEARER,
            )
        assert r.status_code == 200
        assert r.json()["execution_id"] == "exec-1"

    def test_next_task_system_key_for_another_agent_403(self, client):
        """AC b — a piloted agent's queue is never claimable with the system key."""
        with patch("routers.internal.db.validate_mcp_api_key", return_value=SYSTEM_KEY), \
             patch("services.pull_coordination_service.claim_next_task") as claim:
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "alpha", "worker_id": "alpha#w1"},
                headers=_BEARER,
            )
        assert r.status_code == 403
        claim.assert_not_called()

    def test_next_task_system_key_still_needs_the_allowlist(self, client, monkeypatch):
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "alpha")
        with patch("routers.internal.db.validate_mcp_api_key", return_value=SYSTEM_KEY):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": SYS, "worker_id": f"{SYS}#w1"},
                headers=_BEARER,
            )
        assert r.status_code == 403

    def test_result_system_key_reports_its_own_execution(self, client):
        from services.pull_coordination_service import ResultApplyOutcome

        with patch("routers.internal.db.validate_mcp_api_key", return_value=SYSTEM_KEY), \
             patch("routers.internal.db.get_execution",
                   return_value=SimpleNamespace(agent_name=SYS, status="running")), \
             patch("services.pull_coordination_service.apply_task_result",
                   return_value=ResultApplyOutcome("applied", "success")):
            r = client.post(
                "/api/internal/tasks/exec-1/result",
                json={"claim_token": "tok", "status": "success", "content": "ok"},
                headers=_BEARER,
            )
        assert r.status_code == 200 and r.json()["applied"] is True

    def test_result_system_key_on_another_agents_execution_is_uniform_404(self, client):
        with patch("routers.internal.db.validate_mcp_api_key", return_value=SYSTEM_KEY), \
             patch("routers.internal.db.get_execution",
                   return_value=SimpleNamespace(agent_name="alpha", status="running")), \
             patch("services.pull_coordination_service.apply_task_result") as apply:
            r = client.post(
                "/api/internal/tasks/exec-1/result",
                json={"claim_token": "tok", "status": "success"},
                headers=_BEARER,
            )
        assert r.status_code == 404
        apply.assert_not_called()


# ---------------------------------------------------------------------------
# Heartbeat + #1083 callback (routers/agents.py handlers, real predicate)
# ---------------------------------------------------------------------------
class _Req:
    def __init__(self):
        self.headers = {"Authorization": "Bearer trinity_mcp_sys"}


def _heartbeat(agent_name):
    from models import HeartbeatPayload
    from routers.agents import agent_heartbeat

    mock_db = MagicMock()
    mock_db.validate_mcp_api_key.return_value = SYSTEM_KEY
    with patch("routers.agents.db", mock_db), \
         patch("services.heartbeat_service.record_heartbeat", return_value=True) as rec:
        try:
            return asyncio.run(agent_heartbeat(agent_name, HeartbeatPayload(), _Req())), rec
        except Exception as exc:  # HTTPException
            return exc, rec


class TestHeartbeat:
    def test_system_key_heartbeats_the_system_agent(self):
        resp, rec = _heartbeat(SYS)
        assert resp == {"ok": True, "stored": True}
        rec.assert_called_once()

    def test_system_key_heartbeat_for_another_agent_403(self):
        """AC c."""
        exc, rec = _heartbeat("alpha")
        assert getattr(exc, "status_code", None) == 403
        rec.assert_not_called()


class TestCallback:
    def test_system_key_callback_for_another_agent_403(self):
        from models import ExecutionResultEnvelope
        from routers.agents import agent_execution_result

        mock_db = MagicMock()
        mock_db.validate_mcp_api_key.return_value = SYSTEM_KEY
        with patch("routers.agents.db", mock_db):
            with pytest.raises(Exception) as ei:
                asyncio.run(agent_execution_result(
                    "alpha", "exec-1", ExecutionResultEnvelope(status="success", response="ok"), _Req(),
                ))
        assert getattr(ei.value, "status_code", None) == 403
        mock_db.get_execution.assert_not_called()


# ---------------------------------------------------------------------------
# Env: a piloted system agent gets the worker AND the URL it needs
# ---------------------------------------------------------------------------
class TestEnv:
    def test_piloted_system_agent_gets_pull_flag_and_backend_url(self, monkeypatch):
        from services.agent_service.pull_mode import apply_pull_mode_env

        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", SYS)
        env = {}
        apply_pull_mode_env(SYS, env)
        assert env["TRINITY_PULL_MODE"] == "true"
        assert env["TRINITY_BACKEND_URL"]

    def test_unpiloted_system_agent_loses_a_leftover_backend_url(self, monkeypatch):
        """De-pilot restores #1816's unarmed state, including a pre-#1816
        container that still carries the URL."""
        from services.agent_service.pull_mode import apply_pull_mode_env

        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
        env = {"TRINITY_PULL_MODE": "true", "TRINITY_BACKEND_URL": "http://backend:8000"}
        with patch("services.heartbeat_service.clear_heartbeat") as clear:
            apply_pull_mode_env(SYS, env)
        assert "TRINITY_PULL_MODE" not in env
        assert "TRINITY_BACKEND_URL" not in env
        clear.assert_called_once_with(SYS)

    def test_regular_agent_backend_url_untouched(self, monkeypatch):
        from services.agent_service.pull_mode import apply_pull_mode_env

        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
        env = {"TRINITY_BACKEND_URL": "http://backend:8000"}
        apply_pull_mode_env("alpha", env)
        assert env == {"TRINITY_BACKEND_URL": "http://backend:8000"}


def _container(env):
    return SimpleNamespace(attrs={"Config": {"Env": [f"{k}={v}" for k, v in env.items()]}})


class TestDrift:
    def test_piloted_system_agent_without_backend_url_is_drift(self, monkeypatch):
        """A container baked pull=true but no URL runs no worker; it must be
        recreated on its next cold start, not left stuck forever."""
        from services.agent_service.helpers import check_pull_mode_env_matches

        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", SYS)
        assert check_pull_mode_env_matches(_container({"TRINITY_PULL_MODE": "true"}), SYS) is False

    def test_piloted_system_agent_with_both_matches(self, monkeypatch):
        from services.agent_service.helpers import check_pull_mode_env_matches

        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", SYS)
        c = _container({"TRINITY_PULL_MODE": "true", "TRINITY_BACKEND_URL": "http://backend:8000"})
        assert check_pull_mode_env_matches(c, SYS) is True


# ---------------------------------------------------------------------------
# Boot: WARN when piloted (AC d), platform alert while the running container
# has not picked it up (it is never recreated while running, #1816 AC2)
# ---------------------------------------------------------------------------
class TestBootReport:
    def _run(self, monkeypatch, *, allowlist, env, status="running"):
        import services.system_agent_service as sas

        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", allowlist)
        container = _container(env)
        container.status = status
        with patch("services.platform_alerts.observe", return_value="filed") as observe, \
             patch("services.platform_alerts.clear", return_value=0) as clear:
            sas.SystemAgentService()._report_pull_mode(container)
        return observe, clear

    def test_warns_when_allowlist_names_the_system_agent(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        self._run(monkeypatch, allowlist=SYS,
                  env={"TRINITY_PULL_MODE": "true", "TRINITY_BACKEND_URL": "u"})
        assert any("#3814" in r.getMessage() for r in caplog.records)

    def test_running_container_without_pull_env_raises_an_alert(self, monkeypatch):
        observe, _ = self._run(monkeypatch, allowlist=SYS, env={})
        observe.assert_called_once()
        assert observe.call_args.args[:3] == (SYS, "system_agent_pull_drift", SYS)

    def test_running_container_with_pull_env_clears_the_alert(self, monkeypatch):
        observe, clear = self._run(
            monkeypatch, allowlist=SYS,
            env={"TRINITY_PULL_MODE": "true", "TRINITY_BACKEND_URL": "u"},
        )
        observe.assert_not_called()
        clear.assert_called_once_with(SYS, "system_agent_pull_drift", SYS)

    def test_not_piloted_is_silent_and_clears(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        observe, clear = self._run(monkeypatch, allowlist="alpha", env={})
        observe.assert_not_called()
        clear.assert_called_once_with(SYS, "system_agent_pull_drift", SYS)
        assert not any("#3814" in r.getMessage() for r in caplog.records)

    def test_the_kind_is_registered_under_a_reserved_prefix(self):
        from services import platform_alerts
        from services.operator_queue_service import _RESERVED_ID_PREFIXES

        kind = platform_alerts.KINDS["system_agent_pull_drift"]
        assert kind.prefix.startswith(_RESERVED_ID_PREFIXES)
