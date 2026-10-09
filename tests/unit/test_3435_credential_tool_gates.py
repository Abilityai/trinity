"""The backend gates behind the three credential MCP tools, by principal (#3435).

`export_credentials` / `import_credentials` / `get_credential_encryption_key`
are advertised by the MCP server only to `user` and `system` keys (a per-tool
`canAccess` allow-list). That is correct only while the backend refuses every
agent-scoped key on all three routes — this file pins those EXISTING gates so
the MCP advertisement and the backend admission cannot drift apart silently:

  POST /api/agents/{name}/credentials/export|import
      get_owned_agent_by_name (owner or admin, else a uniform 404, #186)
      + reject_agent_principal (agent key -> 403 "human-only", ent#69 Part 2)
  GET /api/credentials/encryption-key
      require_admin (admin role, human, scope in {None, user, system}; #1890, #2323)
      + 503 when CREDENTIAL_ENCRYPTION_KEY is unset

A pin of current behaviour — no backend code changes with it. Principals are
real `models.User` objects with `role`, `agent_name`, `mcp_scope` declared
explicitly: a MagicMock caller carries a truthy `agent_name` and would read as
an agent key, and a stand-in lacking `mcp_scope` fails closed at the admin gate
— both a red (or green) for the wrong reason. The MCP mapper keys on the
literal "human-only" in the 403 detail, so that string is pinned exactly.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.credentials as rc
from models import User

_AGENT = "acme-bot"
_HUMAN_ONLY = "This operation is human-only; agent-scoped keys cannot perform it"


def _owner_jwt():
    return User(id=1, username="owner", role="user", agent_name=None, mcp_scope=None)


def _owner_user_key():
    return User(id=1, username="owner", role="user", agent_name=None, mcp_scope="user")


def _admin_jwt():
    return User(id=2, username="admin", role="admin", agent_name=None, mcp_scope=None)


def _admin_user_key():
    return User(id=2, username="admin", role="admin", agent_name=None, mcp_scope="user")


def _admin_system_key():
    return User(id=2, username="admin", role="admin", agent_name=None, mcp_scope="system")


def _agent_key_of_owner():
    # An agent-scoped key resolves to its OWNER carrying the owner's role.
    return User(id=1, username="owner", role="user", agent_name="bot", mcp_scope="agent")


def _agent_key_of_admin():
    return User(id=2, username="admin", role="admin", agent_name="bot", mcp_scope="agent")


def _stranger():
    return User(id=3, username="stranger", role="user", agent_name=None, mcp_scope="user")


def _ops_key_of_admin():
    return User(id=2, username="admin", role="admin", agent_name=None, mcp_scope="ops")


@pytest.fixture
def harness(monkeypatch):
    """Real router, real owner/admin dependencies; only the principal, the DB
    ownership lookups, Docker and the encryption service are stubbed.

    Every stub lands on the object the running route actually reads, never on
    a name this file bound at import: sibling unit files replace
    `sys.modules["services.credential_encryption"]` at collection time, and the
    handlers resolve that module by a function-local import at request time —
    patching an import-time alias then leaves the real service running."""
    state = {"user": _owner_jwt()}
    app = FastAPI()
    app.include_router(rc.router)
    # The exact dependency object the router's Depends() holds.
    app.dependency_overrides[rc.get_current_user] = lambda: state["user"]

    # The `db` the ownership dependency closes over.
    owner_db = rc.get_owned_agent_by_name.__globals__["db"]
    monkeypatch.setattr(owner_db, "get_agent_owner", lambda name: "owner" if name == _AGENT else None)
    monkeypatch.setattr(
        owner_db, "can_user_share_agent", lambda username, name: username in ("owner", "admin")
    )
    container = MagicMock()
    monkeypatch.setattr(rc, "get_agent_container", lambda name: container)
    monkeypatch.setattr(
        rc, "get_agent_status_from_container", lambda c: MagicMock(status="running")
    )
    monkeypatch.setattr(rc.platform_audit_service, "log", AsyncMock(return_value=None))
    monkeypatch.setattr(rc.credential_requirements_service, "invalidate_report_cache", lambda name: None)
    service = MagicMock()
    service.export_to_agent = AsyncMock(return_value=("/home/developer/.credentials.enc", 2))
    service.import_to_agent = AsyncMock(return_value={".env": "K=V", ".mcp.json": "{}"})
    # The module object `from services.credential_encryption import ...` inside
    # the handlers resolves to now (sys.modules), not an import-time alias.
    ce = importlib.import_module("services.credential_encryption")
    monkeypatch.setattr(ce, "get_credential_encryption_service", lambda: service)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)

    def as_(user):
        state["user"] = user
        return TestClient(app)

    return as_, service


ROUTES = [
    ("export", f"/api/agents/{_AGENT}/credentials/export", "export_to_agent"),
    ("import", f"/api/agents/{_AGENT}/credentials/import", "import_to_agent"),
]


class TestImportExportGate:
    @pytest.mark.parametrize("op,path,method", ROUTES)
    @pytest.mark.parametrize("principal", [_owner_jwt, _owner_user_key, _admin_jwt, _admin_user_key, _admin_system_key])
    def test_owner_or_admin_human_reaches_the_service(self, harness, op, path, method, principal):
        as_, service = harness
        r = as_(principal()).post(path)
        assert r.status_code == 200, r.text
        getattr(service, method).assert_awaited_once_with(_AGENT)

    @pytest.mark.parametrize("op,path,method", ROUTES)
    @pytest.mark.parametrize("principal", [_agent_key_of_owner, _agent_key_of_admin])
    def test_agent_key_is_refused_human_only_even_for_its_owner(self, harness, op, path, method, principal):
        as_, service = harness
        r = as_(principal()).post(path)
        assert r.status_code == 403
        assert r.json()["detail"] == _HUMAN_ONLY
        getattr(service, method).assert_not_awaited()

    @pytest.mark.parametrize("op,path,method", ROUTES)
    def test_non_owner_gets_the_uniform_404(self, harness, op, path, method):
        as_, service = harness
        r = as_(_stranger()).post(path)
        assert r.status_code == 404
        assert r.json()["detail"] == "Agent not found"
        getattr(service, method).assert_not_awaited()


class TestEncryptionKeyGate:
    PATH = "/api/credentials/encryption-key"

    @pytest.mark.parametrize("principal", [_admin_jwt, _admin_user_key, _admin_system_key])
    def test_admin_human_gets_the_key(self, harness, principal):
        as_, _ = harness
        r = as_(principal()).get(self.PATH)
        assert r.status_code == 200, r.text
        assert r.json()["key"] == "ab" * 32
        assert r.json()["algorithm"] == "AES-256-GCM"

    @pytest.mark.parametrize("principal", [_owner_jwt, _owner_user_key])
    def test_non_admin_human_is_refused(self, harness, principal):
        as_, _ = harness
        r = as_(principal()).get(self.PATH)
        assert r.status_code == 403
        assert r.json()["detail"] == "Admin access required"

    @pytest.mark.parametrize("principal", [_agent_key_of_owner, _agent_key_of_admin])
    def test_agent_key_is_refused_human_only_even_for_an_admin_owner(self, harness, principal):
        as_, _ = harness
        r = as_(principal()).get(self.PATH)
        assert r.status_code == 403
        assert r.json()["detail"] == _HUMAN_ONLY
        assert "key" not in r.json()

    def test_a_scope_outside_the_admin_allowlist_is_refused(self, harness):
        as_, _ = harness
        r = as_(_ops_key_of_admin()).get(self.PATH)
        assert r.status_code == 403
        assert "key" not in r.json()

    def test_unset_key_is_503(self, harness, monkeypatch):
        as_, _ = harness
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        r = as_(_admin_jwt()).get(self.PATH)
        assert r.status_code == 503
        assert r.json()["detail"] == "Credential encryption key not configured"
