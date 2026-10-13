"""The sync-health read surfaces carry the backend's verdict (trinity-enterprise#706).

`GET /api/agents/sync-health` (the dashboard dot's batch feed) and
`GET /api/agents/{name}/git/sync-state` (and MCP `get_git_sync_state`, a
pass-through) add the policy's `state` / `reason` / `recommendation` / `binding`
/ `freeze` and the new columns, and keep every pre-existing key unchanged.
Also the shared reader `db.list_sync_health_rows`. Real DB via db_harness; the
handlers are called directly with the Docker-backed accessibility lookup
stubbed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db_harness import db_backend, run as _hrun  # noqa: F401

pytestmark = pytest.mark.unit

_PRE_706_BATCH_KEYS = {
    "agent_name", "auto_sync_enabled", "last_sync_at", "last_sync_status",
    "consecutive_failures", "last_error_summary", "behind_working", "behind_main",
    "ahead_working", "ahead_main", "git_dir_bytes",
}
_NEW_KEYS = {
    "state", "reason", "recommendation", "binding", "dirty_files",
    "divergence_age_s", "last_successful_push_at", "freeze",
}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def db(db_backend):  # noqa: F811
    from database import db as database

    return database


def _bind(db, name, *, source_mode=0, auto_sync=True, freeze=True, sync_enabled=1,
          deleted=False):
    _hrun(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at) "
        "VALUES (:n, 1, '2026-01-01T00:00:00Z', :d)",
        n=name, d="2026-09-01T00:00:00Z" if deleted else None,
    )
    db.create_git_config(
        agent_name=name, github_repo=f"owner/{name}",
        working_branch=f"trinity/{name}/x", instance_id=f"inst-{name}",
        source_mode=bool(source_mode),
    )
    db.set_git_auto_sync_enabled(name, auto_sync)
    db.set_freeze_schedules_if_sync_failing(name, freeze)
    if not sync_enabled:
        _hrun("UPDATE agent_git_config SET sync_enabled = 0 WHERE agent_name = :n", n=name)


def _diverge(db, name, *, hours, ahead=7):
    now = datetime.now(timezone.utc)
    db.upsert_sync_state(
        name, last_sync_status="success", ahead_working=ahead,
        diverged_since=_iso(now - timedelta(hours=hours)),
        dirty_files=791, last_successful_push_at="2026-09-26T09:30:00.000000Z",
        last_check_at=_iso(now - timedelta(seconds=30)),
    )


# --------------------------------------------------------------------------
# Shared reader
# --------------------------------------------------------------------------

def test_reader_returns_config_and_state_for_live_git_agents(db):
    _bind(db, "r-live")
    _diverge(db, "r-live", hours=2)
    _bind(db, "r-nostate")
    _bind(db, "r-deleted", deleted=True)
    _bind(db, "r-off", sync_enabled=0)
    rows = db.list_sync_health_rows()
    assert set(rows) >= {"r-live", "r-nostate"}
    assert not {"r-deleted", "r-off"} & set(rows)
    live = rows["r-live"]
    assert live["config"]["auto_sync_enabled"] is True
    assert live["config"]["source_mode"] is False
    assert live["state"]["ahead_working"] == 7
    assert live["state"]["dirty_files"] == 791
    assert rows["r-nostate"]["state"] is None
    assert set(db.list_sync_health_rows(["r-live", "someone-else"])) == {"r-live"}
    assert db.list_sync_health_rows([]) == {}


# --------------------------------------------------------------------------
# GET /api/agents/sync-health
# --------------------------------------------------------------------------

def _batch(monkeypatch, names):
    import routers.agents as agents_router

    monkeypatch.setattr(agents_router, "get_accessible_agents",
                        lambda user: [{"name": n} for n in names])
    body = asyncio.run(agents_router.get_all_sync_health(current_user=SimpleNamespace()))
    return {e["agent_name"]: e for e in body["agents"]}


def test_batch_carries_the_verdict_and_keeps_every_old_key(db, monkeypatch):
    _bind(db, "b-red")
    _diverge(db, "b-red", hours=26)
    _bind(db, "b-yellow", source_mode=1, auto_sync=False)
    _diverge(db, "b-yellow", hours=26)
    entries = _batch(monkeypatch, ["b-red", "b-yellow", "b-nogit"])

    red = entries["b-red"]
    assert _PRE_706_BATCH_KEYS | _NEW_KEYS <= set(red)
    assert red["state"] == "red"
    assert red["reason"].startswith("diverged 0 behind / 7 ahead for 26h")
    assert red["binding"] == "agent"
    assert red["freeze"] is True
    assert red["dirty_files"] == 791
    assert red["last_successful_push_at"] == "2026-09-26T09:30:00.000000Z"
    assert 26 * 3600 <= red["divergence_age_s"] < 26 * 3600 + 60
    # Pre-existing keys keep their meaning.
    assert (red["auto_sync_enabled"], red["ahead_working"], red["last_sync_status"]) == (
        True, 7, "success")

    yellow = entries["b-yellow"]
    assert (yellow["state"], yellow["binding"], yellow["freeze"]) == (
        "yellow", "deployment", False)

    nogit = entries["b-nogit"]
    assert _PRE_706_BATCH_KEYS | _NEW_KEYS <= set(nogit)
    assert nogit["state"] == "unknown"
    assert (nogit["last_sync_status"], nogit["auto_sync_enabled"]) == ("never", False)


def test_batch_is_scoped_to_accessible_agents(db, monkeypatch):
    _bind(db, "b-mine")
    _bind(db, "b-theirs")
    assert set(_batch(monkeypatch, ["b-mine"])) == {"b-mine"}


# --------------------------------------------------------------------------
# GET /api/agents/{name}/git/sync-state
# --------------------------------------------------------------------------

def _single(name):
    from routers.git import get_agent_sync_state

    return asyncio.run(get_agent_sync_state(name))


def test_sync_state_is_the_row_plus_the_verdict(db):
    _bind(db, "s-red")
    _diverge(db, "s-red", hours=26)
    body = _single("s-red")
    assert body["ahead_working"] == 7
    assert body["diverged_since"] is not None
    assert body["dirty_files"] == 791
    assert body["state"] == "red"
    assert body["freeze"] is True
    assert body["reason"].startswith("diverged 0 behind / 7 ahead for 26h")


def test_sync_state_without_a_row_is_unknown(db):
    _bind(db, "s-none")
    body = _single("s-none")
    assert (body["agent_name"], body["last_sync_status"], body["consecutive_failures"]) == (
        "s-none", "never", 0)
    assert body["state"] == "unknown"
