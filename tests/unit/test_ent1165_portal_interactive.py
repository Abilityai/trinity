"""trinity-enterprise#1165 — `PortalPrincipal.is_interactive`.

The Workspace principal carries whether a PLATFORM person signed in
interactively (a JWT session, `mcp_scope is None`). The enterprise projects
module offers its company-wide people reads (the steward picker) only to such a
principal, so every MCP key — user-scoped, system, ops — and a principal whose
scope cannot be read are False (fail closed). Driven through the real
`get_portal_principal`, with only the token resolver and the user row stubbed.
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


def _principal(monkeypatch, **user_fields):
    from client_portal import portal_auth
    import database

    user = SimpleNamespace(username="alice", role="operator", agent_name=None, connector_agent=None,
                           **user_fields)

    async def _resolve(request, token):
        return user

    monkeypatch.setattr(portal_auth, "decode_portal_session", lambda token: None, raising=False)
    monkeypatch.setattr(portal_auth, "resolve_platform_user_unfloored", _resolve)
    monkeypatch.setattr(portal_auth, "reject_agent_principal", lambda u: None, raising=False)
    monkeypatch.setattr(portal_auth, "_reject_if_blocked", lambda e: None)
    monkeypatch.setattr(portal_auth, "is_person_principal", lambda u: True, raising=False)
    monkeypatch.setattr(portal_auth, "_is_admin_principal", lambda u: False, raising=False)
    monkeypatch.setattr(database.db, "get_user_by_username", lambda name: {"email": "alice@example.com"})
    request = SimpleNamespace(headers={}, scope={}, state=SimpleNamespace())
    return asyncio.run(portal_auth.get_portal_principal(request, SimpleNamespace(headers={}), token="t"))


def test_a_session_jwt_is_interactive(monkeypatch):
    p = _principal(monkeypatch, mcp_scope=None)
    assert p.is_platform is True and p.is_interactive is True
    assert p.credential == "platform_session"


@pytest.mark.parametrize("scope", ["user", "system", "ops"])
def test_no_mcp_key_is_interactive(monkeypatch, scope):
    p = _principal(monkeypatch, mcp_scope=scope)
    assert p.is_platform is True and p.is_interactive is False


def test_an_unreadable_scope_is_not_interactive(monkeypatch):
    """No `mcp_scope` attribute at all reads as a key, never as a session."""
    p = _principal(monkeypatch)
    assert p.is_interactive is False


def test_the_field_defaults_to_false():
    """Every construction that does not say proves nothing (#3404's rule)."""
    from client_portal.portal_auth import PortalPrincipal
    assert PortalPrincipal("a@example.com", True, True).is_interactive is False
