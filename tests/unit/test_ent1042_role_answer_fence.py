"""An answer to an ask addressed to a canon role is fenced to the people
seated in it (abilityai/trinity-enterprise#1042).

An ask raised `to: role:<id>` (ent#1007) is delivered only to the people the
role resolved to at raise time (`resolved_to`), but the Operations answer door
took an answer from anyone with access to the agent. The one ending rule,
`ask_service.may_end`, now fences such an answer to that snapshot — every door
reaches it — and refuses with 403 `not_addressee` and a remedy that names the
role's label, never a person.

Rulings pinned here: an admin without the seat may cancel the ask, never answer
it; cancel is NOT narrowed; the other kinds are unchanged (`to: approver` is
fenced by ent#1053, `test_ent1053_approver_answer_fence.py`); an empty snapshot
fails closed.

Harness: the unit island's per-process SQLite, the real sink, the REAL
operator-queue router; rows are raised through the sink with a fake assignments
provider so `to_role` / `resolved_to` are what production writes. Agent names
and emails are unique per test.
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

ROLE = "head-of-sales"
LABEL = "Head of sales"
REMEDY = (f"This ask was addressed to the {LABEL} role; only a person seated in it can "
          "decide it. Ask someone seated in that role to answer it from their Inbox, or "
          "have it cancelled so the agent can ask again.")


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def agent():
    return f"agent-1042-{uuid.uuid4().hex[:6]}"


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
    """A fake assignments provider: `roles` maps a canon role id to the seated
    emails; `kinds` the same for the four kinds."""

    def __init__(self, roles=None, kinds=None):
        self.roles = roles or {}
        self.kinds = kinds or {}

    def assignment_for(self, agent_name, triggered_by):
        return None

    def people_for(self, agent_name, role):
        value = self.kinds.get(role)
        return None if value is None else {"emails": value}

    def people_for_role_id(self, agent_name, role_id):
        value = self.roles.get(role_id)
        return None if value is None else {"emails": value}


def _provide(**kw):
    from services import assignment_provider
    assignment_provider.register_provider(_Provider(**kw))


def _raise(agent, *, to=f"role:{ROLE}", kind="approval"):
    """One ask through the REAL sink; returns the stored row."""
    from database import db
    from services import ask_service
    rid = f"r-{uuid.uuid4().hex[:10]}"
    body = {"request_id": rid, "type": kind, "title": "Pay invoice",
            "question": "Release the payment?", "to": to}
    if kind == "approval":
        body["options"] = ["approve", "reject"]
        body["proposal"] = {"pay": 500, "to": f"vendor-{rid}"}
    ask_service.raise_ask(agent, body, raised_by="agent", channel="mcp")
    rows = [r for r in db.list_operator_queue_items(agent_name=agent, limit=50)
            if r["request_id"] == rid]
    assert len(rows) == 1, rows
    return rows[0]


def _seated_row(agent, people, **kw):
    _provide(roles={ROLE: [people["alice"], people["bob"]]})
    row = _raise(agent, **kw)
    assert row["to_role"] == f"role:{ROLE}"
    assert sorted(json.loads(row["resolved_to"]) if isinstance(row["resolved_to"], str)
                  else row["resolved_to"]) == sorted([people["alice"], people["bob"]])
    return row


def _actor(email, role="creator"):
    from models import User
    from services import ask_service
    user = User(id=11, username=email.split("@")[0], email=email, role=role)
    return ask_service.Actor(email=email, user=user)


# ---------------------------------------------------------------------------
# The rule itself — `may_end` over rows the sink wrote
# ---------------------------------------------------------------------------

class TestMayEnd:
    def test_a_seated_person_may_answer(self, agent, people):
        from services.ask_service import may_end
        row = _seated_row(agent, people)
        assert may_end(row, _actor(people["alice"])) is True
        assert may_end(row, _actor(people["bob"].upper())) is True   # casefolded

    def test_a_person_outside_the_seat_may_not_answer(self, agent, people):
        from services.ask_service import may_end
        row = _seated_row(agent, people)
        assert may_end(row, _actor(people["carol"])) is False

    def test_an_admin_without_the_seat_may_not_answer(self, agent, people):
        from services.ask_service import may_end
        row = _seated_row(agent, people)
        assert may_end(row, _actor(people["carol"], role="admin")) is False

    def test_cancel_is_not_narrowed(self, agent, people):
        """Ruling T4: the answer is fenced, the cancel keeps today's rule."""
        from services.ask_service import may_end
        row = _seated_row(agent, people)
        assert may_end(row, _actor(people["carol"]), cancelling=True) is True

    def test_a_role_addressed_question_is_fenced_too(self, agent, people):
        from services.ask_service import may_end
        row = _seated_row(agent, people, kind="question")
        assert may_end(row, _actor(people["carol"])) is False
        assert may_end(row, _actor(people["alice"])) is True

    @pytest.mark.parametrize("snapshot", [None, "", "[]", "not json", '"x@example.com"', "{}"])
    def test_an_empty_or_unreadable_snapshot_fails_closed(self, agent, people, snapshot):
        from services.ask_service import may_end
        row = {**_seated_row(agent, people), "resolved_to": snapshot}
        assert may_end(row, _actor(people["alice"])) is False
        assert may_end(row, _actor(people["alice"], role="admin")) is False

    @pytest.mark.parametrize("kind_to", ["primary", "viewer"])
    def test_the_four_kinds_are_unchanged(self, agent, people, kind_to):
        """The other kinds were never fenced: anyone the caller let through may
        answer, as before. `approver` left this list when ent#1053 fenced it to
        its seat (pinned in test_ent1053_approver_answer_fence.py)."""
        from services.ask_service import may_end
        _provide(kinds={kind_to: [people["alice"]]})
        row = _raise(agent, to=kind_to)
        assert row["to_role"] == kind_to
        assert may_end(row, _actor(people["carol"])) is True

    def test_a_row_with_no_to_role_is_unfenced(self, people):
        from services.ask_service import may_end
        assert may_end({"id": "x", "type": "approval", "resolved_to": []},
                       _actor(people["carol"])) is True


# ---------------------------------------------------------------------------
# The real Operations route
# ---------------------------------------------------------------------------

_ROUTES_APP = None
_PRINCIPAL = {"user": None}


def _routes_client():
    """ONE app over the real operator-queue routers for this file; the
    principal is switched per test. `get_current_user` is overridden by
    walking the routes' own dependant trees (the ent#611 pattern)."""
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


class TestRoute:
    @pytest.mark.parametrize("principal", PERSON)
    def test_a_seated_person_answers(self, routes, agent, people, principal):
        uid = _seated_row(agent, people)["id"]
        routes.as_(people["alice"].upper(), **principal)
        res = routes.client.post(f"/api/operator-queue/{uid}/respond", json={"response": "approve"})
        assert res.status_code == 200, res.text
        row = routes.db.get_operator_queue_item(uid)
        assert (row["status"], row["disposition"]) == ("responded", "answered")
        assert [e.disposition for e in routes.sink.events] == ["answered"]

    @pytest.mark.parametrize("principal", PERSON)
    def test_a_shared_person_outside_the_seat_is_refused(self, routes, agent, people, principal):
        uid = _seated_row(agent, people)["id"]
        routes.as_(people["carol"], **principal)
        res = routes.client.post(f"/api/operator-queue/{uid}/respond", json={"response": "approve"})
        assert _refused(res)["message"] == REMEDY
        _untouched(routes, uid)

    def test_an_admin_without_the_seat_cannot_answer_but_may_cancel(self, routes, agent, people):
        uid = _seated_row(agent, people)["id"]
        routes.as_(people["carol"], role="admin")
        res = routes.client.post(f"/api/operator-queue/{uid}/respond", json={"response": "approve"})
        assert _refused(res)["message"] == REMEDY
        _untouched(routes, uid)
        res = routes.client.post(f"/api/operator-queue/{uid}/cancel")
        assert res.status_code == 200, res.text
        assert routes.db.get_operator_queue_item(uid)["disposition"] == "cancelled"

    def test_an_admin_with_no_email_cannot_answer(self, routes, agent, people):
        uid = _seated_row(agent, people)["id"]
        from models import User
        _PRINCIPAL["user"] = User(id=22, username="admin", email=None, role="admin")
        res = routes.client.post(f"/api/operator-queue/{uid}/respond", json={"response": "approve"})
        _refused(res)
        _untouched(routes, uid)

    def test_a_role_addressed_question_is_refused_as_an_ask(self, routes, agent, people):
        uid = _seated_row(agent, people, kind="question")["id"]
        routes.as_(people["carol"])
        res = routes.client.post(f"/api/operator-queue/{uid}/respond",
                                 json={"response": "yes, go ahead"})
        message = _refused(res)["message"]
        assert message == REMEDY and "approval" not in message
        _untouched(routes, uid)

    def test_a_non_seated_person_may_still_cancel_and_bulk_cancel(self, routes, agent, people):
        """Ruling T4 — pinned so a later narrowing is a deliberate change."""
        one = _seated_row(agent, people)["id"]
        two = _seated_row(agent, people)["id"]
        routes.as_(people["carol"])
        assert routes.client.post(f"/api/operator-queue/{one}/cancel").status_code == 200
        res = routes.client.post("/api/operator-queue/bulk-cancel", json={"ids": [two]})
        assert res.status_code == 200, res.text
        assert routes.db.get_operator_queue_item(two)["disposition"] == "cancelled"

    # `approver` dropped: fenced to its seat by ent#1053.
    @pytest.mark.parametrize("kind_to", ["primary"])
    def test_kind_addressed_asks_stay_open_to_anyone_shared(self, routes, agent, people, kind_to):
        _provide(kinds={kind_to: [people["alice"]]})
        uid = _raise(agent, to=kind_to)["id"]
        routes.as_(people["carol"])
        res = routes.client.post(f"/api/operator-queue/{uid}/respond", json={"response": "approve"})
        assert res.status_code == 200, res.text


# ---------------------------------------------------------------------------
# The Workspace door still works for the seated (its own fence is unchanged)
# ---------------------------------------------------------------------------

class TestPortalDoor:
    def test_seated_answers_and_non_seated_reads_not_found(self, agent, people, real_db):
        from client_portal.asks import service
        uid = _seated_row(agent, people)["id"]
        with pytest.raises(service.AskError) as exc:
            service.answer_ask(uid, people["carol"], False, "approve", None)
        assert exc.value.status_code == 404
        service.answer_ask(uid, people["bob"], False, "approve", None)
        assert real_db.get_operator_queue_item(uid)["disposition"] == "answered"


# ---------------------------------------------------------------------------
# Every catch site names the role, never a person
# ---------------------------------------------------------------------------

def _raise_gate(agent, people):
    """A gated-skill approval whose approver is a canon role (ent#848 shape)."""
    from database import db
    from services import ask_service
    rid = f"gate-{uuid.uuid4().hex[:10]}"
    body = {"request_id": rid, "type": "approval", "title": "Run the skill",
            "question": "Allow the run?", "options": ["approve", "reject"],
            "proposal": {"skill": f"pay-{rid}"}}
    ask_service.raise_ask(agent, body, raised_by="gate", channel="gate",
                          addressees=[people["alice"]], to_role_override=f"role:{ROLE}")
    rows = [r for r in db.list_operator_queue_items(agent_name=agent, limit=50)
            if r["request_id"] == rid]
    assert len(rows) == 1 and rows[0]["to_role"] == f"role:{ROLE}", rows
    return rows[0]


class TestRefusalWording:
    def test_a_gate_cancel_refusal_names_the_role(self, routes, agent, people):
        """Cancel is unchanged (gate rows only); its refusal now names the role."""
        uid = _raise_gate(agent, people)["id"]
        routes.as_(people["carol"])
        res = routes.client.post(f"/api/operator-queue/{uid}/cancel")
        assert _refused(res)["message"] == (
            f"This ask was addressed to the {LABEL} role; only a person seated in it "
            "or an admin can cancel it.")
        _untouched(routes, uid)

    def test_the_workspace_sink_layer_names_the_role(self, agent, people, real_db, monkeypatch):
        """The portal's own addressee fence answers 404 first; this is the
        sink's second layer, reached by holing that outer fence."""
        from client_portal.asks import service
        row = _seated_row(agent, people)
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
        uid = _seated_row(agent, people)["id"]
        routes.as_(people["carol"], role="admin")
        view = self._addressed(routes, uid)
        assert view["can_decide"] is False
        assert sorted(view["waiting_on"]) == sorted([people["alice"], people["bob"]])

    def test_a_seated_person_may_decide(self, routes, agent, people):
        uid = _seated_row(agent, people, kind="question")["id"]
        routes.as_(people["bob"].upper())
        assert self._addressed(routes, uid)["can_decide"] is True

    def test_the_list_carries_the_same_view(self, routes, agent, people):
        uid = _seated_row(agent, people)["id"]
        routes.as_(people["carol"])
        res = routes.client.get("/api/operator-queue", params={"agent_name": agent})
        assert res.status_code == 200, res.text
        item = {i["id"]: i for i in res.json()["items"]}[uid]
        assert item["addressed"]["can_decide"] is False

    # `approver` dropped: a seated approver ask carries the view (ent#1053).
    @pytest.mark.parametrize("kind_to", ["primary"])
    def test_kind_addressed_asks_carry_none(self, routes, agent, people, kind_to):
        _provide(kinds={kind_to: [people["alice"]]})
        uid = _raise(agent, to=kind_to)["id"]
        routes.as_(people["carol"])
        assert self._addressed(routes, uid) is None

    @pytest.mark.parametrize("snapshot", [None, "[]", "not json"])
    def test_an_empty_snapshot_reads_as_nobodys_never_as_open(self, agent, people, snapshot):
        """The sink refuses everyone, so the card must not offer the control."""
        from services.ask_service import addressed_view, may_end
        row = {**_seated_row(agent, people), "resolved_to": snapshot}
        actor = _actor(people["alice"], role="admin")
        assert addressed_view(row, actor) == {"waiting_on": [], "can_decide": False}
        assert may_end(row, actor) is False
