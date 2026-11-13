"""The admin verbs for a stranded card, and the named refusal on both doors
(abilityai/trinity-enterprise#1096, OSS routes — C2).

* `POST /api/operator-queue/{id}/readdress {to: rule|admin, reason}` — an
  instance admin who is a person (agent, connector, `ops` and non-admin
  principals refused) re-addresses a pending card BY THE RULE THAT RAISED IT
  (a registered platform family's resolver, or the role a role-addressed row
  went to), or moves a registered family's card to the admin seat. No free
  person picker. Unknown family → 422; unchanged → 409 naming who it waits
  for; the audit row carries the admin and `has_reason`, never an address.
* `POST /{id}/cancel` — an admin ending a seat-fenced card they are not
  addressed on must give a reason (422 `reason_required`).
* `POST /bulk-cancel` — reports `skipped_fenced`.
* `POST /{id}/respond` and `/cancel` — a 403 `not_addressee` carries
  `waiting_on` + `seat`; an admin is told the two verbs; an answer read before
  a re-address is a 409 `readdressed` naming who it waits for now.
* The Workspace door carries the same structured fields and never the admin
  verbs (R11).

Harness: the real operator-queue router over the unit island's SQLite, the
principal swapped per test (`models.User`, the real shape the gates read).
"""
from __future__ import annotations

import asyncio
import os
import threading
import sys
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

import services.ask_service  # noqa: E402,F401
import services.operator_queue_service  # noqa: E402,F401
import services.operator_resume_service  # noqa: E402,F401
import services.skill_gate_service  # noqa: E402,F401

SEAT = "workspace_decider_seat"
REASON = "workspace_operators_reason"
CONSENT = "gate-project-agent-"


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def agent():
    return f"agent-1096r-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def people():
    return {"alice": _uniq("alice"), "bob": _uniq("bob"), "carol": _uniq("carol")}


class _Audit:
    def __init__(self):
        self.rows = []

    async def log(self, **kw):
        self.rows.append(kw)
        return "evt"

    def actions(self, action):
        return [r for r in self.rows if r.get("event_action") == action]


class _WS:
    async def broadcast(self, message):
        return None


class _Provider:
    def __init__(self):
        self.roles = {}

    def assignment_for(self, *a, **k):
        return None

    def people_for(self, agent_name, role):
        return {"emails": list(self.roles.get(role, []))}


@dataclass
class Decider:
    """The shape the enterprise consent module's `decider()` returns."""
    people: List[str] = field(default_factory=list)
    single: Optional[str] = None
    seat: str = "primary"
    operators_reason: Optional[str] = None


@pytest.fixture
def audit():
    return _Audit()


@pytest.fixture
def provider():
    from services import assignment_provider
    p = _Provider()
    assignment_provider.register_provider(p)
    yield p
    assignment_provider.clear_provider()


@pytest.fixture
def decider(monkeypatch):
    """The registered consent rule, controllable per test."""
    from services import ask_service as svc
    state = {"d": Decider(), "calls": []}

    def _resolve(agent_name):
        state["calls"].append(agent_name)
        return state["d"]

    monkeypatch.setattr(svc, "_readdress_resolvers", {})
    svc.register_readdress_resolver(CONSENT, _resolve)
    return state


@pytest.fixture(autouse=True)
def sink(monkeypatch, real_db, audit, provider):
    from services import ask_service as svc
    from services import operator_queue_service as oqs
    from services import operator_resume_service as ors
    from services import skill_gate_service as sgs
    from services.rate_limiter import RateLimitResult

    def _run_now(factory):
        # the routes are async (TestClient runs them on a loop), so the
        # announcement runs to completion on its own thread, synchronously
        t = threading.Thread(target=lambda: asyncio.run(factory()))
        t.start()
        t.join()
    monkeypatch.setattr(ors, "spawn_on_loop", _run_now)
    monkeypatch.setattr(ors, "spawn_ending_dispatch", lambda rows, **kw: None)
    monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda item, **kw: None)
    monkeypatch.setattr(svc, "_opted_in", lambda agent_name: True)
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(svc, "platform_audit_service", audit)
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_owner_email", lambda agent: None)
    monkeypatch.setattr(oqs, "_workspace_attachment",
                        lambda agent, email, **_: (f"thread-{email}", False))
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    monkeypatch.setattr(sgs, "_can_open", lambda agent_name, email: True)
    # every principal here can use the test agent; the per-row fences stay real
    from routers import operator_queue as r
    monkeypatch.setattr(r, "_accessible_set", lambda user: None)
    yield


# --------------------------------------------------------------------------
# The app: the real router, the principal swapped per test
# --------------------------------------------------------------------------

_APP = None
_PRINCIPAL = {"user": None}


def _app():
    global _APP
    if _APP is None:
        from fastapi import FastAPI
        from routers import operator_queue as r

        app = FastAPI()
        app.include_router(r.router)
        found = set()

        def walk(dep):
            for sub in dep.dependencies:
                if getattr(sub.call, "__name__", "") == "get_current_user":
                    found.add(sub.call)
                walk(sub)

        for route in r.router.routes:
            if getattr(route, "dependant", None) is not None:
                walk(route.dependant)
        assert found, "no get_current_user dependency found on the router"
        for fn in found:
            app.dependency_overrides[fn] = lambda: _PRINCIPAL["user"]
        _APP = app
    return _APP


def _user(**kw):
    from models import User
    base = dict(id=7, username="root", role="admin", email="ops@example.com")
    base.update(kw)
    return User(**base)


ADMIN = dict(role="admin", email="ops@example.com")
REFUSED = {
    "agent_key": dict(role="admin", mcp_scope="agent", agent_name="some-agent"),
    "connector_key": dict(role="admin", connector_agent="x"),
    "ops_key": dict(role="admin", mcp_scope="ops"),
    "non_admin_person": dict(role="creator", email="c@example.com"),
}


@pytest.fixture
def as_user():
    from fastapi.testclient import TestClient
    client = TestClient(_app(), raise_server_exceptions=True)

    def _as(**kw):
        _PRINCIPAL["user"] = _user(**kw)
        return client
    yield _as
    _PRINCIPAL["user"] = None


def _svc():
    from services import ask_service
    return ask_service


def _consent(agent, audience, *, seat="primary", reason=None):
    ctx = {SEAT: seat}
    if reason:
        ctx[REASON] = reason
    body = {"request_id": CONSENT + uuid.uuid4().hex[:12], "type": "approval",
            "title": "Add the agent to a project?", "question": "Allow it?",
            "options": ["Approve", "Decline"]}
    return _svc().raise_ask(agent, body, raised_by="gate", channel="gate",
                            addressees=list(audience), to_role_override="primary",
                            attach_chat=False, platform_context=ctx)["id"]


def _skill_gate(agent, approvers):
    body = {"request_id": "gate-" + (uuid.uuid4().hex + uuid.uuid4().hex)[:40], "type": "approval",
            "title": "Run it?", "question": "Approve?", "options": ["Approve", "Decline"]}
    return _svc().raise_ask(agent, body, raised_by="gate", channel="gate",
                            addressees=list(approvers), to_role_override="primary")["id"]


def _note(agent, email):
    body = {"request_id": "gate-note-" + uuid.uuid4().hex[:8], "type": "approval", "title": "n",
            "question": "q", "options": ["Approve", "Decline"]}
    return _svc().raise_ask(agent, body, raised_by="gate", channel="gate", addressee=email)["id"]


def _agent_ask(agent, to="primary"):
    body = {"request_id": f"ask-{uuid.uuid4().hex[:10]}", "type": "question",
            "title": "t", "question": "q", "to": to}
    return _svc().raise_ask(agent, body, raised_by="agent", channel="mcp")["id"]


def _readdress(client, ask_id, to="rule", reason="the owner is on leave"):
    return client.post(f"/api/operator-queue/{ask_id}/readdress", json={"to": to, "reason": reason})


# ===========================================================================
# POST /readdress — who may call it
# ===========================================================================

class TestReaddressAuth:
    @pytest.mark.parametrize("who", sorted(REFUSED))
    def test_only_an_admin_person_may_readdress(self, as_user, agent, people, decider, who, real_db):
        ask = _consent(agent, [people["alice"]], seat="owner")
        decider["d"] = Decider([people["bob"]], people["bob"], "primary")
        res = _readdress(as_user(**REFUSED[who]), ask)
        assert res.status_code == 403
        assert real_db.get_operator_queue_item(ask)["resolved_to"] == [people["alice"]]
        assert decider["calls"] == []

    def test_an_unknown_row_is_the_uniform_404(self, as_user):
        assert _readdress(as_user(**ADMIN), "no-such-row").status_code == 404

    @pytest.mark.parametrize("body", [
        {"to": "rule"},
        {"to": "rule", "reason": "   "},
        {"to": "rule", "reason": "x" * 501},
        {"to": "someone@example.com", "reason": "pick a person"},
        {"reason": "no target"},
    ])
    def test_the_body_is_validated(self, as_user, agent, people, body):
        ask = _consent(agent, [people["alice"]])
        res = as_user(**ADMIN).post(f"/api/operator-queue/{ask}/readdress", json=body)
        assert res.status_code == 422


# ===========================================================================
# POST /readdress — what it does
# ===========================================================================

class TestReaddressByRule:
    def test_a_registered_family_is_moved_by_its_rule(self, as_user, agent, people, decider,
                                                      audit, real_db):
        ask = _consent(agent, [people["alice"]], seat="owner")
        decider["d"] = Decider([people["bob"]], people["bob"], "primary")
        audit.rows.clear()
        res = _readdress(as_user(**ADMIN), ask)
        assert res.status_code == 200, res.text
        assert res.json()["waiting_on"] == [people["bob"]]
        assert res.json()["seat"] == "primary"
        row = real_db.get_operator_queue_item(ask)
        assert row["resolved_to"] == [people["bob"]]
        assert row["context"][SEAT] == "primary"
        assert decider["calls"] == [agent]
        (a,) = audit.actions("platform_readdressed")
        assert a["details"]["reason"] == "admin_readdressed"
        assert a["details"]["has_reason"] is True
        assert a["actor_email"] == "ops@example.com"
        assert people["bob"] not in repr(a["details"]) and "on leave" not in repr(a)

    def test_the_rule_may_land_on_the_admin_seat(self, as_user, agent, people, decider, real_db):
        ask = _consent(agent, [people["alice"]], seat="owner")
        decider["d"] = Decider([], None, "admin", "owner_no_email")
        assert _readdress(as_user(**ADMIN), ask).status_code == 200
        row = real_db.get_operator_queue_item(ask)
        assert row["resolved_to"] is None
        assert row["context"][SEAT] == "admin" and row["context"][REASON] == "owner_no_email"

    def test_an_unchanged_audience_is_a_409_naming_who_it_waits_for(self, as_user, agent, people,
                                                                   decider):
        ask = _consent(agent, [people["alice"]], seat="owner")
        decider["d"] = Decider([people["alice"]], people["alice"], "owner")
        res = _readdress(as_user(**ADMIN), ask)
        assert res.status_code == 409
        d = res.json()["detail"]
        assert d["code"] == "audience_unchanged"
        assert d["waiting_on"] == [people["alice"]] and d["seat"] == "owner"
        assert people["alice"] in d["message"]
        assert "to: admin" in d["message"] or "admin seat" in d["message"]

    def test_a_role_addressed_agent_ask_follows_its_role(self, as_user, agent, people, provider,
                                                         real_db):
        provider.roles["primary"] = [people["alice"]]
        ask = _agent_ask(agent)
        provider.roles["primary"] = [people["bob"]]
        assert _readdress(as_user(**ADMIN), ask).status_code == 200
        assert real_db.get_operator_queue_item(ask)["resolved_to"] == [people["bob"]]

    def test_a_named_family_without_a_rule_is_a_422(self, as_user, agent, people, decider):
        ask = _note(agent, people["alice"])
        res = _readdress(as_user(**ADMIN), ask)
        assert res.status_code == 422
        assert res.json()["detail"]["code"] == "unknown_family"

    def test_an_ended_card_is_a_409(self, as_user, agent, people, decider):
        ask = _consent(agent, [people["alice"]])
        _svc().answer(services.ask_service.db.get_operator_queue_item(ask), response="Approve",
                      response_text=None, actor=_svc().Actor(email=people["alice"]))
        res = _readdress(as_user(**ADMIN), ask)
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "not_pending"


class TestReaddressToAdmin:
    def test_a_consent_card_moves_to_the_admin_seat_and_the_admin_decides_it(
            self, as_user, agent, people, decider, real_db):
        ask = _consent(agent, [people["alice"]], seat="owner")
        client = as_user(**ADMIN)
        res = _readdress(client, ask, to="admin")
        assert res.status_code == 200, res.text
        assert res.json() == {"id": ask, "waiting_on": [], "seat": "admin"}
        row = real_db.get_operator_queue_item(ask)
        assert row["resolved_to"] is None and row["context"][SEAT] == "admin"
        assert decider["calls"] == []  # no rule re-run: the admin chose the seat

        res = client.post(f"/api/operator-queue/{ask}/respond", json={"response": "Approve"})
        assert res.status_code == 200, res.text
        assert real_db.get_operator_queue_item(ask)["disposition"] == "answered"

    def test_a_skill_gate_approval_cannot_go_to_the_admin_seat(self, as_user, agent, people,
                                                               decider, real_db):
        """T3: the skill gate keeps the ent#904 order — its resume reads
        `resolved_to`, so an admin's answer would be refused there."""
        ask = _skill_gate(agent, [people["alice"]])
        res = _readdress(as_user(**ADMIN), ask, to="admin")
        assert res.status_code == 422
        assert res.json()["detail"]["code"] == "admin_seat_unsupported"
        assert real_db.get_operator_queue_item(ask)["resolved_to"] == [people["alice"]]


# ===========================================================================
# Cancel needs a reason from an admin ending someone else's decision
# ===========================================================================

class TestCancelReasonRequired:
    @pytest.mark.parametrize("body", [None, {}, {"reason": "  "}])
    def test_an_admin_ending_someone_elses_card_must_say_why(self, as_user, agent, people, body,
                                                            real_db):
        ask = _consent(agent, [people["alice"]], seat="primary")
        client = as_user(**ADMIN)
        url = f"/api/operator-queue/{ask}/cancel"
        res = client.post(url, json=body) if body is not None else client.post(url)
        assert res.status_code == 422
        assert res.json()["detail"]["code"] == "reason_required"
        assert real_db.get_operator_queue_item(ask)["status"] == "pending"

    def test_with_a_reason_it_ends(self, as_user, agent, people, real_db):
        ask = _consent(agent, [people["alice"]], seat="primary")
        res = as_user(**ADMIN).post(f"/api/operator-queue/{ask}/cancel",
                                    json={"reason": "the project was dropped"})
        assert res.status_code == 200, res.text
        row = real_db.get_operator_queue_item(ask)
        assert row["status"] == "cancelled"
        assert row["disposition_reason"] == "the project was dropped"

    def test_the_addressee_needs_no_reason(self, as_user, agent, people, real_db):
        ask = _consent(agent, [people["alice"]], seat="primary")
        res = as_user(role="creator", email=people["alice"]).post(f"/api/operator-queue/{ask}/cancel")
        assert res.status_code == 200, res.text

    def test_an_unfenced_ask_needs_no_reason(self, as_user, agent, real_db):
        ask = _agent_ask(agent, to="operator")
        assert as_user(**ADMIN).post(f"/api/operator-queue/{ask}/cancel").status_code == 200


class TestBulkReportsFenced:
    def test_the_sweep_reports_the_fenced_rows_it_skipped(self, as_user, agent, people, real_db):
        fenced = _consent(agent, [people["alice"]], seat="primary")
        plain = _agent_ask(agent, to="operator")
        res = as_user(**ADMIN).post("/api/operator-queue/bulk-cancel", json={"ids": [fenced, plain]})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["cancelled"] == 1 and body["skipped"] == 1 and body["skipped_fenced"] == 1
        assert real_db.get_operator_queue_item(fenced)["status"] == "pending"


# ===========================================================================
# The named refusal
# ===========================================================================

class TestNamedRefusalOnTheOperatorDoor:
    def test_a_non_addressee_is_told_who_the_card_waits_for(self, as_user, agent, people):
        ask = _consent(agent, [people["alice"]], seat="primary")
        res = as_user(role="creator", email=people["carol"]).post(
            f"/api/operator-queue/{ask}/respond", json={"response": "Approve"})
        assert res.status_code == 403
        d = res.json()["detail"]
        assert d["code"] == "not_addressee"
        assert d["waiting_on"] == [people["alice"]] and d["seat"] == "primary"
        assert people["alice"] in d["message"]
        assert "re-address" not in d["message"]

    def test_an_admin_is_told_the_two_verbs(self, as_user, agent, people):
        ask = _consent(agent, [people["alice"]], seat="owner")
        res = as_user(**ADMIN).post(f"/api/operator-queue/{ask}/respond", json={"response": "Approve"})
        assert res.status_code == 403
        msg = res.json()["detail"]["message"]
        assert people["alice"] in msg and "re-address" in msg and "reason" in msg

    def test_the_admin_seat_names_the_admins(self, as_user, agent):
        ask = _consent(agent, [], seat="admin", reason="owner_no_email")
        res = as_user(role="creator", email="c@example.com").post(
            f"/api/operator-queue/{ask}/respond", json={"response": "Approve"})
        assert res.status_code == 403
        d = res.json()["detail"]
        assert d["waiting_on"] == [] and d["seat"] == "admin"
        assert "instance admin" in d["message"]

    def test_the_cancel_refusal_carries_the_fields(self, as_user, agent, people):
        ask = _consent(agent, [people["alice"]], seat="primary")
        res = as_user(role="creator", email=people["carol"]).post(
            f"/api/operator-queue/{ask}/cancel", json={"reason": "no"})
        assert res.status_code == 403
        d = res.json()["detail"]
        assert d["waiting_on"] == [people["alice"]] and d["seat"] == "primary"

    def test_an_answer_read_before_a_readdress_is_a_409_naming_the_new_reader(
            self, as_user, agent, people, monkeypatch):
        from routers import operator_queue as r
        ask = _consent(agent, [people["alice"]], seat="primary")
        stale = r.db.get_operator_queue_item(ask)
        assert _svc().readdress_item(ask, [people["bob"]], people["bob"],
                                     reason=_svc().ASSIGNMENT_CHANGED, attach_chat=False)
        real_get = r.db.get_operator_queue_item
        reads = []

        def _stale_once(i):
            # the route's own read is the stale one; every later read is real
            reads.append(i)
            return dict(stale) if i == ask and len(reads) == 1 else real_get(i)
        monkeypatch.setattr(r.db, "get_operator_queue_item", _stale_once)
        res = as_user(role="creator", email=people["alice"]).post(
            f"/api/operator-queue/{ask}/respond", json={"response": "Approve"})
        assert res.status_code == 409
        d = res.json()["detail"]
        assert d["code"] == "readdressed"
        assert d["waiting_on"] == [people["bob"]]
        assert people["bob"] in d["message"]
        assert real_get(ask)["status"] == "pending"


class TestNamedRefusalOnTheWorkspaceDoor:
    def test_the_workspace_refusal_has_the_fields_and_no_admin_verbs(self, agent, people, monkeypatch):
        from client_portal.asks import service as asks
        ask = _consent(agent, [people["alice"]], seat="primary")
        row = services.ask_service.db.get_operator_queue_item(ask)
        monkeypatch.setattr(asks, "_owned_ask", lambda *a, **k: dict(row))

        def _refuse(item, **kw):
            raise _svc().not_addressee(item)
        monkeypatch.setattr(asks.ask_service, "answer", _refuse)
        with pytest.raises(asks.AskError) as exc:
            asks.answer_ask(ask, people["carol"], False, "Approve", None)
        e = exc.value
        assert e.status_code == 403 and e.code == "not_addressee"
        assert e.data == {"waiting_on": [people["alice"]], "seat": "primary"}
        assert people["alice"] in e.detail
        assert "re-address" not in e.detail

    def test_a_workspace_answer_read_before_a_readdress_is_a_named_409(self, agent, people,
                                                                       monkeypatch):
        from client_portal.asks import service as asks
        ask = _consent(agent, [people["alice"]], seat="primary")
        stale = services.ask_service.db.get_operator_queue_item(ask)
        assert _svc().readdress_item(ask, [people["bob"]], people["bob"],
                                     reason=_svc().ASSIGNMENT_CHANGED, attach_chat=False)
        monkeypatch.setattr(asks, "_owned_ask", lambda *a, **k: dict(stale))
        with pytest.raises(asks.AskError) as exc:
            asks.answer_ask(ask, people["alice"], False, "Approve", None)
        assert exc.value.status_code == 409 and exc.value.code == "readdressed"
        # the Workspace never names who it moved to, nor the admin verbs
        assert people["bob"] not in exc.value.detail
        assert "re-address" not in exc.value.detail


# ===========================================================================
# Wiring
# ===========================================================================

class TestWiring:
    def test_the_route_is_registered_on_the_router(self):
        from routers import operator_queue as r
        paths = {(m, rt.path) for rt in r.router.routes for m in getattr(rt, "methods", ())}
        assert ("POST", "/api/operator-queue/{item_id}/readdress") in paths

    def test_the_route_is_human_only_admin(self):
        """The gates the census reads: `require_admin` and `require_person`."""
        from routers import operator_queue as r
        route = next(rt for rt in r.router.routes
                     if getattr(rt, "path", "") == "/api/operator-queue/{item_id}/readdress")
        names = set()

        def walk(dep):
            for sub in dep.dependencies:
                names.add(getattr(sub.call, "__name__", ""))
                walk(sub)
        walk(route.dependant)
        assert {"require_admin", "require_person"} <= names

    def test_the_mcp_header_names_the_human_only_verb(self):
        """R14: the router's first-line `# mcp:` header is the CI-read
        declaration (Invariant #13) — its live consumer is
        `/validate-architecture`, so this pin reads the header text."""
        from routers import operator_queue as r
        with open(r.__file__) as f:
            first = f.readline()
        assert first.startswith("# mcp:")
        assert "readdress → none" in first
