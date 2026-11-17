"""trinity-enterprise#459: telling a pending person an agent was shared with them.

The REAL sharing router and dependency chain over the unit DB. Only the outside
world is replaced: the mail provider's send, and the agent container lookup that
share needs.

  1. who may send: owner and admin; a shared user and a stranger get the uniform
     404; every key is refused, since this sends mail on a person's behalf;
  2. each refusal is named, and a refused call sends nothing and spends no quota;
  3. the mail names the inviter and the agent, points at the Workspace, escapes
     what it quotes, and carries no code;
  4. share and resend spend the same two windows;
  5. a share always stands, and says what happened to its invitation;
  6. every attempt is audited.
"""
from __future__ import annotations

import re
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.unit

PENDING = "pending.person@example.com"
ACTIVE = "active.person@example.com"


def _make_user(db, username, role, *, name=None):
    from db_models import UserCreate
    email = f"{username}@example.com"
    if not db.get_user_by_username(username):
        db.create_user(UserCreate(username=username, email=email, role=role, name=name))
    return username


@pytest.fixture
def world(monkeypatch):
    from fastapi import FastAPI
    from jose import jwt

    from config import ALGORITHM, SECRET_KEY
    from database import db
    from routers import sharing
    from services import agent_invite_service as svc
    from services import rate_limiter
    from services.email_service import email_service

    db.set_setting("terms_required", "false")
    db.set_setting("email_auth_enabled", "true")
    tag = uuid.uuid4().hex[:8]
    agent = f"ent459-{tag}"
    owner = _make_user(db, f"o{tag}", "creator", name="Olga <b>Owner</b>")
    admin = _make_user(db, f"a{tag}", "admin")
    shared = _make_user(db, f"s{tag}", "creator")
    stranger = _make_user(db, f"x{tag}", "creator")
    db.register_agent_owner(agent, owner)
    db.share_agent(agent, owner, f"{shared}@example.com")
    pending = f"{tag}.{PENDING}"
    db.share_agent(agent, owner, pending)

    sent = []

    async def send_email(to, subject, body, html_body=None):
        sent.append(SimpleNamespace(to=to, subject=subject, body=body, html=html_body))
        return True

    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
    rate_limiter.clear_inprocess()
    monkeypatch.setattr(email_service, "send_email", send_email)
    monkeypatch.setattr(email_service, "can_deliver", lambda: True)
    monkeypatch.setenv("FRONTEND_URL", "https://trinity.example.com/")
    monkeypatch.setattr(sharing, "get_agent_container", lambda name: object())
    monkeypatch.setattr(sharing, "manager", None)
    audit = AsyncMock()
    monkeypatch.setattr(sharing.platform_audit_service, "log", audit)

    app = FastAPI()
    app.include_router(sharing.router)
    client = TestClient(app, raise_server_exceptions=False)

    def headers(username):
        token = jwt.encode({"sub": username, "exp": int(time.time()) + 300}, SECRET_KEY, algorithm=ALGORITHM)
        return {"Authorization": f"Bearer {token}"}

    def resend(email=pending, as_=owner, name=agent):
        return client.post(f"/api/agents/{name}/share/{email}/invite", headers=headers(as_))

    def share(email, as_=owner):
        return client.post(f"/api/agents/{agent}/share", json={"email": email}, headers=headers(as_))

    yield SimpleNamespace(
        db=db, svc=svc, tag=tag, agent=agent, owner=owner, admin=admin, shared=shared, stranger=stranger,
        pending=pending, sent=sent, audit=audit, client=client, headers=headers, resend=resend, share=share,
        email_service=email_service, rate_limiter=rate_limiter,
    )
    rate_limiter.clear_inprocess()
    db.delete_setting("terms_required")
    db.delete_setting("email_auth_enabled")


def _actions(world):
    return [c.kwargs for c in world.audit.await_args_list if c.kwargs.get("event_action") == "agent_invite"]


class TestWhoMaySend:
    def test_the_owner_sends_and_it_is_audited(self, world):
        r = world.resend()
        assert r.status_code == 200, r.text
        assert r.json() == {"email": world.pending, "sent": True}
        assert [m.to for m in world.sent] == [world.pending]
        audited = _actions(world)
        assert audited[0]["details"] == {"shared_with": world.pending, "trigger": "resend", "outcome": "sent"}

    def test_an_admin_sends(self, world):
        assert world.resend(as_=world.admin).status_code == 200

    @pytest.mark.parametrize("who", ["shared", "stranger"])
    def test_anyone_else_gets_the_uniform_404_and_nothing_is_sent(self, world, who):
        r = world.resend(as_=getattr(world, who))
        assert r.status_code == 404
        assert world.sent == []

    def test_no_token_is_refused(self, world):
        r = world.client.post(f"/api/agents/{world.agent}/share/{world.pending}/invite")
        assert r.status_code == 401
        assert world.sent == []

    @pytest.mark.parametrize("scope", ["agent", "user"])
    def test_a_key_is_refused_even_the_owners_own(self, world, monkeypatch, scope):
        monkeypatch.setattr(world.db, "validate_mcp_api_key", lambda *_a, **_k: {
            "scope": scope, "agent_name": world.agent if scope == "agent" else None,
            "key_id": "k1", "key_name": scope, "user_id": world.owner,
            "user_email": f"{world.owner}@example.com",
        })
        monkeypatch.setattr(world.db, "get_agent_ephemeral_info", lambda *_a, **_k: {"is_ephemeral": False})
        r = world.client.post(f"/api/agents/{world.agent}/share/{world.pending}/invite",
                              headers={"Authorization": "Bearer trinity_mcp_fake"})
        assert r.status_code == 403
        assert world.sent == []


class TestNamedRefusals:
    def _code(self, r):
        return r.json()["detail"]["code"]

    def test_an_address_this_agent_is_not_shared_with(self, world):
        r = world.resend(email="nobody@example.com")
        assert (r.status_code, self._code(r)) == (404, "not_shared")

    def test_a_person_with_an_account(self, world):
        r = world.resend(email=f"{world.shared}@example.com")
        assert (r.status_code, self._code(r)) == (409, "not_pending")

    def test_no_real_provider_is_refused_not_faked(self, world, monkeypatch):
        monkeypatch.setattr(world.email_service, "can_deliver", lambda: False)
        r = world.resend()
        assert (r.status_code, self._code(r)) == (409, "invite_unavailable")
        assert world.sent == []

    def test_email_sign_in_off_is_refused(self, world):
        world.db.set_setting("email_auth_enabled", "false")
        r = world.resend()
        assert (r.status_code, self._code(r)) == (409, "invite_unavailable")
        assert world.sent == []

    def test_a_policy_refusing_this_address_a_code_is_refused(self, world, monkeypatch):
        from services import login_policy_gate
        monkeypatch.setattr(login_policy_gate, "email_code_allowed_for", lambda email: False)
        r = world.resend()
        assert (r.status_code, self._code(r)) == (409, "invite_unavailable")
        assert world.sent == []

    def test_a_failed_send_is_a_502_and_audited(self, world, monkeypatch):
        monkeypatch.setattr(world.email_service, "send_email", AsyncMock(return_value=False))
        r = world.resend()
        assert (r.status_code, self._code(r)) == (502, "invite_not_sent")
        assert _actions(world)[0]["details"]["outcome"] == "failed"

    def test_a_hung_provider_is_bounded(self, world, monkeypatch):
        import asyncio

        async def hang(*_a, **_k):
            await asyncio.sleep(30)
        monkeypatch.setattr(world.email_service, "send_email", hang)
        monkeypatch.setattr(world.svc, "SEND_TIMEOUT_SECONDS", 0.01)
        started = time.monotonic()
        r = world.resend()
        assert (r.status_code, self._code(r)) == (502, "invite_not_sent")
        assert time.monotonic() - started < 10

    def test_the_address_is_case_insensitive(self, world):
        assert world.resend(email=world.pending.upper()).status_code == 200
        assert world.sent[0].to == world.pending


HOSTILE = [
    "a@x.com,b@y.com",
    "a@x.com;b@y.com",
    "a@x.com b@y.com",
    "a@x.com\r\nBcc: v@y.com",
    "a@x.com\nSubject: hi",
    "Victim <v@y.com>",
    "notanemail",
    "a@b",
    "a@@b.com",
]


class TestOneAddressOnly:
    """The share field is free text; a mail must never treat it as more than one
    recipient, or carry a header in on it."""

    @pytest.mark.parametrize("email", HOSTILE)
    def test_a_share_stands_and_sends_nothing(self, world, email):
        r = world.share(email)
        assert (r.status_code, r.json()["invitation"]) == (200, "invalid_address")
        assert world.sent == []

    @pytest.mark.parametrize("email", [e for e in HOSTILE if "\n" not in e and "@" in e and " " not in e])
    def test_resend_refuses_by_name_and_spends_nothing(self, world, email):
        world.db.share_agent(world.agent, world.owner, email)
        for _ in range(world.svc.ADDRESS_LIMIT + 1):
            r = world.resend(email=email)
            assert (r.status_code, r.json()["detail"]["code"]) == (422, "invalid_address")
        assert world.sent == []

    def test_the_roster_says_no(self, world):
        world.db.share_agent(world.agent, world.owner, "a@x.com,b@y.com")
        rows = world.client.get(f"/api/agents/{world.agent}/access", headers=world.headers(world.owner)).json()
        assert {r["email"]: r["invite_available"] for r in rows}["a@x.com,b@y.com"] is False

    @pytest.mark.parametrize("email", ["", "@x.com", "a@", "a@.com", "a@x.", "a@x", "a@b@c.com", "a b@x.com", "a@x.com\nBcc:b@y.com"])
    def test_not_a_single_address(self, world, email):
        assert not world.svc.is_single_address(email)

    @pytest.mark.parametrize("email", ["pat@example.com", "pat.o+tag@sub.example.co.uk", "o_k-1@example.io"])
    def test_a_real_address_passes(self, world, email):
        assert world.svc.is_single_address(email)


class TestTheMail:
    def test_it_says_who_which_agent_and_where(self, world):
        world.resend()
        mail = world.sent[0]
        assert world.owner in mail.body
        assert "Olga <b>Owner</b>" in mail.body
        assert world.agent in mail.body
        assert "https://trinity.example.com/workspace" in mail.body
        assert world.pending in mail.body

    def test_it_carries_no_code_and_escapes_what_it_quotes(self, world):
        world.resend()
        mail = world.sent[0]
        for text in (mail.body, mail.html):
            without_addresses = text.replace(world.pending, "").replace(world.owner, "").replace(world.agent, "")
            assert not re.search(r"(?<![#\w])\d{6}(?!\w)", re.sub(r"(style|href)=\"[^\"]*\"", "", without_addresses))
        assert "<b>Owner</b>" not in mail.html
        assert "&lt;b&gt;Owner&lt;/b&gt;" in mail.html
        assert mail.subject == world.svc.SUBJECT

    def test_with_no_known_url_it_still_says_where_and_has_no_link(self, world, monkeypatch):
        monkeypatch.delenv("FRONTEND_URL")
        assert world.resend().status_code == 200
        mail = world.sent[0]
        assert "Workspace" in mail.body
        assert "href=" not in mail.html

    def test_it_never_links_to_the_public_chat_host(self, world, monkeypatch):
        # ent#1255: the public host serves only chat; /workspace there is a 403.
        monkeypatch.delenv("FRONTEND_URL")
        monkeypatch.setenv("PUBLIC_CHAT_URL", "https://public.example.com")
        assert world.resend().status_code == 200
        mail = world.sent[0]
        assert "public.example.com" not in mail.body + mail.html
        assert "href=" not in mail.html
        assert "Workspace" in mail.body


class TestLimits:
    def test_the_fourth_to_one_address_in_an_hour_is_a_429(self, world):
        for _ in range(world.svc.ADDRESS_LIMIT):
            assert world.resend().status_code == 200
        r = world.resend()
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "invite_rate_limited"
        assert re.search(r"Try again in \d+ minutes?\.$", r.json()["detail"]["message"])
        assert int(r.headers["Retry-After"]) > 0
        assert len(world.sent) == world.svc.ADDRESS_LIMIT

    def test_one_inviter_cannot_mail_many_addresses(self, world, monkeypatch):
        monkeypatch.setattr(world.svc, "INVITER_LIMIT", 2)
        others = [f"{world.tag}-{i}@example.com" for i in range(3)]
        for other in others:
            world.db.share_agent(world.agent, world.owner, other)
        assert world.resend(email=others[0]).status_code == 200
        assert world.resend(email=others[1]).status_code == 200
        assert world.resend(email=others[2]).status_code == 429
        assert len(world.sent) == 2

    def test_two_owners_do_not_share_an_inviter_window(self, world, monkeypatch):
        monkeypatch.setattr(world.svc, "INVITER_LIMIT", 1)
        assert world.resend().status_code == 200
        assert world.resend(as_=world.admin).status_code == 200

    def test_a_refused_call_spends_no_quota(self, world):
        for _ in range(world.svc.ADDRESS_LIMIT + 1):
            assert world.resend(email=f"{world.shared}@example.com").status_code == 409
        assert world.resend().status_code == 200

    def test_nothing_being_deliverable_spends_no_quota(self, world, monkeypatch):
        monkeypatch.setattr(world.email_service, "can_deliver", lambda: False)
        for _ in range(world.svc.ADDRESS_LIMIT + 1):
            assert world.resend().status_code == 409
        monkeypatch.setattr(world.email_service, "can_deliver", lambda: True)
        assert world.resend().status_code == 200

    def test_a_share_and_unshare_loop_is_held_to_the_same_window(self, world):
        email = f"{world.tag}-loop@example.com"
        outcomes = []
        for _ in range(world.svc.ADDRESS_LIMIT + 1):
            outcomes.append(world.share(email).json()["invitation"])
            world.db.unshare_agent(world.agent, world.owner, email)
        assert outcomes == ["sent"] * world.svc.ADDRESS_LIMIT + ["rate_limited"]
        assert len(world.sent) == world.svc.ADDRESS_LIMIT

    def test_a_share_spends_what_resend_then_sees(self, world):
        email = f"{world.tag}-both@example.com"
        assert world.share(email).json()["invitation"] == "sent"
        for _ in range(world.svc.ADDRESS_LIMIT - 1):
            assert world.resend(email=email).status_code == 200
        assert world.resend(email=email).status_code == 429


class TestOnShare:
    def _shared(self, world, email):
        return any(s.shared_with_email == email for s in world.db.get_agent_shares(world.agent))

    def test_a_pending_person_is_invited_and_audited(self, world):
        email = f"{world.tag}-new@example.com"
        r = world.share(email)
        assert r.status_code == 200
        assert r.json()["invitation"] == "sent"
        assert [m.to for m in world.sent] == [email]
        assert _actions(world)[0]["details"] == {"shared_with": email, "trigger": "share", "outcome": "sent"}

    def test_someone_with_an_account_gets_no_mail(self, world):
        r = world.share(f"{world.stranger}@example.com")
        assert r.json()["invitation"] == "not_needed"
        assert world.sent == []
        assert _actions(world) == []

    def test_no_provider_still_shares_and_says_so(self, world, monkeypatch):
        monkeypatch.setattr(world.email_service, "can_deliver", lambda: False)
        email = f"{world.tag}-noprov@example.com"
        r = world.share(email)
        assert (r.status_code, r.json()["invitation"]) == (200, "unavailable")
        assert self._shared(world, email)
        assert world.sent == []

    def test_a_failed_mail_still_shares_and_says_so(self, world, monkeypatch):
        monkeypatch.setattr(world.email_service, "send_email", AsyncMock(return_value=False))
        email = f"{world.tag}-fail@example.com"
        r = world.share(email)
        assert (r.status_code, r.json()["invitation"]) == (200, "failed")
        assert self._shared(world, email)
        assert _actions(world)[0]["details"]["outcome"] == "failed"

    def test_a_fault_while_inviting_still_shares(self, world, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("database gone")
        monkeypatch.setattr(world.svc, "_pending_row", boom)
        email = f"{world.tag}-boom@example.com"
        r = world.share(email)
        assert (r.status_code, r.json()["invitation"]) == (200, "failed")
        assert self._shared(world, email)


class TestRoster:
    def _rows(self, world):
        r = world.client.get(f"/api/agents/{world.agent}/access", headers=world.headers(world.owner))
        assert r.status_code == 200
        return {row["email"]: row for row in r.json()}

    def test_only_a_pending_row_can_be_invited(self, world):
        rows = self._rows(world)
        assert rows[world.pending]["invite_available"] is True
        assert rows[f"{world.shared}@example.com"]["invite_available"] is False

    def test_a_pending_row_says_no_when_nothing_can_be_sent(self, world, monkeypatch):
        monkeypatch.setattr(world.email_service, "can_deliver", lambda: False)
        assert self._rows(world)[world.pending]["invite_available"] is False
