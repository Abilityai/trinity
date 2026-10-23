"""
Nevermined card delegation routes (trinity-enterprise#966).

Drives the real router through FastAPI with only the principal substituted:
the real `require_admin` gate, the real service and the real (tmp SQLite) db
with real AES-256-GCM encryption. Nevermined itself is never dialled.
"""
import logging
import secrets as _secrets
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.unit

WIDGET_SECRET = "wsec-" + "Q" * 24
AGENT_KEY = "sandbox:akey-" + "Z" * 24
SECRETS = (WIDGET_SECRET, AGENT_KEY, "Q" * 24, "Z" * 24)


def _user(**kw):
    from models import User

    base = dict(id=1, username="admin", email="admin@example.com", role="admin")
    base.update(kw)
    return User(**base)


ADMIN = dict()
OPERATOR = dict(id=2, username="ada", email="ada@example.com", role="creator")
AGENT_KEY_PRINCIPAL = dict(agent_name="alpha", mcp_scope="agent")


@pytest.fixture
def env(monkeypatch):
    from database import db
    from db.engine import get_engine
    import inspect

    from routers import nevermined_delegation as router_mod
    from services.credential_encryption import CredentialEncryptionService

    monkeypatch.setattr(
        db._nevermined_delegation_ops, "_encryption_service",
        CredentialEncryptionService(key=_secrets.token_hex(32)),
    )
    audit = AsyncMock()
    monkeypatch.setattr(router_mod.platform_audit_service, "log", audit)
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM nevermined_organization"))
        conn.execute(text("DELETE FROM agent_delegations"))
        conn.execute(text("DELETE FROM agent_ownership WHERE agent_name LIKE 'ent966-%'"))

    app = FastAPI()
    app.include_router(router_mod.router)
    principal = {"user": _user()}
    # Override the principal the router's OWN gate depends on: another test's
    # module eviction can leave `dependencies` re-imported under us.
    get_current_user = inspect.signature(router_mod.require_admin).parameters[
        "current_user"].default.dependency
    app.dependency_overrides[get_current_user] = lambda: principal["user"]

    class Env:
        client = TestClient(app)

        @staticmethod
        def as_(**kw):
            principal["user"] = _user(**kw)

        @staticmethod
        def active_binding(agent):
            with get_engine().begin() as conn:
                conn.execute(text(
                    "INSERT INTO agent_ownership (agent_name, owner_id, created_at)"
                    " VALUES (:a, 1, 'now')"), {"a": agent})
                conn.execute(text(
                    "INSERT INTO agent_delegations (id, agent_name, delegation_id, environment,"
                    " created_by, created_at) VALUES (:i, :a, 'dlg-12345678', 'sandbox', 'ada', 'now')"),
                    {"i": "row-" + agent, "a": agent})

        @staticmethod
        def drop_ownership(agent):
            with get_engine().begin() as conn:
                conn.execute(text("DELETE FROM agent_ownership WHERE agent_name = :a"), {"a": agent})

    Env.audit = audit
    yield Env
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM nevermined_organization"))
        conn.execute(text("DELETE FROM agent_delegations"))
        conn.execute(text("DELETE FROM agent_ownership WHERE agent_name LIKE 'ent966-%'"))


def _put(env, **body):
    payload = {"org_id": "org-1", "environment": "sandbox",
               "widget_secret": WIDGET_SECRET, "agent_api_key": AGENT_KEY}
    payload.update(body)
    return env.client.put("/api/nevermined/organization", json=payload)


def _no_secret(resp):
    for s in SECRETS:
        assert s not in resp.text


class TestOrganization:
    def test_not_configured_is_a_200_state(self, env):
        r = env.client.get("/api/nevermined/organization")
        assert r.status_code == 200
        assert r.json() == {"configured": False, "org_id": None, "environment": None,
                            "updated_at": None, "updated_by": None, "active_delegations": 0}

    def test_create_then_read_never_returns_a_secret(self, env):
        r = _put(env)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["configured"] is True and body["org_id"] == "org-1"
        assert body["environment"] == "sandbox" and body["updated_by"] == "admin@example.com"
        _no_secret(r)
        g = env.client.get("/api/nevermined/organization")
        assert g.json()["configured"] is True
        _no_secret(g)

    def test_secrets_are_stored_encrypted(self, env):
        from database import db
        from db.engine import get_engine

        _put(env)
        with get_engine().connect() as conn:
            raw = conn.execute(text("SELECT encrypted_credentials FROM nevermined_organization")).scalar()
        assert WIDGET_SECRET not in raw and AGENT_KEY not in raw
        assert db.get_nevermined_organization_secrets() == {
            "widget_secret": WIDGET_SECRET, "agent_api_key": AGENT_KEY}

    def test_first_create_requires_both_secrets(self, env):
        r = _put(env, agent_api_key=None)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "secret_required"
        assert r.json()["detail"]["missing"] == ["agent_api_key"]

    def test_update_without_secrets_keeps_them(self, env):
        from database import db

        _put(env)
        r = _put(env, org_id="org-2", widget_secret=None, agent_api_key=None)
        assert r.status_code == 200 and r.json()["org_id"] == "org-2"
        assert db.get_nevermined_organization_secrets()["agent_api_key"] == AGENT_KEY
        details = env.audit.await_args.kwargs["details"]
        assert details["rotated"] == [] and details["created"] is False

    def test_audit_names_rotated_secret_never_its_value(self, env):
        _put(env)
        _put(env, widget_secret="wsec-rotated-" + "R" * 20, agent_api_key=None)
        call = env.audit.await_args.kwargs
        assert call["event_action"] == "nevermined_organization_set"
        assert call["details"]["rotated"] == ["widget_secret"]
        assert "R" * 20 not in repr(call)

    def test_prefixed_key_must_match_environment(self, env):
        r = _put(env, environment="live")
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "environment_mismatch"
        _no_secret(r)

    def test_environment_change_refused_while_a_mandate_is_active(self, env):
        _put(env)
        env.active_binding("ent966-a")
        r = _put(env, environment="live", agent_api_key=None)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "delegations_active"
        assert r.json()["detail"]["active_delegations"] == 1

    def test_key_rotation_refused_while_a_mandate_is_active(self, env):
        _put(env)
        env.active_binding("ent966-a")
        r = _put(env, agent_api_key="sandbox:akey-new-" + "N" * 20)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "delegations_active"
        # widget secret rotation does not touch the agents' keys — allowed
        assert _put(env, widget_secret="wsec-other-" + "W" * 20, agent_api_key=None).status_code == 200

    def test_delete_refused_while_a_mandate_is_active(self, env):
        _put(env)
        env.active_binding("ent966-a")
        r = env.client.delete("/api/nevermined/organization")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "delegations_active"
        assert env.client.get("/api/nevermined/organization").json()["configured"] is True

    def test_deleted_agent_row_does_not_block(self, env):
        _put(env)
        env.active_binding("ent966-a")
        env.drop_ownership("ent966-a")  # the agent is gone; its KEEP row stays
        assert env.client.get("/api/nevermined/organization").json()["active_delegations"] == 0
        r = env.client.delete("/api/nevermined/organization")
        assert r.status_code == 200 and r.json()["configured"] is False
        assert env.audit.await_args.kwargs["event_action"] == "nevermined_organization_delete"

    @pytest.mark.parametrize("method", ["get", "put", "delete"])
    def test_non_admin_is_refused(self, env, method):
        env.as_(**OPERATOR)
        kwargs = {"json": {"org_id": "o", "environment": "sandbox"}} if method == "put" else {}
        r = getattr(env.client, method)("/api/nevermined/organization", **kwargs)
        assert r.status_code == 403

    @pytest.mark.parametrize("method", ["get", "put", "delete"])
    def test_agent_key_is_refused_even_when_owner_is_admin(self, env, method):
        env.as_(**AGENT_KEY_PRINCIPAL)
        kwargs = {"json": {"org_id": "o", "environment": "sandbox"}} if method == "put" else {}
        r = getattr(env.client, method)("/api/nevermined/organization", **kwargs)
        assert r.status_code == 403

    def test_secrets_never_logged(self, env, caplog):
        caplog.set_level(logging.DEBUG)
        _put(env)
        _put(env, widget_secret="wsec-rotated-" + "R" * 20, agent_api_key=None)
        for s in SECRETS + ("R" * 20,):
            assert s not in caplog.text

    def test_newline_in_secret_is_rejected(self, env):
        r = _put(env, widget_secret="abc\nNVM_API_KEY=x")
        assert r.status_code == 422

    def test_unicode_line_separator_in_secret_is_rejected(self, env):
        """`str.splitlines()` — the agent's `.env` splitter — breaks on U+2028."""
        r = _put(env, agent_api_key="sandbox:akey\u2028NVM_ENVIRONMENT=live")
        assert r.status_code == 422
        assert "newline" in r.text


class TestProductionWiring:
    def test_main_registers_the_router(self):
        """`main.py` includes the router (AST — importing main is too heavy here)."""
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "src" / "backend" / "main.py").read_text()
        tree = ast.parse(src)
        imported = any(
            isinstance(n, ast.ImportFrom) and n.module == "routers.nevermined_delegation"
            for n in ast.walk(tree)
        )
        included = any(
            isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "include_router"
            and n.args and getattr(n.args[0], "id", "") == "nevermined_delegation_router"
            for n in ast.walk(tree)
        )
        assert imported and included


# ---------------------------------------------------------------------------
# Checkpoint B — widget session
# ---------------------------------------------------------------------------

SESSION_TOKEN = "eyJ-session-" + "T" * 32
SESSION_URL = "/api/nevermined/agents/ent966-a/widget-session"


@pytest.fixture
def session_env(env, monkeypatch):
    """`env` + an owned agent, an organization, and a mock Nevermined."""
    import httpx
    from database import db
    from routers import nevermined_delegation as router_mod
    from services import nevermined_delegation_service as svc

    owners = {"ent966-a": {"admin", "ada"}}
    monkeypatch.setattr(db, "get_agent_owner", lambda name: {"owner_username": "ada"} if name in owners else None)
    monkeypatch.setattr(db, "can_user_share_agent", lambda user, name: user in owners.get(name, set()))
    limits = []
    monkeypatch.setattr(router_mod, "enforce_rate_limit", lambda *a, **k: limits.append(a))

    upstream = {"status": 200, "body": {"token": SESSION_TOKEN, "expiresAt": "2026-10-22T23:00:00Z"},
                "raise": None, "requests": []}

    def handler(request):
        upstream["requests"].append(request)
        if upstream["raise"]:
            raise upstream["raise"]
        if isinstance(upstream["body"], (dict, list)):
            return httpx.Response(upstream["status"], json=upstream["body"])
        return httpx.Response(upstream["status"], text=upstream["body"])

    monkeypatch.setattr(svc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    _put(env)
    env.upstream = upstream
    env.limits = limits
    env.as_(**OPERATOR)
    return env


class TestWidgetSession:
    def test_mints_for_the_signed_in_person(self, session_env):
        import json

        r = session_env.client.post(SESSION_URL)
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "no-store"
        assert r.json() == {"session": {"token": SESSION_TOKEN, "expiresAt": "2026-10-22T23:00:00Z"},
                            "environment": "sandbox", "org_id": "org-1"}
        (req,) = session_env.upstream["requests"]
        assert str(req.url) == "https://api.sandbox.nevermined.app/api/v1/widgets/session"
        assert json.loads(req.content) == {"orgId": "org-1", "email": "ada@example.com",
                                           "rawSecret": WIDGET_SECRET}
        assert session_env.limits == [("nvm-session:ada:ent966-a", 10, 60)]
        for s in (WIDGET_SECRET, AGENT_KEY):
            assert s not in r.text

    def test_live_environment_dials_live(self, session_env):
        session_env.as_()
        _put(session_env, environment="live", agent_api_key="live:akey-" + "L" * 20)
        session_env.as_(**OPERATOR)
        assert session_env.client.post(SESSION_URL).status_code == 200
        assert session_env.upstream["requests"][-1].url.host == "api.live.nevermined.app"

    def test_not_configured(self, session_env):
        session_env.as_()
        session_env.client.delete("/api/nevermined/organization")
        session_env.as_(**OPERATOR)
        r = session_env.client.post(SESSION_URL)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "nevermined_not_configured"
        assert session_env.upstream["requests"] == []

    def test_no_email(self, session_env):
        session_env.as_(**{**OPERATOR, "email": None})
        r = session_env.client.post(SESSION_URL)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "email_required"
        assert session_env.upstream["requests"] == []

    def test_not_owner_is_uniform_404(self, session_env):
        session_env.as_(id=3, username="eve", email="eve@example.com", role="creator")
        a = session_env.client.post(SESSION_URL)
        b = session_env.client.post("/api/nevermined/agents/ent966-missing/widget-session")
        assert a.status_code == b.status_code == 404 and a.json() == b.json()
        assert session_env.upstream["requests"] == []

    def test_agent_key_is_refused(self, session_env):
        session_env.as_(**{**OPERATOR, "agent_name": "ent966-a", "mcp_scope": "agent"})
        r = session_env.client.post(SESSION_URL)
        assert r.status_code == 403
        assert session_env.upstream["requests"] == []

    def test_unreachable(self, session_env):
        import httpx

        session_env.upstream["raise"] = httpx.ConnectTimeout("timed out")
        r = session_env.client.post(SESSION_URL)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "nevermined_unreachable"
        assert r.headers["cache-control"] == "no-store"

    def test_busy(self, session_env, monkeypatch):
        import contextlib
        from services import nevermined_delegation_service as svc

        @contextlib.asynccontextmanager
        async def full():
            raise svc.DelegationBusy("full")
            yield  # pragma: no cover

        monkeypatch.setattr(svc, "delegation_slot", full)
        r = session_env.client.post(SESSION_URL)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "nevermined_busy"
        assert r.headers["retry-after"] == "5"
        assert session_env.upstream["requests"] == []

    @pytest.mark.parametrize("status", [401, 403])
    def test_rejected_credentials_never_echo_upstream(self, session_env, status):
        session_env.upstream.update(status=status, body={"error": "bad secret " + WIDGET_SECRET})
        r = session_env.client.post(SESSION_URL)
        assert r.status_code == 502
        detail = r.json()["detail"]
        assert detail["code"] == "nevermined_rejected" and detail["upstream_status"] == status
        assert "widget secret" in detail["message"] and "bad secret" not in r.text
        assert WIDGET_SECRET not in r.text

    def test_upstream_5xx_and_unreadable_body(self, session_env):
        session_env.upstream.update(status=500, body="boom internal")
        r = session_env.client.post(SESSION_URL)
        assert r.status_code == 502 and r.json()["detail"]["upstream_status"] == 500
        assert "boom" not in r.text
        session_env.upstream.update(status=200, body="<html>not json</html>")
        r = session_env.client.post(SESSION_URL)
        assert r.status_code == 502 and r.json()["detail"]["code"] == "nevermined_rejected"

    def test_rate_limit_fires_before_upstream(self, session_env, monkeypatch):
        from fastapi import HTTPException
        from routers import nevermined_delegation as router_mod

        def deny(*a, **k):
            raise HTTPException(status_code=429, detail="slow down", headers={"Retry-After": "9"})

        monkeypatch.setattr(router_mod, "enforce_rate_limit", deny)
        r = session_env.client.post(SESSION_URL)
        assert r.status_code == 429
        assert session_env.upstream["requests"] == []

    def test_secret_and_token_never_logged(self, session_env, caplog):
        import httpx

        caplog.set_level(logging.DEBUG)
        session_env.client.post(SESSION_URL)
        session_env.upstream.update(status=401, body={"error": "nope " + WIDGET_SECRET})
        session_env.client.post(SESSION_URL)
        session_env.upstream["raise"] = httpx.ConnectError("conn " + WIDGET_SECRET)
        session_env.client.post(SESSION_URL)
        assert "upstream_status=200" in caplog.text  # the success line was captured
        for s in (WIDGET_SECRET, AGENT_KEY, SESSION_TOKEN, "Q" * 24, "Z" * 24, "T" * 32):
            assert s not in caplog.text


class TestDelegationGate:
    @pytest.mark.asyncio
    async def test_own_gate_never_takes_a_facilitator_slot(self, monkeypatch):
        """Saturating the delegation gate leaves the paid-chat facilitator gate free."""
        from services import nevermined_delegation_service as svc
        from services import nevermined_payment_service as pay

        monkeypatch.setattr(svc, "DELEGATION_SLOT_WAIT_SECONDS", 0.01)
        held = []
        for _ in range(svc.NEVERMINED_DELEGATION_MAX_INFLIGHT):
            cm = svc.delegation_slot()
            await cm.__aenter__()
            held.append(cm)
        with pytest.raises(svc.DelegationBusy):
            async with svc.delegation_slot():
                pass
        async with pay.facilitator_slot():
            pass  # still free
        for cm in held:
            await cm.__aexit__(None, None, None)
        async with svc.delegation_slot():
            pass


class TestWidgetSessionCensus:
    def test_route_is_in_the_person_class(self):
        """#2996: the census walker reads the session route as `person`, the
        organization routes as `admin_tier` — no listed exception needed."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _route_census as census

        routes = census.walk().routes
        prefix = "routers/nevermined_delegation.py::"
        assert routes[prefix + "create_widget_session"].code_class == "person"
        for name in ("get_nevermined_organization", "put_nevermined_organization",
                     "delete_nevermined_organization"):
            assert routes[prefix + name].code_class == "admin_tier"


# ---------------------------------------------------------------------------
# Checkpoint C — delegation binding
# ---------------------------------------------------------------------------

DLG_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
DLG_URL = "/api/nevermined/agents/ent966-a/delegation"
LIVE_BODY = {
    "id": DLG_ID, "status": "ACTIVE", "spendingLimitCents": 5000, "amountSpentCents": 800,
    "remainingBudgetCents": 4200, "transactionCount": 3, "maxTransactions": 50,
    "currency": "usd", "expiresAt": "2026-11-14T20:00:00Z",
    "transactions": [{"id": "t1", "amountCents": 800}],
}
EVE = dict(id=3, username="eve", email="eve@example.com", role="creator")
AGENT_ENV_SECRET = "agent-env-" + "S" * 24


@pytest.fixture
def dlg_env(env, monkeypatch):
    """`env` + two owned agents (`ent966-a`, `ent966-b` — both ada's), the
    organization, a mock Nevermined and a mock agent container."""
    import httpx
    from database import db
    from db.engine import get_engine
    from routers import nevermined_delegation as router_mod
    from services import nevermined_delegation_service as svc

    owners = {"ent966-a": {"admin", "ada"}, "ent966-b": {"admin", "ada"}}
    monkeypatch.setattr(db, "get_agent_owner", lambda name: {"owner_username": "ada"} if name in owners else None)
    monkeypatch.setattr(db, "can_user_access_agent", lambda user, name: user in owners.get(name, set()))
    monkeypatch.setattr(db, "can_user_share_agent", lambda user, name: user in owners.get(name, set()))
    with get_engine().begin() as conn:
        for a in owners:
            conn.execute(text("INSERT INTO agent_ownership (agent_name, owner_id, created_at) VALUES (:a, 2, 'now')"), {"a": a})
    limits = []
    monkeypatch.setattr(router_mod, "enforce_rate_limit", lambda *a, **k: limits.append(a))

    upstream = {"status": 200, "body": LIVE_BODY, "raise": None, "requests": [],
                "delete_status": 200}

    def handler(request):
        upstream["requests"].append(request)
        if upstream["raise"]:
            raise upstream["raise"]
        if request.method == "DELETE":
            return httpx.Response(upstream["delete_status"], json={})
        if isinstance(upstream["body"], (dict, list)):
            return httpx.Response(upstream["status"], json=upstream["body"])
        return httpx.Response(upstream["status"], text=upstream["body"])

    monkeypatch.setattr(svc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    agent = {"env": "OTHER_KEY=keep-me\nSOME_SECRET=" + AGENT_ENV_SECRET + "\n", "running": True,
             "reads": 0, "injects": [], "inject_status": 200, "raise": None, "read_status": 200}

    def agent_handler(request):
        if agent["raise"]:
            raise agent["raise"]
        if request.url.path == "/api/credentials/read":
            agent["reads"] += 1
            return httpx.Response(agent["read_status"], json={"files": {".env": agent["env"]}})
        if request.url.path == "/api/credentials/inject":
            import json as _json
            files = _json.loads(request.content)["files"]
            agent["injects"].append(files)
            if agent["inject_status"] == 200:
                agent["env"] = files[".env"]
            return httpx.Response(agent["inject_status"], json={"status": "ok"})
        return httpx.Response(404)  # pragma: no cover

    monkeypatch.setattr(svc, "_agent_client", lambda name: httpx.AsyncClient(transport=httpx.MockTransport(agent_handler)))
    monkeypatch.setattr(svc, "_agent_running", lambda name: agent["running"])
    invalidated = []
    monkeypatch.setattr(router_mod.credential_requirements_service, "invalidate_report_cache", invalidated.append)
    _put(env)
    env.upstream = upstream
    env.limits = limits
    env.agent = agent
    env.invalidated = invalidated
    env.as_(**OPERATOR)
    return env


def _bind_row(env, agent="ent966-a", delegation_id=DLG_ID, **cols):
    from db.engine import get_engine

    row = dict(id="row-" + agent + "-" + delegation_id[:8], agent_name=agent, delegation_id=delegation_id,
               payment_method_id="pm_0123456789", provider="stripe", environment="sandbox",
               api_key_injected=1, created_by="ada@example.com", created_at="2026-10-22T20:00:00Z",
               revoked_at=None, revoked_by=None)
    row.update(cols)
    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO agent_delegations (id, agent_name, delegation_id, payment_method_id, provider,"
            " environment, api_key_injected, created_by, created_at, revoked_at, revoked_by)"
            " VALUES (:id, :agent_name, :delegation_id, :payment_method_id, :provider, :environment,"
            " :api_key_injected, :created_by, :created_at, :revoked_at, :revoked_by)"), row)
    return row


UNBOUND = {"agent_name": "ent966-a", "configured": True, "funded": False,
           "delegation": None, "live": None, "history": []}


class TestReadDelegation:
    def test_not_configured_is_a_200_state(self, dlg_env):
        dlg_env.as_()
        dlg_env.client.delete("/api/nevermined/organization")
        dlg_env.as_(**OPERATOR)
        r = dlg_env.client.get(DLG_URL)
        assert r.status_code == 200
        assert r.json() == {**UNBOUND, "configured": False}
        assert dlg_env.upstream["requests"] == []

    def test_unbound(self, dlg_env):
        r = dlg_env.client.get(DLG_URL)
        assert r.status_code == 200 and r.json() == UNBOUND
        assert dlg_env.upstream["requests"] == []
        assert dlg_env.limits == [("nvm-read:ada:ent966-a", 30, 60)]

    def test_bound_copies_every_number_from_nevermined(self, dlg_env):
        _bind_row(dlg_env)
        r = dlg_env.client.get(DLG_URL)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["configured"] and body["funded"]
        assert body["delegation"] == {
            "delegation_id": DLG_ID, "payment_method_id": "pm_0123456789", "provider": "stripe",
            "environment": "sandbox", "created_by": "ada@example.com", "created_at": "2026-10-22T20:00:00Z"}
        live = body["live"]
        assert live.pop("fetched_at")
        assert live == {"source": "nevermined", "status": "active", "remaining_cents": 4200,
                        "spending_limit_cents": 5000, "spent_cents": 800, "transactions_used": 3,
                        "max_transactions": 50, "currency": "usd", "expires_at": "2026-11-14T20:00:00Z",
                        "error": None}
        assert body["history"] == [{"delegation_id": DLG_ID, "created_at": "2026-10-22T20:00:00Z", "revoked_at": None}]
        (req,) = dlg_env.upstream["requests"]
        assert str(req.url) == f"https://api.sandbox.nevermined.app/api/v1/delegation/{DLG_ID}/transactions?offset=0"
        assert req.headers["authorization"] == "Bearer " + AGENT_KEY
        _no_secret(r)

    def test_remaining_is_null_when_absent_never_limit_minus_spent(self, dlg_env):
        _bind_row(dlg_env)
        dlg_env.upstream["body"] = {"status": "active", "spendingLimitCents": 5000, "amountSpentCents": 800}
        live = dlg_env.client.get(DLG_URL).json()["live"]
        assert live["remaining_cents"] is None
        assert live["spending_limit_cents"] == 5000 and live["spent_cents"] == 800
        assert live["transactions_used"] is None and live["expires_at"] is None

    def test_envelope_shapes_and_unknown_status(self, dlg_env):
        _bind_row(dlg_env)
        dlg_env.upstream["body"] = {"delegation": {"status": "Exhausted", "remainingBudgetCents": 0}}
        live = dlg_env.client.get(DLG_URL).json()["live"]
        assert live["status"] == "exhausted" and live["remaining_cents"] == 0 and live["source"] == "nevermined"
        dlg_env.upstream["body"] = {"transactions": []}
        live = dlg_env.client.get(DLG_URL).json()["live"]
        assert live["status"] == "unknown" and live["error"] is None and live["source"] == "nevermined"
        dlg_env.upstream["body"] = "<html>"
        live = dlg_env.client.get(DLG_URL).json()["live"]
        assert live["status"] == "unknown" and live["error"] is None

    @pytest.mark.parametrize("failure,error", [
        ("unreachable", "nevermined_unreachable"), ("busy", "nevermined_busy"),
        ("401", "nevermined_rejected"), ("500", "nevermined_rejected"), ("404", "nevermined_rejected"),
    ])
    def test_upstream_failure_is_unavailable_with_every_number_null(self, dlg_env, monkeypatch, failure, error):
        import contextlib
        import httpx
        from services import nevermined_delegation_service as svc

        _bind_row(dlg_env)
        if failure == "unreachable":
            dlg_env.upstream["raise"] = httpx.ReadTimeout("slow " + AGENT_KEY)
        elif failure == "busy":
            @contextlib.asynccontextmanager
            async def full():
                raise svc.DelegationBusy("full")
                yield  # pragma: no cover
            monkeypatch.setattr(svc, "delegation_slot", full)
        else:
            dlg_env.upstream.update(status=int(failure), body={"error": "nope " + AGENT_KEY})
        r = dlg_env.client.get(DLG_URL)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["funded"] is True and body["delegation"]["delegation_id"] == DLG_ID
        live = body["live"]
        assert live["source"] == "unavailable" and live["status"] == "unknown" and live["error"] == error
        for k in ("remaining_cents", "spending_limit_cents", "spent_cents", "transactions_used",
                  "max_transactions", "currency", "expires_at"):
            assert live[k] is None
        _no_secret(r)

    def test_history_lists_revoked_rows_newest_first(self, dlg_env):
        _bind_row(dlg_env, delegation_id="old-0123456789", created_at="2026-10-01T00:00:00Z",
                  revoked_at="2026-10-02T00:00:00Z", revoked_by="ada")
        _bind_row(dlg_env)
        body = dlg_env.client.get(DLG_URL).json()
        assert [h["delegation_id"] for h in body["history"]] == [DLG_ID, "old-0123456789"]
        assert body["history"][1]["revoked_at"] == "2026-10-02T00:00:00Z"
        assert "replaced_at" not in body["history"][0]

    def test_not_accessible_is_uniform_404(self, dlg_env):
        dlg_env.as_(**EVE)
        a = dlg_env.client.get(DLG_URL)
        b = dlg_env.client.get("/api/nevermined/agents/ent966-missing/delegation")
        assert a.status_code == b.status_code == 404 and a.json() == b.json()

    def test_agent_key_reads_itself_but_a_sibling_is_the_uniform_404(self, dlg_env):
        _bind_row(dlg_env)
        _bind_row(dlg_env, agent="ent966-b", delegation_id="b-0123456789")
        dlg_env.as_(**{**OPERATOR, "agent_name": "ent966-a", "mcp_scope": "agent"})
        own = dlg_env.client.get(DLG_URL)
        assert own.status_code == 200 and own.json()["delegation"]["delegation_id"] == DLG_ID
        sibling = dlg_env.client.get("/api/nevermined/agents/ent966-b/delegation")
        missing = dlg_env.client.get("/api/nevermined/agents/ent966-missing/delegation")
        assert sibling.status_code == missing.status_code == 404 and sibling.json() == missing.json()
        assert len(dlg_env.upstream["requests"]) == 1  # only the self read dialled Nevermined

    def test_agent_key_with_reach_to_a_sibling_still_gets_the_uniform_404(self, dlg_env, monkeypatch):
        """An `agent_permissions` edge lets the access gate admit the sibling
        (ent#629 "may call"); reading its budget stays self-only."""
        from database import db

        monkeypatch.setattr(db, "is_agent_permitted", lambda src, dst: (src, dst) == ("ent966-a", "ent966-b"))
        _bind_row(dlg_env, agent="ent966-b", delegation_id="b-0123456789")
        dlg_env.as_(**{**OPERATOR, "agent_name": "ent966-a", "mcp_scope": "agent"})
        sibling = dlg_env.client.get("/api/nevermined/agents/ent966-b/delegation")
        missing = dlg_env.client.get("/api/nevermined/agents/ent966-missing/delegation")
        assert sibling.status_code == missing.status_code == 404 and sibling.json() == missing.json()
        assert dlg_env.upstream["requests"] == []

    def test_single_flight_shares_one_upstream_call(self, dlg_env):
        import asyncio
        from database import db
        from services import nevermined_delegation_service as svc

        _bind_row(dlg_env)
        org = {**db.get_nevermined_organization(), **db.get_nevermined_organization_secrets()}

        async def go():
            return await asyncio.gather(*[svc.fetch_live(org, DLG_ID, agent_name="ent966-a") for _ in range(5)])

        results = asyncio.run(go())
        assert len(dlg_env.upstream["requests"]) == 1
        assert all(r["remaining_cents"] == 4200 for r in results)

    def test_malformed_id_never_reaches_a_url(self, dlg_env):
        from services import nevermined_delegation_service as svc

        for bad in ("../x", "short", "a" * 129, "id?x=1", "id/transactions", "dlg 12345678"):
            with pytest.raises(svc.InvalidDelegationId):
                svc.validate_delegation_id(bad)
        _bind_row(dlg_env, delegation_id="../../v1/organization")
        live = dlg_env.client.get(DLG_URL).json()["live"]
        assert live["source"] == "unavailable" and dlg_env.upstream["requests"] == []

    def test_no_secret_or_token_reaches_a_log_line(self, dlg_env, caplog):
        import httpx

        caplog.set_level(logging.DEBUG)
        _bind_row(dlg_env)
        dlg_env.client.get(DLG_URL)
        dlg_env.upstream.update(status=401, body={"error": "nope " + AGENT_KEY})
        dlg_env.client.get(DLG_URL)
        dlg_env.upstream["raise"] = httpx.ConnectError("conn " + AGENT_KEY)
        dlg_env.client.get(DLG_URL)
        assert "upstream_status=200" in caplog.text
        for s in SECRETS + (SESSION_TOKEN,):
            assert s not in caplog.text


class TestDelegationCensus:
    def test_get_is_listed_agent_callable_with_the_pinned_path(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _route_census as census

        key = "routers/nevermined_delegation.py::get_agent_delegation"
        assert census.AGENT_CALLABLE[key][0] == "GET /api/nevermined/agents/{agent_name}/delegation"
        route = census.walk().routes[key]
        assert route.code_class not in ("person", "admin_tier")


BIND_BODY = {"delegationId": DLG_ID, "paymentMethodId": "pm_0123456789", "provider": "stripe"}


def _env_keys(text):
    return {line.split("=", 1)[0]: line.split("=", 1)[1] for line in text.splitlines() if "=" in line}


class TestBind:
    def test_happy_path_one_upstream_call_then_env_write(self, dlg_env):
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["funded"] is True and body["configured"] is True
        assert body["effective"] == "next_execution" and body["api_key_injected"] is True
        assert body["delegation"]["delegation_id"] == DLG_ID
        assert body["delegation"]["payment_method_id"] == "pm_0123456789"
        assert body["delegation"]["created_by"] == "ada@example.com"
        assert body["live"]["status"] == "active" and body["live"]["remaining_cents"] == 4200
        assert [h["delegation_id"] for h in body["history"]] == [DLG_ID]
        # exactly ONE upstream call, the verify, whose payload became `live`
        (req,) = dlg_env.upstream["requests"]
        assert req.method == "GET" and req.url.path == f"/api/v1/delegation/{DLG_ID}/transactions"
        # the agent's .env carries the three keys, everything else intact
        keys = _env_keys(dlg_env.agent["env"])
        assert keys["NVM_DELEGATION_ID"] == f'"{DLG_ID}"'
        assert keys["NVM_API_KEY"] == f'"{AGENT_KEY}"' and keys["NVM_ENVIRONMENT"] == '"sandbox"'
        assert keys["OTHER_KEY"] == "keep-me" and keys["SOME_SECRET"] == AGENT_ENV_SECRET
        assert len(dlg_env.agent["injects"]) == 1 and dlg_env.agent["reads"] == 2  # read, re-read, inject
        assert dlg_env.limits == [("nvm-bind:ada:ent966-a", 10, 60)]
        assert dlg_env.invalidated == ["ent966-a"]
        call = dlg_env.audit.await_args.kwargs
        assert call["event_action"] == "nevermined_delegation_bind"
        assert call["details"] == {"delegation_id": DLG_ID, "payment_method_id": "pm_0123456789",
                                   "provider": "stripe", "environment": "sandbox", "api_key_injected": True}
        _no_secret(r)
        assert AGENT_ENV_SECRET not in r.text

    def test_snake_case_body_accepted(self, dlg_env):
        r = dlg_env.client.post(DLG_URL, json={"delegation_id": DLG_ID})
        assert r.status_code == 201 and r.json()["delegation"]["payment_method_id"] is None

    @pytest.mark.parametrize("bad", ["../x", "short", "a" * 129, "id?x=1", f"{DLG_ID}/x", "dlg 12345678"])
    def test_malformed_id_is_422_before_any_upstream_call(self, dlg_env, bad):
        r = dlg_env.client.post(DLG_URL, json={"delegationId": bad})
        assert r.status_code == 422
        r = dlg_env.client.post(DLG_URL, json={"delegationId": DLG_ID, "paymentMethodId": bad})
        assert r.status_code == 422
        assert dlg_env.upstream["requests"] == [] and dlg_env.agent["reads"] == 0

    def test_not_configured(self, dlg_env):
        dlg_env.as_()
        dlg_env.client.delete("/api/nevermined/organization")
        dlg_env.as_(**OPERATOR)
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "nevermined_not_configured"
        assert dlg_env.upstream["requests"] == [] and dlg_env.limits == []

    def test_rate_limit_fires_before_upstream(self, dlg_env, monkeypatch):
        from fastapi import HTTPException
        from routers import nevermined_delegation as router_mod

        def deny(*a, **k):
            raise HTTPException(status_code=429, detail="slow down", headers={"Retry-After": "9"})

        monkeypatch.setattr(router_mod, "enforce_rate_limit", deny)
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 429
        assert dlg_env.upstream["requests"] == [] and dlg_env.agent["reads"] == 0
        monkeypatch.setattr(router_mod, "enforce_rate_limit", lambda *a, **k: None)
        assert dlg_env.client.get(DLG_URL).json()["funded"] is False

    def test_second_bind_while_active_is_refused_without_upstream_or_env(self, dlg_env):
        _bind_row(dlg_env)
        r = dlg_env.client.post(DLG_URL, json={"delegationId": "new-0123456789"})
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "delegation_active" and detail["delegation_id"] == DLG_ID
        assert "revoke" in detail["message"].lower() and "Nevermined" in detail["message"]
        assert dlg_env.upstream["requests"] == []
        assert dlg_env.agent["reads"] == 0 and dlg_env.agent["injects"] == []
        assert dlg_env.client.get(DLG_URL).json()["delegation"]["delegation_id"] == DLG_ID

    def test_agent_not_running(self, dlg_env):
        dlg_env.agent["running"] = False
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "agent_not_running"
        assert dlg_env.upstream["requests"] == [] and dlg_env.agent["reads"] == 0

    def test_delegation_not_found(self, dlg_env):
        dlg_env.upstream.update(status=404, body={"error": "no"})
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "delegation_not_found"
        assert dlg_env.agent["reads"] == 0 and dlg_env.client.get(DLG_URL).json()["funded"] is False

    @pytest.mark.parametrize("status", ["expired", "REVOKED", "Exhausted"])
    def test_delegation_inactive(self, dlg_env, status):
        dlg_env.upstream["body"] = {**LIVE_BODY, "status": status}
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "delegation_inactive"
        assert r.json()["detail"]["status"] == status.lower()
        assert dlg_env.agent["reads"] == 0

    @pytest.mark.parametrize("body", [{"spendingLimitCents": 5000}, {"transactions": []}, "<html>", {"status": ""}])
    def test_absent_status_does_not_block_the_bind(self, dlg_env, body):
        """F5: the shape is undocumented; only an explicit non-active status refuses."""
        dlg_env.upstream["body"] = body
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 201, r.text
        assert r.json()["live"]["status"] == "unknown" and r.json()["live"]["source"] == "nevermined"

    def test_unreachable_writes_nothing(self, dlg_env):
        import httpx

        dlg_env.upstream["raise"] = httpx.ConnectTimeout("t " + AGENT_KEY)
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "nevermined_unreachable"
        assert dlg_env.agent["reads"] == 0 and dlg_env.agent["injects"] == []
        assert dlg_env.client.get(DLG_URL).json()["history"] == []
        assert dlg_env.audit.await_count == 1  # only the org PUT

    @pytest.mark.parametrize("status,code,http", [(401, "nevermined_rejected", 502), (403, "nevermined_rejected", 502),
                                                  (500, "nevermined_unreachable", 503), (418, "nevermined_rejected", 502)])
    def test_upstream_refusals_write_nothing(self, dlg_env, status, code, http):
        dlg_env.upstream.update(status=status, body={"error": "x " + AGENT_KEY})
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == http and r.json()["detail"]["code"] == code
        assert dlg_env.agent["reads"] == 0 and dlg_env.client.get(DLG_URL).json()["history"] == []
        _no_secret(r)

    def test_busy(self, dlg_env, monkeypatch):
        import contextlib
        from services import nevermined_delegation_service as svc

        @contextlib.asynccontextmanager
        async def full():
            raise svc.DelegationBusy("full")
            yield  # pragma: no cover

        monkeypatch.setattr(svc, "delegation_slot", full)
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "nevermined_busy"
        assert r.headers["retry-after"] == "5" and dlg_env.agent["reads"] == 0

    def test_env_read_failure_writes_no_row(self, dlg_env):
        dlg_env.agent["read_status"] = 500
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "agent_unreachable"
        assert dlg_env.agent["injects"] == []
        assert dlg_env.client.get(DLG_URL).json()["history"] == []

    def test_inject_failure_compensates_by_deleting_the_inserted_row(self, dlg_env):
        dlg_env.agent["inject_status"] = 500
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "agent_unreachable"
        assert "Nevermined" in r.json()["detail"]["message"]
        assert len(dlg_env.agent["injects"]) == 1  # the write was attempted
        state = dlg_env.client.get(DLG_URL).json()
        assert state["funded"] is False and state["history"] == []  # the row is gone, not revoked
        assert dlg_env.audit.await_count == 1 and dlg_env.invalidated == []

    def test_inject_transport_error_compensates_too(self, dlg_env):
        import httpx

        original = dlg_env.agent

        # the reads succeed, the inject blows up at the transport
        def handler(request):
            if request.url.path == "/api/credentials/inject":
                raise httpx.ConnectError("gone")
            original["reads"] += 1
            return httpx.Response(200, json={"files": {".env": original["env"]}})

        from services import nevermined_delegation_service as svc
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(svc, "_agent_client", lambda name: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "agent_unreachable"
        assert dlg_env.client.get(DLG_URL).json()["history"] == []

    def test_direct_db_race_is_bind_in_progress(self, dlg_env, monkeypatch):
        """Two binds at once: the service's own check passes for both, the partial
        unique index refuses the second insert."""
        from database import db

        _bind_row(dlg_env)  # the first bind's row is already active
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(db, "get_active_agent_delegation", lambda name: None)
            r = dlg_env.client.post(DLG_URL, json={"delegationId": "second-0123456789"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "bind_in_progress"
        assert dlg_env.agent["injects"] == []
        state = dlg_env.client.get(DLG_URL).json()
        assert state["delegation"]["delegation_id"] == DLG_ID and len(state["history"]) == 1

    def test_pre_existing_api_key_is_not_claimed(self, dlg_env):
        dlg_env.agent["env"] = "NVM_API_KEY=hand-pasted-personal-key\n"
        r = dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert r.status_code == 201 and r.json()["api_key_injected"] is False
        assert dlg_env.audit.await_args.kwargs["details"]["api_key_injected"] is False
        # the org key still overwrites the value (Premise 6), but revoke will leave it
        assert _env_keys(dlg_env.agent["env"])["NVM_API_KEY"] == f'"{AGENT_KEY}"'

    def test_rebind_carries_api_key_injected_after_a_revoke_with_the_agent_down(self, dlg_env):
        """H1: revoke while the agent is down leaves NVM_API_KEY in .env; the
        next bind must still remember Trinity introduced it."""
        assert dlg_env.client.post(DLG_URL, json=BIND_BODY).status_code == 201
        dlg_env.agent["running"] = False
        r = dlg_env.client.delete(DLG_URL)
        assert r.status_code == 200 and r.json()["env_removed"] is False
        assert "NVM_API_KEY" in _env_keys(dlg_env.agent["env"])
        dlg_env.agent["running"] = True
        r = dlg_env.client.post(DLG_URL, json={"delegationId": "second-0123456789"})
        assert r.status_code == 201 and r.json()["api_key_injected"] is True
        r = dlg_env.client.delete(DLG_URL)
        assert r.status_code == 200 and r.json()["env_removed"] is True
        assert "NVM_API_KEY" not in _env_keys(dlg_env.agent["env"])

    def test_agent_key_is_refused(self, dlg_env):
        dlg_env.as_(**{**OPERATOR, "agent_name": "ent966-a", "mcp_scope": "agent"})
        assert dlg_env.client.post(DLG_URL, json=BIND_BODY).status_code == 403
        assert dlg_env.client.delete(DLG_URL).status_code == 403
        assert dlg_env.upstream["requests"] == []

    def test_not_owner_is_uniform_404(self, dlg_env):
        dlg_env.as_(**EVE)
        for method in ("post", "delete"):
            kw = {"json": BIND_BODY} if method == "post" else {}
            a = getattr(dlg_env.client, method)(DLG_URL, **kw)
            b = getattr(dlg_env.client, method)("/api/nevermined/agents/ent966-missing/delegation", **kw)
            assert a.status_code == b.status_code == 404 and a.json() == b.json()
        assert dlg_env.upstream["requests"] == []


class TestRevoke:
    def test_happy_path_upstream_first_then_row_then_env(self, dlg_env):
        assert dlg_env.client.post(DLG_URL, json=BIND_BODY).status_code == 201
        dlg_env.upstream["requests"].clear()
        r = dlg_env.client.delete(DLG_URL)
        assert r.status_code == 200, r.text
        assert r.json() == {"revoked": True, "delegation_id": DLG_ID, "env_removed": True, "env_note": None}
        (req,) = dlg_env.upstream["requests"]
        assert req.method == "DELETE" and str(req.url) == f"https://api.sandbox.nevermined.app/api/v1/delegation/{DLG_ID}"
        assert req.headers["authorization"] == "Bearer " + AGENT_KEY
        keys = _env_keys(dlg_env.agent["env"])
        assert not {"NVM_DELEGATION_ID", "NVM_API_KEY", "NVM_ENVIRONMENT"} & set(keys)
        assert keys["OTHER_KEY"] == "keep-me" and keys["SOME_SECRET"] == AGENT_ENV_SECRET
        state = dlg_env.client.get(DLG_URL).json()
        assert state["funded"] is False and state["history"][0]["revoked_at"]
        call = dlg_env.audit.await_args.kwargs
        assert call["event_action"] == "nevermined_delegation_revoke"
        assert call["details"] == {"delegation_id": DLG_ID, "environment": "sandbox",
                                   "env_removed": True, "api_key_removed": True}
        assert dlg_env.invalidated[-1] == "ent966-a"
        assert "Nevermined" in dlg_env.client.delete(DLG_URL).json()["detail"]["message"]

    def test_hand_configured_key_survives_revoke(self, dlg_env):
        _bind_row(dlg_env, api_key_injected=0)
        dlg_env.agent["env"] = 'NVM_API_KEY=personal\nNVM_DELEGATION_ID="x"\nNVM_ENVIRONMENT=live\n'
        r = dlg_env.client.delete(DLG_URL)
        assert r.status_code == 200 and r.json()["env_removed"] is True
        keys = _env_keys(dlg_env.agent["env"])
        assert keys == {"NVM_API_KEY": "personal", "NVM_ENVIRONMENT": "live"}
        assert dlg_env.audit.await_args.kwargs["details"]["api_key_removed"] is False

    def test_upstream_404_is_revoked(self, dlg_env):
        _bind_row(dlg_env)
        dlg_env.upstream["delete_status"] = 404
        r = dlg_env.client.delete(DLG_URL)
        assert r.status_code == 200 and r.json()["revoked"] is True
        assert dlg_env.client.get(DLG_URL).json()["funded"] is False

    @pytest.mark.parametrize("failure,http,code", [
        ("500", 503, "nevermined_unreachable"), ("401", 502, "nevermined_rejected"),
        ("409", 502, "nevermined_rejected"), ("raise", 503, "nevermined_unreachable"), ("busy", 503, "nevermined_busy"),
    ])
    def test_upstream_failure_changes_nothing_locally(self, dlg_env, monkeypatch, failure, http, code):
        import contextlib
        import httpx
        from services import nevermined_delegation_service as svc

        _bind_row(dlg_env)
        dlg_env.agent["env"] = 'NVM_DELEGATION_ID="x"\nNVM_API_KEY=k\n'
        if failure == "raise":
            dlg_env.upstream["raise"] = httpx.ReadTimeout("t")
        mp = pytest.MonkeyPatch()
        if failure == "busy":
            @contextlib.asynccontextmanager
            async def full():
                raise svc.DelegationBusy("full")
                yield  # pragma: no cover
            mp.setattr(svc, "delegation_slot", full)
        elif failure != "raise":
            dlg_env.upstream["delete_status"] = int(failure)
        try:
            r = dlg_env.client.delete(DLG_URL)
        finally:
            mp.undo()
        assert r.status_code == http and r.json()["detail"]["code"] == code
        assert "Nevermined" in r.json()["detail"]["message"]
        assert dlg_env.agent["injects"] == [] and dlg_env.agent["reads"] == 0
        dlg_env.upstream["raise"] = None
        state = dlg_env.client.get(DLG_URL).json()
        assert state["funded"] is True and state["history"][0]["revoked_at"] is None
        assert dlg_env.audit.await_count == 1

    def test_agent_down_revokes_with_env_note(self, dlg_env):
        _bind_row(dlg_env)
        dlg_env.agent["running"] = False
        r = dlg_env.client.delete(DLG_URL)
        assert r.status_code == 200
        body = r.json()
        assert body["revoked"] is True and body["env_removed"] is False
        assert body["env_note"].startswith("agent_not_running") and "Nevermined" in body["env_note"]
        assert dlg_env.agent["reads"] == 0
        assert dlg_env.client.get(DLG_URL).json()["funded"] is False
        assert dlg_env.audit.await_args.kwargs["details"]["env_removed"] is False

    def test_lost_local_race_skips_env_and_audit(self, dlg_env, monkeypatch):
        """A concurrent revoke stamped the row first: Nevermined already refuses
        the mandate, so this request touches neither the env nor the audit log."""
        from database import db

        _bind_row(dlg_env)
        audits_before = dlg_env.audit.await_count
        monkeypatch.setattr(db, "revoke_agent_delegation", lambda row_id, revoked_by: None)
        r = dlg_env.client.delete(DLG_URL)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["revoked"] is True and body["delegation_id"] == DLG_ID and body["env_removed"] is False
        assert "Nevermined" in body["env_note"] and "concurrent" in body["env_note"]
        assert len(dlg_env.upstream["requests"]) == 1  # upstream-first order is unchanged
        assert dlg_env.agent["injects"] == [] and dlg_env.agent["reads"] == 0
        assert dlg_env.audit.await_count == audits_before

    def test_env_removal_failure_still_revokes(self, dlg_env):
        _bind_row(dlg_env)
        dlg_env.agent["inject_status"] = 500
        r = dlg_env.client.delete(DLG_URL)
        assert r.status_code == 200 and r.json()["env_removed"] is False
        assert r.json()["env_note"].startswith("agent_unreachable")
        assert dlg_env.client.get(DLG_URL).json()["funded"] is False

    def test_no_active_delegation(self, dlg_env):
        r = dlg_env.client.delete(DLG_URL)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "no_active_delegation"
        assert dlg_env.upstream["requests"] == []

    def test_not_configured(self, dlg_env):
        _bind_row(dlg_env)
        dlg_env.as_()
        # the org cannot be deleted while a mandate is active; drop it directly
        from db.engine import get_engine
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM nevermined_organization"))
        dlg_env.as_(**OPERATOR)
        r = dlg_env.client.delete(DLG_URL)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "nevermined_not_configured"


class TestBindRevokeSecrets:
    def test_no_secret_token_or_env_line_reaches_a_log_line(self, dlg_env, caplog):
        import httpx

        caplog.set_level(logging.DEBUG)
        dlg_env.client.post(DLG_URL, json=BIND_BODY)
        dlg_env.client.delete(DLG_URL)
        dlg_env.agent["inject_status"] = 500
        dlg_env.client.post(DLG_URL, json=BIND_BODY)
        dlg_env.agent["inject_status"] = 200
        dlg_env.upstream.update(status=401, body={"error": "nope " + AGENT_KEY})
        dlg_env.client.post(DLG_URL, json=BIND_BODY)
        dlg_env.upstream["raise"] = httpx.ConnectError("conn " + AGENT_KEY)
        dlg_env.client.post(DLG_URL, json=BIND_BODY)
        assert "Nevermined delegation bound to ent966-a" in caplog.text
        assert "Nevermined delegation revoked for ent966-a" in caplog.text
        for s in SECRETS + (SESSION_TOKEN, AGENT_ENV_SECRET, "keep-me", "OTHER_KEY="):
            assert s not in caplog.text
