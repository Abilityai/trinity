"""An ask addressed `to: approver` reaches only seated approvers who can open
the agent, and is refused by name when none can (abilityai/trinity#4501).

The answer to an `approver` ask is fenced to the people the raise resolved
(`resolved_to`, abilityai/trinity-enterprise#1053). The raise itself applied no
reach check to the `approver` kind, so a seated approver the agent is not
shared with was recorded as the only person who may answer an ask they can
never see: nobody could decide it. An ask to a canon role (`role:<id>`) and a
gated-skill approval already refuse that case; this file pins the same rule
for the kind.

Harness: the unit island's per-process SQLite, the REAL sink and the REAL
operator-queue routers. People have no platform account unless a test gives
them one, so reach is the Workspace roster (`client_portal.service.
agent_on_roster`), stubbed with the set of emails the agent is shared with.
Announcements run synchronously (the ent#1095 form), so "nothing was written"
has a positive control.
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


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def agent():
    return f"agent-4501-{uuid.uuid4().hex[:6]}"


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


def _body(to="approver", *, kind="approval", request_id=None):
    body = {"request_id": request_id or f"r-{uuid.uuid4().hex[:10]}", "type": kind,
            "title": "Pay invoice", "question": "Release the payment?", "to": to}
    if kind == "approval":
        body["options"] = ["approve", "reject"]
    return body


def _raise(agent, body=None, **kw):
    from services import ask_service
    return ask_service.raise_ask(agent, body or _body(**kw), raised_by="agent", channel="mcp")


def _rows(agent):
    from database import db
    return db.list_operator_queue_items(agent_name=agent, limit=50)


def _resolved(row):
    stored = row["resolved_to"]
    return sorted(json.loads(stored) if isinstance(stored, str) else (stored or []))


def _actor(email, role="creator"):
    from models import User
    from services import ask_service
    return ask_service.Actor(email=email,
                             user=User(id=11, username=email.split("@")[0], email=email, role=role))


def _refused(agent, **kw):
    from services import ask_service
    with pytest.raises(ask_service.AskRejected) as exc:
        _raise(agent, **kw)
    return exc.value


# ---------------------------------------------------------------------------
# The sink
# ---------------------------------------------------------------------------

class TestRaise:
    def test_a_seated_approver_who_cannot_open_the_agent_refuses_the_raise(
            self, agent, people, sink):
        _seat(approver=[people["alice"]])
        refusal = _refused(agent)
        assert (refusal.status_code, refusal.code) == (422, CODE)
        assert refusal.extra.get("role") == "approver"
        assert "share" in refusal.message.lower()
        assert agent in refusal.message
        assert "@" not in refusal.message, "the refusal names the role, never a person"
        assert "primary" not in refusal.message and "operator" not in refusal.message, (
            "the remedy is to share the agent, not to send the approval to someone else")
        assert _rows(agent) == []
        assert sink.audit == [] and sink.sent == []

    def test_control_a_reachable_approver_is_filed_and_announced(
            self, agent, people, shared, sink):
        """The positive control of the test above: the same raise, with the
        agent shared, writes a row, an audit entry and a broadcast."""
        shared.add(people["alice"])
        _seat(approver=[people["alice"]])
        receipt = _raise(agent)
        assert receipt["status"] == "created" and receipt["resolved"] is True
        [row] = _rows(agent)
        assert _resolved(row) == [people["alice"]]
        assert sink.audit and sink.sent

    def test_only_the_approvers_who_can_open_the_agent_are_recorded(
            self, agent, people, shared):
        from services import ask_service
        shared.add(people["alice"])
        _seat(approver=[people["alice"], people["bob"]])
        _raise(agent)
        [row] = _rows(agent)
        assert row["to_role"] == "approver"
        assert _resolved(row) == [people["alice"]]
        # One reader is the single addressee and gets the Main attachment.
        assert row["addressed_to_email"] == people["alice"]
        assert (row.get("context") or {}).get("workspace_session_id") == f"thread-{people['alice']}"
        # The fence follows the recorded audience.
        assert ask_service.may_end(row, _actor(people["alice"])) is True
        assert ask_service.may_end(row, _actor(people["bob"])) is False

    def test_every_reachable_approver_is_recorded(self, agent, people, shared):
        shared.update({people["alice"], people["bob"]})
        _seat(approver=[people["alice"], people["bob"]])
        _raise(agent)
        [row] = _rows(agent)
        assert _resolved(row) == sorted([people["alice"], people["bob"]])
        assert row["addressed_to_email"] is None

    def test_an_admin_approver_the_agent_is_not_shared_with_is_reached(
            self, agent, people, monkeypatch):
        """A platform account is asked `can_user_access_agent` (owner, shared
        or admin): an admin decides in Operations without a share."""
        from services import role_addressing
        real_user = role_addressing.db.get_user_by_email
        real_access = role_addressing.db.can_user_access_agent
        monkeypatch.setattr(
            role_addressing.db, "get_user_by_email",
            lambda email: {"username": "adm-4501"} if email == people["alice"] else real_user(email))
        monkeypatch.setattr(
            role_addressing.db, "can_user_access_agent",
            lambda username, name: True if username == "adm-4501" else real_access(username, name))
        _seat(approver=[people["alice"]])
        assert _raise(agent)["status"] == "created"
        [row] = _rows(agent)
        assert _resolved(row) == [people["alice"]]

    def test_a_lookup_that_fails_counts_as_cannot_open(
            self, agent, people, shared, monkeypatch):
        from services import role_addressing
        shared.add(people["alice"])
        _seat(approver=[people["alice"]])

        def boom(email):
            raise RuntimeError("users table unreadable")

        monkeypatch.setattr(role_addressing.db, "get_user_by_email", boom)
        assert _refused(agent).code == CODE
        assert _rows(agent) == []

    def test_an_unfilled_seat_keeps_its_own_refusal(self, agent):
        _seat(approver=[])
        refusal = _refused(agent)
        assert (refusal.status_code, refusal.code) == (422, "role_unassigned")
        assert refusal.message == ("Nobody fills the approver role for this agent yet; address "
                                   "the ask to primary or operator.")

    @pytest.mark.parametrize("kind", ["primary", "viewer"])
    def test_the_other_kinds_are_not_filtered(self, agent, people, kind):
        """Their answer is not fenced to the seat, so the owner or an admin can
        still decide: a person who cannot open the agent is recorded as before."""
        _seat(**{kind: [people["alice"]]})
        assert _raise(agent, to=kind, kind="question")["status"] == "created"
        [row] = _rows(agent)
        assert row["to_role"] == kind and _resolved(row) == [people["alice"]]

    def test_a_replay_of_an_ask_already_filed_is_not_refused(self, agent, people, shared):
        shared.add(people["alice"])
        _seat(approver=[people["alice"]])
        body = _body(request_id="r-4501-replay")
        assert _raise(agent, body)["status"] == "created"
        shared.clear()
        again = _raise(agent, body)
        assert again["status"] == "replayed"
        assert len(_rows(agent)) == 1

    def test_the_refusal_is_logged_without_a_person(self, agent, people, caplog):
        _seat(approver=[people["alice"]])
        with caplog.at_level(logging.WARNING, logger="services.ask_service"):
            _refused(agent)
        lines = [r.getMessage() for r in caplog.records if r.name == "services.ask_service"]
        assert any(agent in line and "approver" in line for line in lines), lines
        assert not any("@" in line for line in lines), lines


# ---------------------------------------------------------------------------
# Re-addressing reads the same rule
# ---------------------------------------------------------------------------

class TestReaddress:
    def _readdress(self, agent):
        from services import ask_service as svc
        return svc.readdress_role(agent, "approver", reason=svc.ASSIGNMENT_CHANGED)

    def test_a_seat_that_moves_to_someone_who_cannot_open_the_agent_leaves_the_row(
            self, agent, people, shared):
        shared.add(people["alice"])
        _seat(approver=[people["alice"]])
        _raise(agent)
        _seat(approver=[people["bob"]])
        assert self._readdress(agent) == {"readdressed": 0, "unchanged": 1}
        [row] = _rows(agent)
        assert _resolved(row) == [people["alice"]]

    def test_control_a_seat_that_moves_to_someone_who_can_open_it_moves_the_row(
            self, agent, people, shared):
        shared.update({people["alice"], people["bob"]})
        _seat(approver=[people["alice"]])
        _raise(agent)
        _seat(approver=[people["bob"]])
        assert self._readdress(agent) == {"readdressed": 1, "unchanged": 0}
        [row] = _rows(agent)
        assert _resolved(row) == [people["bob"]]


# ---------------------------------------------------------------------------
# The real route (the MCP `ask_operator` tool proxies to it)
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
        _seat(approver=[people["alice"]])
        res = self._post(route, agent)
        assert res.status_code == 422, res.text
        detail = res.json()["detail"]
        assert detail["code"] == CODE and detail["role"] == "approver"
        assert "@" not in json.dumps(detail)
        assert _rows(agent) == []

    def test_control_the_route_files_it_once_the_agent_is_shared(
            self, route, agent, people, shared):
        shared.add(people["alice"])
        _seat(approver=[people["alice"]])
        res = self._post(route, agent)
        assert res.status_code == 201, res.text
        assert res.json()["to_role"] == "approver" and res.json()["resolved"] is True
        assert len(_rows(agent)) == 1
