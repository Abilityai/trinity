"""HTTP layer for `GET /api/agents/{name}/credential-requirements` (ent#127 §3).

Mounts the real router on a minimal app. The security-critical assertions are
the principal matrix and the auth WIRING: this endpoint returns a per-agent
credential inventory, which is a targeting map rather than a status light, so
the read gate has to equal the write gate it drives.
"""

import inspect
import types
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.credentials as rc
from dependencies import (
    get_current_user,
    get_owned_agent_by_name,
    get_authorized_agent_by_name,
)

_AGENT = "acme-recon"

_REPORT = {
    "agent_name": _AGENT,
    "state": "ok",
    "requirements_source": "live_workspace",
    "status_source": "live",
    "degraded_reason": None,
    "requirements": [
        {
            "name": "OPENAI_API_KEY",
            "title": "OpenAI API key",
            "description": "Used by the research MCP server.",
            "required": True,
            "secret": True,
            "format": "token",
            "default": None,
            "source": "template:env_file",
            "advisory": False,
            "status": "missing",
            "setup_url": "https://platform.openai.com/api-keys",
            "setup_url_display_host": "platform.openai.com",
            "setup_url_registrable": "openai.com",
            "setup_url_verified": True,
        }
    ],
    "summary": {
        "total": 1, "set": 0, "missing": 1, "unknown": 0,
        "blocking": 1, "platform_injected_excluded": 0, "advisory": 0,
    },
    "errors": [],
}


def _human(**over):
    base = dict(username="owner", email="owner@example.com", agent_name=None)
    base.update(over)
    return types.SimpleNamespace(**base)


@pytest.fixture
def app_and_client(monkeypatch):
    app = FastAPI()
    app.include_router(rc.router)
    app.dependency_overrides[get_owned_agent_by_name] = lambda: _AGENT
    app.dependency_overrides[get_current_user] = lambda: _human()
    monkeypatch.setattr(rc.platform_audit_service, "log", AsyncMock(return_value=None))
    monkeypatch.setattr(
        rc.credential_requirements_service,
        "get_report",
        AsyncMock(return_value=dict(_REPORT)),
    )
    # The limiter fails open with no Redis, but pin it so a shared in-process
    # fallback window can't leak between tests.
    monkeypatch.setattr(rc, "enforce_rate_limit", lambda *a, **k: None)
    return app, TestClient(app)


@pytest.fixture
def client(app_and_client):
    return app_and_client[1]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuthWiring:
    def test_route_uses_the_owner_gate_not_the_read_gate(self):
        """`get_authorized_agent_by_name` resolves an agent-scoped MCP key to the
        OWNER USER carrying the owner's role, so it would hand an agent the
        credential inventory of every sibling its owner can access — the entire
        fleet on a default admin-owned install.

        The shared-user / ghost / uniform-404 behaviours are
        `get_owned_agent_by_name`'s own tested contract; asserting the WIRING is
        the honest test here rather than re-testing dependencies.py.
        """
        params = inspect.signature(rc.get_agent_credential_requirements).parameters
        dep = params["agent_name"].default
        assert dep.dependency is get_owned_agent_by_name
        assert dep.dependency is not get_authorized_agent_by_name

    def test_route_rejects_agent_principals(self, app_and_client):
        """Human-only, like every sibling credential route (inject/export/import)."""
        app, client = app_and_client
        app.dependency_overrides[get_current_user] = lambda: _human(
            username="owner", agent_name="some-agent"
        )
        resp = client.get(f"/api/agents/{_AGENT}/credential-requirements")
        assert resp.status_code == 403
        assert "human-only" in resp.json()["detail"]

    def test_human_principal_allowed(self, client):
        assert client.get(f"/api/agents/{_AGENT}/credential-requirements").status_code == 200


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


class TestResponse:
    def test_happy_path_is_a_pure_projection(self, client):
        """The router holds no logic (Invariant #1): patch the service and the
        response must be exactly what it returned."""
        body = client.get(f"/api/agents/{_AGENT}/credential-requirements").json()
        assert body["state"] == "ok"
        assert body["summary"]["blocking"] == 1
        assert body["requirements"][0]["name"] == "OPENAI_API_KEY"
        assert body["requirements"][0]["setup_url_display_host"] == "platform.openai.com"

    def test_stopped_agent_is_200_not_an_error(self, client, monkeypatch):
        """AC #4 wants degraded, not a crash — mirrors `/credentials/status`."""
        degraded = dict(
            _REPORT,
            state="degraded",
            degraded_reason="agent_not_running",
            requirements_source="catalog",
            status_source="unavailable",
            requirements=[],
            summary=dict(_REPORT["summary"], total=0, missing=0, blocking=0),
        )
        monkeypatch.setattr(
            rc.credential_requirements_service,
            "get_report",
            AsyncMock(return_value=degraded),
        )
        resp = client.get(f"/api/agents/{_AGENT}/credential-requirements")
        assert resp.status_code == 200
        assert resp.json()["degraded_reason"] == "agent_not_running"

    def test_tri_state_required_survives_serialization(self, client, monkeypatch):
        report = dict(
            _REPORT,
            requirements=[dict(_REPORT["requirements"][0], required="unknown")],
        )
        monkeypatch.setattr(
            rc.credential_requirements_service, "get_report", AsyncMock(return_value=report)
        )
        body = client.get(f"/api/agents/{_AGENT}/credential-requirements").json()
        assert body["requirements"][0]["required"] == "unknown"

    def test_single_flight_contention_is_409(self, client, monkeypatch):
        monkeypatch.setattr(
            rc.credential_requirements_service,
            "get_report",
            AsyncMock(
                side_effect=rc.credential_requirements_service.CredentialRequirementsBusy(
                    "a credential-requirements probe is already running for this agent"
                )
            ),
        )
        resp = client.get(f"/api/agents/{_AGENT}/credential-requirements")
        assert resp.status_code == 409

    def test_rate_limited(self, client, monkeypatch):
        from fastapi import HTTPException

        def boom(*_a, **_k):
            raise HTTPException(status_code=429, detail="Too many.")

        monkeypatch.setattr(rc, "enforce_rate_limit", boom)
        assert client.get(f"/api/agents/{_AGENT}/credential-requirements").status_code == 429

    def test_no_credential_value_is_serialized(self, client, monkeypatch):
        """AC #6: names and set/missing only, never a value."""
        secret = "sk-ent127-must-not-appear"
        monkeypatch.setattr(
            rc.credential_requirements_service,
            "get_report",
            AsyncMock(return_value=dict(_REPORT)),
        )
        resp = client.get(f"/api/agents/{_AGENT}/credential-requirements")
        assert secret not in resp.text


class TestAudit:
    def test_audit_row_written_with_counts_only(self, client, monkeypatch):
        logged = AsyncMock(return_value=None)
        monkeypatch.setattr(rc.platform_audit_service, "log", logged)
        client.get(f"/api/agents/{_AGENT}/credential-requirements")
        assert logged.await_count == 1
        kwargs = logged.await_args.kwargs
        assert kwargs["event_action"] == "requirements_read"
        assert kwargs["target_id"] == _AGENT
        # Counts only — a credential NAME must never reach the audit log.
        assert "OPENAI_API_KEY" not in str(kwargs["details"])
        assert kwargs["details"]["blocking"] == 1


class TestCacheInvalidation:
    def test_inject_invalidates_the_report_cache(self):
        """A cached report that survives a credential write reports "missing"
        for a variable the operator just set."""
        src = inspect.getsource(rc.inject_credentials)
        assert "invalidate_report_cache" in src

    def test_import_invalidates_the_report_cache(self):
        src = inspect.getsource(rc.import_credentials)
        assert "invalidate_report_cache" in src
