"""A `credential` ask is recorded only for a reader who can reach its secret
field: the seated primary, else the agent's owner, else nobody and the raise is
refused by name (abilityai/trinity#4579).

A credential ask has ONE answering door, the Workspace secret field
(`client_portal.asks.service.provide_credential`); any other answer is refused
before anything is read (`credential_answer_via_field`). That door opens for
the ask's single reader when the agent is on their Workspace roster: shared
with them, or owned by them. The raise resolved `primary` with no reach check,
so a seated primary the agent was not shared with became the only reader of an
ask they could never see: nobody could fill it and it never expired.

The rule is narrower than the one an `approver` ask uses
(`role_addressing.can_open_agent`, #4501): that one also counts an admin's
access to any agent, because an approval can be decided in Operations. A
credential cannot.

Harness: the one of `test_4501_approver_reach.py` (the unit island's
per-process SQLite, the REAL sink, the REAL door and the REAL route). Two
modes. The stubbed roster is the set of emails the agent is on the roster of,
with the owner a plain lookup, for the chain itself. The `real_roster` fixture
puts the real `agent_on_roster` and the real owner lookup back, over real
account, ownership and share rows, for the arms a stub cannot see: an owner is
on the roster only through the owned read.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

# Bound at collection time — see test_ent747_748_ask_discuss_dismiss.py.
import services.ask_service  # noqa: E402,F401
import services.operator_queue_service  # noqa: E402,F401
import services.operator_resume_service  # noqa: E402,F401
import services.role_addressing  # noqa: E402,F401
import client_portal.service  # noqa: E402,F401

CODE = "role_cannot_open_agent"
NAME = "ACME_API_TOKEN"

# The real lookups, bound before any fixture patches them: the owned arm of the
# roster and the owner's email are what the default (nobody seated) path stands
# on, and a stub that answers for both hides a fix that refuses every owner.
_REAL_ON_ROSTER = client_portal.service.agent_on_roster
_REAL_OWNER_EMAIL = services.ask_service._owner_email


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def agent():
    return f"agent-4579-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def people():
    return {"alice": _uniq("alice"), "bob": _uniq("bob"), "carol": _uniq("carol")}


@pytest.fixture(autouse=True)
def shared(monkeypatch):
    """The emails the agent is shared with. Empty unless a test adds one, so
    nobody can open the agent by default."""
    import client_portal.service as portal_service
    emails = set()
    monkeypatch.setattr(
        portal_service, "agent_on_roster",
        lambda agent_name, email, include_owned=True: (email or "").lower() in emails)
    return emails


class _Provider:
    """A fake assignments provider: `kinds` maps a kind to the seated emails."""

    def __init__(self, kinds):
        self.kinds = kinds

    def assignment_for(self, agent_name, triggered_by):
        return None

    def people_for(self, agent_name, role):
        value = self.kinds.get(role)
        return None if value is None else {"emails": value}

    def people_for_role_id(self, agent_name, role_id):
        return None


@pytest.fixture(autouse=True)
def sink(monkeypatch, real_db):
    """The real sink over the real SQLite. Audit rows and broadcasts are
    recorded, and every announcement runs synchronously, so a refusal can be
    shown to have written nothing while a successful raise writes both."""
    from services import ask_service as svc
    from services import assignment_provider
    from services import operator_queue_service as oqs
    from services import operator_resume_service as ors
    from services.rate_limiter import RateLimitResult

    audit, sent = [], []

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    class _WS:
        async def broadcast(self, message):
            sent.append(json.loads(message))

    monkeypatch.setattr(ors, "spawn_on_loop", lambda factory: asyncio.run(factory()))
    monkeypatch.setattr(ors, "spawn_ending_dispatch", lambda rows, **kw: None)
    monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda item, **kw: None)
    monkeypatch.setattr(svc, "_opted_in", lambda agent_name: True)
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_owner_email", lambda agent: None)
    monkeypatch.setattr(oqs, "_workspace_attachment",
                        lambda agent, email, **_: (f"thread-{email}", False))
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    assignment_provider.clear_provider()
    yield SimpleNamespace(audit=audit, sent=sent)
    assignment_provider.clear_provider()


def _seat(**kinds):
    from services import assignment_provider
    assignment_provider.register_provider(_Provider(kinds))


def _body(*, kind="credential", request_id=None):
    body = {"request_id": request_id or f"r-{uuid.uuid4().hex[:10]}", "type": kind,
            "title": f"Needs {NAME}", "question": "Please provide the token.", "to": "primary"}
    if kind == "credential":
        body["context"] = {"credential_name": NAME}
    return body


def _raise(agent, body=None, **kw):
    from services import ask_service
    return ask_service.raise_ask(agent, body or _body(**kw), raised_by="agent", channel="mcp")


def _rows(agent):
    from database import db
    return db.list_operator_queue_items(agent_name=agent, limit=50)


def _pending_credential_rows(agent):
    return [r for r in _rows(agent)
            if r.get("type") == "credential" and r.get("status") == "pending"]


def _resolved(row):
    stored = row["resolved_to"]
    return sorted(json.loads(stored) if isinstance(stored, str) else (stored or []))


def _door_opens(row, email=None, *, is_platform=False):
    """The secret field's own door (`_owned_ask`), asked as `email` (default:
    the row's reader). `is_platform` is the session kind: a signed-in platform
    person sees the agents they own, an outside client only what was shared."""
    from client_portal.asks import service as asks
    try:
        asks._owned_ask(row["id"], email or row["addressed_to_email"], is_platform)
    except asks.AskError:
        return False
    return True


def _refused(agent, **kw):
    from services import ask_service
    with pytest.raises(ask_service.AskRejected) as exc:
        _raise(agent, **kw)
    return exc.value


@pytest.fixture
def owner(monkeypatch):
    """Give the agent an owner with an email, in the stubbed mode. The owner is
    on the roster only when a test adds the address to `shared`."""
    from services import ask_service as svc
    email = _uniq("owner")
    monkeypatch.setattr(svc, "_owner_email", lambda agent_name: email)
    return email


# ---------------------------------------------------------------------------
# The invariant, whatever the remedy
# ---------------------------------------------------------------------------

def test_no_credential_ask_waits_on_a_reader_the_field_refuses(agent, people):
    """The defect of #4579, stated as the state that must not exist: a pending
    credential row whose only reader gets the door's 404. A refused raise and a
    raise handed to someone who can open the field both satisfy it."""
    from services import ask_service
    _seat(primary=[people["alice"]])
    try:
        _raise(agent)
    except ask_service.AskRejected:
        pass
    stuck = [r["id"] for r in _pending_credential_rows(agent) if not _door_opens(r)]
    assert stuck == [], "a credential ask is waiting on a reader who cannot open its field"


def test_control_a_reader_on_the_roster_gets_a_row_the_field_opens(agent, people, shared, sink):
    """The positive control: the same raise with the agent shared files a row,
    announces it, and the door opens for its reader."""
    shared.add(people["alice"])
    _seat(primary=[people["alice"]])
    receipt = _raise(agent)
    assert receipt["status"] == "created" and receipt["resolved"] is True
    [row] = _pending_credential_rows(agent)
    assert row["to_role"] == "primary"
    assert row["addressed_to_email"] == people["alice"] and _resolved(row) == [people["alice"]]
    assert _door_opens(row)
    assert sink.audit and sink.sent


# ---------------------------------------------------------------------------
# The chain: the seated primary, else the owner, else refused
# ---------------------------------------------------------------------------

class TestChain:
    def test_a_primary_off_the_roster_hands_the_ask_to_the_owner(
            self, agent, people, shared, owner):
        shared.add(owner)
        _seat(primary=[people["alice"]])
        receipt = _raise(agent)
        assert receipt["status"] == "created" and receipt["resolved"] is True
        assert receipt["to_role"] == "primary"
        [row] = _pending_credential_rows(agent)
        assert row["addressed_to_email"] == owner and _resolved(row) == [owner]
        assert (row.get("context") or {}).get("workspace_session_id") == f"thread-{owner}"
        assert _door_opens(row)
        assert not _door_opens(row, people["alice"]), "the seated primary was never its reader"

    def test_nobody_who_can_open_the_field_refuses_the_raise(self, agent, people, sink):
        """The seated primary is off the roster and the owner has no email."""
        _seat(primary=[people["alice"]])
        refusal = _refused(agent)
        assert (refusal.status_code, refusal.code) == (422, CODE)
        assert refusal.extra.get("role") == "primary"
        assert agent in refusal.message and "share" in refusal.message.lower()
        assert "@" not in refusal.message, "the refusal names roles, never a person"
        assert _rows(agent) == []
        assert sink.audit == [] and sink.sent == []

    def test_an_owner_off_the_roster_does_not_stand_in(self, agent, people, owner):
        _seat(primary=[people["alice"]])
        assert _refused(agent).code == CODE
        assert _rows(agent) == []

    def test_nobody_seated_and_an_owner_on_the_roster_is_filed_for_the_owner(
            self, agent, shared, owner):
        shared.add(owner)
        _raise(agent)
        [row] = _pending_credential_rows(agent)
        assert row["addressed_to_email"] == owner and _door_opens(row)

    def test_nobody_seated_and_an_owner_off_the_roster_is_refused(self, agent, owner):
        """Was a dead end too: the owner stood in and the field refused them
        (a system agent is on nobody's roster)."""
        assert _refused(agent).code == CODE
        assert _rows(agent) == []

    def test_two_seated_primaries_are_still_no_person(self, agent, people, shared):
        shared.update({people["alice"], people["bob"]})
        _seat(primary=[people["alice"], people["bob"]])
        refusal = _refused(agent)
        assert (refusal.status_code, refusal.code) == (422, "no_person")
        assert _rows(agent) == []

    def test_no_owner_and_nobody_seated_is_still_no_person(self, agent):
        refusal = _refused(agent)
        assert (refusal.status_code, refusal.code) == (422, "no_person")

    def test_a_question_to_a_primary_off_the_roster_is_unchanged(self, agent, people, owner, shared):
        """Only the credential kind has one door. A question can be answered in
        Operations by anyone with access, so it keeps its seated reader."""
        shared.add(owner)
        _seat(primary=[people["alice"]])
        receipt = _raise(agent, kind="question")
        assert receipt["status"] == "created"
        [row] = _rows(agent)
        assert row["type"] == "question" and row["addressed_to_email"] == people["alice"]

    def test_an_unreadable_roster_refuses_and_the_log_names_nobody(
            self, agent, people, owner, monkeypatch, caplog):
        import client_portal.service as portal_service

        def boom(agent_name, email, include_owned=True):
            raise RuntimeError("roster down")

        monkeypatch.setattr(portal_service, "agent_on_roster", boom)
        _seat(primary=[people["alice"]])
        with caplog.at_level(logging.INFO):
            assert _refused(agent).code == CODE
        assert _rows(agent) == []
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert agent in logged
        assert people["alice"] not in logged and owner not in logged

    def test_a_replay_of_a_row_filed_before_the_reader_lost_the_agent_is_replayed(
            self, agent, people, shared):
        shared.add(people["alice"])
        _seat(primary=[people["alice"]])
        body = _body()
        assert _raise(agent, dict(body))["status"] == "created"
        shared.clear()
        assert _raise(agent, dict(body))["status"] == "replayed"
        assert len(_rows(agent)) == 1


# ---------------------------------------------------------------------------
# The arms a stub cannot see: real accounts, ownership and shares
# ---------------------------------------------------------------------------

@pytest.fixture
def real_roster(monkeypatch, real_db, agent):
    """The real `agent_on_roster` and the real owner lookup, over a real owner
    account that owns `agent`. Returns helpers to add accounts and shares."""
    import client_portal.service as portal_service
    from db_models import UserCreate
    from services import ask_service as svc

    monkeypatch.setattr(portal_service, "agent_on_roster", _REAL_ON_ROSTER)
    monkeypatch.setattr(svc, "_owner_email", _REAL_OWNER_EMAIL)

    def account(role="user"):
        email = _uniq(role)
        real_db.create_user(UserCreate(username=email.split("@")[0], role=role, email=email))
        return email

    def suspend(email):
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import users
        with get_engine().begin() as conn:
            conn.execute(update(users).where(users.c.email == email)
                         .values(suspended_at="2026-11-14T00:00:00Z"))

    owner_email = account("creator")
    owner_username = owner_email.split("@")[0]
    real_db.register_agent_owner(agent, owner_username)

    def share(email):
        assert real_db.share_agent(agent, owner_username, email) is not None

    return SimpleNamespace(owner=owner_email, account=account, share=share, suspend=suspend)


class TestRealRoster:
    def test_nobody_seated_reaches_the_owner_through_the_owned_roster(self, agent, real_roster):
        """The default path. The owner is on the roster only through the owned
        read; a rule that asked the shared roster alone would refuse every
        owner, with a remedy (share it with yourself) the platform refuses."""
        receipt = _raise(agent)
        assert receipt["status"] == "created"
        [row] = _pending_credential_rows(agent)
        assert row["addressed_to_email"] == real_roster.owner
        assert _door_opens(row, is_platform=True)

    def test_a_shared_email_with_no_account_is_its_reader(self, agent, real_roster):
        guest = _uniq("guest")
        real_roster.share(guest)
        _seat(primary=[guest])
        _raise(agent)
        [row] = _pending_credential_rows(agent)
        assert row["addressed_to_email"] == guest
        assert _door_opens(row)

    def test_an_admin_seated_as_primary_but_off_the_roster_is_not_the_reader(
            self, agent, real_roster):
        """An admin can open any agent in Operations, and Operations cannot
        fill a credential. The Workspace roster decides, so the owner is asked."""
        from services import role_addressing
        admin = real_roster.account("admin")
        assert role_addressing.can_open_agent(agent, admin) is True, (
            "the control: the approver rule would have accepted this person")
        _seat(primary=[admin])
        _raise(agent)
        [row] = _pending_credential_rows(agent)
        assert row["addressed_to_email"] == real_roster.owner
        assert not _door_opens(row, admin, is_platform=True)

    def test_a_suspended_primary_is_skipped(self, agent, real_roster):
        """A deactivated account cannot sign in at any door, so a share made
        before the suspension does not make it a reader."""
        gone = real_roster.account("user")
        real_roster.share(gone)
        real_roster.suspend(gone)
        _seat(primary=[gone])
        _raise(agent)
        [row] = _pending_credential_rows(agent)
        assert row["addressed_to_email"] == real_roster.owner

    def test_a_suspended_owner_does_not_stand_in(self, agent, people, real_roster):
        real_roster.suspend(real_roster.owner)
        _seat(primary=[people["alice"]])
        assert _refused(agent).code == CODE
        assert _rows(agent) == []


# ---------------------------------------------------------------------------
# A seat change re-addresses a pending credential ask by the same chain
# ---------------------------------------------------------------------------

class TestReaddress:
    def _filed_for_alice(self, agent, people, shared):
        shared.add(people["alice"])
        _seat(primary=[people["alice"]])
        _raise(agent)

    def _readdress(self, agent):
        from services import ask_service as svc
        return svc.readdress_role(agent, "primary", reason=svc.ASSIGNMENT_CHANGED)

    def test_control_a_seat_that_moves_to_someone_on_the_roster_moves_the_row(
            self, agent, people, shared):
        self._filed_for_alice(agent, people, shared)
        shared.add(people["bob"])
        _seat(primary=[people["bob"]])
        assert self._readdress(agent) == {"readdressed": 1, "unchanged": 0}
        [row] = _pending_credential_rows(agent)
        assert _resolved(row) == [people["bob"]] and _door_opens(row)

    def test_a_seat_that_moves_to_someone_off_the_roster_moves_the_row_to_the_owner(
            self, agent, people, shared, owner):
        self._filed_for_alice(agent, people, shared)
        shared.add(owner)
        _seat(primary=[people["bob"]])
        assert self._readdress(agent) == {"readdressed": 1, "unchanged": 0}
        [row] = _pending_credential_rows(agent)
        assert _resolved(row) == [owner] and _door_opens(row)

    def test_a_seat_that_moves_where_nobody_can_open_the_field_leaves_the_row(
            self, agent, people, shared):
        self._filed_for_alice(agent, people, shared)
        _seat(primary=[people["bob"]])
        assert self._readdress(agent) == {"readdressed": 0, "unchanged": 1}
        [row] = _pending_credential_rows(agent)
        assert _resolved(row) == [people["alice"]]

    def test_a_question_row_still_follows_the_seat(self, agent, people, shared):
        """Re-addressing every other kind keeps the plain resolution."""
        shared.add(people["alice"])
        _seat(primary=[people["alice"]])
        _raise(agent, kind="question")
        _seat(primary=[people["bob"]])
        assert self._readdress(agent) == {"readdressed": 1, "unchanged": 0}
        [row] = _rows(agent)
        assert _resolved(row) == [people["bob"]]


# ---------------------------------------------------------------------------
# The real route (the MCP `ask_for_credential` tool proxies to it)
# ---------------------------------------------------------------------------

_ROUTES_APP = None
_PRINCIPAL = {"user": None}


def _routes_client():
    """ONE app over the real operator-queue routers for this file (the
    ent#1042 / ent#611 pattern)."""
    global _ROUTES_APP
    from fastapi.testclient import TestClient
    if _ROUTES_APP is None:
        from fastapi import FastAPI
        from routers import operator_queue as r
        app = FastAPI()
        app.include_router(r.router)
        app.include_router(r.agent_router)
        found = set()

        def walk(dependant):
            for sub in dependant.dependencies:
                if getattr(sub.call, "__name__", "") == "get_current_user":
                    found.add(sub.call)
                walk(sub)

        for route in list(r.router.routes) + list(r.agent_router.routes):
            if getattr(route, "dependant", None) is not None:
                walk(route.dependant)
        assert found, "no get_current_user dependency on the operator-queue routes"
        for call in found:
            app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
        _ROUTES_APP = app
    return TestClient(_ROUTES_APP, raise_server_exceptions=True)


class TestRaiseRoute:
    @pytest.fixture
    def route(self, agent, real_db, monkeypatch):
        from models import User
        from routers import operator_queue as r
        from services import operator_resume_service as ors
        monkeypatch.setattr(r, "_websocket_manager", None)
        # The route runs inside the test client's event loop, where the sink
        # fixture's synchronous `asyncio.run` cannot; these tests assert the
        # status and the row, the announcements are pinned at the sink above.
        monkeypatch.setattr(ors, "spawn_on_loop", lambda factory: None)
        real_owner, real_access = real_db.get_agent_owner, real_db.can_user_access_agent
        monkeypatch.setattr(real_db, "get_agent_owner",
                            lambda name: {"owner_username": "op"} if name == agent else real_owner(name))
        monkeypatch.setattr(real_db, "can_user_access_agent",
                            lambda user, name: True if name == agent else real_access(user, name))
        _PRINCIPAL["user"] = User(id=7, username="op", email="op@example.com", role="admin",
                                  mcp_scope="agent", agent_name=agent)
        return _routes_client()

    def _post(self, route, agent):
        return route.post(f"/api/agents/{agent}/operator-queue", json=_body())

    def test_the_route_answers_422_by_name_and_files_nothing(self, route, agent, people):
        _seat(primary=[people["alice"]])
        res = self._post(route, agent)
        assert res.status_code == 422, res.text
        detail = res.json()["detail"]
        assert detail["code"] == CODE and detail["role"] == "primary"
        assert "@" not in json.dumps(detail)
        assert _rows(agent) == []

    def test_control_the_route_files_it_once_the_agent_is_shared(
            self, route, agent, people, shared):
        shared.add(people["alice"])
        _seat(primary=[people["alice"]])
        res = self._post(route, agent)
        assert res.status_code == 201, res.text
        assert res.json()["to_role"] == "primary" and res.json()["resolved"] is True
        assert len(_rows(agent)) == 1
