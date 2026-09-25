"""
trinity-enterprise#703 — the container's pull cycle (invariant G3: human and
fleet work reaches the agent within a bound).

Real repos: a bare "origin", the agent's clone, and a "human" clone that pushes
underneath the agent. The cycle is `routers/git.py::_run_pull_once`:
  - behind with nothing local → fast-forward
  - behind with a local commit → rebase, the local commit on top
  - behind with uncommitted edits → stashed and re-applied, edits intact
  - incoming changes colliding with uncommitted edits → the pull is UNDONE:
    same HEAD, same edits, failure recorded (never lost to the stash)
  - a rebase conflict → aborted, recorded, nothing reset
  - an execution in flight (or unknown) → skipped, the tree untouched
  - the push's own fields are never touched by a pull
Plus the loop gate (flag read live, env fallback) and both migration tracks.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import subprocess
from pathlib import Path

import httpx
import pytest

# `agent_server` is the real base-image package, installed by
# tests/unit/conftest.py::_preload_real_agent_server.
from agent_server import auto_sync
from agent_server.routers import git as git_router

_ROOT = Path(__file__).resolve().parents[2]


def _git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, timeout=20, check=check)


def _out(repo, *args):
    return _git(repo, *args).stdout.strip()


def _identity(repo):
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """(agent, origin, human); nothing running on the agent by default."""
    origin, agent, human = tmp_path / "origin.git", tmp_path / "agent", tmp_path / "human"
    origin.mkdir()
    agent.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    _git(agent, "init", "-q", "-b", "main")
    _identity(agent)
    _git(agent, "remote", "add", "origin", str(origin))
    (agent / "role.md").write_text("role v1\n")
    (agent / "notes.md").write_text("notes v1\n")
    (agent / ".gitignore").write_text(".trinity/\n")
    _git(agent, "add", "-A")
    _git(agent, "commit", "-q", "-m", "init")
    _git(agent, "push", "-q", "-u", "origin", "main")
    (agent / ".trinity").mkdir()
    _git(tmp_path, "clone", "-q", str(origin), str(human))
    _identity(human)
    monkeypatch.setattr(git_router, "_executions_in_flight", lambda: 0)
    return agent, origin, human


def _human_push(human, name, content):
    _git(human, "pull", "-q", "--rebase", "origin", "main")
    (human / name).write_text(content)
    _git(human, "add", name)
    _git(human, "commit", "-q", "-m", f"human: {name}")
    _git(human, "push", "-q", "origin", "HEAD:main")
    return _out(human, "rev-parse", "HEAD")


def _state(agent):
    return git_router._read_sync_state_file(agent)


class TestPull:
    def test_behind_and_clean_fast_forwards(self, world):
        agent, _, human = world
        sha = _human_push(human, "role.md", "role v2 — edited on GitHub\n")

        result = git_router._run_pull_once(agent)

        assert result["status"] == "success"
        assert _out(agent, "rev-parse", "HEAD") == sha
        assert (agent / "role.md").read_text() == "role v2 — edited on GitHub\n"
        st = _state(agent)
        assert st["last_pull_status"] == "success" and st["behind_after_pull"] == 0
        assert st["last_pull_at"]

    def test_a_local_commit_is_rebased_on_top(self, world):
        agent, _, human = world
        sha = _human_push(human, "role.md", "role v2\n")
        (agent / "notes.md").write_text("notes v2 — the agent's\n")
        _git(agent, "commit", "-qam", "agent: notes")

        assert git_router._run_pull_once(agent)["status"] == "success"
        assert _out(agent, "rev-parse", "HEAD~1") == sha
        assert _out(agent, "log", "-1", "--format=%s") == "agent: notes"
        assert (agent / "role.md").read_text() == "role v2\n"

    def test_uncommitted_edits_survive_a_pull(self, world):
        """A source-mode agent never commits: its standing edits must ride through."""
        agent, _, human = world
        sha = _human_push(human, "role.md", "role v2\n")
        (agent / "notes.md").write_text("notes — uncommitted\n")

        assert git_router._run_pull_once(agent)["status"] == "success"
        assert _out(agent, "rev-parse", "HEAD") == sha
        assert (agent / "notes.md").read_text() == "notes — uncommitted\n"
        assert (agent / "role.md").read_text() == "role v2\n"
        assert _out(agent, "stash", "list") == ""

    def test_incoming_changes_colliding_with_edits_are_undone_not_lost(self, world):
        """The autostash trap: its re-apply conflicts, leaves the edits only in
        the stash, and reports success. Here the pull is undone instead."""
        agent, _, human = world
        before = _out(agent, "rev-parse", "HEAD")
        _human_push(human, "notes.md", "notes — human rewrite\n")
        (agent / "notes.md").write_text("notes — agent's uncommitted edit\n")

        result = git_router._run_pull_once(agent)

        assert result["status"] == "failed"
        assert result["error"] == "local edits conflict with incoming changes on main"
        assert _out(agent, "rev-parse", "HEAD") == before
        assert (agent / "notes.md").read_text() == "notes — agent's uncommitted edit\n"
        assert _out(agent, "stash", "list") == ""
        assert _state(agent)["behind_after_pull"] == 1

    def test_a_rebase_conflict_is_aborted_and_recorded(self, world):
        agent, _, human = world
        _human_push(human, "notes.md", "notes — human\n")
        (agent / "notes.md").write_text("notes — agent\n")
        _git(agent, "commit", "-qam", "agent: notes")
        local = _out(agent, "rev-parse", "HEAD")

        result = git_router._run_pull_once(agent)

        assert result["status"] == "failed"
        assert result["error"] == "diverged: rebase conflict on main"
        assert _out(agent, "rev-parse", "HEAD") == local
        assert not (agent / ".git" / "rebase-merge").exists()
        assert (agent / "notes.md").read_text() == "notes — agent\n"

    @pytest.mark.parametrize("running, reason", [(2, "execution in flight"),
                                                 (None, "execution state unknown")])
    def test_never_while_an_execution_runs(self, world, monkeypatch, running, reason):
        agent, _, human = world
        before = _out(agent, "rev-parse", "HEAD")
        _human_push(human, "role.md", "role v2\n")
        monkeypatch.setattr(git_router, "_executions_in_flight", lambda: running)

        result = git_router._run_pull_once(agent)

        assert result == {"status": "skipped", "error": reason}
        assert _out(agent, "rev-parse", "HEAD") == before

    def test_an_execution_starting_mid_cycle_stops_it_before_the_tree(self, world, monkeypatch):
        agent, _, human = world
        before = _out(agent, "rev-parse", "HEAD")
        _human_push(human, "role.md", "role v2\n")
        answers = iter([0, 1])  # idle at the start, busy after the fetch
        monkeypatch.setattr(git_router, "_executions_in_flight", lambda: next(answers))

        result = git_router._run_pull_once(agent)

        assert result["status"] == "skipped"
        assert _out(agent, "rev-parse", "HEAD") == before

    def test_a_busy_repo_is_skipped_without_writing_state(self, world):
        agent, _, _ = world
        assert git_router._REPO_LOCK.acquire(blocking=False)
        try:
            assert git_router._run_pull_once(agent) == {"status": "skipped", "reason": "repo_busy"}
        finally:
            git_router._REPO_LOCK.release()
        assert _state(agent)["last_pull_status"] == "never"

    def test_a_pull_never_touches_the_push_fields(self, world):
        agent, _, human = world
        git_router._write_sync_state_file(agent, "failed", last_error_summary="push boom")
        _human_push(human, "role.md", "role v2\n")

        git_router._run_pull_once(agent)

        st = _state(agent)
        assert (st["last_sync_status"], st["consecutive_failures"], st["last_error_summary"]) == (
            "failed", 1, "push boom")
        assert st["last_pull_status"] == "success"

    def test_up_to_date_and_unpushed_branch_are_quiet_successes(self, world):
        agent, _, _ = world
        assert git_router._run_pull_once(agent)["status"] == "success"
        _git(agent, "checkout", "-q", "-b", "trinity/agent/new")
        assert git_router._run_pull_once(agent)["status"] == "success"
        assert _state(agent)["behind_after_pull"] == 0


class TestLoopGate:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
        monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_test")
        monkeypatch.setenv("AGENT_NAME", "a1")
        monkeypatch.delenv("GIT_SYNC_PULL", raising=False)
        monkeypatch.setattr(auto_sync, "_last_pull_resolved", None)

    def _cycle(self, repo, answer):
        seen, ran = [], []

        def handler(request):
            seen.append(str(request.url))
            if isinstance(answer, Exception):
                raise answer
            return httpx.Response(answer[0], json=answer[1])

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await auto_sync.run_one_pull_cycle(c, repo, lambda h: ran.append(h) or {"status": "success"})

        result = asyncio.new_event_loop().run_until_complete(go())
        return result, ran, seen

    def test_the_flag_is_read_live_on_its_own_endpoint(self, tmp_path):
        (tmp_path / ".git").mkdir()
        _, ran, seen = self._cycle(tmp_path, (200, {"pull_sync_enabled": True}))
        assert ran == [tmp_path]
        assert seen == ["http://backend:8000/api/agents/a1/git/pull-sync"]

    def test_off_skips(self, tmp_path):
        (tmp_path / ".git").mkdir()
        _, ran, _ = self._cycle(tmp_path, (200, {"pull_sync_enabled": False}))
        assert ran == []

    @pytest.mark.parametrize("env, expected", [("true", True), (None, False)])
    def test_platform_down_falls_back_to_the_env(self, tmp_path, monkeypatch, env, expected):
        (tmp_path / ".git").mkdir()
        if env:
            monkeypatch.setenv("GIT_SYNC_PULL", env)
        _, ran, _ = self._cycle(tmp_path, httpx.ConnectError("down"))
        assert bool(ran) is expected

    def test_pull_interval_defaults_to_the_push_interval(self, monkeypatch):
        monkeypatch.setenv("GIT_SYNC_INTERVAL_SECONDS", "600")
        monkeypatch.delenv("GIT_SYNC_PULL_INTERVAL_SECONDS", raising=False)
        assert auto_sync.get_pull_interval_seconds() == 600
        monkeypatch.setenv("GIT_SYNC_PULL_INTERVAL_SECONDS", "300")
        assert auto_sync.get_pull_interval_seconds() == 300


# ---------------------------------------------------------------------------
# Both migration tracks: columns + the backfill (pull on where auto-sync is on)
# ---------------------------------------------------------------------------

@pytest.fixture()
def pre_migration_db():
    """The two tables as they stood BEFORE this migration."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE agent_git_config (agent_name TEXT PRIMARY KEY, "
                 "auto_sync_enabled INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE agent_sync_state (agent_name TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO agent_git_config VALUES (?, ?)",
                     [("pushing", 1), ("quiet", 0), ("unset", None)])
    conn.commit()
    yield conn
    conn.close()


def _pull_flags(conn):
    return dict(conn.execute("SELECT agent_name, pull_sync_enabled FROM agent_git_config"))


def test_sqlite_migration_adds_columns_and_backfills_only_auto_sync_agents(pre_migration_db):
    from db.migrations import _migrate_pull_sync
    _migrate_pull_sync(pre_migration_db.cursor(), pre_migration_db)
    _migrate_pull_sync(pre_migration_db.cursor(), pre_migration_db)  # idempotent
    assert _pull_flags(pre_migration_db) == {"pushing": 1, "quiet": 0, "unset": 0}
    cols = {r[1] for r in pre_migration_db.execute("PRAGMA table_info(agent_sync_state)")}
    assert {"last_pull_at", "last_pull_status", "behind_after_pull"} <= cols


def test_alembic_revision_chains_on_the_head_and_backfills_the_same(pre_migration_db, monkeypatch):
    path = _ROOT / "src/backend/migrations/versions/0076_pull_sync.py"
    spec = importlib.util.spec_from_file_location("rev0076_703", path)
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)
    assert (rev.revision, rev.down_revision) == ("0076_pull_sync", "0075_auto_sync_enabled_backfill")
    # PostgreSQL's `ADD COLUMN IF NOT EXISTS` is not SQLite; run the statements
    # through a translating executor so the backfill is exercised for real.
    executed = []

    def fake_execute(sql):
        executed.append(sql)
        stmt = sql.replace(" IF NOT EXISTS", "")
        pre_migration_db.execute(stmt)

    monkeypatch.setattr(rev.op, "execute", fake_execute)
    rev.upgrade()
    assert _pull_flags(pre_migration_db) == {"pushing": 1, "quiet": 0, "unset": 0}
    assert all("IF NOT EXISTS" in s for s in executed if "ADD COLUMN" in s)


def test_the_migration_is_registered_on_the_sqlite_track():
    from db.migrations import MIGRATIONS
    assert "pull_sync" in [name for name, _ in MIGRATIONS]
