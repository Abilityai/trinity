"""The Workspace roster includes the agents a platform user OWNS (ent#357).

The roster has always been built from ``agent_sharing``, and Trinity refuses to
share an agent with its own owner ("Cannot share an agent with yourself"). That
was invisible while the surface was external-clients-only. The Workspace rename
makes a signed-in platform user a first-class audience, and it turned that into
the whole experience: click Workspace, land on an empty page whose "New chat"
button does nothing, because there is no agent to start a chat with.

So a platform session's roster is `shared-with-me ∪ owned-by-me`, and an
external client's roster is deliberately NOT. That asymmetry is the security
property worth testing hardest: a portal-token session must keep seeing exactly
what was shared with it, or the rename would have handed clients agents nobody
gave them.

Runs against a throwaway sqlite seeded with the OSS tables the queries read.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def roster_db(tmp_path, monkeypatch):
    """Fresh sqlite with the OSS tables the roster joins over."""
    db_file = tmp_path / "trinity-roster.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as oss_metadata, agent_ownership, agent_sharing, system_settings, users,
    )
    # `system_settings` too: `get_roster` does a function-local
    # `from services import tts_service` for its voice check, so the module
    # attribute cannot be monkeypatched from here and the real settings read
    # runs against this database.
    oss_metadata.create_all(
        get_engine(), tables=[users, agent_ownership, agent_sharing, system_settings]
    )
    yield get_engine()


def _seed(engine, *, users_rows=(), agents=(), shares=()):
    from db.tables import agent_ownership, agent_sharing, users
    now = "2026-08-10T00:00:00Z"
    with engine.begin() as conn:
        for uid, username, email in users_rows:
            conn.execute(users.insert().values(
                id=uid, username=username, email=email, role="user",
                created_at=now, updated_at=now,
            ))
        for name, owner_id, is_system in agents:
            conn.execute(agent_ownership.insert().values(
                agent_name=name, owner_id=owner_id, created_at=now,
                is_system=is_system, deleted_at=None,
            ))
        for name, email in shares:
            conn.execute(agent_sharing.insert().values(
                agent_name=name, shared_with_email=email, shared_by_id=1,
                created_at=now,
            ))


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------

def test_an_owner_sees_the_agents_they_own(roster_db):
    """The reported symptom: an owner's roster was always empty."""
    from client_portal import db

    _seed(
        roster_db,
        users_rows=[(1, "alice", "alice@example.com")],
        agents=[("scout", 1, 0), ("sage", 1, 0)],
    )

    assert db.get_shared_roster("alice@example.com") == []   # the old behaviour
    owned = [r["agent_name"] for r in db.get_owned_roster("alice@example.com")]
    assert owned == ["sage", "scout"]


def test_owned_excludes_system_and_soft_deleted_agents(roster_db):
    """Same exclusions as the shared query — a system agent is not a client
    surface, and a soft-deleted one is on its way out."""
    from db.tables import agent_ownership
    from client_portal import db

    _seed(
        roster_db,
        users_rows=[(1, "alice", "alice@example.com")],
        agents=[("scout", 1, 0), ("trinity-system", 1, 1), ("gone", 1, 0)],
    )
    with roster_db.begin() as conn:
        conn.execute(
            agent_ownership.update()
            .where(agent_ownership.c.agent_name == "gone")
            .values(deleted_at="2026-08-09T00:00:00Z")
        )

    owned = [r["agent_name"] for r in db.get_owned_roster("alice@example.com")]
    assert owned == ["scout"]


def test_owned_is_scoped_to_the_asking_email(roster_db):
    from client_portal import db

    _seed(
        roster_db,
        users_rows=[(1, "alice", "alice@example.com"), (2, "bob", "bob@example.com")],
        agents=[("scout", 1, 0), ("bobs-agent", 2, 0)],
    )

    assert [r["agent_name"] for r in db.get_owned_roster("alice@example.com")] == ["scout"]
    assert [r["agent_name"] for r in db.get_owned_roster("bob@example.com")] == ["bobs-agent"]


def test_email_match_is_case_insensitive_like_the_shared_path(roster_db):
    from client_portal import db

    _seed(
        roster_db,
        users_rows=[(1, "alice", "Alice@Example.COM")],
        agents=[("scout", 1, 0)],
    )
    assert [r["agent_name"] for r in db.get_owned_roster("alice@example.com")] == ["scout"]


def test_no_email_is_an_empty_roster_not_every_agent(roster_db):
    """A missing identity must never degrade to "show everything"."""
    from client_portal import db

    _seed(
        roster_db,
        users_rows=[(1, "alice", "alice@example.com")],
        agents=[("scout", 1, 0)],
    )
    assert db.get_owned_roster("") == []
    assert db.get_owned_roster(None) == []


def test_owned_rows_carry_the_shared_row_shape(roster_db):
    """The service merges the two lists without special-casing either, so the
    columns have to match — a missing key would KeyError at card build."""
    from client_portal import db

    _seed(
        roster_db,
        users_rows=[(1, "alice", "alice@example.com"), (2, "bob", "bob@example.com")],
        agents=[("scout", 1, 0), ("shared-one", 2, 0)],
        shares=[("shared-one", "alice@example.com")],
    )

    shared = db.get_shared_roster("alice@example.com")[0]
    owned = db.get_owned_roster("alice@example.com")[0]
    assert set(owned) == set(shared), "owned/shared row shapes diverged"


# ---------------------------------------------------------------------------
# The gate — who gets the union
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_platform_session_gets_shared_plus_owned(roster_db):
    from client_portal import service

    _seed(
        roster_db,
        users_rows=[(1, "alice", "alice@example.com"), (2, "bob", "bob@example.com")],
        agents=[("scout", 1, 0), ("shared-one", 2, 0)],
        shares=[("shared-one", "alice@example.com")],
    )
    roster = await service.get_roster("alice@example.com", include_owned=True)
    names = sorted(a.name for a in roster.agents)
    assert names == ["scout", "shared-one"]


@pytest.mark.asyncio
async def test_an_external_client_gets_ONLY_what_was_shared(roster_db):
    """The security half. A portal-token session must never see an agent it was
    not given — including agents owned by a platform user with the same email.
    """
    from client_portal import service

    _seed(
        roster_db,
        users_rows=[(1, "alice", "alice@example.com"), (2, "bob", "bob@example.com")],
        agents=[("scout", 1, 0), ("shared-one", 2, 0)],
        shares=[("shared-one", "alice@example.com")],
    )
    roster = await service.get_roster("alice@example.com")   # default: no union
    assert [a.name for a in roster.agents] == ["shared-one"]


@pytest.mark.asyncio
async def test_an_agent_both_owned_and_shared_appears_once(roster_db):
    from client_portal import service

    _seed(
        roster_db,
        users_rows=[(1, "alice", "alice@example.com")],
        agents=[("scout", 1, 0)],
        shares=[("scout", "alice@example.com")],   # contrived, but the union must dedupe
    )
    roster = await service.get_roster("alice@example.com", include_owned=True)
    assert [a.name for a in roster.agents] == ["scout"]


def test_the_union_is_opt_in_at_the_signature(roster_db):
    """`include_owned` defaulting to False is what keeps every other caller —
    and any future one — on the client-safe behaviour."""
    import inspect
    from client_portal import service

    sig = inspect.signature(service.get_roster)
    assert sig.parameters["include_owned"].default is False


def test_only_the_platform_path_is_wired_to_the_union():
    """Pins the route: the union must be driven by `is_platform`, never by a
    constant. A hardcoded True here would hand external clients owned agents.
    """
    import ast
    import inspect
    from client_portal import router

    src = inspect.getsource(router.my_agents)
    tree = ast.parse(src.strip())
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get_roster"
    ]
    assert calls, "my_agents no longer calls get_roster"
    kw = {k.arg: k for k in calls[0].keywords}
    assert "include_owned" in kw, "the union is not passed at all"
    value = kw["include_owned"].value
    assert not isinstance(value, ast.Constant), (
        "include_owned is a constant — it must come from the principal's "
        "is_platform flag, or external clients get owned agents"
    )
