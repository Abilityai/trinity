"""
#3406 — a public link's own 401 never carries the platform's Bearer challenge.

The browser decides whose credential a 401 rejected by reading
`WWW-Authenticate` (`src/frontend/src/utils/platformSession.js`,
`isBearerChallenge`). On a public chat link (`/chat/:token`) the operator's
JWT rides every request, so the same 401 status can mean two things:

* the platform credential failed — `get_current_user` answers with
  `WWW-Authenticate: Bearer`; the page ends that dead session in place;
* the link's OWN 24-hour session expired — the link routes answer a bare 401;
  the page shows the visitor the verify card and leaves the operator alone.

If a link-session 401 ever gained the challenge, an operator previewing their
own expired link would be signed out of the platform — the second victim the
issue reports. If `get_current_user` lost it, a dead operator token on a public
page would go on retrying the WebSocket ticket every 5 seconds. Both halves of
the contract are pinned here, by DRIVING the handlers, not by reading them.

Driven: `dependencies.get_current_user`, `dependencies.oauth2_scheme`, and the
five `routers.public` link-session handlers, with the link check, the rate
limit and the session lookup stubbed.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _request(headers=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/api/x", "headers": raw,
                    "query_string": b""})


def _challenge(exc: HTTPException):
    return (exc.headers or {}).get("WWW-Authenticate")


def _raises_401(coro):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(coro)
    assert caught.value.status_code == 401
    return caught.value


# ---------------------------------------------------------------------------
# The platform credential: always challenged
# ---------------------------------------------------------------------------

def test_a_dead_platform_token_is_answered_with_the_bearer_challenge(monkeypatch):
    import dependencies

    # Not a JWT, and not an MCP key either — the path a revoked or expired
    # browser session takes after its JWT decode fails.
    monkeypatch.setattr(dependencies.db, "validate_mcp_api_key", lambda token: None)
    exc = _raises_401(dependencies.get_current_user(_request(), "not-a-live-token"))
    assert _challenge(exc) == "Bearer"


def test_no_token_at_all_is_answered_with_the_bearer_challenge():
    import dependencies

    exc = _raises_401(dependencies.oauth2_scheme(_request()))
    assert _challenge(exc) == "Bearer"


# ---------------------------------------------------------------------------
# The link's own session: never challenged
# ---------------------------------------------------------------------------

@pytest.fixture
def email_link(monkeypatch):
    """An email-required link whose visitor session is no longer valid."""
    import routers.public as pub
    from services import public_chat_service

    link = {"id": "link-1", "agent_name": "fin"}
    monkeypatch.setattr(pub, "_get_client_ip", lambda request: "1.2.3.4")
    monkeypatch.setattr(pub, "check_public_link_rate_limit", lambda ip: None)
    monkeypatch.setattr(pub, "_validate_public_link", lambda token: link)
    monkeypatch.setattr(public_chat_service, "agent_requires_email", lambda agent: True)
    monkeypatch.setattr(pub.db, "validate_session", lambda link_id, token: (False, None))
    monkeypatch.setattr(public_chat_service.db, "validate_session", lambda link_id, token: (False, None))
    monkeypatch.setattr(pub.db, "get_execution", lambda eid: SimpleNamespace(
        id=eid, agent_name="fin", triggered_by="public", status="running",
        source_user_email="visitor@example.com"))
    return pub, link


@pytest.mark.parametrize("session_token", ["expired-session", None],
                         ids=["expired session", "no session"])
def test_link_session_401s_carry_no_challenge(email_link, session_token):
    pub, link = email_link
    from db_models import PublicChatRequest

    req = _request()
    handlers = {
        "intro": pub.get_agent_intro("tok", req, session_token=session_token),
        "history": pub.get_public_chat_history("tok", req, session_token=session_token),
        "session clear": pub.clear_public_session("tok", req, session_token=session_token),
        "chat": pub.public_chat("tok", PublicChatRequest(message="hi", session_token=session_token), req),
        "terminate": pub.public_terminate_execution("tok", "e1", req, session_token=session_token),
    }
    for name, coro in handlers.items():
        exc = _raises_401(coro)
        assert _challenge(exc) is None, f"{name}: a link-session 401 must not challenge for Bearer"
