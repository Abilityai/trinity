"""trinity-enterprise#1024 — an admin's user MCP key may act on behalf of an agent.

Operator ruling (2026-10-30): "admin user key should be able to do whatever it
wants." The agent-identity tools refused every key that carries no agent
identity, so a local hub on the operator's admin key could not raise an ask,
report, record metrics or write a canvas without dispatching an agent to do a
one-line call.

The shape:
  * The MCP sends `X-Trinity-Act-As-Agent: <agent>` when a tool is given
    `agent_name`. `get_current_user` admits it for exactly one principal — a
    `user`-scoped MCP key whose user is an admin — and refuses it, with a named
    403 that says why, for every other one (agent, system, connector,
    portal_delegate, ops, a non-admin user key, a browser session). An admitted
    request writes one audit row naming BOTH the acting user and the agent.
  * The self-acting gates admit the admitted agent, and only on its own path:
    `get_self_acting_agent` (asks) and `assert_own_agent` (report, metrics,
    canvas). Acting for X never reaches Y.
  * An on-behalf ask's own audit row says who acted (`acted_by: user:<id>`), so
    an admin acting as an agent is never indistinguishable from the agent.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

HEADER = "X-Trinity-Act-As-Agent"


def _user(scope="user", role="admin", **kw):
    from models import User

    return User(id=7, username="ops-admin", email="admin@example.com", role=role,
                mcp_scope=scope, mcp_key_id="key-1", mcp_key_name="hub", **kw)


def _request(headers=None, method="POST", path="/api/agents/x/operator-queue"):
    return SimpleNamespace(headers=headers or {}, method=method, scope={"path": path},
                           client=SimpleNamespace(host="127.0.0.1"))


@pytest.fixture
def audit(monkeypatch):
    from services import platform_audit_service as pas

    rows = []

    async def log(**kw):
        rows.append(kw)
    monkeypatch.setattr(pas.platform_audit_service, "log", log)
    return rows


def _admit(user, headers):
    from dependencies import admit_act_as

    asyncio.run(admit_act_as(_request(headers), user))
    return user


# ---------------------------------------------------------------------------
# Admission — the scope matrix
# ---------------------------------------------------------------------------


def test_an_admin_user_key_is_admitted_and_audited(audit):
    from dependencies import act_as_agent_of

    user = _admit(_user(), {HEADER: "worker"})
    assert act_as_agent_of(user) == "worker"
    assert len(audit) == 1
    row = audit[0]
    assert row["event_action"] == "acted_as_agent"
    assert row["actor_user"] is user
    assert row["actor_agent_name"] == "worker"
    assert row["details"]["acted_by"] == "user:7"
    assert row["details"]["agent_name"] == "worker"
    assert row["details"]["method"] == "POST"


def test_no_header_changes_nothing(audit):
    from dependencies import act_as_agent_of

    user = _admit(_user(), {})
    assert act_as_agent_of(user) is None
    assert audit == []


@pytest.mark.parametrize("make, why", [
    (lambda: _user(role="user"), "admin"),
    (lambda: _user(role="creator"), "admin"),
    (lambda: _user(scope="agent", agent_name="worker"), "agent"),
    (lambda: _user(scope="system"), "system"),
    (lambda: _user(scope="connector", connector_agent="worker"), "connector"),
    (lambda: _user(scope="portal_delegate", portal_delegate=True), "portal_delegate"),
    (lambda: _user(scope="ops"), "ops"),
    (lambda: _user(scope=None), "browser"),
], ids=["user-key", "creator-key", "agent", "system", "connector", "portal_delegate", "ops", "jwt"])
def test_every_other_principal_is_refused_with_a_reason(audit, make, why):
    with pytest.raises(HTTPException) as exc:
        _admit(make(), {HEADER: "worker"})
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "act_as_not_permitted"
    assert why in exc.value.detail["message"]
    assert audit == []


@pytest.mark.parametrize("bad", ["", "   ", "x" * 129])
def test_a_malformed_target_is_a_400(audit, bad):
    with pytest.raises(HTTPException) as exc:
        _admit(_user(), {HEADER: bad})
    assert exc.value.status_code == 400
    assert audit == []


def test_a_failed_audit_write_does_not_fail_the_request(monkeypatch):
    from dependencies import act_as_agent_of
    from services import platform_audit_service as pas

    async def boom(**kw):
        raise RuntimeError("audit down")
    monkeypatch.setattr(pas.platform_audit_service, "log", boom)
    assert act_as_agent_of(_admit(_user(), {HEADER: "worker"})) == "worker"


# ---------------------------------------------------------------------------
# The self-acting gates admit the admitted agent, on its own path only
# ---------------------------------------------------------------------------


@pytest.fixture
def authorized(monkeypatch):
    import dependencies

    monkeypatch.setattr(dependencies, "get_authorized_agent", lambda name, user: name)


def test_an_admitted_admin_acts_as_the_agent_on_the_ask_routes(audit, authorized):
    from dependencies import get_self_acting_agent

    user = _admit(_user(), {HEADER: "worker"})
    assert get_self_acting_agent(name="worker", current_user=user) == "worker"


def test_acting_for_one_agent_never_reaches_another(audit, authorized):
    from dependencies import get_self_acting_agent

    user = _admit(_user(), {HEADER: "worker"})
    with pytest.raises(HTTPException) as exc:
        get_self_acting_agent(name="other", current_user=user)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "agent_identity_required"


def test_an_admin_key_without_the_header_is_still_refused_and_told_how(authorized):
    from dependencies import get_self_acting_agent

    with pytest.raises(HTTPException) as exc:
        get_self_acting_agent(name="worker", current_user=_user())
    assert exc.value.detail["code"] == "agent_identity_required"
    assert "agent_name" in exc.value.detail["message"]


def test_agent_keys_still_act_only_as_themselves(authorized):
    from dependencies import get_self_acting_agent

    assert get_self_acting_agent(name="worker", current_user=_user(scope="agent", agent_name="worker")) == "worker"
    with pytest.raises(HTTPException):
        get_self_acting_agent(name="other", current_user=_user(scope="agent", agent_name="worker"))


def test_the_report_metrics_canvas_self_gate_holds_the_admitted_agent(audit):
    from dependencies import assert_own_agent

    user = _admit(_user(), {HEADER: "worker"})
    assert_own_agent(user, "worker")                 # its own path: through
    with pytest.raises(HTTPException) as exc:
        assert_own_agent(user, "other")              # another agent's: refused
    assert exc.value.status_code == 403
    assert_own_agent(_user(), "other")               # no header: unchanged


# ---------------------------------------------------------------------------
# An on-behalf ask says who acted
# ---------------------------------------------------------------------------


def _raise(actor, monkeypatch):
    from services import ask_service, operator_resume_service

    captured = []
    monkeypatch.setattr(ask_service, "_announce", lambda rows, trigger: captured.extend(rows))
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda fn: fn())
    agent = f"agent-1024-{uuid.uuid4().hex[:6]}"
    ask_service.raise_ask(
        agent,
        {"request_id": f"r-{uuid.uuid4().hex[:8]}", "type": "question",
         "title": "Approve?", "question": "May I proceed?"},
        raised_by="agent", channel="mcp", actor_user=actor,
    )
    return next(r for r in captured if r["event_action"] == "raised")


def test_an_on_behalf_ask_records_the_acting_user(audit, monkeypatch):
    row = _raise(_admit(_user(), {HEADER: "worker"}), monkeypatch)
    assert row["details"]["acted_by"] == "user:7"
    assert row["actor_email"] == "admin@example.com"


def test_an_agents_own_ask_carries_no_acted_by(monkeypatch):
    row = _raise(_user(scope="agent", agent_name="worker"), monkeypatch)
    assert "acted_by" not in row["details"]


def _ending_rows(monkeypatch):
    from services import ask_service

    rows = []
    monkeypatch.setattr(ask_service, "_ended", lambda event, audit, trigger: rows.extend(audit))
    return rows


def _new_ask(agent, actor, monkeypatch, **extra):
    from services import ask_service, operator_resume_service

    monkeypatch.setattr(ask_service, "_announce", lambda rows, trigger: None)
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda fn: fn())
    request_id = f"r-{uuid.uuid4().hex[:8]}"
    ask_service.raise_ask(
        agent,
        {"request_id": request_id, "type": "question", "title": "Approve?", "question": "May I proceed?", **extra},
        raised_by="agent", channel="mcp", actor_user=actor,
    )
    return request_id


def test_an_on_behalf_withdrawal_records_the_acting_user(audit, monkeypatch):
    from services import ask_service

    actor = _admit(_user(), {HEADER: "worker"})
    agent = f"agent-1024-{uuid.uuid4().hex[:6]}"
    request_id = _new_ask(agent, actor, monkeypatch)
    rows = _ending_rows(monkeypatch)
    ask_service.withdraw(agent, request_id, "no longer needed", actor_user=actor)
    assert rows[0]["event_action"] == "withdrawn"
    assert rows[0]["details"]["acted_by"] == "user:7"


def test_an_on_behalf_replacement_records_the_acting_user(audit, monkeypatch):
    actor = _admit(_user(), {HEADER: "worker"})
    agent = f"agent-1024-{uuid.uuid4().hex[:6]}"
    first = _new_ask(agent, actor, monkeypatch)
    rows = _ending_rows(monkeypatch)
    _new_ask(agent, actor, monkeypatch, replaces=first)
    assert rows[0]["event_action"] == "replaced"
    assert rows[0]["details"]["acted_by"] == "user:7"


# Through the real door. Every test above calls `admit_act_as` directly, so they
# would all stay green if `get_current_user` stopped calling it.
class TestThroughTheRealDoor:
    @pytest.fixture
    def door(self, monkeypatch):
        from fastapi import Depends, FastAPI
        from starlette.testclient import TestClient

        import database
        from dependencies import act_as_agent_of, get_current_user

        database.db.set_setting("terms_required", "false")
        row = {"id": 7, "username": "ops-admin", "email": "admin@example.com", "role": "admin", "suspended_at": None}
        monkeypatch.setattr(database.db, "get_user_by_email", lambda *_a, **_k: row)
        monkeypatch.setattr(database.db, "get_user_by_username", lambda *_a, **_k: row)
        monkeypatch.setattr(database.db, "get_agent_ephemeral_info", lambda *_a, **_k: {"is_ephemeral": False})

        def key(scope):
            monkeypatch.setattr(database.db, "validate_mcp_api_key", lambda *_a, **_k: {
                "scope": scope, "agent_name": "worker" if scope == "agent" else None,
                "key_id": "k1", "key_name": scope, "user_id": "ops-admin", "user_email": "admin@example.com",
            })

        app = FastAPI()

        @app.post("/probe", status_code=201)
        async def probe(user=Depends(get_current_user)):
            return {"acting_as": act_as_agent_of(user)}

        yield TestClient(app), key
        database.db.delete_setting("terms_required")

    def _post(self, client, **headers):
        return client.post("/probe", headers={"Authorization": "Bearer trinity_mcp_fake", **headers})

    def test_an_admins_user_key_is_admitted_with_one_audit_row(self, door, audit):
        client, key = door
        key("user")
        r = self._post(client, **{HEADER: "worker"})
        assert (r.status_code, r.json()["acting_as"]) == (201, "worker")
        assert [row["event_action"] for row in audit] == ["acted_as_agent"]

    def test_an_agent_key_is_refused_with_no_audit_row(self, door, audit):
        client, key = door
        key("agent")
        r = self._post(client, **{HEADER: "other"})
        assert (r.status_code, r.json()["detail"]["code"]) == (403, "act_as_not_permitted")
        assert audit == []

    def test_no_header_is_no_act_as(self, door, audit):
        client, key = door
        key("user")
        r = self._post(client)
        assert (r.status_code, r.json()["acting_as"]) == (201, None)
        assert audit == []
