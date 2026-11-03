"""An answer to an ask addressed `to: approver` is fenced to the approver seat
(abilityai/trinity-enterprise#1053).

ent#1042 fenced the answer to an ask addressed to a canon role id to the people
seated in it and left the `approver` kind open: anyone with access to the agent
could answer an ask the agent had addressed to the approver. The one ending
rule, `ask_service.may_end`, now fences that answer to the raise-time snapshot
(`resolved_to`) too, and every door reaches it.

Rulings pinned here: the carve-out is by snapshot — a legacy `approver` row
with an empty or unreadable snapshot stays answerable by anyone with access,
as before (a new one cannot be empty: an unfilled seat refuses the raise); an
admin outside the seat may cancel but never answer; the cancel is not narrowed;
the refusal names the approver role through `role_message`; the card's
`addressed` view comes from the same predicate.

Harness: the unit island's per-process SQLite, the real sink and the REAL
operator-queue router (the ent#1042 shape); rows are raised through the sink
with a fake assignments provider so `to_role` / `resolved_to` are what
production writes.
"""
from __future__ import annotations

import json
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
import client_portal.asks.service  # noqa: E402,F401

REMEDY = ("This ask was addressed to the approver role; only a person seated in it can "
          "decide it. Ask someone seated in that role to answer it from their Inbox, or "
          "have it cancelled so the agent can ask again.")

# What a legacy row may store where today's sink always writes a list. An
# unparseable value cannot be served by any route (the row reader decodes it
# and raises, before and after this change), so it is pinned at the predicate.
STORED_EMPTY = [pytest.param(None, id="null"), pytest.param("[]", id="empty-list")]
EMPTY_SNAPSHOTS = STORED_EMPTY + [pytest.param("not json", id="unparseable")]


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def agent():
    return f"agent-1053-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def people():
    return {"alice": _uniq("alice"), "bob": _uniq("bob"), "carol": _uniq("carol")}


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    """Everyone can open every agent (the ent#881 reach check at raise)."""
    import client_portal.service as portal_service
    monkeypatch.setattr(portal_service, "agent_on_roster",
                        lambda agent_name, email, include_owned=True: True)


@pytest.fixture(autouse=True)
def sink(monkeypatch, real_db):
    """The real sink over the real SQLite; audit rows, broadcasts and ending
    events recorded so a refusal can be shown to have written nothing."""
    from services import ask_service as svc
    from services import assignment_provider
    from services import operator_queue_service as oqs
    from services import operator_resume_service as ors
    from services.rate_limiter import RateLimitResult

    audit, sent, events = [], [], []

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    class _WS:
        async def broadcast(self, message):
            sent.append(json.loads(message))

    monkeypatch.setattr(ors, "spawn_on_loop", lambda factory: None)
    monkeypatch.setattr(ors, "spawn_ending_dispatch", lambda rows, **kw: None)
    monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda item, **kw: None)
    monkeypatch.setattr(svc, "_opted_in", lambda agent_name: True)
    monkeypatch.setattr(svc, "_observers", [events.append])
    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_owner_email", lambda agent: None)
    monkeypatch.setattr(oqs, "_workspace_attachment",
                        lambda agent, email, **_: (f"thread-{email}", False))
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    assignment_provider.clear_provider()
    yield SimpleNamespace(audit=audit, sent=sent, events=events)
    assignment_provider.clear_provider()


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


def _approver_row(agent, people, *, kind="approval"):
    """One `to: approver` ask through the REAL sink, the seat being alice and
    bob; returns the stored row."""
    from database import db
    from services import assignment_provider, ask_service
    assignment_provider.register_provider(
        _Provider({"approver": [people["alice"], people["bob"]]}))
    rid = f"r-{uuid.uuid4().hex[:10]}"
    body = {"request_id": rid, "type": kind, "title": "Pay invoice",
            "question": "Release the payment?", "to": "approver"}
    if kind == "approval":
        body["options"] = ["approve", "reject"]
        body["proposal"] = {"pay": 500, "to": f"vendor-{rid}"}
    ask_service.raise_ask(agent, body, raised_by="agent", channel="mcp")
    rows = [r for r in db.list_operator_queue_items(agent_name=agent, limit=50)
            if r["request_id"] == rid]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["to_role"] == "approver"
    stored = row["resolved_to"]
    assert sorted(json.loads(stored) if isinstance(stored, str) else stored) == sorted(
        [people["alice"], people["bob"]])
    return row


def _legacy(row, snapshot):
    """Write a legacy snapshot onto a stored row, as a pre-snapshot row reads."""
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE operator_queue SET resolved_to = :s WHERE id = :id"),
                     {"s": snapshot, "id": row["id"]})
    return row["id"]


def _actor(email, role="creator"):
    from models import User
    from services import ask_service
    user = User(id=11, username=email.split("@")[0], email=email, role=role)
    return ask_service.Actor(email=email, user=user)


# ---------------------------------------------------------------------------
# The real Operations route (door 1; the MCP respond tool proxies to it)
# ---------------------------------------------------------------------------

_ROUTES_APP = None
_PRINCIPAL = {"user": None}


def _routes_client():
    """ONE app over the real operator-queue routers for this file; the
    principal is switched per test (the ent#1042 / ent#611 pattern)."""
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


@pytest.fixture
def routes(real_db, sink, agent, monkeypatch):
    """The real routers and sink. A non-admin principal is SHARED the agent, so
    a refusal is the seat fence and never the access check (both are 403)."""
    from models import User
    from routers import operator_queue as r
    monkeypatch.setattr(r, "_websocket_manager", None)
    real_accessible = r._accessible_set
    monkeypatch.setattr(r, "_accessible_set",
                        lambda u: real_accessible(u) if u.role == "admin" else {agent})

    def as_(email, role="creator", **kw):
        _PRINCIPAL["user"] = User(id=21, username=email.split("@")[0], email=email,
                                  role=role, **kw)

    return SimpleNamespace(client=_routes_client(), as_=as_, db=real_db, sink=sink)


PERSON = [pytest.param({}, id="jwt"), pytest.param({"mcp_scope": "user"}, id="user-key")]


def _respond(routes, uid, response="approve"):
    return routes.client.post(f"/api/operator-queue/{uid}/respond", json={"response": response})


def _refused(res):
    assert res.status_code == 403, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "not_addressee", detail
    assert "@" not in json.dumps(detail)
    return detail


def _untouched(routes, uid):
    row = routes.db.get_operator_queue_item(uid)
    assert row["status"] == "pending"
    assert row["response"] is None and row["disposition"] is None
    assert routes.sink.audit == [] and routes.sink.events == []
    assert routes.sink.sent == []


def _answered(routes, uid):
    row = routes.db.get_operator_queue_item(uid)
    assert (row["status"], row["disposition"]) == ("responded", "answered")
    assert [e.disposition for e in routes.sink.events] == ["answered"]


class TestRoute:
    @pytest.mark.parametrize("principal", PERSON)
    def test_a_shared_person_outside_the_seat_is_refused(self, routes, agent, people, principal):
        uid = _approver_row(agent, people)["id"]
        routes.as_(people["carol"], **principal)
        assert _refused(_respond(routes, uid))["message"] == REMEDY
        _untouched(routes, uid)

    @pytest.mark.parametrize("principal", PERSON)
    def test_a_seated_person_answers(self, routes, agent, people, principal):
        uid = _approver_row(agent, people)["id"]
        routes.as_(people["alice"].upper(), **principal)   # casefolded
        res = _respond(routes, uid)
        assert res.status_code == 200, res.text
        _answered(routes, uid)

    def test_an_approver_question_is_fenced_too(self, routes, agent, people):
        uid = _approver_row(agent, people, kind="question")["id"]
        routes.as_(people["carol"])
        message = _refused(_respond(routes, uid, "yes, go ahead"))["message"]
        assert message == REMEDY and "approval" not in message
        _untouched(routes, uid)

    def test_an_admin_outside_the_seat_cannot_answer_but_may_cancel(self, routes, agent, people):
        uid = _approver_row(agent, people)["id"]
        routes.as_(people["carol"], role="admin")
        assert _refused(_respond(routes, uid))["message"] == REMEDY
        _untouched(routes, uid)
        res = routes.client.post(f"/api/operator-queue/{uid}/cancel")
        assert res.status_code == 200, res.text
        assert routes.db.get_operator_queue_item(uid)["disposition"] == "cancelled"

    def test_cancel_is_not_narrowed(self, routes, agent, people):
        """Ruling (taste 2): any person with access may still cancel or
        bulk-cancel, so an ask nobody seated will answer can be cleared."""
        one = _approver_row(agent, people)["id"]
        two = _approver_row(agent, people)["id"]
        routes.as_(people["carol"])
        assert routes.client.post(f"/api/operator-queue/{one}/cancel").status_code == 200
        res = routes.client.post("/api/operator-queue/bulk-cancel", json={"ids": [two]})
        assert res.status_code == 200, res.text
        assert routes.db.get_operator_queue_item(two)["disposition"] == "cancelled"

    @pytest.mark.parametrize("snapshot", STORED_EMPTY)
    def test_a_legacy_row_with_no_snapshot_stays_answerable(self, routes, agent, people, snapshot):
        """The Ruling's carve-out: an empty snapshot is NOT 'nobody may answer'
        for `approver` (unlike a role id) — anyone with access, as before."""
        uid = _legacy(_approver_row(agent, people), snapshot)
        routes.as_(people["carol"])
        res = _respond(routes, uid)
        assert res.status_code == 200, res.text
        _answered(routes, uid)


# ---------------------------------------------------------------------------
# The Workspace door
# ---------------------------------------------------------------------------

class TestPortalDoor:
    def test_a_seated_client_answers(self, agent, people, real_db):
        from client_portal.asks import service
        uid = _approver_row(agent, people)["id"]
        service.answer_ask(uid, people["bob"], False, "approve", None)
        assert real_db.get_operator_queue_item(uid)["disposition"] == "answered"

    def test_the_workspace_sink_layer_names_the_role(self, agent, people, real_db, monkeypatch):
        """The portal's own addressee fence answers first; this is the sink's
        second layer, reached by holing that outer fence."""
        from client_portal.asks import service
        row = _approver_row(agent, people)
        monkeypatch.setattr(service, "_owned_ask", lambda item_id, *a, **k: dict(row))
        with pytest.raises(service.AskError) as exc:
            service.answer_ask(row["id"], people["carol"], False, "approve", None)
        assert (exc.value.status_code, exc.value.code, exc.value.detail) == (
            403, "not_addressee", REMEDY)
        assert real_db.get_operator_queue_item(row["id"])["status"] == "pending"


# ---------------------------------------------------------------------------
# The card goes read-only before the click (`addressed`, the ent#980 path)
# ---------------------------------------------------------------------------

class TestProjection:
    def _addressed(self, routes, uid):
        res = routes.client.get(f"/api/operator-queue/{uid}")
        assert res.status_code == 200, res.text
        return res.json()["addressed"]

    def test_a_person_outside_the_seat_reads_whose_it_is(self, routes, agent, people):
        uid = _approver_row(agent, people)["id"]
        routes.as_(people["carol"], role="admin")
        view = self._addressed(routes, uid)
        assert view["can_decide"] is False
        assert sorted(view["waiting_on"]) == sorted([people["alice"], people["bob"]])

    def test_a_seated_person_may_decide(self, routes, agent, people):
        uid = _approver_row(agent, people)["id"]
        routes.as_(people["bob"].upper())
        assert self._addressed(routes, uid)["can_decide"] is True

    @pytest.mark.parametrize("snapshot", STORED_EMPTY)
    def test_a_legacy_row_with_no_snapshot_carries_none(self, routes, agent, people, snapshot):
        """Open to anyone, so no 'waiting on' card — the sink's own answer."""
        uid = _legacy(_approver_row(agent, people), snapshot)
        routes.as_(people["carol"])
        assert self._addressed(routes, uid) is None

    @pytest.mark.parametrize("snapshot", EMPTY_SNAPSHOTS)
    def test_the_predicate_keeps_every_empty_snapshot_open(self, agent, people, snapshot):
        from services.ask_service import addressed_view, may_end
        row = {**_approver_row(agent, people), "resolved_to": snapshot}
        actor = _actor(people["carol"])
        assert may_end(row, actor) is True
        assert addressed_view(row, actor) is None
