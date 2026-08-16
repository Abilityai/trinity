"""#2198 — the Workspace sidebar's thread list in ONE query instead of N+1.

The sidebar renders a merged, cross-agent, recency-sorted list, so it asked the
per-agent route once per rostered agent — on bootstrap, on every thread open,
and on every completed turn. Each of those cost 2-3 DB queries, because
`list_sessions` re-resolves the roster through `agent_on_roster` before it
touches the session table.

What actually has to be true
----------------------------
The performance claim is the easy part. The property that matters is that
collapsing N gated reads into one ungated-looking read did NOT widen what the
caller can see. Two things enforce it, and both are tested here:

  * The batch is scoped by `roster_agent_names`, the SAME set `agent_on_roster`
    enforces per agent — extracted so the boundary is one implementation rather
    than two that merely agree today. The equivalence test below is what would
    catch them drifting.
  * `agent_name IN (:agents)` is not an optimisation, it is the tenant scope.
    Filtering on `client_email` alone would return threads for an agent that was
    un-shared — rows the per-agent route hides.

`include_owned` is the second boundary: a platform session sees the agents it
OWNS (ent#357), an external client must see exactly what was shared with them.
`search_chats` gets this wrong today (it reads only the shared roster), which is
precisely why this test asserts it in both directions rather than inheriting
that function's shape.

Runs against a throwaway sqlite carrying the real table, so the expanding
bindparam and the ORDER BY are the code under test rather than a mock of them.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.unit

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture()
def portal_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-batch-sessions.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import metadata as oss_metadata, enterprise_portal_sessions
    oss_metadata.create_all(get_engine(), tables=[enterprise_portal_sessions])
    yield get_engine()


def _session(engine, *, agent, email, last_at=None, created="2026-08-01T00:00:00Z",
             title=None, sid=None):
    from db.tables import enterprise_portal_sessions as t
    sid = sid or uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(t.insert().values(
            id=sid, agent_name=agent, client_email=email.lower(),
            title=title, created_at=created, last_message_at=last_at,
            message_count=0,
        ))
    return sid


# ---------------------------------------------------------------------------
# The DB layer
# ---------------------------------------------------------------------------

def test_batch_equals_the_union_of_the_per_agent_reads(portal_db):
    """THE security property, stated as an equality.

    For every agent on the roster, the batch restricted to that agent must equal
    what the per-agent route returns. Anything else is either a leak or a thread
    that renders in the sidebar and 404s when opened.
    """
    from client_portal import db as pdb

    _session(portal_db, agent="scribe", email=ALICE, last_at="2026-08-03T00:00:00Z")
    _session(portal_db, agent="scout", email=ALICE, last_at="2026-08-05T00:00:00Z")
    _session(portal_db, agent="scribe", email=ALICE, last_at="2026-08-04T00:00:00Z")

    batch = pdb.list_portal_sessions_for_agents(ALICE, ["scribe", "scout"])
    for agent in ("scribe", "scout"):
        per_agent = pdb.list_portal_sessions(agent, ALICE)
        from_batch = [r for r in batch if r["agent_name"] == agent]
        assert [r["id"] for r in from_batch] == [r["id"] for r in per_agent]


def test_an_unshared_agent_is_absent_even_though_the_email_matches(portal_db):
    """The one way to get this wrong: filter on `client_email` alone.

    Alice has threads with `ghost`, but `ghost` is not on her roster — she was
    un-shared. The per-agent route 404s her; the batch must simply not return
    the rows.
    """
    from client_portal import db as pdb

    _session(portal_db, agent="scribe", email=ALICE, last_at="2026-08-03T00:00:00Z")
    _session(portal_db, agent="ghost", email=ALICE, last_at="2026-08-09T00:00:00Z")

    rows = pdb.list_portal_sessions_for_agents(ALICE, ["scribe"])
    assert {r["agent_name"] for r in rows} == {"scribe"}


def test_another_clients_threads_are_never_returned(portal_db):
    from client_portal import db as pdb

    _session(portal_db, agent="scribe", email=ALICE)
    _session(portal_db, agent="scribe", email=BOB)

    rows = pdb.list_portal_sessions_for_agents(ALICE, ["scribe"])
    assert len(rows) == 1


def test_email_match_is_case_insensitive_like_the_per_agent_read(portal_db):
    """`list_portal_sessions` lowercases python-side at the bind; so must this,
    or a caller signed in as `Alice@` would see an empty sidebar."""
    from client_portal import db as pdb

    _session(portal_db, agent="scribe", email=ALICE)
    assert len(pdb.list_portal_sessions_for_agents("ALICE@EXAMPLE.COM", ["scribe"])) == 1


def test_rows_carry_agent_name(portal_db):
    """The per-agent query omits it — the caller knew it. A flat list cannot."""
    from client_portal import db as pdb

    _session(portal_db, agent="scribe", email=ALICE)
    rows = pdb.list_portal_sessions_for_agents(ALICE, ["scribe"])
    assert rows[0]["agent_name"] == "scribe"


def test_ordering_is_recency_across_agents_with_created_at_as_the_fallback(portal_db):
    """A thread with no messages yet sorts by `created_at` — the sidebar is one
    merged list, so the fallback has to work ACROSS agents, not within one."""
    from client_portal import db as pdb

    newest = _session(portal_db, agent="scout", email=ALICE, last_at="2026-08-09T00:00:00Z")
    middle = _session(portal_db, agent="scribe", email=ALICE, last_at="2026-08-05T00:00:00Z")
    empty = _session(portal_db, agent="scribe", email=ALICE,
                     last_at=None, created="2026-08-07T00:00:00Z")

    ids = [r["id"] for r in pdb.list_portal_sessions_for_agents(ALICE, ["scribe", "scout"])]
    assert ids == [newest, empty, middle]


def test_empty_agent_list_returns_empty_without_touching_the_db(portal_db, monkeypatch):
    """An expanding bindparam RAISES on an empty list, so the guard is required,
    not merely tidy."""
    from client_portal import db as pdb
    from db import engine as engine_mod

    def _boom():
        raise AssertionError("no SQL should be issued for an empty roster")
    monkeypatch.setattr(pdb, "get_engine", _boom)

    assert pdb.list_portal_sessions_for_agents(ALICE, []) == []


def test_a_roster_larger_than_the_sqlite_variable_ceiling_still_works(portal_db):
    """SQLITE_MAX_VARIABLE_NUMBER is 999 on SQLite < 3.32 and the expanding
    bindparam emits one placeholder per agent. Without chunking, a large fleet
    would turn today's always-working N single-agent queries into a hard 500 —
    on the Workspace bootstrap path.
    """
    from client_portal import db as pdb

    agents = [f"agent-{i:04d}" for i in range(1200)]
    _session(portal_db, agent="agent-0007", email=ALICE, last_at="2026-08-03T00:00:00Z")
    _session(portal_db, agent="agent-1100", email=ALICE, last_at="2026-08-04T00:00:00Z")

    rows = pdb.list_portal_sessions_for_agents(ALICE, agents)
    assert {r["agent_name"] for r in rows} == {"agent-0007", "agent-1100"}
    # chunking must not break the global ordering contract
    assert [r["agent_name"] for r in rows] == ["agent-1100", "agent-0007"]


# ---------------------------------------------------------------------------
# The service layer — the roster boundary
# ---------------------------------------------------------------------------

def _wire_roster(monkeypatch, shared, owned):
    from client_portal import service as svc
    monkeypatch.setattr(svc.db, "get_shared_roster",
                        lambda e: [{"agent_name": n} for n in shared])
    monkeypatch.setattr(svc.db, "get_owned_roster",
                        lambda e: [{"agent_name": n} for n in owned])
    return svc


def test_roster_scope_is_the_same_set_the_per_agent_gate_enforces(monkeypatch):
    """`agent_on_roster` and the batch must not merely agree — they must be one
    implementation. This asserts the equality that the extraction guarantees."""
    svc = _wire_roster(monkeypatch, shared=["scribe"], owned=["mine"])

    for include_owned in (False, True):
        names = svc.roster_agent_names(ALICE, include_owned)
        for candidate in ("scribe", "mine", "ghost"):
            assert (candidate in names) is svc.agent_on_roster(
                candidate, ALICE, include_owned
            ), (candidate, include_owned)


def test_a_portal_client_never_receives_the_owned_union(monkeypatch):
    """ent#358: a platform session sees what it OWNS; an external client sees
    exactly what was shared. `search_chats` gets this wrong today, which is why
    it is asserted here rather than inherited."""
    svc = _wire_roster(monkeypatch, shared=["scribe"], owned=["mine"])
    captured = {}
    monkeypatch.setattr(svc.db, "list_portal_sessions_for_agents",
                        lambda email, names: captured.setdefault("names", names) or [])

    svc.list_all_sessions(ALICE, include_owned=False)
    assert captured["names"] == ["scribe"]

    captured.clear()
    svc.list_all_sessions(ALICE, include_owned=True)
    assert captured["names"] == ["mine", "scribe"]


def test_the_roster_is_resolved_once_not_once_per_agent(monkeypatch):
    """The whole point. `list_sessions` re-resolved the roster per agent, so a
    10-agent sidebar cost 10 roster resolutions per refresh."""
    svc = _wire_roster(monkeypatch, shared=["a", "b", "c", "d"], owned=[])
    calls = {"n": 0}

    real = svc.db.get_shared_roster

    def counting(email):
        calls["n"] += 1
        return real(email)
    monkeypatch.setattr(svc.db, "get_shared_roster", counting)
    monkeypatch.setattr(svc.db, "list_portal_sessions_for_agents", lambda e, n: [])

    svc.list_all_sessions(ALICE, include_owned=False)
    assert calls["n"] == 1


def test_an_empty_roster_short_circuits_before_any_query(monkeypatch):
    svc = _wire_roster(monkeypatch, shared=[], owned=[])

    def _boom(*a, **k):
        raise AssertionError("no query should run for an empty roster")
    monkeypatch.setattr(svc.db, "list_portal_sessions_for_agents", _boom)

    assert svc.list_all_sessions(ALICE, include_owned=False) == {"sessions": []}


# ---------------------------------------------------------------------------
# The principal boundary (#2198 / plan §12.5 E7)
# ---------------------------------------------------------------------------
#
# `get_portal_principal`'s platform branch resolves the caller through
# `get_current_user`, which accepts MCP keys — and an agent-scoped key resolves
# to its OWNER carrying the owner's role (the ent#293/#297 trap). Before #2198
# that meant any agent's injected TRINITY_MCP_API_KEY reached the portal as
# `is_platform=True` for its owner: a REST path around the MCP layer's
# agent-to-agent permission matrix, and the new batch route turned "N calls
# against N discovered names" into one call returning the owner's whole thread
# index. The fix is at the DEPENDENCY, so every portal route inherits it.

from types import SimpleNamespace  # noqa: E402


class _FakeUser(SimpleNamespace):
    pass


def _wire_principal(monkeypatch, *, agent_name=None, mcp_scope=None,
                    email="Owner@Example.com"):
    from client_portal import portal_auth as pa
    from client_portal import db as portal_db
    import database

    monkeypatch.setattr(pa, "decode_portal_session", lambda t: None)
    monkeypatch.setattr(portal_db, "is_client_blocked", lambda e: False)
    monkeypatch.setattr(database.db, "get_user_by_username",
                        lambda u: {"email": email})

    async def fake_get_current_user(request, token):
        return _FakeUser(username="owner", agent_name=agent_name,
                         mcp_scope=mcp_scope)

    monkeypatch.setattr(pa, "get_current_user", fake_get_current_user)
    return pa


@pytest.mark.asyncio
async def test_an_agent_scoped_key_is_rejected_at_the_portal_boundary(monkeypatch):
    """E7: an agent's injected key must never traverse the Workspace as its
    owner. 403 from the dependency, before any roster or session read."""
    from fastapi import HTTPException

    pa = _wire_principal(monkeypatch, agent_name="acme-sage", mcp_scope="agent")
    with pytest.raises(HTTPException) as exc:
        await pa.get_portal_principal(SimpleNamespace(), SimpleNamespace(headers={}),
                                      token="trinity_mcp_x")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_a_users_own_key_and_a_jwt_still_pass_as_platform(monkeypatch):
    """The portal is a USE surface: a user scripting their own Workspace with
    their own user-scoped key stays legitimate, as does a plain JWT.
    `User.agent_name` is set only for scope='agent', so both pass."""
    pa = _wire_principal(monkeypatch, agent_name=None, mcp_scope="user")
    principal = await pa.get_portal_principal(
        SimpleNamespace(), SimpleNamespace(headers={}), token="trinity_mcp_y")
    assert principal.is_platform is True
    assert principal.email == "owner@example.com"


@pytest.mark.asyncio
async def test_a_portal_session_token_is_untouched_by_the_agent_fence(monkeypatch):
    """The rejection sits on the PLATFORM branch only — an external client's
    portal session token never resolves through get_current_user."""
    from client_portal import portal_auth as pa
    from client_portal import db as portal_db

    monkeypatch.setattr(pa, "decode_portal_session", lambda t: "client@example.com")
    monkeypatch.setattr(portal_db, "is_client_blocked", lambda e: False)
    monkeypatch.setattr(pa, "portal_session_needs_rotation", lambda t: False)

    principal = await pa.get_portal_principal(
        SimpleNamespace(), SimpleNamespace(headers={}), token="portal-token")
    assert principal == ("client@example.com", False)
