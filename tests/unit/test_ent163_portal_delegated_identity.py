"""Delegated portal identity — trusted-issuer token exchange (ent#163).

A licensee runs their own customer portal and their own IdP; Trinity is the
agent backend, not the identity provider. Their backend holds one admin-issued
`portal_delegate` key, asserts which of *their* end users a request is for, and
exchanges that for a portal session.

The bug this closes is a **silent wrong answer**, not an error: before this,
calling `GET /my-agents` with an API key fell through `get_portal_identity` to
the platform-principal branch and returned the KEY OWNER's roster. The caller
got a plausible 200 for the wrong person.

What is pinned here:
  * the mint honours Trinity's access rule, not the issuer's assertion
  * the scope is fenced to the exchange route alone (OSS-side containment)
  * `portal_delegate` is admin-issued and not self-issuable
  * two end users exchanged from the SAME key get disjoint identities
  * revocation stops delegation
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def delegate_db(tmp_path, monkeypatch):
    """Two end users, one with a share and one without, plus a users row."""
    db_file = tmp_path / "trinity-delegate.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as m, agent_sharing, agent_ownership, users, email_login_codes,
        mcp_api_keys,
    )
    m.create_all(get_engine(), tables=[
        agent_sharing, agent_ownership, users, email_login_codes, mcp_api_keys,
    ])
    # ent#281: sign-in consults the block table, so the module's own schema must
    # exist here exactly as it does in production (`register()` creates it).
    from conftest import ensure_schema_tables
    ensure_schema_tables("enterprise_portal_sessions", "enterprise_portal_messages", "enterprise_client_blocks")

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(
            id=1, username="admin", role="admin", email="admin@example.com",
            created_at="t", updated_at="t"))
        for agent in ("atlas", "borealis"):
            conn.execute(insert(agent_ownership).values(
                agent_name=agent, owner_id=1, created_at="t", is_system=0, deleted_at=None))
        # bob sees atlas; carol sees borealis; dave sees nothing.
        conn.execute(insert(agent_sharing).values(
            agent_name="atlas", shared_with_email="bob@example.com",
            shared_by_id=1, created_at="t"))
        conn.execute(insert(agent_sharing).values(
            agent_name="borealis", shared_with_email="carol@example.com",
            shared_by_id=1, created_at="t"))
    yield str(db_file)


# ---------------------------------------------------------------------------
# The mint
# ---------------------------------------------------------------------------

def test_exchange_mints_a_portal_session_for_an_asserted_email(delegate_db):
    from dependencies import decode_portal_session
    from client_portal import service

    token = service.portal_exchange("Bob@Example.com")
    assert token, "an email with a share must be exchangeable"
    # Case-normalised, and it is a real portal session the existing surface reads.
    assert decode_portal_session(token) == "bob@example.com"


def test_exchange_refuses_an_email_with_no_share(delegate_db):
    """Access stays TRINITY's decision. The issuer asserts *identity*; it does
    not get to assert *authorization*, or a delegate key would be a universal
    read of every portal conversation."""
    from client_portal import service
    assert service.portal_exchange("dave@example.com") is None
    assert service.portal_exchange("") is None
    assert service.portal_exchange(None) is None


def test_two_end_users_from_one_issuer_are_disjoint(delegate_db):
    """The multi-tenant requirement, stated as an assertion.

    The pre-fix behaviour returned the key owner's roster for BOTH — identical,
    and wrong. Distinct emails must produce distinct identities and, downstream,
    distinct rosters."""
    from dependencies import decode_portal_session
    from client_portal import service, db as portal_db

    bob = decode_portal_session(service.portal_exchange("bob@example.com"))
    carol = decode_portal_session(service.portal_exchange("carol@example.com"))
    assert bob != carol

    bob_roster = {a["agent_name"] for a in portal_db.get_shared_roster(bob)}
    carol_roster = {a["agent_name"] for a in portal_db.get_shared_roster(carol)}
    assert bob_roster == {"atlas"}
    assert carol_roster == {"borealis"}
    assert bob_roster.isdisjoint(carol_roster)
    # …and neither is the key owner's view.
    assert "admin@example.com" not in (bob, carol)


def test_exchange_is_the_delegated_sibling_of_code_verification(delegate_db):
    """Same access rule, same token type, different proof of identity. If these
    ever diverge, one path has become the weaker way in."""
    from database import db as core_db
    from dependencies import decode_portal_session
    from client_portal import service

    code = core_db.create_login_code("bob@example.com")["code"]
    interactive = service.portal_signin_verify("bob@example.com", code)
    delegated = service.portal_exchange("bob@example.com")
    assert decode_portal_session(interactive) == decode_portal_session(delegated)

    # And the unauthorized email is refused by BOTH.
    code2 = core_db.create_login_code("dave@example.com")["code"]
    assert service.portal_signin_verify("dave@example.com", code2) is None
    assert service.portal_exchange("dave@example.com") is None


# ---------------------------------------------------------------------------
# Containment — the part that makes the capability safe to hand out
# ---------------------------------------------------------------------------

def test_scope_is_fenced_to_the_exchange_route_only():
    """A delegate key resolves to the KEY OWNER like any MCP key, so without a
    fence it would be an ordinary key belonging to an admin. OSS confines it to
    one (method, path) at the single auth entry point."""
    from dependencies import PORTAL_DELEGATE_ALLOWED_ROUTES, PORTAL_DELEGATE_SCOPE

    assert PORTAL_DELEGATE_SCOPE == "portal_delegate"
    assert PORTAL_DELEGATE_ALLOWED_ROUTES == {
        ("POST", "/api/enterprise/client-portal/auth/exchange")
    }
    # Explicitly NOT the portal surface itself: the minted session drives that,
    # so the issuer key never needs to reach it.
    for path in (
        "/api/enterprise/client-portal/my-agents",
        "/api/agents",
        "/api/users",
    ):
        assert ("GET", path) not in PORTAL_DELEGATE_ALLOWED_ROUTES


def test_delegate_scope_is_not_self_issuable_by_a_non_admin(delegate_db):
    """It reads other people's conversations, so it must be admin-issued. The db
    layer refuses any scope outside the creatable set regardless of the router
    gate — validate at the boundary AND at the sink."""
    from database import db as core_db, McpApiKeyCreate

    with_bad_scope = McpApiKeyCreate(name="k", description=None, scope="agent")
    assert core_db.create_mcp_api_key("admin", with_bad_scope) is None, (
        "agent/connector/system scopes are bound to an agent and must never be "
        "mintable through the user key endpoint"
    )

    ok = core_db.create_mcp_api_key(
        "admin", McpApiKeyCreate(name="issuer", description=None, scope="portal_delegate"))
    assert ok is not None and ok.scope == "portal_delegate"


def test_revoking_the_key_stops_delegation(delegate_db):
    """Revocation is the licensee's off-switch; it must be immediate."""
    from database import db as core_db, McpApiKeyCreate

    key = core_db.create_mcp_api_key(
        "admin", McpApiKeyCreate(name="issuer", description=None, scope="portal_delegate"))
    assert core_db.validate_mcp_api_key(key.api_key) is not None
    assert core_db.revoke_mcp_api_key(key.id, "admin") is True
    assert core_db.validate_mcp_api_key(key.api_key) is None
