"""A `user`-role member assigned to a companion answers its asks in the Inbox
(trinity-enterprise#837 AC7).

The whole path a Workspace-only person takes, with nothing that decides the
outcome stubbed:

* a REAL `user` account and a REAL platform JWT for it, resolved by the Workspace
  door (`get_portal_principal`) — so the floor PR 1 put in `get_current_user`
  is not what this test bypasses: the door is the unfloored resolver, and the
  principal it yields is the one the Inbox reads;
* the REAL roster (agent_sharing + agent_ownership) — the ent#816 tests stub
  `agent_on_roster` to True, which is exactly the property that decides this;
* an ask `to: primary`, resolved through the assignment seam to that person.

Two facts follow, and the Access tab is built on the second (decision D4,
2026-10-12): an assigned `user` the agent IS shared with sees the ask and
answers it; one the agent is NOT shared with never sees it — an assignment is a
relationship, not an access grant (ent#500) — which is why assigning someone on
the Access tab also shares the agent with them.

Harness: the unit island's per-process SQLite; names and emails are unique per
test. The sink's side effects (dispatch, broadcast, audit) are stubbed as in
test_ent816_shared_asks.py.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

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
import client_portal.asks.service  # noqa: E402,F401
import client_portal.db  # noqa: E402,F401


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture(autouse=True)
def sink(monkeypatch, real_db):
    """The real sink over the real SQLite; only its side effects are stubbed."""
    import json as _json
    from services import ask_service as svc
    from services import assignment_provider
    from services import operator_queue_service as oqs
    from services import operator_resume_service as ors
    from services.rate_limiter import RateLimitResult

    class _Audit:
        async def log(self, **kw):
            return "evt"

    class _WS:
        async def broadcast(self, message):
            _json.loads(message)

    monkeypatch.setattr(ors, "spawn_on_loop", lambda factory: None)
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
    yield
    assignment_provider.clear_provider()


class _Provider:
    """The assignment seam: `primary` resolves to the assigned person."""

    def __init__(self, primary_email):
        self.primary_email = primary_email

    def assignment_for(self, agent_name, triggered_by):
        return None

    def people_for(self, agent_name, role):
        return {"emails": [self.primary_email]} if role == "primary" else None


@pytest.fixture
def companion(real_db):
    """An agent owned by an operator, and a Workspace-only member assigned as
    its primary. Sharing is the variable each test sets."""
    from db_models import UserCreate
    from services import assignment_provider

    owner = real_db.create_user(UserCreate(
        username=_uniq("owner"), password=None, role="operator",
        email=f"{_uniq('owner')}@example.com",
    ))
    agent = f"companion-{uuid.uuid4().hex[:6]}"
    real_db.register_agent_owner(agent, owner["username"])
    member_email = f"{_uniq('dana')}@example.com"
    member = real_db.get_or_create_email_user(member_email)   # no whitelist row → `user`
    assert member["role"] == "user"
    assignment_provider.register_provider(_Provider(member_email))
    return {"agent": agent, "owner": owner, "member": member, "email": member_email}


def _principal(username, monkeypatch):
    """The Workspace door, fed a real platform JWT for `username`."""
    import client_portal.db as portal_db
    from client_portal.portal_auth import get_portal_principal
    from config import ALGORITHM, SECRET_KEY
    from jose import jwt
    from starlette.requests import Request
    from starlette.responses import Response

    monkeypatch.setattr(portal_db, "is_client_blocked", lambda *_a, **_k: False)
    token = jwt.encode({"sub": username, "exp": int(time.time()) + 300}, SECRET_KEY, algorithm=ALGORITHM)
    request = Request({
        "type": "http", "method": "GET", "path": "/api/portal/asks", "root_path": "",
        "query_string": b"", "scheme": "http", "server": ("backend", 8000),
        "headers": [(b"host", b"backend:8000")],
    })
    return asyncio.run(get_portal_principal(request, Response(), token=token))


def _raise_to_primary(agent):
    from services import ask_service
    rid = f"r-{uuid.uuid4().hex[:10]}"
    return ask_service.raise_ask(agent, {
        "request_id": rid, "type": "approval", "title": "Send the quarterly brief",
        "question": "Send it to the board?", "options": ["approve", "reject"],
        "to": "primary", "proposal": {"send": "brief", "id": rid},
    }, raised_by="agent", channel="mcp")


def _svc():
    from client_portal.asks import service
    return service


class TestAnAssignedUserAnswersInTheInbox:
    def test_shared_and_assigned_the_user_sees_and_answers_the_ask(self, real_db, companion, monkeypatch):
        real_db.share_agent(companion["agent"], companion["owner"]["username"], companion["email"])
        receipt = _raise_to_primary(companion["agent"])

        principal = _principal(companion["member"]["username"], monkeypatch)
        assert (principal.email, principal.is_platform) == (companion["email"], True)

        page = _svc().list_asks_page(principal.email, principal.is_platform)
        assert [a.id for a in page.items] == [receipt["id"]]

        answered = _svc().answer_ask(receipt["id"], principal.email, principal.is_platform, "approve", None)
        assert (answered.status, answered.ended_by) == ("answered", "you")
        row = real_db.get_operator_queue_item(receipt["id"])
        assert (row["response"], row["responded_by_email"]) == ("approve", companion["email"])

    def test_an_assignment_alone_does_not_reach_the_inbox(self, companion, monkeypatch):
        """Not shared: the agent is not on the person's roster, so its ask is in
        nobody's Inbox — and every per-ask door answers the uniform 404. This is
        what the Access tab's assign = share closes."""
        receipt = _raise_to_primary(companion["agent"])
        principal = _principal(companion["member"]["username"], monkeypatch)

        assert _svc().list_asks_page(principal.email, principal.is_platform).items == []
        with pytest.raises(_svc().AskError) as exc:
            _svc().answer_ask(receipt["id"], principal.email, principal.is_platform, "approve", None)
        assert (exc.value.status_code, exc.value.code) == (404, "not_found")
