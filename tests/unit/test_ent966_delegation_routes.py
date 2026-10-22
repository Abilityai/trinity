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
