"""trinity-enterprise#1066 — who may reach the secret field's route.

`POST /api/enterprise/client-portal/asks/{id}/credential` is a Workspace door
(`get_portal_principal`), so the portal principal's own policy owns it
(#2998 census class `portal`) and this file pins what that policy means HERE,
in the ent#719 shape — driven through the real router with the real
dependency, only the credential resolvers stood in for:

* an agent-scoped key is refused at the dependency (403), BEFORE the row is
  read — an agent's injected key must never type a secret into its own or a
  sibling's environment through its owner's identity;
* a system key and an ops key reach the portal but are not a PERSON: 403
  `person_required`, nothing read;
* a Workspace-only `user` (the ent#837 principal, admitted by the portal
  resolver) and a portal session token are admitted; the owner's own
  user-scoped key and a signed-in session too;
* a stranger — a person the ask was not addressed to — gets the uniform 404;
* the typed-answer route refuses a credential ask by name (422), so the field
  is the ONLY door.

The sink itself (`credential_sink.set_env_value`) is stood in for and
recorded: this file is about the door, `test_ent1066_credential_sink.py` is
about what happens behind it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

OWNER = "owner-1066-route@example.com"
AGENT = "agent-1066-route"
NAME = "VAULT_SERVICE_TOKEN"
VALUE = "vst-route-test-value-xxxxxxxx"
PORTAL_TOKEN = "portal-session-token"
STRANGER_TOKEN = "portal-session-stranger"
PLATFORM_TOKEN = "platform-credential"

_PRINCIPAL = {"user": None}
_APP = None


def _user(**kw):
    from models import User
    base = {"id": 7, "username": "owner1066", "email": OWNER, "role": "admin"}
    base.update(kw)
    return User(**base)


PERSONS = [
    pytest.param({}, id="jwt-admin"),
    pytest.param({"mcp_scope": "user"}, id="own-user-key"),
    pytest.param({"role": "user"}, id="workspace-only-user"),
    pytest.param({"role": "user", "mcp_scope": "user"}, id="workspace-only-user-key"),
]
NOT_PERSONS = [
    pytest.param({"mcp_scope": "system"}, id="system-key"),
    pytest.param({"mcp_scope": "ops"}, id="ops-key"),
]
AGENT_KEYS = [
    pytest.param({"mcp_scope": "agent", "agent_name": AGENT}, id="own-agent-key"),
    pytest.param({"mcp_scope": "agent", "agent_name": "some-sibling"}, id="sibling-agent-key"),
]


def _app():
    global _APP
    if _APP is None:
        from fastapi import FastAPI
        from fastapi.exceptions import RequestValidationError
        from client_portal.asks.router import router
        from error_handlers import validation_error_without_input
        app = FastAPI()
        app.include_router(router)
        # The production composition root (`main.py`) installs this handler on
        # the app so a 422 never echoes the rejected value (trinity-enterprise#109);
        # the stand-in app carries the same wiring so the malformed-body case
        # below proves the route's own contract under it.
        app.add_exception_handler(RequestValidationError, validation_error_without_input)
        _APP = app
    return _APP


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def door(real_db, monkeypatch):
    """The real router + the real `get_portal_principal`; the session decoder
    and the platform resolver stood in for; the sink recorded."""
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    import database
    import services.ask_service as svc
    import services.credential_sink as sink
    from client_portal import db as portal_db
    from client_portal import portal_auth as pa
    import client_portal.service as portal_service

    monkeypatch.setattr(portal_service, "agent_on_roster", lambda *a, **k: True)
    monkeypatch.setattr(portal_db, "is_client_blocked", lambda e: False)
    monkeypatch.setattr(pa, "portal_session_needs_rotation", lambda t: False)
    monkeypatch.setattr(pa, "portal_session_minted_by", lambda t: "otp")
    sessions = {PORTAL_TOKEN: OWNER, STRANGER_TOKEN: "stranger@example.com"}
    monkeypatch.setattr(pa, "decode_portal_session", lambda t: sessions.get(t))

    async def resolve(request, token):
        if token != PLATFORM_TOKEN or _PRINCIPAL["user"] is None:
            raise HTTPException(status_code=401, detail="bad token")
        return _PRINCIPAL["user"]
    monkeypatch.setattr(pa, "resolve_platform_user_unfloored", resolve)
    monkeypatch.setattr(database.db, "get_user_by_username",
                        lambda u: {"email": _PRINCIPAL["user"].email} if _PRINCIPAL["user"] else None)

    reads = {"n": 0}
    real_get = real_db.get_operator_queue_item

    def counted(item_id):
        reads["n"] += 1
        return real_get(item_id)
    monkeypatch.setattr(real_db, "get_operator_queue_item", counted)

    writes = []

    async def fake_set(agent_name, name, value, **kw):
        writes.append((agent_name, name, value, kw))
    monkeypatch.setattr(sink, "set_env_value", fake_set)

    class _Audit:
        async def log(self, **kw):
            return "evt"
    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", None)
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda a: False, raising=False)

    def as_(**principal):
        _PRINCIPAL["user"] = _user(**principal)

    as_()
    client = TestClient(_app(), raise_server_exceptions=True)
    return SimpleNamespace(client=client, as_=as_, db=real_db, writes=writes, reads=reads)


def _pending(real_db, rid, **over):
    item = {"id": rid, "type": "credential", "status": "pending", "priority": "medium",
            "title": f"Needs {NAME}", "question": "to open the vault", "options": None,
            "context": {"credential_name": NAME}, "created_at": "2026-11-01T10:00:00Z",
            "addressed_to_email": OWNER}
    item.update(over)
    return real_db.create_operator_queue_item(AGENT, item)


def _post(door, uid, token=PLATFORM_TOKEN, value=VALUE):
    return door.client.post(f"/api/enterprise/client-portal/asks/{uid}/credential",
                            json={"value": value}, headers={"Authorization": f"Bearer {token}"})


class TestRefusedPrincipals:
    @pytest.mark.parametrize("principal", AGENT_KEYS)
    def test_an_agent_key_is_refused_at_the_dependency_before_the_row_is_read(self, door, principal):
        uid = _pending(door.db, f"rt-agent-{principal['agent_name']}")
        door.as_(**principal)
        door.reads["n"] = 0
        res = _post(door, uid)
        assert res.status_code == 403, res.text
        assert door.reads["n"] == 0 and door.writes == []
        assert VALUE not in res.text
        assert door.db.get_operator_queue_item(uid)["status"] == "pending"

    def test_an_agent_key_learns_nothing_about_a_missing_id(self, door):
        door.as_(mcp_scope="agent", agent_name=AGENT)
        res = _post(door, "no-such-uuid")
        assert res.status_code == 403

    @pytest.mark.parametrize("principal", NOT_PERSONS)
    def test_a_machine_principal_is_not_a_person(self, door, principal):
        uid = _pending(door.db, f"rt-machine-{principal['mcp_scope']}")
        door.as_(**principal)
        door.reads["n"] = 0
        res = _post(door, uid)
        assert res.status_code == 403, res.text
        assert res.json()["detail"]["code"] == "person_required"
        assert door.reads["n"] == 0 and door.writes == []

    def test_a_stranger_gets_the_uniform_404(self, door):
        uid = _pending(door.db, "rt-stranger")
        res = _post(door, uid, token=STRANGER_TOKEN)
        assert res.status_code == 404 and res.json()["detail"]["code"] == "not_found"
        assert door.writes == []

    def test_no_credential_at_all_is_a_401(self, door):
        uid = _pending(door.db, "rt-anon")
        res = door.client.post(f"/api/enterprise/client-portal/asks/{uid}/credential",
                               json={"value": VALUE})
        assert res.status_code == 401 and door.writes == []


class TestAdmittedPrincipals:
    @pytest.mark.parametrize("principal", PERSONS)
    def test_a_person_on_the_platform_reaches_the_sink(self, door, principal):
        uid = _pending(door.db, f"rt-ok-{len(principal)}-{principal.get('role', 'admin')}-{principal.get('mcp_scope')}")
        door.as_(**principal)
        res = _post(door, uid)
        assert res.status_code == 200, res.text
        assert [(w[0], w[1], w[2]) for w in door.writes] == [(AGENT, NAME, VALUE)]
        assert door.writes[0][3]["actor_email"] == OWNER
        assert door.writes[0][3]["ask_id"] == uid
        body = res.json()
        assert body["status"] == "answered" and body["kind"] == "credential"
        assert body["credential_name"] == NAME and body["answer"] == "provided"
        assert VALUE not in res.text

    def test_a_portal_session_reaches_the_sink(self, door):
        uid = _pending(door.db, "rt-portal")
        res = _post(door, uid, token=PORTAL_TOKEN)
        assert res.status_code == 200, res.text
        assert len(door.writes) == 1 and door.writes[0][2] == VALUE
        assert door.writes[0][3]["actor_email"] == OWNER

    def test_the_route_is_the_only_door_for_the_kind(self, door):
        uid = _pending(door.db, "rt-only-door")
        res = door.client.post(f"/api/enterprise/client-portal/asks/{uid}/answer",
                               json={"response": VALUE},
                               headers={"Authorization": f"Bearer {PORTAL_TOKEN}"})
        assert res.status_code == 422 and res.json()["detail"]["code"] == "credential_answer_via_field"
        assert VALUE not in res.text
        assert door.db.get_operator_queue_item(uid)["status"] == "pending"

    def test_a_malformed_body_is_a_422_that_never_echoes_the_value(self, door):
        uid = _pending(door.db, "rt-body")
        res = door.client.post(f"/api/enterprise/client-portal/asks/{uid}/credential",
                               json={"secret": VALUE},
                               headers={"Authorization": f"Bearer {PORTAL_TOKEN}"})
        assert res.status_code == 422 and VALUE not in res.text
        assert door.writes == []

    def test_the_list_shows_the_name_to_its_addressee(self, door):
        uid = _pending(door.db, "rt-list")
        res = door.client.get("/api/enterprise/client-portal/asks",
                              params={"agent_name": AGENT},
                              headers={"Authorization": f"Bearer {PORTAL_TOKEN}"})
        assert res.status_code == 200
        mine = [a for a in res.json() if a["id"] == uid]
        assert mine and mine[0]["kind"] == "credential" and mine[0]["credential_name"] == NAME
