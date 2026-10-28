"""#4000 — `agent_sync_state.git_stats_at`: when the agent last measured its
repo (git_dir_bytes / pack_count / loose_objects).

RD-04 tells "measured a day ago" from "measured a minute ago" with this column.
It is agent-authored (`.trinity/sync-state.json`), so it crosses the same
timestamp boundary as `last_successful_push_at`; an absent or bad value keeps
the stored one (old base images never send it).

Round-trips through the REAL SyncStateOperations over the full schema — a live
select is the only thing that catches a missed `db/tables.py` column.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND_STR = str(Path(__file__).resolve().parents[2] / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401  (fixture)
from db_harness import run as _hrun  # noqa: E402

pytestmark = pytest.mark.unit

# Same restore discipline as test_1595_sync_health_signals.py (#762).
_STUBBED_MODULE_NAMES = ["database", "services.sync_health_service", "services.agent_client"]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def tmp_db(db_backend, monkeypatch):
    for modname in list(sys.modules):
        if modname == "database" or modname.startswith("db.") \
                or modname in ("services.sync_health_service", "services.agent_client"):
            if modname in ("db.engine", "db.tables", "db.schema"):
                continue
            sys.modules.pop(modname, None)
    monkeypatch.setitem(sys.modules, "services.agent_client",
                        types.SimpleNamespace(AgentClient=MagicMock()))
    _hrun("INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
          "VALUES ('alpha', 1, '2026-01-01T00:00:00Z')")
    return db_backend


def _sync_once(**sync_state):
    from services.sync_health_service import SyncHealthService
    sync_state.setdefault("last_sync_status", "success")
    sync_state.setdefault("last_sync_at", "2026-10-27T10:00:00+00:00")
    service = SyncHealthService(poll_interval=0)
    service._fetch_git_status = AsyncMock(return_value={
        "git_enabled": True, "last_commit": {"sha": "deadbeef"},
        "ahead_working": 0, "behind_working": 0, "sync_state": sync_state,
    })
    asyncio.run(service._sync_agent(types.SimpleNamespace(agent_name="alpha")))
    from database import db
    return db.get_sync_state("alpha")


def test_upsert_round_trips_and_none_keeps_prior(tmp_db):
    from db.sync_state import SyncStateOperations
    ops = SyncStateOperations()
    ops.upsert("alpha", last_sync_status="success", git_stats_at="2026-10-27T10:00:00Z")
    assert ops.get("alpha")["git_stats_at"] == "2026-10-27T10:00:00Z"
    ops.upsert("alpha", last_sync_status="success")
    assert ops.get("alpha")["git_stats_at"] == "2026-10-27T10:00:00Z"


def test_sync_health_persists_git_stats_at_as_utc_iso(tmp_db):
    row = _sync_once(git_stats_at="2026-10-27T12:30:00+02:00", pack_count=3)
    assert row["git_stats_at"].startswith("2026-10-27T10:30:00")


@pytest.mark.parametrize("bad", [
    None, "", "yesterday", "2026-10-27T10:00:00",  # naive: not our writer
    "2999-01-01T00:00:00Z",  # far future would read as fresh forever
    12345,
])
def test_sync_health_rejects_bad_git_stats_at_and_keeps_prior(tmp_db, bad):
    _sync_once(git_stats_at="2026-10-27T09:00:00Z")
    row = _sync_once(git_stats_at=bad)
    assert row["git_stats_at"].startswith("2026-10-27T09:00:00")


def test_old_base_image_without_the_key_keeps_null(tmp_db):
    row = _sync_once(pack_count=3)
    assert row["git_stats_at"] is None
