"""
SyncHealthService tests (#389 S1).

The service polls each git-enabled agent on an interval, pulls the dual
ahead/behind + sync-state from its `/api/git/status` response, upserts the
`agent_sync_state` row, and emits a `sync_failing` operator-queue entry when
consecutive_failures crosses the threshold.

These are pure unit tests — the AgentClient is replaced with an in-memory
fake so no agent containers are needed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _BACKEND / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_schema = _load("db/schema.py", "_schema_sh")
_migrations = _load("db/migrations.py", "_migrations_sh")


pytestmark = pytest.mark.unit


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    _schema.init_schema(cursor, conn)
    _migrations.run_all_migrations(cursor, conn)
    conn.commit()
    conn.close()
    # Evict cached modules aggressively so production code (the
    # DatabaseManager singleton especially) sees the new DB path.
    for modname in list(sys.modules):
        if modname == "database" or modname.startswith("db.") \
                or modname == "services.sync_health_service":
            sys.modules.pop(modname, None)
    yield db_path


@pytest.fixture
def seed_agent(tmp_db):
    def _seed(name: str, auto_sync: bool = True):
        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES (?, 1, datetime('now'))",
            (name,),
        )
        conn.execute(
            """INSERT INTO agent_git_config
               (id, agent_name, github_repo, working_branch, instance_id,
                created_at, sync_enabled, auto_sync_enabled)
               VALUES (?, ?, ?, ?, ?, datetime('now'), 1, ?)""",
            (name + "-git", name, "org/repo", f"trinity/{name}/abc123",
             "abc123", 1 if auto_sync else 0),
        )
        conn.commit()
        conn.close()
    return _seed


def _status_payload(status="success", ahead_working=0, behind_working=0, error=None):
    return {
        "git_enabled": True,
        "branch": "trinity/alpha/abc123",
        "remote_url": "https://github.com/org/repo",
        "last_commit": {"sha": "deadbeef"},
        "changes": [],
        "changes_count": 0,
        "ahead": 0,
        "behind": 0,
        "ahead_main": 0,
        "behind_main": 0,
        "ahead_working": ahead_working,
        "behind_working": behind_working,
        "sync_state": {
            "last_sync_status": status,
            "last_sync_at": "2026-04-18T10:00:00+00:00",
            "last_error_summary": error,
            "consecutive_failures": 0,  # agent-side counter, backend recomputes
        },
        "sync_status": "up_to_date",
    }


@pytest.fixture
def service(tmp_db):
    """SyncHealthService instance with a stub AgentClient."""
    from services.sync_health_service import SyncHealthService  # noqa: WPS433
    svc = SyncHealthService(poll_interval=0)
    return svc


class TestSyncStatePersistence:
    """Each poll cycle upserts a row per agent."""

    @pytest.mark.asyncio
    async def test_success_recorded(self, service, seed_agent):
        seed_agent("alpha")
        fake_status = _status_payload(status="success")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=fake_status)):
            await service._poll_cycle()
        from database import db
        row = db.get_sync_state("alpha")
        assert row is not None
        assert row["last_sync_status"] == "success"
        assert row["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_failure_increments_counter(self, service, seed_agent):
        seed_agent("alpha")
        payload = _status_payload(status="failed", error="push failed")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=payload)):
            await service._poll_cycle()
            await service._poll_cycle()
        from database import db
        row = db.get_sync_state("alpha")
        assert row["consecutive_failures"] == 2
        assert row["last_sync_status"] == "failed"

    @pytest.mark.asyncio
    async def test_unreachable_agent_is_skipped(self, service, seed_agent):
        seed_agent("alpha")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=None)):
            await service._poll_cycle()
        from database import db
        # Agent unreachable → no row written.
        assert db.get_sync_state("alpha") is None


class TestOperatorQueueEmission:
    """sync_failing entry emitted when consecutive_failures crosses 3."""

    @pytest.mark.asyncio
    async def test_no_entry_on_first_two_failures(self, service, seed_agent):
        seed_agent("alpha")
        payload = _status_payload(status="failed", error="e1")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=payload)):
            await service._poll_cycle()
            await service._poll_cycle()
        from database import db
        items = db.list_operator_queue_items(agent_name="alpha")
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_entry_emitted_on_third_failure(self, service, seed_agent):
        seed_agent("alpha")
        payload = _status_payload(status="failed", error="boom")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=payload)):
            await service._poll_cycle()
            await service._poll_cycle()
            await service._poll_cycle()
        from database import db
        items = db.list_operator_queue_items(agent_name="alpha")
        sync_failing = [i for i in items if i["type"] == "sync_failing"]
        assert len(sync_failing) == 1
        assert "boom" in (sync_failing[0].get("context") or {}).get(
            "last_error_summary", "")

    @pytest.mark.asyncio
    async def test_success_resets_counter_and_allows_future_emissions(
        self, service, seed_agent
    ):
        seed_agent("alpha")
        fail = _status_payload(status="failed", error="e")
        ok = _status_payload(status="success")

        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=fail)):
            await service._poll_cycle()
            await service._poll_cycle()
            await service._poll_cycle()
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=ok)):
            await service._poll_cycle()  # success resets counter
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=fail)):
            await service._poll_cycle()
            await service._poll_cycle()
            await service._poll_cycle()  # third failure since reset

        from database import db
        items = db.list_operator_queue_items(agent_name="alpha")
        sync_failing = [i for i in items if i["type"] == "sync_failing"]
        # Two distinct failure series → two entries (distinct IDs by timestamp).
        assert len(sync_failing) == 2


class TestBehindWorkingRedFlag:
    """Record behind_working so the dashboard can colour a red dot on P6 writes."""

    @pytest.mark.asyncio
    async def test_behind_working_recorded(self, service, seed_agent):
        seed_agent("alpha")
        payload = _status_payload(status="success", behind_working=2)
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=payload)):
            await service._poll_cycle()
        from database import db
        row = db.get_sync_state("alpha")
        assert row["behind_working"] == 2
