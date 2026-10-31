"""The backend gates behind the three credential MCP tools, by principal (#3435).

`export_credentials` / `import_credentials` / `get_credential_encryption_key`
are advertised by the MCP server to keys it does not give an agent session (a
per-tool `canAccess` allow-list). This file pins the backend admission behind
each route, by principal, so the MCP advertisement and the backend cannot drift
apart silently:

  POST /api/agents/{name}/credentials/export
      require_person (a session or the person's own `user` key; every other scope,
      `system` included, gets 403 `HUMAN_ONLY_DETAIL`; trinity-enterprise#719)
      + get_owned_agent_by_name (owner or admin, else a uniform 404, #186)
  POST /api/agents/{name}/credentials/import
      require_interactive (a signed-in session only: a restore installs whatever
      the archive holds, so it is a bind; every key gets 403; trinity-enterprise#719)
      + get_owned_agent_by_name
  GET /api/credentials/encryption-key
      require_admin (admin role, human, scope in {None, user, system}; #1890, #2323)
      + 503 when CREDENTIAL_ENCRYPTION_KEY is unset

Principals are real `models.User` objects with `role`, `agent_name`, `mcp_scope`
declared explicitly: a MagicMock caller carries a truthy `agent_name` and would
read as an agent key, and a stand-in lacking `mcp_scope` fails closed at the
admin gate — both a red (or green) for the wrong reason. Each refusal's detail is
pinned exactly, because the MCP mapper reads it.
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
# trinity-enterprise#719: the export and import gates' refusals.
_HUMAN_ONLY_DETAIL = rc.get_current_user.__globals__["HUMAN_ONLY_DETAIL"]
_INTERACTIVE_ONLY = (
    "This operation requires an interactive session; MCP API keys cannot perform it"
)


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
    # A signed-in session, so both gates admit it and the owner fence answers.
    return User(id=3, username="stranger", role="user", agent_name=None, mcp_scope=None)


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
EXPORT, IMPORT = ROUTES

# Who reaches the service: export is PERSON (a session or the person's own `user`
# key), import is INTERACTIVE (a session only). Every other principal is refused
# with the route's own detail.
_ADMITTED = [
    (EXPORT, _owner_jwt), (EXPORT, _owner_user_key), (EXPORT, _admin_jwt), (EXPORT, _admin_user_key),
    (IMPORT, _owner_jwt), (IMPORT, _admin_jwt),
]
_REFUSAL = {"export": _HUMAN_ONLY_DETAIL, "import": _INTERACTIVE_ONLY}


class TestImportExportGate:
    @pytest.mark.parametrize("route,principal", _ADMITTED)
    def test_owner_or_admin_person_reaches_the_service(self, harness, route, principal):
        op, path, method = route
        as_, service = harness
        r = as_(principal()).post(path)
        assert r.status_code == 200, r.text
        getattr(service, method).assert_awaited_once_with(_AGENT)

    @pytest.mark.parametrize("op,path,method", ROUTES)
    def test_the_system_key_is_refused(self, harness, op, path, method):
        """trinity-enterprise#719: was admitted on both; a machine key that holds
        no grant power neither backs up nor restores an agent's credentials."""
        as_, service = harness
        r = as_(_admin_system_key()).post(path)
        assert r.status_code == 403
        assert r.json()["detail"] == _REFUSAL[op]
        getattr(service, method).assert_not_awaited()

    @pytest.mark.parametrize("principal", [_owner_user_key, _admin_user_key])
    def test_import_refuses_the_persons_user_key(self, harness, principal):
        op, path, method = IMPORT
        as_, service = harness
        r = as_(principal()).post(path)
        assert r.status_code == 403
        assert r.json()["detail"] == _INTERACTIVE_ONLY
        service.import_to_agent.assert_not_awaited()

    @pytest.mark.parametrize("op,path,method", ROUTES)
    @pytest.mark.parametrize("principal", [_agent_key_of_owner, _agent_key_of_admin])
    def test_agent_key_is_refused_human_only_even_for_its_owner(self, harness, op, path, method, principal):
        as_, service = harness
        r = as_(principal()).post(path)
        assert r.status_code == 403
        assert r.json()["detail"] == _REFUSAL[op]
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
