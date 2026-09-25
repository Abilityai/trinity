"""
#3010 — the auto-sync toggle is authoritative and live.

  - the agent's loop asks the owner's flag EVERY cycle: OFF → the next cycle is
    skipped, ON → the next cycle runs, a flip between two cycles is honoured
    with no recreate (driven through a real `httpx.MockTransport`)
  - the fallbacks: 404 "Git not configured" → off; backend down / 5xx / any
    other 404 → the env, which the last recreate derived from the same flag
  - the loop starts whenever the agent can ask the platform, so ON works on an
    agent whose container never baked GIT_SYNC_AUTO
  - `/api/git/status` reports the value the loop is running with
  - the one-shot backfill, both tracks, against real SQLite: live
    non-source-mode ghosts are set; everything else is left alone
(Recreate-after-OFF stays OFF: tests/unit/test_ent109_git_env_seam.py
TestGitSyncAuto. Creation writes the flag for exactly the predicate set, ghosts
included: tests/unit/test_2069_gitignore_at_creation.py
TestCreationDbFlagMatchesPredicate.)
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

# `agent_server` is the real base-image package, installed by
# tests/unit/conftest.py::_preload_real_agent_server.
from agent_server import auto_sync

AGENT = "a1"
FLAG_URL = f"http://backend:8000/api/agents/{AGENT}/git/auto-sync"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _agent_env(monkeypatch):
    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_test")
    monkeypatch.setenv("AGENT_NAME", AGENT)
    monkeypatch.delenv("GIT_SYNC_AUTO", raising=False)
    monkeypatch.setattr(auto_sync, "_last_resolved", None)


class _Platform:
    """A backend double answering GET /git/auto-sync; `answer` is mutable so a
    test can flip the owner's toggle between two cycles."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        status, body = self.answer
        return httpx.Response(status, json=body)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _cycle(platform, home):
    ran = []

    def run_once(h):
        ran.append(h)
        return {"status": "success"}

    async def go():
        async with platform.client() as client:
            return await auto_sync.run_one_cycle(client, home, run_once)

    result = _run(go())
    return result, ran


class TestLiveToggle:
    def test_off_skips_the_next_cycle(self, repo, monkeypatch):
        monkeypatch.setenv("GIT_SYNC_AUTO", "true")  # baked ON at creation
        platform = _Platform((200, {"agent_name": AGENT, "auto_sync_enabled": False}))
        result, ran = _cycle(platform, repo)
        assert result is None and ran == []
        assert auto_sync.current_auto_sync_enabled() is False

    def test_on_runs_the_next_cycle_without_a_baked_env(self, repo):
        platform = _Platform((200, {"agent_name": AGENT, "auto_sync_enabled": True}))
        result, ran = _cycle(platform, repo)
        assert result == {"status": "success"} and ran == [repo]
        assert auto_sync.current_auto_sync_enabled() is True

    def test_a_flip_between_cycles_is_honoured(self, repo):
        platform = _Platform((200, {"auto_sync_enabled": True}))
        assert _cycle(platform, repo)[1] == [repo]
        platform.answer = (200, {"auto_sync_enabled": False})
        assert _cycle(platform, repo)[1] == []
        platform.answer = (200, {"auto_sync_enabled": True})
        assert _cycle(platform, repo)[1] == [repo]

    def test_the_read_uses_the_agents_own_key_on_its_own_path(self, repo):
        platform = _Platform((200, {"auto_sync_enabled": True}))
        _cycle(platform, repo)
        req = platform.requests[0]
        assert str(req.url) == FLAG_URL
        assert req.method == "GET"
        assert req.headers["Authorization"] == "Bearer trinity_mcp_test"

    def test_no_repo_means_no_platform_call(self, tmp_path):
        platform = _Platform((200, {"auto_sync_enabled": True}))
        result, ran = _cycle(platform, tmp_path)
        assert result is None and ran == [] and platform.requests == []


class TestFallbacks:
    def test_git_not_configured_is_off(self, repo, monkeypatch):
        monkeypatch.setenv("GIT_SYNC_AUTO", "true")
        platform = _Platform((404, {"detail": "Git not configured"}))
        assert _cycle(platform, repo)[1] == []

    @pytest.mark.parametrize("env", ["true", None])
    @pytest.mark.parametrize(
        "answer",
        [
            (500, {"detail": "boom"}),
            (404, {"detail": "Agent not found"}),  # uniform 404: not "no git"
            (403, {"detail": "nope"}),
            httpx.ConnectError("backend down"),
            httpx.ReadTimeout("slow"),
        ],
        ids=["5xx", "uniform-404", "403", "connect-error", "timeout"],
    )
    def test_platform_trouble_falls_back_to_the_env(self, repo, monkeypatch, env, answer):
        if env:
            monkeypatch.setenv("GIT_SYNC_AUTO", env)
        ran = _cycle(_Platform(answer), repo)[1]
        assert ran == ([repo] if env else [])

    def test_non_json_200_falls_back_to_the_env(self, repo, monkeypatch):
        monkeypatch.setenv("GIT_SYNC_AUTO", "true")

        class _Garbled(_Platform):
            def handler(self, request):
                return httpx.Response(200, content=b"<html>proxy</html>")

        assert _cycle(_Garbled(None), repo)[1] == [repo]

    def test_without_platform_coords_the_env_decides(self, repo, monkeypatch):
        monkeypatch.delenv("TRINITY_MCP_API_KEY")
        monkeypatch.setenv("GIT_SYNC_AUTO", "true")
        platform = _Platform((200, {"auto_sync_enabled": False}))
        assert _cycle(platform, repo)[1] == [repo]
        assert platform.requests == []


class TestLoopStart:
    def test_starts_without_a_baked_env_when_it_can_ask(self):
        assert auto_sync.should_run_auto_sync() is False
        assert auto_sync.should_start_loop() is True

    def test_does_not_start_when_it_can_neither_ask_nor_was_baked(self, monkeypatch):
        monkeypatch.delenv("TRINITY_BACKEND_URL")
        assert auto_sync.should_start_loop() is False

    def test_starts_on_a_baked_env_alone(self, monkeypatch):
        monkeypatch.delenv("TRINITY_BACKEND_URL")
        monkeypatch.setenv("GIT_SYNC_AUTO", "true")
        assert auto_sync.should_start_loop() is True

    def test_before_the_first_cycle_the_running_value_is_the_env(self, monkeypatch):
        monkeypatch.setenv("GIT_SYNC_AUTO", "true")
        assert auto_sync.current_auto_sync_enabled() is True


class TestStatusReportsTheRunningValue:
    def test_git_status_carries_auto_sync_enabled(self, tmp_path, monkeypatch):
        import subprocess
        from agent_server.routers import git as git_router

        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        monkeypatch.setattr(auto_sync, "_last_resolved", False)
        payload = git_router._compute_git_status(tmp_path)
        assert payload["auto_sync_enabled"] is False
        monkeypatch.setattr(auto_sync, "_last_resolved", True)
        assert git_router._compute_git_status(tmp_path)["auto_sync_enabled"] is True


# ---------------------------------------------------------------------------
# The one-shot backfill — both tracks
# ---------------------------------------------------------------------------

@pytest.fixture()
def backfill_db():
    from db.schema import TABLES
    conn = sqlite3.connect(":memory:")
    for t in ("agent_ownership", "agent_git_config"):
        conn.execute(TABLES[t])
    now = "2026-09-25T00:00:00.000000Z"

    def agent(name, *, ghost=False, deleted=None, source_mode=0, auto=0):
        conn.execute(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at, is_ephemeral) "
            "VALUES (?,1,?,?,?)", (name, now, deleted, 1 if ghost else 0))
        conn.execute(
            "INSERT INTO agent_git_config (id, agent_name, github_repo, working_branch, instance_id, "
            "created_at, source_mode, auto_sync_enabled) VALUES (?,?,?,?,?,?,?,?)",
            (f"id-{name}", name, f"o/{name}", f"trinity/{name}/x", "x", now, source_mode, auto))

    agent("ghost", ghost=True)                                   # the slice → 1
    agent("ghost-source-mode", ghost=True, source_mode=1)        # never baked → 0
    agent("ghost-deleted", ghost=True, deleted=now)              # gone → 0
    agent("ghost-already-on", ghost=True, auto=1)                # untouched → 1
    agent("owner-disabled")                                      # non-ghost OFF → 0 (the OFF now sticks)
    agent("initialized-later")                                   # /git/initialize shape → 0
    agent("regular-on", auto=1)                                  # untouched → 1
    conn.commit()
    yield conn
    conn.close()


EXPECTED = {
    "ghost": 1, "ghost-source-mode": 0, "ghost-deleted": 0, "ghost-already-on": 1,
    "owner-disabled": 0, "initialized-later": 0, "regular-on": 1,
}


def _flags(conn):
    return dict(conn.execute("SELECT agent_name, auto_sync_enabled FROM agent_git_config"))


def test_sqlite_backfill_sets_only_live_non_source_ghosts(backfill_db):
    from db.migrations import _migrate_auto_sync_enabled_backfill
    _migrate_auto_sync_enabled_backfill(backfill_db.cursor(), backfill_db)
    assert _flags(backfill_db) == EXPECTED
    _migrate_auto_sync_enabled_backfill(backfill_db.cursor(), backfill_db)  # idempotent
    assert _flags(backfill_db) == EXPECTED


def test_alembic_backfill_matches_the_sqlite_track(backfill_db, monkeypatch):
    import sqlalchemy as sa
    path = Path(__file__).resolve().parents[2] / "src/backend/migrations/versions/0075_auto_sync_enabled_backfill.py"
    spec = importlib.util.spec_from_file_location("rev0075_3010", path)
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)
    assert (rev.revision, rev.down_revision) == (
        "0075_auto_sync_enabled_backfill", "0074_role_readiness_rollout_seed")

    engine = sa.create_engine("sqlite://", creator=lambda: backfill_db)
    with engine.begin() as conn:
        monkeypatch.setattr(rev.op, "get_bind", lambda: conn)
        rev.upgrade()
        rev.upgrade()
    assert _flags(backfill_db) == EXPECTED


def test_the_backfill_is_registered_on_the_sqlite_track():
    from db.migrations import MIGRATIONS
    names = [name for name, _ in MIGRATIONS]
    assert "auto_sync_enabled_backfill" in names
