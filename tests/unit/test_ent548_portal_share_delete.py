"""ent#548 — the Workspace delete permission matrix, and where a dismissal lives.

Three cases with three different mechanisms, and the AC names the matrix itself
as the test target:

  | my own upload              | a real delete (`rm` in the inbox)          |
  | an agent share, as viewer  | a per-viewer dismissal; the share untouched |
  | an agent share, as owner   | a soft revoke for everyone                  |

Four properties here are worth more than the happy paths.

**Ownership is session-type dependent, by construction.** `PortalPrincipal` is
`(email, is_platform)` and carries no role, so `include_owned` is
`principal.is_platform` at every call site (ent#358). A **non-owner admin is a
viewer** in the Workspace, and so is an **owner signed in with a magic-link
portal token**. Both are correct — the Workspace scope is what was shared with
you — and both would look like bugs to someone who had not read this.

**Access-first.** `portal_revoke_shared_file` gates roster → ownership → row.
The tempting order (row lookup, 404, then ownership) is existence-then-access,
the shape OSS invariant #8 forbids, and the difference is observable: it would
tell a non-owner whether a share id exists.

**The dismissal does NOT validate the file_id.** A 404 for an unknown id would
be an existence oracle over every share in the install. `set_chat_star` already
resolved that exact fork by capping rows instead, and this follows it.

**The rows follow the agent.** `agent_shared_files` is a CASCADE `AgentRef`, so
an agent delete hard-deletes the shares WITHOUT going through the revoke
sweeper. Dismissals keyed on those ids would be orphaned forever, which is why
the table carries `agent_name` and is registered too.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def shares_db(tmp_path, monkeypatch):
    """Real SQLite over the four tables these tests touch, seeded idempotently.

    Idempotent because `get_engine()` honours `DATABASE_URL` and the PostgreSQL
    tier points every test at ONE shared database (the #2242 lesson from
    `test_ent308_inbox_dir_collision.py`) — an `insert(...).values(id=1)` there
    is a setup error, not a test failure.
    """
    db_file = tmp_path / "trinity-548.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as m, agent_sharing, agent_ownership, users, system_settings,
        agent_shared_files, portal_file_dismissals,
    )
    m.create_all(get_engine(), tables=[
        agent_sharing, agent_ownership, users, system_settings,
        agent_shared_files, portal_file_dismissals,
    ])

    from sqlalchemy import delete, insert, select

    def _clear(conn):
        conn.execute(delete(agent_sharing).where(agent_sharing.c.agent_name.in_(["atlas", "borealis"])))
        conn.execute(delete(agent_shared_files).where(agent_shared_files.c.agent_name.in_(["atlas", "borealis"])))
        conn.execute(delete(portal_file_dismissals).where(
            portal_file_dismissals.c.agent_name.in_(["atlas", "borealis"])))

    with get_engine().begin() as conn:
        for uid, name, email in [(1, "alice", "alice@example.com")]:
            if not conn.execute(select(users.c.id).where(users.c.id == uid)).first():
                conn.execute(insert(users).values(
                    id=uid, username=name, role="creator", email=email,
                    created_at="t", updated_at="t"))
        for agent in ("atlas", "borealis"):
            if not conn.execute(select(agent_ownership.c.agent_name)
                                .where(agent_ownership.c.agent_name == agent)).first():
                conn.execute(insert(agent_ownership).values(
                    agent_name=agent, owner_id=1, created_at="t", is_system=0, deleted_at=None))
        _clear(conn)
        # bob is a CLIENT of atlas — shared with, never an owner.
        conn.execute(insert(agent_sharing).values(
            agent_name="atlas", shared_with_email="bob@example.com",
            shared_by_id=1, created_at="t"))
        for fid, agent in [("f1", "atlas"), ("f2", "atlas"), ("fx", "borealis")]:
            conn.execute(insert(agent_shared_files).values(
                id=fid, agent_name=agent, filename=f"{fid}.pdf",
                stored_filename=f"{fid}.bin", size_bytes=10, mime_type="application/pdf",
                download_token=f"tok-{fid}", created_by=agent, created_at="t",
                expires_at="2099-01-01T00:00:00Z", revoked_at=None,
                one_time=0, consumed_at=None, download_count=0, last_downloaded_at=None))

    yield get_engine()

    with get_engine().begin() as conn:
        _clear(conn)


BOB = "bob@example.com"       # a client of atlas
ALICE = "alice@example.com"   # the owner of atlas


def _err(fn, *a, **kw):
    from client_portal.service import ClientPortalError
    with pytest.raises(ClientPortalError) as exc:
        fn(*a, **kw)
    return exc.value


# --------------------------------------------------------------------------- #
# `owned` on the card, and the predicate behind it
# --------------------------------------------------------------------------- #

def test_the_roster_tags_which_arm_each_row_came_from(shares_db):
    from client_portal import service

    shared = service._roster_rows(BOB, include_owned=False)
    assert [r["agent_name"] for r in shared] == ["atlas"]
    assert shared[0]["owned"] is False

    owned = service._roster_rows(ALICE, include_owned=True)
    assert {r["agent_name"] for r in owned} == {"atlas", "borealis"}
    assert all(r["owned"] for r in owned)


def test_portal_owns_agent_is_the_same_membership_the_card_renders(shares_db):
    """If these two could drift, the UI would offer a button the server refuses —
    or hide one it would have allowed."""
    from client_portal import service

    assert service.portal_owns_agent(ALICE, "atlas", include_owned=True) is True
    assert service.portal_owns_agent(BOB, "atlas", include_owned=False) is False
    assert service.portal_owns_agent(BOB, "atlas", include_owned=True) is False


def test_an_owner_on_a_portal_token_is_a_viewer(shares_db):
    """`include_owned` IS `principal.is_platform`, so a magic-link session
    carries no platform identity to own anything with. Intended (ent#358), and
    pinned here because it will look like a bug to the next reader."""
    from client_portal import service
    assert service.portal_owns_agent(ALICE, "atlas", include_owned=False) is False


def test_a_non_owner_admin_is_a_viewer_too(shares_db):
    """Stricter than the platform surface, and correct: the Workspace scope is
    what was shared with you, not what your role could reach elsewhere. There is
    no role on a PortalPrincipal to consult even if we wanted to."""
    from client_portal.portal_auth import PortalPrincipal
    from client_portal import service

    admin = PortalPrincipal(email="carol@example.com", is_platform=True)
    assert service.portal_owns_agent(admin.email, "atlas", include_owned=admin.is_platform) is False


def test_the_card_carries_owned_and_defaults_closed():
    from client_portal.models import PortalAgentCard
    assert PortalAgentCard(name="atlas").owned is False


# --------------------------------------------------------------------------- #
# The matrix — revoke
# --------------------------------------------------------------------------- #

def test_a_viewer_cannot_revoke_and_is_told_the_alternative(shares_db):
    from client_portal import service
    err = _err(service.portal_revoke_shared_file, "atlas", BOB, "f1", include_owned=False)
    assert err.status_code == 403
    assert "own list" in err.detail, "a refusal must name the thing the caller CAN do"


def test_the_403_precedes_the_row_lookup(shares_db, monkeypatch):
    """Access-first (invariant #8). With the opposite order, a viewer asking
    about a nonexistent id would get 404 and about a real one 403 — an
    enumeration oracle over every share in the install."""
    from database import db as core_db
    from client_portal import service

    looked: list[str] = []
    real = core_db.get_agent_shared_file
    monkeypatch.setattr(core_db, "get_agent_shared_file",
                        lambda fid: (looked.append(fid), real(fid))[1])

    assert _err(service.portal_revoke_shared_file, "atlas", BOB, "f1",
                include_owned=False).status_code == 403
    assert looked == [], "the row must not be read before the caller is authorized"

    # And the two answers a viewer gets are identical whether the id exists.
    assert _err(service.portal_revoke_shared_file, "atlas", BOB, "no-such-id",
                include_owned=False).status_code == 403


def test_the_owner_revokes_and_the_row_goes_soft(shares_db):
    from database import db as core_db
    from client_portal import service

    service.portal_revoke_shared_file("atlas", ALICE, "f1", include_owned=True)
    row = core_db.get_agent_shared_file("f1")
    assert row["revoked_at"], "soft revoke — the sweeper reclaims the bytes within the grace window"


def test_a_second_revoke_is_not_an_error(shares_db):
    from client_portal import service
    service.portal_revoke_shared_file("atlas", ALICE, "f1", include_owned=True)
    service.portal_revoke_shared_file("atlas", ALICE, "f1", include_owned=True)


def test_an_unknown_id_and_a_cross_agent_id_are_the_same_404(shares_db, monkeypatch):
    """`fx` belongs to borealis. The caller's authority is scoped to atlas, so
    anything else does not exist as far as this route is concerned — and it must
    not be revoked."""
    from database import db as core_db
    from client_portal import service

    revoked: list[str] = []
    monkeypatch.setattr(core_db, "revoke_agent_shared_file", lambda fid: revoked.append(fid))

    for fid in ("no-such-id", "fx"):
        err = _err(service.portal_revoke_shared_file, "atlas", ALICE, fid, include_owned=True)
        assert err.status_code == 404, fid
        assert err.detail == "File not found"
    assert revoked == [], "a cross-agent id must never reach the revoke primitive"


def test_this_route_404s_where_its_operator_sibling_204s(shares_db):
    """A DELIBERATE divergence, recorded so nobody "aligns" it.

    `routers/agent_files.py`'s revoke is documented idempotent-204 for a missing
    id. This is an EXTERNAL surface, where a 204 for a nonexistent id against a
    404 for another agent's id is an enumeration differential.
    """
    import inspect
    from client_portal import service
    assert "404" in inspect.getdoc(service.portal_revoke_shared_file)


def test_off_roster_is_the_uniform_404_before_anything(shares_db):
    from client_portal import service
    for fn in (service.portal_revoke_shared_file, service.portal_dismiss_shared_file):
        err = _err(fn, "borealis", BOB, "f1", include_owned=False)
        assert err.status_code == 404
        assert err.detail == "Agent not found"


# --------------------------------------------------------------------------- #
# The matrix — dismiss
# --------------------------------------------------------------------------- #

def test_a_dismissal_hides_the_file_from_that_viewer_only(shares_db, monkeypatch):
    from client_portal import service

    monkeypatch.setattr(service, "get_portal_base_url", lambda: "")
    before = service.portal_documents("atlas", BOB)
    assert {d["id"] for d in before["documents"]} == {"f1", "f2"}

    service.portal_dismiss_shared_file("atlas", BOB, "f1", include_owned=False)

    after = service.portal_documents("atlas", BOB)
    assert {d["id"] for d in after["documents"]} == {"f2"}

    # The share itself is untouched, and another viewer still sees it.
    from database import db as core_db
    assert core_db.get_agent_shared_file("f1")["revoked_at"] is None
    other = service.portal_documents("atlas", ALICE, include_owned=True)
    assert {d["id"] for d in other["documents"]} == {"f1", "f2"}


def test_a_dismissal_is_idempotent(shares_db):
    from client_portal import db as portal_db
    from client_portal import service

    service.portal_dismiss_shared_file("atlas", BOB, "f1", include_owned=False)
    service.portal_dismiss_shared_file("atlas", BOB, "f1", include_owned=False)
    assert portal_db.count_file_dismissals(BOB) == 1


def test_a_dismissal_does_not_validate_the_file_id(shares_db):
    """Deliberate. A 404 for "no such file" would be an existence oracle over
    every share id in the install (invariant #8); the row cap bounds the write
    instead — the same answer `set_chat_star` reached for the same fork."""
    from client_portal import db as portal_db
    from client_portal import service

    service.portal_dismiss_shared_file("atlas", BOB, "a-id-that-does-not-exist",
                                       include_owned=False)
    assert portal_db.dismissed_file_ids(BOB) == {"a-id-that-does-not-exist"}


def test_the_dismissal_write_is_row_capped(shares_db, monkeypatch):
    """Without the cap, "does not validate the id" makes a portal session an
    unbounded write primitive — the two decisions are a pair, not two choices."""
    from client_portal import db as portal_db
    from client_portal import service

    monkeypatch.setattr(portal_db, "MAX_FILE_DISMISSALS", 2)
    service.portal_dismiss_shared_file("atlas", BOB, "a", include_owned=False)
    service.portal_dismiss_shared_file("atlas", BOB, "b", include_owned=False)
    err = _err(service.portal_dismiss_shared_file, "atlas", BOB, "c", include_owned=False)
    assert err.status_code == 409


def test_dismissals_are_scoped_to_one_email(shares_db):
    from client_portal import db as portal_db
    from client_portal import service

    service.portal_dismiss_shared_file("atlas", BOB, "f1", include_owned=False)
    assert portal_db.dismissed_file_ids(ALICE) == set()


# --------------------------------------------------------------------------- #
# The rows follow the file, and the agent
# --------------------------------------------------------------------------- #

def test_the_revoke_sweeper_removes_the_dismissals_with_the_share(shares_db):
    from database import db as core_db
    from client_portal import db as portal_db
    from client_portal import service

    service.portal_dismiss_shared_file("atlas", BOB, "f1", include_owned=False)
    # Expire the row so the sweeper's own predicate selects it.
    from sqlalchemy import update
    from db.tables import agent_shared_files
    with shares_db.begin() as conn:
        conn.execute(update(agent_shared_files)
                     .where(agent_shared_files.c.id == "f1")
                     .values(expires_at="2000-01-01T00:00:00Z"))

    core_db.delete_expired_and_revoked_shared_files()
    assert portal_db.dismissed_file_ids(BOB) == set()


def test_delete_for_agent_removes_them_too(shares_db):
    """The OTHER purge path, and the one the AgentRef registration exists for —
    `agent_shared_files` is CASCADE, so an agent delete never goes through the
    sweeper above."""
    from database import db as core_db
    from client_portal import db as portal_db
    from client_portal import service

    service.portal_dismiss_shared_file("atlas", BOB, "f1", include_owned=False)
    core_db.delete_shared_files_for_agent("atlas")
    assert portal_db.dismissed_file_ids(BOB) == set()


def test_the_table_is_registered_in_the_agent_cleanup_registry():
    """A table referencing an agent without an `AgentRef` entry fails
    `test_agent_cleanup_parity.py` — this asserts the registration directly, so
    the reason is legible here and not only in a generic parity failure."""
    from db.agent_cleanup import AGENT_REFS, Policy

    refs = [r for r in AGENT_REFS if r.table == "portal_file_dismissals"]
    assert len(refs) == 1
    assert refs[0].column == "agent_name"
    assert refs[0].policy is Policy.CASCADE


# --------------------------------------------------------------------------- #
# Both migration tracks (Invariant #9)
# --------------------------------------------------------------------------- #

def test_the_sqlite_track_declares_the_table():
    from db import migrations, schema

    assert "portal_file_dismissals" in schema.TABLES
    assert any(sql for sql in schema.INDEXES if "portal_file_dismissals" in sql)
    assert any(name == "portal_file_dismissals_table" for name, _fn in migrations.MIGRATIONS)


def test_the_postgres_track_declares_it_on_the_same_line():
    """A revision that forks the version line applies ZERO revisions, silently
    (#2068) — `scripts/ci/check_alembic_heads.py` is the guard, and this pins the
    parent this revision was written against."""
    from pathlib import Path

    rev = (Path(__file__).resolve().parents[2]
           / "src" / "backend" / "migrations" / "versions"
           / "0058_portal_file_dismissals.py")
    src = rev.read_text()
    assert 'revision = "0058_portal_file_dismissals"' in src
    assert 'down_revision = "0057_portal_messages_voice_source"' in src


def test_the_primary_key_leads_with_the_email_the_read_path_filters_on():
    """`dismissed_file_ids(email)` is `WHERE client_email = ?`, called per
    participant per turn end. A PK led by `file_id` would serve only the
    sweeper — the one reader that asks the other question, and the one the
    secondary index is for."""
    from db.tables import portal_file_dismissals

    pk = [c.name for c in portal_file_dismissals.primary_key.columns]
    assert pk[0] == "client_email"
    assert set(pk) == {"client_email", "file_id"}
