"""
A delegate-minted Workspace session is not a proven person for self-approval,
and the self-approval audit row says which credential it rested on (#3404).

A Workspace session minted through a `portal_delegate` key (`portal_exchange`)
was the same token as a person's own emailed-code session, so the holder of a
delegate key could run a gated skill for any approver it could mint a session
for. The backstop's audit row (`current_user=None`) recorded no credential
kind. And `PortalPrincipal.is_person` defaulted to True.

Targets, each through its own layer:
  * the mint — `client_portal.service.portal_signin_verify` / `portal_exchange`
    (the real token), read back by `portal_auth.get_portal_principal`;
  * the rotation — `dependencies.renew_portal_session` keeps what the session
    was minted by;
  * the route — `client_portal.router.portal_chat` / `portal_chat_stream`;
  * the turn — `client_portal.service.portal_chat` builds the gate requester;
  * the backstop — `TaskExecutionService.execute_task` over a real per-test
    database (the `test_ent751_gate_entries` world): held or run, and the row.

Stubbed: the share lookup, the login-code check, the block lookup, Redis
(fakeredis), and what the reused harnesses already stub.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_harness import db_backend  # noqa: E402,F401
from test_ent751_gate_entries import (  # noqa: E402,F401
    FIN, OWNER_EMAIL, _GATE, _stop_at_admission, _workspace, db, world)

pytestmark = pytest.mark.unit

CLIENT = "client@example.com"


# ---------------------------------------------------------------------------
# The mint and the principal
# ---------------------------------------------------------------------------

@pytest.fixture()
def portal(monkeypatch):
    """The real mint + the real principal dependency, with the lookups stubbed."""
    import database
    import dependencies
    import fakeredis
    from client_portal import db as portal_db
    from client_portal import portal_auth, service

    monkeypatch.setattr(service, "email_has_access", lambda email: True)
    monkeypatch.setattr(database.db, "verify_login_code", lambda email, code: True)
    monkeypatch.setattr(portal_db, "is_client_blocked", lambda email: False)
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: fake)
    return SimpleNamespace(service=service, auth=portal_auth, deps=dependencies)


def _principal_for(portal, token):
    return asyncio.run(portal.auth.get_portal_principal(
        SimpleNamespace(), SimpleNamespace(headers={}), token=token))


def test_a_persons_own_code_session_may_self_approve(portal):
    token = portal.service.portal_signin_verify(CLIENT, "123456")
    principal = _principal_for(portal, token)
    assert (principal.email, principal.is_platform, principal.is_person) == (CLIENT, False, True)
    assert principal.self_approves is True
    assert principal.credential == "portal_session:otp"


def test_a_delegate_minted_session_is_not_proven_for_self_approval(portal):
    """The reported defect. It stays a person for the ask routes (ent#611 is
    not narrowed here) — only self-approval is withheld."""
    token = portal.service.portal_exchange(CLIENT)
    principal = _principal_for(portal, token)
    assert (principal.email, principal.is_platform, principal.is_person) == (CLIENT, False, True)
    assert principal.self_approves is False
    assert principal.credential == "portal_session:delegate"


def test_rotation_keeps_what_the_session_was_minted_by(portal):
    """A slid session is a new token; a delegate session must not become a
    person's own by being used, nor a person's own lose its proof."""
    for mint, expected in ((lambda: portal.service.portal_exchange(CLIENT), False),
                           (lambda: portal.service.portal_signin_verify(CLIENT, "1"), True)):
        fresh = portal.deps.renew_portal_session(mint())
        assert fresh
        assert _principal_for(portal, fresh).self_approves is expected


def test_a_session_that_does_not_say_how_it_was_minted_is_unproven(portal):
    """A token from before the claim existed cannot be shown to be the person's
    own, so it does not self-approve (it still signs in and still answers)."""
    token = portal.deps.create_portal_session_token(CLIENT)
    principal = _principal_for(portal, token)
    assert principal.is_person is True
    assert principal.self_approves is False
    assert principal.credential == "portal_session:unmarked"


@pytest.mark.parametrize("scope,person,credential", [
    (None, True, "platform_session"),
    ("user", True, "mcp_key:user"),
    ("system", False, "mcp_key:system"),
])
def test_a_platform_principal_self_approves_only_as_a_person(portal, monkeypatch, scope, person, credential):
    import database

    user = SimpleNamespace(username="u1", role="user", mcp_scope=scope, agent_name=None,
                           connector_agent=None, portal_delegate=False)

    async def current_user(request, token):
        return user
    # trinity-enterprise#837: the Workspace door resolves a platform credential
    # through the unfloored resolver, not `get_current_user`.
    monkeypatch.setattr(portal.auth, "resolve_platform_user_unfloored", current_user)
    monkeypatch.setattr(database.db, "get_user_by_username", lambda name: {"email": "U1@Example.com"})
    principal = _principal_for(portal, "not-a-portal-session")
    assert (principal.is_person, principal.self_approves) == (person, person)
    assert principal.credential == credential


def test_a_principal_built_without_the_facts_proves_nothing():
    """The fail-open default: a constructor that forgets is not a person."""
    from client_portal.portal_auth import PortalPrincipal

    principal = PortalPrincipal(CLIENT, False)
    assert principal.is_person is False
    assert principal.self_approves is False


# ---------------------------------------------------------------------------
# The route and the turn
# ---------------------------------------------------------------------------

from test_ent751_gate_callers import (  # noqa: E402
    AGENT, _P_EMAIL, _P_SESSION, _portal_turn_raising, _refused, _stub_route)
from test_ent751_gate_callers import svc  # noqa: E402,F401


def test_both_workspace_routes_withhold_self_approval_from_a_delegate_session(monkeypatch):
    from starlette.requests import Request
    from client_portal.models import PortalChatRequest
    from client_portal.portal_auth import PortalPrincipal

    portal_router = _stub_route(monkeypatch)
    calls: dict = {}

    async def _chat(*a, **kw):
        calls["sync"] = kw
        return {"response": "ok", "cost": 0.0, "session_id": "s1", "message_id": None}

    async def _start(*a, **kw):
        calls["stream"] = kw
        return {"execution_id": "e1", "session_id": "s1", "wait_budget_seconds": 60}

    monkeypatch.setattr(portal_router.service, "portal_chat", _chat)
    monkeypatch.setattr(portal_router.service, "start_portal_turn", _start)
    # A person for the ask routes, unproven for self-approval.
    principal = PortalPrincipal(_P_EMAIL, False, True, False, self_approves=False,
                                credential="portal_session:delegate")
    body = PortalChatRequest(message="/pay-invoice 1")
    asyncio.run(portal_router.portal_chat(AGENT, body, principal=principal))
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
    asyncio.run(portal_router.portal_chat_stream(AGENT, body, request, principal=principal))
    for path in ("sync", "stream"):
        assert calls[path]["gate_is_person"] is False, path
        assert calls[path]["gate_credential"] == "portal_session:delegate", path


def test_the_turn_tells_the_gate_which_credential_asked(svc, monkeypatch):
    from client_portal.service import ClientPortalError

    seen = _portal_turn_raising(svc, monkeypatch, _refused())
    with pytest.raises(ClientPortalError):
        asyncio.run(svc.portal_chat(AGENT, "hi", _P_EMAIL, session_id=_P_SESSION,
                                    gate_is_person=True, gate_credential="portal_session:otp"))
    req = seen["gate_requester"]
    assert (req.is_person, req.credential) == (True, "portal_session:otp")


# ---------------------------------------------------------------------------
# The backstop: held or run, and what the audit row says
# ---------------------------------------------------------------------------

def _requester(portal_principal):
    """The gate requester exactly as `client_portal.service.portal_chat` builds it."""
    from client_portal import service

    return service._gate_requester(portal_principal.email, portal_principal.self_approves,
                                   portal_principal.credential)


def test_a_delegate_minted_session_for_the_approver_is_held_not_run(world, portal, monkeypatch):
    seen = _stop_at_admission(monkeypatch)
    principal = _principal_for(portal, portal.service.portal_exchange(OWNER_EMAIL))
    with pytest.raises(_GATE.SkillApprovalRequired):
        _workspace(message="/pay-invoice 1", source_user_email=OWNER_EMAIL,
                   gate_requester=_requester(principal))
    assert seen == []
    assert db.count_pending_gate_requests(FIN) == 1
    assert [a for a in world.audits if a.get("event_action") == "skill_gate_self_approved"] == []


def test_the_approvers_own_session_runs_it_and_the_audit_row_names_the_credential(
        world, portal, monkeypatch):
    seen = _stop_at_admission(monkeypatch)
    principal = _principal_for(portal, portal.service.portal_signin_verify(OWNER_EMAIL, "1"))
    _workspace(message="/pay-invoice 1", source_user_email=OWNER_EMAIL,
               gate_requester=_requester(principal))
    assert seen, "the approver's own request must reach admission"
    rows = [a for a in world.audits if a.get("event_action") == "skill_gate_self_approved"]
    assert len(rows) == 1 and rows[0]["actor_email"] == OWNER_EMAIL
    assert rows[0]["details"]["credential"] == "portal_session:otp"
