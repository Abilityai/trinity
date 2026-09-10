"""#2689 — `/auth/exchange` must actually run, not just resolve.

`portal_auth_exchange` read the sliding-session idle window through a name the
module never imports:

    idle_s, _ = settings_service.get_portal_session_policy()   # NameError

so every call to the ent#163 trusted-issuer seam answered **500**. Present on
`dev` and on `main` — released, not a dev-only regression. Introduced by ent#375
(#2099), which added the idle-window read to the response; `dependencies.py`
performs the identical read correctly and the router copied the call without the
import.

WHY EIGHT TESTS MISSED IT. `test_ent163_portal_delegated_identity.py` and
`test_163_portal_delegate_scope.py` both exercise `service.portal_exchange(...)`
— the service — and assert true things about the mint, the access rule, scope
fencing and revocation. None of them executes the ROUTE HANDLER, and the
`NameError` is in the handler body, three lines after the service returns. A
test that never runs the code under test cannot fail when that code is broken.

So this file's contract is narrow and deliberate: **drive the handler**. It
asserts almost nothing about behaviour the other files already own; its job is
to be the test that goes red when the endpoint cannot answer.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def delegate_db(tmp_path, monkeypatch):
    """One email with a share, one without. Mirrors the ent#163 fixture."""
    db_file = tmp_path / "trinity-2689.db"
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
    ensure_schema_tables(
        "enterprise_portal_sessions", "enterprise_portal_messages",
        "enterprise_client_blocks",
    )

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(
            id=1, username="admin", role="admin", email="admin@example.com",
            created_at="t", updated_at="t"))
        conn.execute(insert(agent_ownership).values(
            agent_name="atlas", owner_id=1, created_at="t", is_system=0, deleted_at=None))
        conn.execute(insert(agent_sharing).values(
            agent_name="atlas", shared_with_email="bob@example.com",
            shared_by_id=1, created_at="t"))
    yield str(db_file)


def _request():
    return SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.1"),
        url=SimpleNamespace(path="/api/enterprise/client-portal/auth/exchange"),
        state=SimpleNamespace(request_id="req-2689"),
    )


def _delegate():
    return SimpleNamespace(
        id=1, username="issuer", email="admin@example.com", role="admin",
        portal_delegate=True, mcp_scope="portal_delegate", agent_name=None,
    )


async def _call(email: str):
    from client_portal.models import PortalExchangeRequest
    from client_portal.router import portal_auth_exchange
    return await portal_auth_exchange(
        PortalExchangeRequest(email=email), _request(), _delegate(),
    )


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_route_answers_instead_of_raising(delegate_db):
    """Before the fix this raised NameError, which FastAPI serves as a 500."""
    from dependencies import decode_portal_session

    out = await _call("Bob@Example.com")

    assert out.email == "bob@example.com", "the address is case-normalised"
    assert decode_portal_session(out.token) == "bob@example.com", (
        "the token the route hands back must be a real portal session"
    )


@pytest.mark.asyncio
async def test_expires_in_is_the_real_idle_window(delegate_db):
    """The value ent#375 added, and the reason the broken line existed at all.

    Asserted against `dependencies._portal_session_policy()` — the ONE reader of
    that setting — rather than a literal, so a change to the policy moves both
    together instead of turning this into a second source of truth.
    """
    import dependencies as deps

    idle_s, _absolute_s = deps._portal_session_policy()
    out = await _call("bob@example.com")

    assert out.expires_in == idle_s
    assert out.expires_in > 0, "a non-positive lifetime would be unusable"


@pytest.mark.asyncio
async def test_the_settings_read_cannot_500_an_auth_path(delegate_db, monkeypatch):
    """A settings failure degrades; it does not take the endpoint down.

    This is why the fix reuses `dependencies._portal_session_policy` rather than
    re-importing `settings_service` in the router: the degrade is part of the
    read, and a second copy of the call would be a second chance to omit it.
    """
    import services.settings_service as ss

    def _boom(*a, **k):
        raise RuntimeError("settings backend unavailable")

    monkeypatch.setattr(ss.settings_service, "get_portal_session_policy", _boom)

    out = await _call("bob@example.com")
    assert out.token, "the exchange still succeeds"
    assert out.expires_in > 0, "and still reports a usable lifetime"


# ---------------------------------------------------------------------------
# The gates the route already owned — pinned here because they now run
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_non_delegate_principal_is_refused_before_any_work(delegate_db):
    from fastapi import HTTPException
    from client_portal.models import PortalExchangeRequest
    from client_portal.router import portal_auth_exchange

    plain_admin = SimpleNamespace(
        id=1, username="admin", email="admin@example.com", role="admin",
        portal_delegate=False, mcp_scope=None, agent_name=None,
    )
    with pytest.raises(HTTPException) as e:
        await portal_auth_exchange(
            PortalExchangeRequest(email="bob@example.com"), _request(), plain_admin,
        )
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_an_email_with_no_share_is_refused(delegate_db):
    """Trinity's access rule, not the issuer's assertion — reached through the
    route this time, so the 403 is the one a caller actually receives."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        await _call("dave@example.com")
    assert e.value.status_code == 403
