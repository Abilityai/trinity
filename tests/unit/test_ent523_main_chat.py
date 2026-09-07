"""ent#523 — the pinned Main chat, Reset, and the landing rule.

Every (user, agent) pair has one **Main** chat: the place the agent reaches you
when no conversation named itself. Reset retires it and starts the agent cold.
The properties worth pinning are the ones where the obvious code is subtly
wrong:

  * **One live Main, enforced by the database.** `ensure_main_session` is
    reachable from two request paths and runs in every uvicorn worker, so a
    check-then-insert races two Mains into existence. The partial unique index
    is the guard, and its `WHERE is_main = 1` predicate is load-bearing: an
    archived row keeps its (agent, client) pair forever, so an unconditional
    unique index would refuse the SECOND Reset.

  * **Cold is a property of the new row.** Reset does not call the Session
    tab's `reset_session_memory`; a fresh row simply carries no
    `cached_claude_session_id`, and the turn engine resumes only on a cached
    id. Pinning this stops a future edit from "helpfully" wiring in a second
    reset primitive that would also clear the ARCHIVE's resume state.

  * **A person's title survives being archived.** The archive keeps whatever
    name it had; only an untitled one is named, and dated, because these
    accumulate in one list.

  * **Resetting an untouched Main is a no-op that says so.** Archiving anyway
    mints an empty thread per click and files it under a name nobody chose.

  * **The landing rule is one edit.** `_resolve_session_id(..., None)` resolves
    to Main, which is what makes an ask (ent#364/#429) and a brief (ent#498)
    land there without either caller knowing about Main at all.

Runs the db-layer tests against a throwaway sqlite carrying the real tables and
the real partial index, so the invariant under test is the schema's own rather
than a mock of it.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

ALICE = "alice@example.com"
BOB = "bob@example.com"
AGENT = "scribe"
NOW = "2026-09-07T10:00:00Z"


@pytest.fixture()
def portal_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-main-chat.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from sqlalchemy import text
    from db.engine import get_engine
    from db.tables import (
        metadata as oss_metadata,
        enterprise_portal_messages,
        enterprise_portal_sessions,
    )
    engine = get_engine()
    oss_metadata.create_all(engine, tables=[
        enterprise_portal_messages,
        enterprise_portal_sessions,
    ])
    # `tables.py` carries the COLUMNS; the partial unique index lives in the
    # index list in `schema.py`, and it is the whole point of this feature — so
    # it is created here from the same DDL rather than left out of the harness.
    with engine.begin() as conn:
        conn.execute(text(_main_index_ddl()))
    yield engine


def _main_index_ddl() -> str:
    """The index DDL, read out of `db/schema.py` rather than retyped.

    A copy here would let the test keep passing against an index the product no
    longer creates — which is exactly the failure mode the index prevents.
    """
    from db import schema
    for stmt in schema.INDEXES:
        if "idx_portal_sessions_main" in stmt:
            return stmt
    raise AssertionError("idx_portal_sessions_main is not declared in db/schema.py")


# ---------------------------------------------------------------------------
# Schema — both tracks, and the predicate that makes Reset repeatable
# ---------------------------------------------------------------------------

def test_columns_and_index_are_declared_in_schema():
    from db import schema
    ddl = schema.TABLES["enterprise_portal_sessions"]
    assert "is_main" in ddl
    assert "archived_at" in ddl
    idx = _main_index_ddl()
    assert "UNIQUE" in idx.upper()
    # The predicate is the difference between "Reset works twice" and "Reset
    # works once", so it is asserted rather than assumed.
    assert "WHERE is_main = 1" in idx


def test_both_migration_tracks_carry_the_change():
    """Invariant #3 — a schema change lands on SQLite AND PostgreSQL."""
    from db import migrations
    names = [name for name, _ in migrations.MIGRATIONS]
    assert "portal_session_main_chat" in names

    from pathlib import Path
    rev = Path(__file__).resolve().parents[2] / (
        "src/backend/migrations/versions/0054_portal_session_main_chat.py"
    )
    body = rev.read_text()
    # Chained off `0053_user_ui_preferences`, not `0052`: both were written
    # against 0052 in parallel and dev's landed first, so sharing a
    # `down_revision` would have been TWO HEADS — and `upgrade head` is
    # singular, so a forked graph applies zero revisions, not just the newer
    # one. Pinned by id rather than by "some parent" so a future rebase of this
    # revision is a deliberate edit here too.
    assert 'down_revision = "0053_user_ui_preferences"' in body
    # `IF NOT EXISTS` throughout: a fresh PostgreSQL database is built from
    # schema.py first and only then runs the revisions.
    assert body.count("IF NOT EXISTS") >= 3


def test_tables_py_mirrors_the_new_columns():
    from db.tables import enterprise_portal_sessions as t
    assert "is_main" in t.c
    assert "archived_at" in t.c


# ---------------------------------------------------------------------------
# ensure_main_session
# ---------------------------------------------------------------------------

def test_main_is_created_once_and_reused(portal_db, monkeypatch):
    from client_portal import service
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)

    first = service.ensure_main_session(AGENT, ALICE)
    second = service.ensure_main_session(AGENT, ALICE)
    assert first == second

    from client_portal import db as pdb
    rows = pdb.list_portal_sessions(AGENT, ALICE)
    assert [r["id"] for r in rows] == [first]
    assert rows[0]["is_main"] == 1


def test_main_is_per_pair(portal_db, monkeypatch):
    from client_portal import service
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)
    assert service.ensure_main_session(AGENT, ALICE) != service.ensure_main_session(AGENT, BOB)
    assert service.ensure_main_session(AGENT, ALICE) != service.ensure_main_session("other", ALICE)


def test_a_lost_insert_race_returns_the_winners_row(portal_db, monkeypatch):
    """The index decides, and the loser adopts rather than raising.

    Simulated by letting the real INSERT run inside the "someone else got there
    first" branch: the second call sees no row, tries to insert, and the index
    refuses it. That is precisely what two workers do.
    """
    from client_portal import service, db as pdb
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)

    winner = service.ensure_main_session(AGENT, ALICE)

    # Blind the SELECT once, so the next call takes the insert path against a
    # table that already has a Main — the race, deterministically.
    calls = {"n": 0}
    real = pdb.get_main_portal_session_id

    def blind_once(agent, email):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real(agent, email)

    monkeypatch.setattr(service.db, "get_main_portal_session_id", blind_once)
    assert service.ensure_main_session(AGENT, ALICE) == winner


# ---------------------------------------------------------------------------
# The landing rule
# ---------------------------------------------------------------------------

def test_a_homeless_turn_lands_in_main(portal_db, monkeypatch):
    from client_portal import service
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)

    main = service.ensure_main_session(AGENT, ALICE)
    # A newer, more-recently-active thread exists — the pre-ent#523 rule would
    # have chosen it.
    from client_portal import db as pdb
    pdb.create_portal_session("other", AGENT, ALICE, NOW)
    pdb.touch_portal_session("other", "2026-09-07T12:00:00Z")

    assert service._resolve_session_id(AGENT, ALICE, None) == main


def test_an_ask_raised_outside_a_chat_lands_in_main(portal_db, monkeypatch):
    """ent#364/#429 reach the rule through `ensure_thread_for_ask`, which is why
    the landing rule is one edit rather than one per caller."""
    from client_portal import service
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)
    main = service.ensure_main_session(AGENT, ALICE)
    assert service.ensure_thread_for_ask(AGENT, ALICE) == main


def test_an_explicit_session_id_still_wins(portal_db, monkeypatch):
    from client_portal import service, db as pdb
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)
    service.ensure_main_session(AGENT, ALICE)
    pdb.create_portal_session("named", AGENT, ALICE, NOW)
    assert service._resolve_session_id(AGENT, ALICE, "named") == "named"


def test_new_thread_still_opens_a_fresh_one(portal_db, monkeypatch):
    from client_portal import service
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)
    main = service.ensure_main_session(AGENT, ALICE)
    fresh = service._resolve_session_id(AGENT, ALICE, None, new_thread=True)
    assert fresh != main


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def _used_main(monkeypatch, title=None):
    """A Main with history, which is what makes Reset a real archive."""
    from client_portal import service, db as pdb
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)
    main = service.ensure_main_session(AGENT, ALICE)
    pdb.add_portal_message("m1", AGENT, ALICE, "user", "hello", None, NOW, session_id=main)
    pdb.touch_portal_session(main, NOW)
    if title:
        pdb.rename_portal_session(main, AGENT, ALICE, title)
    return service, pdb, main


def test_reset_archives_and_mints_a_fresh_main(portal_db, monkeypatch):
    service, pdb, main = _used_main(monkeypatch)
    monkeypatch.setattr(service, "get_turn_inflight", lambda *_a, **_k: None)

    out = service.reset_main_session(AGENT, ALICE)
    assert out["archived_session_id"] == main
    assert out["main_session_id"] != main

    rows = {r["id"]: r for r in pdb.list_portal_sessions(AGENT, ALICE)}
    assert rows[main]["is_main"] == 0 and rows[main]["archived_at"]
    assert rows[out["main_session_id"]]["is_main"] == 1
    assert not rows[out["main_session_id"]]["archived_at"]


def test_the_fresh_main_is_cold_without_a_second_reset_primitive(portal_db, monkeypatch):
    """"Starts cold" is the new row's property, not an action on the old one."""
    from client_portal import db as pdb
    service, _pdb, main = _used_main(monkeypatch)
    monkeypatch.setattr(service, "get_turn_inflight", lambda *_a, **_k: None)
    pdb.update_cached_claude_session_id(main, "claude-uuid-1")

    out = service.reset_main_session(AGENT, ALICE)
    assert pdb.get_cached_claude_session_id(out["main_session_id"]) is None
    # And the ARCHIVE keeps its own resume state: it is an ordinary past chat.
    assert pdb.get_cached_claude_session_id(main) == "claude-uuid-1"


def test_reset_writes_one_system_line_naming_the_archive(portal_db, monkeypatch):
    from client_portal import db as pdb
    service, _pdb, _main = _used_main(monkeypatch, title="Pipeline questions")
    monkeypatch.setattr(service, "get_turn_inflight", lambda *_a, **_k: None)

    out = service.reset_main_session(AGENT, ALICE)
    msgs = pdb.get_portal_messages(AGENT, ALICE, session_id=out["main_session_id"])
    assert [m["role"] for m in msgs] == ["system"]
    assert "Pipeline questions" in msgs[0]["content"]


def test_reset_keeps_a_persons_title_on_the_archive(portal_db, monkeypatch):
    service, pdb, main = _used_main(monkeypatch, title="Pipeline questions")
    monkeypatch.setattr(service, "get_turn_inflight", lambda *_a, **_k: None)

    out = service.reset_main_session(AGENT, ALICE)
    assert out["archived_title"] == "Pipeline questions"
    rows = {r["id"]: r for r in pdb.list_portal_sessions(AGENT, ALICE)}
    assert rows[main]["title"] == "Pipeline questions"


def test_an_untitled_archive_is_named_and_dated(portal_db, monkeypatch):
    service, _pdb, _main = _used_main(monkeypatch)
    monkeypatch.setattr(service, "get_turn_inflight", lambda *_a, **_k: None)
    out = service.reset_main_session(AGENT, ALICE)
    # Dated, because these accumulate in one list and three rows reading
    # "Previous conversation" tell the person nothing about which is which.
    assert out["archived_title"].startswith("Previous conversation")
    assert out["archived_title"] != "Previous conversation"


def test_reset_is_repeatable(portal_db, monkeypatch):
    """The second Reset is what an unconditional unique index would refuse."""
    from client_portal import db as pdb
    service, _pdb, _main = _used_main(monkeypatch)
    monkeypatch.setattr(service, "get_turn_inflight", lambda *_a, **_k: None)

    first = service.reset_main_session(AGENT, ALICE)["main_session_id"]
    pdb.add_portal_message("m2", AGENT, ALICE, "user", "again", None, NOW, session_id=first)
    pdb.touch_portal_session(first, NOW)
    second = service.reset_main_session(AGENT, ALICE)
    assert second["archived_session_id"] == first

    live = [r for r in pdb.list_portal_sessions(AGENT, ALICE) if r["is_main"]]
    assert len(live) == 1


def test_resetting_an_untouched_main_is_a_no_op(portal_db, monkeypatch):
    from client_portal import service, db as pdb
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)
    monkeypatch.setattr(service, "get_turn_inflight", lambda *_a, **_k: None)
    main = service.ensure_main_session(AGENT, ALICE)

    out = service.reset_main_session(AGENT, ALICE)
    assert out["main_session_id"] == main
    assert out["archived_session_id"] is None
    # Nothing was minted and nothing was archived.
    assert len(pdb.list_portal_sessions(AGENT, ALICE)) == 1


def test_reset_is_refused_while_a_turn_is_in_flight(portal_db, monkeypatch):
    service, _pdb, _main = _used_main(monkeypatch)
    monkeypatch.setattr(service, "get_turn_inflight", lambda *_a, **_k: "exec-1")

    with pytest.raises(service.MainResetRefused) as exc:
        service.reset_main_session(AGENT, ALICE)
    assert exc.value.code == "turn_in_flight"
    assert exc.value.status_code == 409
    # From the closed set — a ninth category for one route would widen a
    # vocabulary whose value is being small.
    assert exc.value.category in service.PORTAL_FAILURE_CATEGORIES


def test_reset_off_roster_is_the_uniform_404(portal_db, monkeypatch):
    from client_portal import service
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: False)
    with pytest.raises(service.ClientPortalError) as exc:
        service.reset_main_session("someone-elses-agent", ALICE)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Reads carry the flags
# ---------------------------------------------------------------------------

def test_list_puts_main_first_and_carries_the_flags(portal_db, monkeypatch):
    from client_portal import service, db as pdb
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)
    pdb.create_portal_session("older", AGENT, ALICE, NOW)
    pdb.touch_portal_session("older", "2026-09-07T23:00:00Z")
    main = service.ensure_main_session(AGENT, ALICE)

    rows = service.list_sessions(AGENT, ALICE)["sessions"]
    assert rows[0]["id"] == main, "Main is pinned first even when it is the least recent"
    assert set(rows[0]) >= {"is_main", "archived_at"}


def test_opening_an_agent_is_what_mints_main(portal_db, monkeypatch):
    """The pinned tab has to exist on the first visit, and `list_sessions` is
    the one per-agent read on that path."""
    from client_portal import service, db as pdb
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)
    assert pdb.get_main_portal_session_id(AGENT, ALICE) is None
    service.list_sessions(AGENT, ALICE)
    assert pdb.get_main_portal_session_id(AGENT, ALICE) is not None


def test_the_cross_agent_batch_does_not_mint(portal_db, monkeypatch):
    """It spans every rostered agent and runs on every sidebar refresh, so
    ensuring there would write a row per agent the person never opened."""
    from client_portal import service, db as pdb
    monkeypatch.setattr(service, "roster_agent_names", lambda *a, **k: {AGENT, "other"})
    service.list_all_sessions(ALICE)
    assert pdb.get_main_portal_session_id(AGENT, ALICE) is None
    assert pdb.get_main_portal_session_id("other", ALICE) is None
