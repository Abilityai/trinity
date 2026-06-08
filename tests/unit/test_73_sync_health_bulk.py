"""Unit tests for the #73 bulk auto-sync lookup (Fix 2).

`GET /api/agents/sync-health` previously called db.get_git_auto_sync_enabled()
once per agent (an N+1 on an otherwise-bulk endpoint). Fix 2 replaces that with
a single scoped query, db.get_all_git_auto_sync_enabled(accessible).

These tests run against the migrated temp DB pinned by tests/unit/conftest.py
(no live server). They prove:
  - the bulk map equals the per-agent lookup for every agent (the exact
    substitution the router makes) — i.e. /sync-health output is unchanged;
  - agents with no agent_git_config row are absent -> caller defaults False;
  - the query is scoped (F6): an inaccessible agent is excluded.

Issue: https://github.com/Abilityai/trinity/issues/73
"""
from __future__ import annotations

import pytest

from database import db
from db.connection import get_db_connection

_PREFIX = "t73sync-"
_ON = f"{_PREFIX}on"
_OFF = f"{_PREFIX}off"
_NOCONFIG = f"{_PREFIX}noconfig"
_INACCESSIBLE = f"{_PREFIX}inaccessible"


def _cleanup():
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM agent_git_config WHERE agent_name LIKE ?", (f"{_PREFIX}%",)
        )
        conn.commit()


@pytest.fixture
def fixture_agents():
    """Create git-config rows for the test agents, then tear them down."""
    _cleanup()
    # _ON and _OFF and _INACCESSIBLE get config rows; _NOCONFIG deliberately
    # has none. Each agent gets a DISTINCT github_repo — agent_git_config has a
    # partial UNIQUE index on (github_repo, working_branch), so reusing one repo
    # would collide on the 2nd insert.
    for name in (_ON, _OFF, _INACCESSIBLE):
        created = db.create_git_config(
            agent_name=name,
            github_repo=f"owner/{name}",
            working_branch="main",
            instance_id=f"inst-{name}",
        )
        assert created is not None, f"fixture setup failed to create {name}"
    db.set_git_auto_sync_enabled(_ON, True)
    db.set_git_auto_sync_enabled(_OFF, False)
    db.set_git_auto_sync_enabled(_INACCESSIBLE, True)
    yield
    _cleanup()


@pytest.mark.unit
def test_bulk_map_matches_per_agent_and_is_scoped(fixture_agents):
    accessible = {_ON, _OFF, _NOCONFIG}

    bulk = db.get_all_git_auto_sync_enabled(accessible)

    # Only agents WITH a config row in the accessible set appear.
    assert bulk == {_ON: True, _OFF: False}

    # No-config agent is absent -> caller's .get(name, False) yields False.
    assert bulk.get(_NOCONFIG, False) is False

    # F6 scope: an out-of-scope agent (has auto-sync ON) is excluded.
    assert _INACCESSIBLE not in bulk

    # Parity: this is the exact substitution the /sync-health router makes —
    # auto_sync_map.get(name, False) must equal the old per-agent call for
    # every accessible agent.
    for name in accessible:
        assert bulk.get(name, False) == db.get_git_auto_sync_enabled(name)


@pytest.mark.unit
def test_unscoped_returns_all_config_rows(fixture_agents):
    """With no scope, every config row is returned (used by callers that want
    the whole fleet)."""
    full = db.get_all_git_auto_sync_enabled()
    assert full.get(_ON) is True
    assert full.get(_OFF) is False
    assert full.get(_INACCESSIBLE) is True
    assert _NOCONFIG not in full


@pytest.mark.unit
def test_empty_scope_returns_empty_map(fixture_agents):
    """An empty accessible set must not fall through to the unscoped query."""
    assert db.get_all_git_auto_sync_enabled(set()) == {}


@pytest.mark.unit
def test_scoped_query_chunks_large_set(fixture_agents, monkeypatch):
    """#73: a scope larger than the SQLite host-param chunk size is split into
    multiple IN(...) queries and merged correctly (guards the param-cap fix).

    Forcing the chunk size to 2 over a 3-agent scope exercises the multi-chunk
    merge path without inserting 900+ rows. Patched on the method's __globals__
    (not via sys.modules) so it survives the unit-conftest module juggling.
    """
    from db.schedules import ScheduleOperations

    monkeypatch.setitem(
        ScheduleOperations.get_all_git_auto_sync_enabled.__globals__,
        "_SQLITE_MAX_IN_VARS",
        2,
    )

    scope = {_ON, _OFF, _INACCESSIBLE}  # 3 names, chunk size 2 -> 2 queries
    bulk = db.get_all_git_auto_sync_enabled(scope)

    # Merged result across both chunks equals the per-agent truth for every name.
    assert bulk == {_ON: True, _OFF: False, _INACCESSIBLE: True}
    for name in scope:
        assert bulk[name] == db.get_git_auto_sync_enabled(name)
