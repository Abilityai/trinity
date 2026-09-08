"""Operator controls over a signed-in client: log out + block (ent#281).

Before this, a client who signed in through sharing held a 12-hour portal
session an operator could not end. The only levers were natural expiry and a
backend restart (`SECRET_KEY` rotation), which logs out *everyone*. Two controls
close that, and they are deliberately not the same shape:

  log out — end live sessions NOW. Global by construction: a portal token carries
            an email and no agent, so there is no per-agent session to end.
            Best-effort (Redis cutoff) and honest when it does not land.
  block   — bar the email from signing in again, anywhere. Admin-only, durable
            (a DB row), and therefore the control that still works with Redis
            down. Not delete: history/threads survive and unblock restores access.

The revocation mechanism is a per-email *cutoff*, not a jti list: `jti` is random
per token and nothing indexes email → issued jtis. The cutoff makes revocation
O(1) and — the property these tests care about most — impossible to forget on a
new mint path, because every mint goes through `create_portal_session_token`.

What is pinned here:
  * a revoke kills already-issued tokens and NOT tokens issued afterwards
  * an undatable (pre-#281) token is treated as revoked — fail closed
  * BOTH mint paths are gated: interactive OTP verify and the ent#163 delegated
    exchange, which is the hole the issue explicitly names
  * a blocked client's LIVE session dies even when the revoke never landed
    (Redis down) — the durable half doing its job
  * block ≠ delete, and unblock restores access with data intact
  * block is admin-only; log out is not
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def portal_db(tmp_path, monkeypatch):
    """Two clients with shares (bob, carol), one without (dave), + portal schema."""
    db_file = tmp_path / "trinity-281.db"
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
        conn.execute(insert(agent_sharing).values(
            agent_name="atlas", shared_with_email="bob@example.com",
            shared_by_id=1, created_at="2026-07-01T00:00:00Z"))
        conn.execute(insert(agent_sharing).values(
            agent_name="borealis", shared_with_email="carol@example.com",
            shared_by_id=1, created_at="2026-07-02T00:00:00Z"))
    yield str(db_file)


@pytest.fixture()
def fake_redis(monkeypatch):
    """A real fakeredis behind `get_breaker_redis` — exercises the actual SETEX/GET
    path rather than a stub that could agree with a wrong implementation."""
    import fakeredis
    import dependencies as deps

    r = fakeredis.FakeRedis()
    monkeypatch.setattr(deps, "get_breaker_redis", lambda: r)
    return r


@pytest.fixture()
def no_redis(monkeypatch):
    """Redis unavailable — the condition under which a log-out cannot land."""
    import dependencies as deps
    monkeypatch.setattr(deps, "get_breaker_redis", lambda: None)


# ---------------------------------------------------------------------------
# The OSS revocation primitive
# ---------------------------------------------------------------------------

def test_revoke_kills_an_already_issued_token(portal_db, fake_redis):
    from dependencies import (
        create_portal_session_token, decode_portal_session,
        revoke_portal_sessions_for_email,
    )

    token = create_portal_session_token("bob@example.com")
    assert decode_portal_session(token) == "bob@example.com"

    assert revoke_portal_sessions_for_email("bob@example.com") is True
    assert decode_portal_session(token) is None, "the live session must die immediately"


def test_revoke_does_not_kill_a_token_issued_afterwards(portal_db, fake_redis):
    """The cutoff is a point in time, not a permanent ban — that distinction is
    what separates 'log out' from 'block'. A logged-out client signs back in."""
    import time
    from dependencies import (
        create_portal_session_token, decode_portal_session,
        revoke_portal_sessions_for_email,
    )

    revoke_portal_sessions_for_email("bob@example.com")
    time.sleep(1.1)  # `iat` has 1s resolution and the check rejects iat <= cutoff
    fresh = create_portal_session_token("bob@example.com")
    assert decode_portal_session(fresh) == "bob@example.com"


def test_revoke_is_scoped_to_one_email(portal_db, fake_redis):
    from dependencies import (
        create_portal_session_token, decode_portal_session,
        revoke_portal_sessions_for_email,
    )

    bob = create_portal_session_token("bob@example.com")
    carol = create_portal_session_token("carol@example.com")
    revoke_portal_sessions_for_email("bob@example.com")

    assert decode_portal_session(bob) is None
    assert decode_portal_session(carol) == "carol@example.com", "one client's logout must not hit another"


def test_a_token_with_no_iat_is_treated_as_revoked(portal_db, fake_redis):
    """Fail CLOSED for undatable tokens.

    Only tokens minted before ent#281 shipped lack `iat`, and only for an email an
    operator actively revoked. Letting a token that cannot be shown to post-date
    the kill switch survive it is the worse failure.
    """
    from datetime import datetime, timedelta, timezone
    from dependencies import (
        ALGORITHM, SECRET_KEY, PORTAL_SESSION_SCOPE, decode_portal_session,
        revoke_portal_sessions_for_email,
    )
    from jose import jwt

    legacy = jwt.encode(
        {
            "scope": PORTAL_SESSION_SCOPE,
            "email": "bob@example.com",
            "exp": datetime.utcnow() + timedelta(hours=12),
            "jti": "legacy-token",
        },
        SECRET_KEY, algorithm=ALGORITHM,
    )
    assert decode_portal_session(legacy) == "bob@example.com", "valid before any revoke"

    revoke_portal_sessions_for_email("bob@example.com")
    assert decode_portal_session(legacy) is None


def test_revoke_reports_failure_when_redis_is_down(portal_db, no_redis):
    """A log-out that did not land must not report success — the operator would
    believe the session was ended when it is still live."""
    from dependencies import revoke_portal_sessions_for_email
    assert revoke_portal_sessions_for_email("bob@example.com") is False


# ---------------------------------------------------------------------------
# Block gates BOTH mint paths
# ---------------------------------------------------------------------------

def test_block_refuses_interactive_signin(portal_db, fake_redis):
    from client_portal import service

    assert service.email_has_access("bob@example.com") is True
    service.block_client("bob@example.com", actor_id="1",
                         actor_email="admin@example.com", reason="abuse")
    assert service.email_has_access("bob@example.com") is False
    assert service.portal_signin_request("bob@example.com") is None


def test_block_refuses_the_delegated_exchange(portal_db, fake_redis):
    """The hole the issue names: a licensee backend holding a `portal_delegate`
    key must not be able to mint around the block."""
    from client_portal import service

    assert service.portal_exchange("bob@example.com") is not None
    service.block_client("bob@example.com", actor_id="1", actor_email="admin@example.com")
    assert service.portal_exchange("bob@example.com") is None


def test_block_revokes_live_sessions_too(portal_db, fake_redis):
    """A block that leaves a 12-hour session running is not a block."""
    from dependencies import create_portal_session_token, decode_portal_session
    from client_portal import service

    token = create_portal_session_token("bob@example.com")
    result = service.block_client("bob@example.com", actor_id="1", actor_email="admin@example.com")

    assert result["sessions_revoked"] is True
    assert decode_portal_session(token) is None


def test_a_blocked_client_is_refused_even_when_the_revoke_never_landed(portal_db, monkeypatch):
    """The durable half carrying the feature.

    With Redis down the session cutoff cannot be written, so the client's live
    token still decodes. The block row is what stops them — this is the exact
    reason block is a DB row and not just a revoke.
    """
    import fakeredis
    import dependencies as deps
    from fastapi import HTTPException

    # Mint while Redis works, then take Redis away before blocking.
    r = fakeredis.FakeRedis()
    monkeypatch.setattr(deps, "get_breaker_redis", lambda: r)
    token = deps.create_portal_session_token("bob@example.com")

    from client_portal import service
    from client_portal.portal_auth import _reject_if_blocked

    monkeypatch.setattr(deps, "get_breaker_redis", lambda: None)
    result = service.block_client("bob@example.com", actor_id="1", actor_email="admin@example.com")
    assert result["sessions_revoked"] is False, "precondition: the revoke did NOT land"
    assert deps.decode_portal_session(token) == "bob@example.com", \
        "precondition: the token still decodes, so only the block can stop them"

    with pytest.raises(HTTPException) as exc:
        _reject_if_blocked("bob@example.com")
    assert exc.value.status_code == 403


def test_the_identity_gate_is_fail_closed_on_a_lookup_error(portal_db, monkeypatch):
    """A block that evaporates because a query failed is not a block."""
    from fastapi import HTTPException
    from client_portal import db as portal_db_mod
    from client_portal.portal_auth import _reject_if_blocked

    def _boom(email):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(portal_db_mod, "is_client_blocked", _boom)
    with pytest.raises(HTTPException) as exc:
        _reject_if_blocked("bob@example.com")
    assert exc.value.status_code == 403


def test_signin_is_fail_closed_on_a_lookup_error(portal_db, monkeypatch):
    from client_portal import db as portal_db_mod
    from client_portal import service

    def _boom(email):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(portal_db_mod, "is_client_blocked", _boom)
    assert service.email_has_access("bob@example.com") is False


# ---------------------------------------------------------------------------
# Block is not delete (ent#21 consistency)
# ---------------------------------------------------------------------------

def test_block_retains_data_and_unblock_restores_access(portal_db, fake_redis):
    from client_portal import db as portal_db_mod
    from client_portal import service

    portal_db_mod.create_portal_session("s1", "atlas", "bob@example.com", "2026-07-10T00:00:00Z")
    portal_db_mod.add_portal_message("m1", "atlas", "bob@example.com", "user",
                                     "hello", None, "2026-07-10T00:00:00Z", session_id="s1")

    service.block_client("bob@example.com", actor_id="1", actor_email="admin@example.com")
    assert service.email_has_access("bob@example.com") is False
    # The client's data is untouched while blocked.
    assert len(portal_db_mod.get_portal_messages("atlas", "bob@example.com")) == 1

    out = service.unblock_client("bob@example.com")
    assert out["was_blocked"] is True
    assert service.email_has_access("bob@example.com") is True
    assert len(portal_db_mod.get_portal_messages("atlas", "bob@example.com")) == 1


def test_unblock_is_honest_when_there_was_nothing_to_lift(portal_db, fake_redis):
    from client_portal import service
    assert service.unblock_client("carol@example.com")["was_blocked"] is False


def test_re_blocking_updates_rather_than_erroring(portal_db, fake_redis):
    """A double-click must not surface as an error the operator has to interpret."""
    from client_portal import db as portal_db_mod
    from client_portal import service

    service.block_client("bob@example.com", actor_id="1", actor_email="admin@example.com", reason="first")
    service.block_client("bob@example.com", actor_id="1", actor_email="admin@example.com", reason="second")
    assert portal_db_mod.get_client_block("bob@example.com")["reason"] == "second"


# ---------------------------------------------------------------------------
# Normalisation + validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", "not-an-email", "no@tld", "a b@c.com", "@example.com"])
def test_a_malformed_email_is_a_named_422(portal_db, fake_redis, bad):
    from client_portal.service import ClientPortalError, normalize_client_email

    with pytest.raises(ClientPortalError) as exc:
        normalize_client_email(bad)
    assert exc.value.status_code == 422
    assert "email" in exc.value.detail.lower()


def test_block_matches_regardless_of_the_case_it_was_typed_in(portal_db, fake_redis):
    """`agent_sharing` stores whatever case the operator typed; the block store,
    the session token and the gates all key on the lowercased address. If they
    disagreed, blocking `Bob@Example.com` would leave `bob@example.com` signed in.
    """
    from client_portal import service

    service.block_client("  BOB@Example.COM  ", actor_id="1", actor_email="admin@example.com")
    assert service.email_has_access("bob@example.com") is False


# ---------------------------------------------------------------------------
# The operator's status view
# ---------------------------------------------------------------------------

def test_roster_shows_state_so_the_operator_can_see_it_took_effect(portal_db, fake_redis):
    from client_portal import db as portal_db_mod
    from client_portal import service

    portal_db_mod.create_portal_session("s1", "atlas", "bob@example.com", "2026-07-10T00:00:00Z")
    portal_db_mod.touch_portal_session("s1", "2026-07-11T09:00:00Z", added=2, title_if_empty="hi")

    roster = service.get_agent_client_roster("atlas")
    assert [c["email"] for c in roster] == ["bob@example.com"], "roster is the agent's shares"
    entry = roster[0]
    assert entry["blocked"] is False
    assert entry["last_active"] == "2026-07-11T09:00:00Z"
    assert entry["message_count"] == 2
    assert entry["sessions_revoked_at"] is None
    assert "active_sessions" not in entry, (
        "portal sessions are stateless JWTs — a live-session count would be a guess"
    )

    service.block_client("bob@example.com", actor_id="1",
                         actor_email="admin@example.com", reason="spam")
    entry = service.get_agent_client_roster("atlas")[0]
    assert entry["blocked"] is True
    assert entry["block_reason"] == "spam"
    assert entry["blocked_by_email"] == "admin@example.com"
    assert entry["sessions_revoked_at"] is not None, "the log-out that rode along is visible"


def test_roster_is_scoped_to_the_agent(portal_db, fake_redis):
    """An operator standing at one agent must not see another agent's clients."""
    from client_portal import service

    assert [c["email"] for c in service.get_agent_client_roster("borealis")] == ["carol@example.com"]


def test_a_block_is_global_and_shows_on_every_agents_roster(portal_db, fake_redis):
    """The scope decision made explicit: block bars the person, not the pairing.

    Carol is shared only on `borealis`; blocking her there is what bars her
    everywhere. If this ever becomes per-(agent, email), this test is the one
    that should fail first.
    """
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_sharing
    from client_portal import service

    with get_engine().begin() as conn:
        conn.execute(insert(agent_sharing).values(
            agent_name="atlas", shared_with_email="carol@example.com",
            shared_by_id=1, created_at="2026-07-03T00:00:00Z"))

    service.block_client("carol@example.com", actor_id="1", actor_email="admin@example.com")
    for agent in ("atlas", "borealis"):
        entry = [c for c in service.get_agent_client_roster(agent)
                 if c["email"] == "carol@example.com"][0]
        assert entry["blocked"] is True, f"the block must be visible from {agent} too"


# ---------------------------------------------------------------------------
# The permission boundary between the two controls
# ---------------------------------------------------------------------------
#
# `OwnedAgentByName` already established the caller owns the agent they are
# standing at. The question these pin is what that ownership buys: ending a
# session (yes) vs barring a person platform-wide (no — admin only), because a
# global effect granted to any owner lets one owner lock a client out of a
# different owner's agents.

def _User(role="user", username="olivia", email="olivia@example.com",
          agent_name=None, user_id=7):
    """The REAL principal model, not a stub.

    `assert_admin` runs `_reject_connector_principal` first, which reads fields a
    hand-rolled stub does not have — so a stub would silently test a different
    object than production passes.
    """
    from models import User
    return User(id=user_id, username=username, email=email, role=role,
                agent_name=agent_name)


class _Req:
    class _C:
        host = "10.0.0.1"

    client = _C()
    url = type("U", (), {"path": "/api/enterprise/client-portal/x"})()

    class _S:
        request_id = "req-1"

    state = _S()


@pytest.fixture()
def quiet_audit(monkeypatch):
    """The audit sink writes to the OSS audit_log table, which this fixture's DB
    does not create. Record calls instead — the assertions are about *whether* an
    action is audited, not about the platform sink's own storage.

    Patch the ROUTER's binding, not `services.platform_audit_service`'s singleton.
    `router.py` does a module-level `from services.platform_audit_service import
    platform_audit_service`, so it holds whichever singleton was live when IT was
    imported. That key is on conftest's #762 invariant-restore list, so the module
    object in `sys.modules` is swapped back to the baseline between tests — and
    once the two diverge, patching the singleton patches an object the router
    never calls. The real sink then runs against this fixture's DB, which has no
    `audit_log` table, and the assertion fails on an empty list — which reads like
    "the code forgot to audit" rather than "the patch missed".

    The router module object the test holds is the one the test calls into, so
    patching its attribute is correct by construction, whatever sys.modules says.
    """
    from client_portal import router as portal_router

    calls = []

    async def _log(**kw):
        calls.append(kw)

    monkeypatch.setattr(portal_router.platform_audit_service, "log", _log)
    return calls


def test_a_non_admin_owner_may_log_a_client_out(portal_db, fake_redis, quiet_audit):
    import asyncio
    from dependencies import create_portal_session_token, decode_portal_session
    from client_portal import router as portal_router

    token = create_portal_session_token("bob@example.com")
    result = asyncio.run(portal_router.logout_agent_client(
        "atlas", "bob@example.com", _Req(), _User(role="user")))

    assert result.revoked is True
    assert decode_portal_session(token) is None
    assert quiet_audit[0]["event_action"] == "portal_client_logout"


def test_a_non_admin_owner_may_not_block(portal_db, fake_redis, quiet_audit):
    import asyncio
    from fastapi import HTTPException
    from client_portal import router as portal_router
    from client_portal.models import PortalBlockRequest
    from client_portal import service

    with pytest.raises(HTTPException) as exc:
        asyncio.run(portal_router.block_agent_client(
            "atlas", "bob@example.com", PortalBlockRequest(), _Req(), _User(role="user")))
    assert exc.value.status_code == 403
    assert service.email_has_access("bob@example.com") is True, "the refusal must not half-apply"
    assert not quiet_audit, "a refused action writes no success row"


def test_an_admin_may_block_and_it_is_audited(portal_db, fake_redis, quiet_audit):
    import asyncio
    from client_portal import router as portal_router
    from client_portal.models import PortalBlockRequest
    from client_portal import service

    result = asyncio.run(portal_router.block_agent_client(
        "atlas", "bob@example.com", PortalBlockRequest(reason="abuse"), _Req(),
        _User(role="admin", username="admin", email="admin@example.com")))

    assert result.blocked is True
    assert service.email_has_access("bob@example.com") is False
    row = quiet_audit[0]
    assert row["event_action"] == "portal_client_block"
    assert row["target_id"] == "bob@example.com"
    assert row["details"]["reason"] == "abuse"


def test_an_agent_scoped_key_may_not_block(portal_db, fake_redis, quiet_audit):
    """`assert_admin` rejects an agent principal before the role check.

    An agent-scoped key resolves to its OWNER and carries the owner's role, so on
    a default admin-owned install a role check alone would let a prompt-injected
    agent bar a human (the trinity-ops-agent#232 shape).
    """
    import asyncio
    from fastapi import HTTPException
    from client_portal import router as portal_router
    from client_portal.models import PortalBlockRequest
    from client_portal import service

    with pytest.raises(HTTPException) as exc:
        asyncio.run(portal_router.block_agent_client(
            "atlas", "bob@example.com", PortalBlockRequest(), _Req(),
            _User(role="admin", agent_name="atlas")))
    assert exc.value.status_code == 403
    assert service.email_has_access("bob@example.com") is True


def test_unblock_is_admin_only_too(portal_db, fake_redis, quiet_audit):
    """An asymmetric pair would be the bug: if any owner could unblock, the
    admin-only block would be trivially undone by the person it constrains."""
    import asyncio
    from fastapi import HTTPException
    from client_portal import router as portal_router

    with pytest.raises(HTTPException) as exc:
        asyncio.run(portal_router.unblock_agent_client(
            "atlas", "bob@example.com", _Req(), _User(role="user")))
    assert exc.value.status_code == 403


def test_a_malformed_email_never_reaches_the_store(portal_db, fake_redis, quiet_audit):
    import asyncio
    from fastapi import HTTPException
    from client_portal import router as portal_router

    with pytest.raises(HTTPException) as exc:
        asyncio.run(portal_router.logout_agent_client(
            "atlas", "not-an-email", _Req(), _User(role="user")))
    assert exc.value.status_code == 422
    assert not quiet_audit


def test_an_agent_scoped_key_may_not_log_a_client_out(portal_db, fake_redis, quiet_audit):
    """Log-out is a lesser power than block, but still a decision about a
    person's access — and an agent-scoped key resolves to its owner, so without
    an explicit human-only guard a prompt-injected agent could disconnect the
    clients of the agent it runs as."""
    import asyncio
    from fastapi import HTTPException
    from dependencies import create_portal_session_token, decode_portal_session
    from client_portal import router as portal_router

    token = create_portal_session_token("bob@example.com")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portal_router.logout_agent_client(
            "atlas", "bob@example.com", _Req(), _User(role="admin", agent_name="atlas")))
    assert exc.value.status_code == 403
    assert decode_portal_session(token) == "bob@example.com", "the refusal must not half-apply"


def test_an_agent_scoped_key_may_not_unblock(portal_db, fake_redis, quiet_audit):
    import asyncio
    from fastapi import HTTPException
    from client_portal import router as portal_router

    with pytest.raises(HTTPException) as exc:
        asyncio.run(portal_router.unblock_agent_client(
            "atlas", "bob@example.com", _Req(), _User(role="admin", agent_name="atlas")))
    assert exc.value.status_code == 403
