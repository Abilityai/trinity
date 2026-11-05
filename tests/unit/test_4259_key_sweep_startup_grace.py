"""The orphaned-key sweep never revokes a key whose agent is still being created (#4259).

`crud.create_agent` mints the agent-scoped MCP key BEFORE the container exists
and writes the `agent_ownership` row only after it does. The #1745 sweep
defines "orphaned" as "no live ownership row", so a cleanup tick landing in
that window revoked a healthy agent's key one second after it was minted — the
agent's first MCP call then failed 401 and nothing repaired it.

Two behaviours pinned here:

* a key younger than the start-up grace window is skipped by the sweep,
  whatever the ownership table says; a key older than it with no live row is
  still revoked (the #1745 security property is unchanged outside the window);
* the sweep's `[Cleanup]` line names the agents whose keys it revoked, so an
  operator can see what happened to whom.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture()
def keys_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-keys.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import metadata as m, agent_ownership, users, mcp_api_keys
    m.create_all(get_engine(), tables=[agent_ownership, users, mcp_api_keys])

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(id=1, username="alice", role="admin",
                                          created_at="t", updated_at="t"))
    yield str(db_file)


def _iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mk_agent(name: str):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_ownership
    with get_engine().begin() as conn:
        conn.execute(insert(agent_ownership).values(
            agent_name=name, owner_id=1, created_at=_iso()))


def _mk_key(key_id: str, agent: str, created_at: str):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import mcp_api_keys
    with get_engine().begin() as conn:
        conn.execute(insert(mcp_api_keys).values(
            id=key_id, name=key_id, key_prefix="trinity_mcp_x", key_hash=f"h-{key_id}",
            created_at=created_at, is_active=1, user_id=1,
            agent_name=agent, scope="agent"))


def _active(key_id: str) -> int:
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().connect() as conn:
        return conn.execute(
            text("SELECT is_active FROM mcp_api_keys WHERE id = :i"), {"i": key_id}
        ).scalar()


# --- the grace window ----------------------------------------------------------

def test_key_minted_seconds_ago_survives_the_sweep(keys_db):
    """The race: the key exists, the ownership row does not yet."""
    from database import db

    _mk_key("k-starting", "starting-agent", created_at=_iso(timedelta(seconds=1)))

    assert db.deactivate_orphaned_agent_keys() == []
    assert _active("k-starting") == 1


def test_key_older_than_the_grace_window_with_no_live_row_is_still_revoked(keys_db):
    from database import db

    _mk_key("k-ghost", "never-existed", created_at=_iso(timedelta(minutes=11)))

    assert db.deactivate_orphaned_agent_keys() == ["never-existed"]
    assert _active("k-ghost") == 0


def test_key_minted_nine_minutes_ago_is_still_inside_the_window(keys_db):
    """The window is ten minutes, not a few seconds: a slow create (a large
    template snapshot) is still covered at nine minutes, so a grace lowered to
    30 s would fail here rather than silently shrink the protection."""
    from database import db

    _mk_key("k-slow", "slow-create", created_at=_iso(timedelta(minutes=9)))

    assert db.deactivate_orphaned_agent_keys() == []
    assert _active("k-slow") == 1


def test_ownership_row_arriving_inside_the_window_keeps_the_key_for_good(keys_db, monkeypatch):
    """Create path end-to-end: mint, a sweep tick, the ownership row lands,
    and every later sweep (simulated by a zero grace) leaves the key alone."""
    from database import db
    import db.mcp_keys as mcp_keys_mod

    _mk_key("k-race", "racer", created_at=_iso())
    assert db.deactivate_orphaned_agent_keys() == []      # tick inside the window
    _mk_agent("racer")                                    # ownership row lands

    monkeypatch.setattr(mcp_keys_mod, "ORPHANED_KEY_GRACE_SECONDS", 0)
    assert db.deactivate_orphaned_agent_keys() == []      # long after the window
    assert _active("k-race") == 1


def test_live_ownership_row_is_never_orphaned_whatever_the_key_age(keys_db):
    """AC1: the container state is irrelevant; the ownership row is the truth."""
    from database import db

    _mk_agent("stopped-but-owned")
    _mk_key("k-old", "stopped-but-owned", created_at=_iso(timedelta(days=30)))

    assert db.deactivate_orphaned_agent_keys() == []
    assert _active("k-old") == 1


def test_one_name_per_revoked_key_sorted_and_idempotent(keys_db):
    """One entry per KEY (the report keeps counting keys, as it did); the
    sweep's log line dedups the names."""
    from database import db

    old = _iso(timedelta(minutes=30))
    _mk_key("k-b1", "beta", created_at=old)
    _mk_key("k-b2", "beta", created_at=old)
    _mk_key("k-a", "alpha", created_at=old)

    assert db.deactivate_orphaned_agent_keys() == ["alpha", "beta", "beta"]
    assert db.deactivate_orphaned_agent_keys() == []     # idempotent


# --- the sweep's log line names the agents -------------------------------------

def test_cleanup_log_line_names_the_revoked_agents(monkeypatch, caplog):
    """Drives the real `_sweep_retention_772` so the hand-off from the db call to
    the log line is what is pinned — deleting that call goes red here."""
    from services import cleanup_service

    monkeypatch.setattr(cleanup_service, "_read_retention_settings", lambda: (0, 0, 0, 0))
    stub = SimpleNamespace(
        scrub_terminal_backlog_metadata=lambda *a, **k: 0,
        resummarize_legacy_tool_calls=lambda *a, **k: 0,
        deactivate_orphaned_agent_keys=lambda *a, **k: ["beta", "alpha", "beta"],
    )
    monkeypatch.setattr(cleanup_service, "db", stub)

    report = cleanup_service.CleanupReport()
    with caplog.at_level(logging.INFO, logger="services.cleanup_service"):
        cleanup_service.CleanupService()._sweep_retention_772(report)

    assert report.orphaned_agent_keys_revoked == 3           # keys, as before
    lines = [r.getMessage() for r in caplog.records if "MCP key" in r.getMessage()]
    assert lines, caplog.text
    assert lines[0].endswith("(#1745): alpha, beta")        # named, deduped, sorted
    assert "Deactivated 3 MCP key(s)" in lines[0]
