"""#4666: a non-owner admin can add themselves to an agent's access.

The Workspace roster is the agents a person owns plus the agents shared with
them, and the admin role deliberately does not widen it. The share route
refused any share whose target was the CALLER's email, so a non-owner admin
could share an agent with everyone except themselves, and the agent could not
reach their Workspace. The refusal is now against the agent's OWNER, for whom
a share is redundant.

The REAL sharing router and DB layer over the unit DB, as in
test_ent459_operator_invite.py. Only the mail send, the container lookup and
the audit sink are replaced.
"""
from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.unit


def _make_user(db, username, role):
    from db_models import UserCreate
    if not db.get_user_by_username(username):
        db.create_user(UserCreate(username=username, email=f"{username}@example.com", role=role))
    return username


@pytest.fixture
def world(monkeypatch):
    from fastapi import FastAPI
    from jose import jwt

    from config import ALGORITHM, SECRET_KEY
    from database import db
    from routers import sharing
    from services import rate_limiter
    from services.email_service import email_service

    db.set_setting("terms_required", "false")
    db.set_setting("email_auth_enabled", "true")
    tag = uuid.uuid4().hex[:8]
    agent = f"i4666-{tag}"
    owner = _make_user(db, f"o{tag}", "creator")
    admin = _make_user(db, f"a{tag}", "admin")
    creator = _make_user(db, f"c{tag}", "creator")
    db.register_agent_owner(agent, owner)

    sent = []

    async def send_email(to, subject, body, html_body=None):
        sent.append(to)
        return True

    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
    rate_limiter.clear_inprocess()
    monkeypatch.setattr(email_service, "send_email", send_email)
    monkeypatch.setattr(email_service, "can_deliver", lambda: True)
    monkeypatch.setattr(sharing, "get_agent_container", lambda name: object())
    monkeypatch.setattr(sharing, "manager", None)
    audit = AsyncMock()
    monkeypatch.setattr(sharing.platform_audit_service, "log", audit)

    app = FastAPI()
    app.include_router(sharing.router)
    client = TestClient(app, raise_server_exceptions=False)

    def share(email, as_):
        token = jwt.encode({"sub": as_, "exp": int(time.time()) + 300}, SECRET_KEY, algorithm=ALGORITHM)
        return client.post(f"/api/agents/{agent}/share", json={"email": email},
                           headers={"Authorization": f"Bearer {token}"})

    yield SimpleNamespace(db=db, agent=agent, owner=owner, admin=admin, creator=creator,
                          sent=sent, audit=audit, share=share)
    rate_limiter.clear_inprocess()
    db.delete_setting("terms_required")
    db.delete_setting("email_auth_enabled")


def _shared_emails(world):
    return {s.shared_with_email for s in world.db.get_agent_shares(world.agent)}


def _share_audits(world):
    return [c.kwargs for c in world.audit.await_args_list if c.kwargs.get("event_action") == "share"]


class TestAdminSelfAdd:
    def test_a_non_owner_admin_can_add_themselves(self, world):
        email = f"{world.admin}@example.com"
        r = world.share(email, as_=world.admin)
        assert r.status_code == 200, r.text
        assert r.json()["invitation"] == "not_needed"
        assert email in _shared_emails(world)

    def test_the_self_add_sends_no_invitation_and_is_audited(self, world):
        email = f"{world.admin}@example.com"
        assert world.share(email.upper(), as_=world.admin).status_code == 200
        assert world.sent == []
        [audit] = _share_audits(world)
        assert audit["details"] == {"shared_with": email.upper()}

    def test_an_admin_still_cannot_share_with_the_owner(self, world):
        r = world.share(f"{world.owner}@example.com", as_=world.admin)
        assert r.status_code == 400
        assert "owner" in r.json()["detail"]
        assert _shared_emails(world) == set()


class TestOwnerSelfShare:
    def test_the_owner_is_refused_and_told_why(self, world):
        r = world.share(f"{world.owner}@example.com", as_=world.owner)
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail.startswith("Cannot share an agent with yourself")
        assert "already on your Workspace" in detail
        assert _shared_emails(world) == set()
        assert world.sent == []

    def test_the_owner_can_still_share_with_an_admin(self, world):
        email = f"{world.admin}@example.com"
        assert world.share(email, as_=world.owner).status_code == 200
        assert email in _shared_emails(world)


class TestNonAdminNonOwner:
    def test_a_non_admin_who_does_not_own_the_agent_is_still_refused(self, world):
        r = world.share(f"{world.creator}@example.com", as_=world.creator)
        assert r.status_code == 404
        assert _shared_emails(world) == set()


class TestDbLayer:
    def test_the_db_refuses_a_share_with_the_owner_whoever_calls(self, world):
        db = world.db
        assert db.share_agent(world.agent, world.admin, f"{world.owner}@example.com") is None
        assert db.share_agent(world.agent, world.owner, f" {world.owner.upper()}@EXAMPLE.COM ") is None

    def test_the_db_lets_an_admin_share_with_themselves(self, world):
        share = world.db.share_agent(world.agent, world.admin, f"{world.admin}@example.com")
        assert share is not None
        assert share.shared_with_email == f"{world.admin}@example.com"
