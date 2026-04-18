"""
S7 Layer 0/1/2 unit tests for branch-ownership enforcement (issue #382).

Covers:
  (i)  `reserve_and_generate_instance_id` retries when `ls-remote` shows a
       collision.
  (ii) After the configured max retries the helper raises.
  (iii) The partial UNIQUE index on `agent_git_config(github_repo,
        working_branch) WHERE source_mode = 0` rejects duplicate bindings
        for working-branch agents and accepts duplicates for source-mode
        agents (intentional — source-mode agents share e.g. `main`).
  (iv) The migration pre-flight surfaces existing duplicate bindings and
       refuses to install the UNIQUE index until operators resolve them.

The tests use an in-memory SQLite database with just the pieces of schema
they need — no live backend required. `ls-remote` is patched so no network
is hit.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Path / module stubs so we can import the backend's git_service cleanly
# without importing database.py (which wires up the full DB on import).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# tests/utils shadows src/backend/utils, so stub the helpers module we
# need from the backend side.
if "utils.helpers" not in sys.modules:
    _helpers = types.ModuleType("utils.helpers")
    _helpers.utc_now = lambda: datetime.now(timezone.utc)  # type: ignore[attr-defined]
    _helpers.utc_now_iso = lambda: datetime.now(timezone.utc).isoformat()  # type: ignore[attr-defined]
    _helpers.to_utc_iso = lambda v: str(v)  # type: ignore[attr-defined]
    _helpers.parse_iso_timestamp = (  # type: ignore[attr-defined]
        lambda s: datetime.fromisoformat(s.rstrip("Z"))
    )
    sys.modules["utils.helpers"] = _helpers


def _ensure_module_stubs() -> None:
    """Provide lightweight stubs for modules git_service.py imports at import time."""
    if "database" not in sys.modules:
        database = types.ModuleType("database")

        class AgentGitConfig:  # minimal container, only fields the tests touch
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class GitSyncResult:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class _Db:  # replaced by the test fixtures when they need DB calls
            def create_git_config(self, *args, **kwargs):
                raise NotImplementedError("test did not install a db stub")

        database.AgentGitConfig = AgentGitConfig
        database.GitSyncResult = GitSyncResult
        database.db = _Db()
        sys.modules["database"] = database

    if "services.docker_service" not in sys.modules:
        docker_service = types.ModuleType("services.docker_service")
        docker_service.get_agent_container = lambda *a, **kw: None

        async def _exec(*args, **kwargs):
            return {"exit_code": 0, "output": ""}

        docker_service.execute_command_in_container = _exec
        # Make "services" a package rooted at src/backend/services so that
        # `from services import git_service` picks up the real module.
        if "services" not in sys.modules:
            services_pkg = types.ModuleType("services")
            services_pkg.__path__ = [str(_BACKEND / "services")]  # type: ignore[attr-defined]
            sys.modules["services"] = services_pkg
        sys.modules["services.docker_service"] = docker_service


_ensure_module_stubs()

# Clear any cached git_service to pick up edits between the test file and
# the implementation.
sys.modules.pop("services.git_service", None)
from services import git_service  # noqa: E402  (after sys.path tweak)


# ---------------------------------------------------------------------------
# In-memory SQLite helpers
# ---------------------------------------------------------------------------
GIT_CONFIG_DDL = """
    CREATE TABLE agent_git_config (
        id TEXT PRIMARY KEY,
        agent_name TEXT UNIQUE NOT NULL,
        github_repo TEXT NOT NULL,
        working_branch TEXT NOT NULL,
        instance_id TEXT NOT NULL,
        source_branch TEXT DEFAULT 'main',
        source_mode INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        last_sync_at TEXT,
        last_commit_sha TEXT,
        sync_enabled INTEGER DEFAULT 1,
        sync_paths TEXT,
        github_pat_encrypted TEXT
    )
"""

# This is the index under test — S7 Layer 2.
PARTIAL_UNIQUE_DDL = (
    "CREATE UNIQUE INDEX idx_git_config_repo_branch_unique "
    "ON agent_git_config(github_repo, working_branch) "
    "WHERE source_mode = 0"
)


def _insert_row(
    cursor: sqlite3.Cursor,
    *,
    agent_name: str,
    github_repo: str,
    working_branch: str,
    source_mode: int,
    instance_id: str = "deadbeef",
) -> None:
    cursor.execute(
        """
        INSERT INTO agent_git_config
            (id, agent_name, github_repo, working_branch, instance_id,
             source_branch, source_mode, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"cfg-{agent_name}",
            agent_name,
            github_repo,
            working_branch,
            instance_id,
            "main",
            source_mode,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(GIT_CONFIG_DDL)
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Layer 2 — partial UNIQUE index behaviour
# ---------------------------------------------------------------------------
class TestPartialUniqueConstraint:
    def test_rejects_duplicate_working_branches_for_source_mode_0(self, db_conn):
        """Two agents cannot bind to the same (repo, branch) when source_mode=0."""
        db_conn.execute(PARTIAL_UNIQUE_DDL)
        cur = db_conn.cursor()

        _insert_row(
            cur,
            agent_name="alpaca-a",
            github_repo="org/alpaca",
            working_branch="trinity/alpaca/a702560e",
            source_mode=0,
        )
        db_conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            _insert_row(
                cur,
                agent_name="alpaca-b",
                github_repo="org/alpaca",
                working_branch="trinity/alpaca/a702560e",
                source_mode=0,
            )
            db_conn.commit()

    def test_allows_duplicate_branches_for_source_mode_1(self, db_conn):
        """Source-mode agents intentionally share a branch (e.g. main). Partial index excludes them."""
        db_conn.execute(PARTIAL_UNIQUE_DDL)
        cur = db_conn.cursor()

        _insert_row(
            cur,
            agent_name="reader-a",
            github_repo="org/docs",
            working_branch="main",
            source_mode=1,
        )
        _insert_row(
            cur,
            agent_name="reader-b",
            github_repo="org/docs",
            working_branch="main",
            source_mode=1,
        )
        db_conn.commit()

        rows = cur.execute(
            "SELECT agent_name FROM agent_git_config WHERE source_mode = 1"
        ).fetchall()
        assert sorted(r[0] for r in rows) == ["reader-a", "reader-b"]

    def test_allows_same_branch_across_different_repos(self, db_conn):
        """The uniqueness scope is per-repo: same branch name, different repo is fine."""
        db_conn.execute(PARTIAL_UNIQUE_DDL)
        cur = db_conn.cursor()

        _insert_row(
            cur,
            agent_name="alpaca-a",
            github_repo="org/alpaca",
            working_branch="trinity/alpaca/a702560e",
            source_mode=0,
        )
        _insert_row(
            cur,
            agent_name="alpaca-b",
            github_repo="org/other",
            working_branch="trinity/alpaca/a702560e",
            source_mode=0,
        )
        db_conn.commit()


# ---------------------------------------------------------------------------
# Layer 0 — reserve_and_generate_instance_id
# ---------------------------------------------------------------------------
def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeDb:
    """Minimal db stub: records each create_git_config call."""

    def __init__(self):
        self.created = []

    def create_git_config(
        self,
        agent_name,
        github_repo,
        working_branch,
        instance_id,
        sync_paths=None,
        source_branch="main",
        source_mode=False,
    ):
        self.created.append(
            {
                "agent_name": agent_name,
                "github_repo": github_repo,
                "working_branch": working_branch,
                "instance_id": instance_id,
                "source_branch": source_branch,
                "source_mode": source_mode,
            }
        )
        return types.SimpleNamespace(
            agent_name=agent_name,
            github_repo=github_repo,
            working_branch=working_branch,
            instance_id=instance_id,
        )


class TestReserveAndGenerateInstanceId:
    def test_helper_exists(self):
        assert hasattr(
            git_service, "reserve_and_generate_instance_id"
        ), "S7 Layer 0 helper reserve_and_generate_instance_id must exist"
        assert hasattr(
            git_service, "check_remote_branch_exists"
        ), "S7 Layer 0 helper check_remote_branch_exists must exist"

    def test_returns_fresh_branch_when_remote_is_clean(self, monkeypatch):
        """Happy path: remote reports no collision, helper returns on first try."""

        async def _no_collision(github_repo, branch):
            return False

        fake_db = _FakeDb()
        monkeypatch.setattr(git_service, "check_remote_branch_exists", _no_collision)
        monkeypatch.setattr(git_service, "db", fake_db, raising=False)

        instance_id, working_branch = _run(
            git_service.reserve_and_generate_instance_id(
                agent_name="alpaca",
                github_repo="org/alpaca",
            )
        )

        assert working_branch == f"trinity/alpaca/{instance_id}"
        assert len(instance_id) == 8
        assert len(fake_db.created) == 1
        assert fake_db.created[0]["working_branch"] == working_branch

    def test_retries_on_remote_collision(self, monkeypatch):
        """When ls-remote reports a collision the helper picks a new UUID and retries."""
        calls = {"n": 0}

        async def _collide_twice_then_clear(github_repo, branch):
            calls["n"] += 1
            return calls["n"] <= 2  # collide on 1st and 2nd probe, clear on 3rd

        fake_db = _FakeDb()
        monkeypatch.setattr(
            git_service, "check_remote_branch_exists", _collide_twice_then_clear
        )
        monkeypatch.setattr(git_service, "db", fake_db, raising=False)

        instance_id, working_branch = _run(
            git_service.reserve_and_generate_instance_id(
                agent_name="alpaca",
                github_repo="org/alpaca",
            )
        )

        assert calls["n"] == 3
        assert working_branch == f"trinity/alpaca/{instance_id}"
        assert len(fake_db.created) == 1

    def test_raises_after_max_retries(self, monkeypatch):
        """If every probe collides the helper gives up with a descriptive error."""

        async def _always_collide(github_repo, branch):
            return True

        fake_db = _FakeDb()
        monkeypatch.setattr(git_service, "check_remote_branch_exists", _always_collide)
        monkeypatch.setattr(git_service, "db", fake_db, raising=False)

        with pytest.raises(RuntimeError) as exc_info:
            _run(
                git_service.reserve_and_generate_instance_id(
                    agent_name="alpaca",
                    github_repo="org/alpaca",
                )
            )

        msg = str(exc_info.value).lower()
        assert "collision" in msg or "retr" in msg or "reserve" in msg
        assert fake_db.created == []  # never inserted a row

    def test_db_insert_failure_triggers_retry(self, monkeypatch):
        """If the DB unique constraint rejects the row we try a fresh ID."""

        async def _no_collision(github_repo, branch):
            return False

        calls = {"inserts": 0}

        class _FlakyDb:
            def __init__(self):
                self.created = []

            def create_git_config(self, *args, **kwargs):
                calls["inserts"] += 1
                if calls["inserts"] == 1:
                    raise sqlite3.IntegrityError(
                        "UNIQUE constraint failed: idx_git_config_repo_branch_unique"
                    )
                self.created.append(kwargs)
                return types.SimpleNamespace(**kwargs)

        flaky = _FlakyDb()
        monkeypatch.setattr(git_service, "check_remote_branch_exists", _no_collision)
        monkeypatch.setattr(git_service, "db", flaky, raising=False)

        instance_id, working_branch = _run(
            git_service.reserve_and_generate_instance_id(
                agent_name="alpaca",
                github_repo="org/alpaca",
            )
        )

        assert calls["inserts"] == 2
        assert flaky.created and flaky.created[0]["working_branch"] == working_branch


# ---------------------------------------------------------------------------
# Migration pre-flight
# ---------------------------------------------------------------------------
class TestMigrationPreflight:
    def test_detects_existing_duplicates(self, db_conn):
        """
        The migration must surface duplicate bindings (same repo+branch with
        source_mode=0) before trying to create the partial UNIQUE index, so
        operators can fix them first.
        """
        # Import the migration helper lazily — the test runs before the
        # helper exists at TDD-red time, which is the desired failure mode.
        sys.modules.pop("db.migrations", None)
        try:
            from db.migrations import (  # type: ignore[import]
                _find_duplicate_working_branches,
            )
        except ImportError as exc:  # pragma: no cover — TDD red path
            pytest.fail(
                f"S7 migration helper _find_duplicate_working_branches is missing: {exc}"
            )

        cur = db_conn.cursor()
        _insert_row(
            cur,
            agent_name="alpaca-a",
            github_repo="org/alpaca",
            working_branch="trinity/alpaca/a702560e",
            source_mode=0,
        )
        _insert_row(
            cur,
            agent_name="alpaca-b",
            github_repo="org/alpaca",
            working_branch="trinity/alpaca/a702560e",
            source_mode=0,
        )
        # A third, unrelated binding should NOT appear in the result.
        _insert_row(
            cur,
            agent_name="other",
            github_repo="org/other",
            working_branch="trinity/other/deadbeef",
            source_mode=0,
        )
        db_conn.commit()

        duplicates = _find_duplicate_working_branches(cur)

        assert len(duplicates) == 1, f"expected one duplicate group, got {duplicates!r}"
        group = duplicates[0]
        assert group["github_repo"] == "org/alpaca"
        assert group["working_branch"] == "trinity/alpaca/a702560e"
        assert sorted(group["agent_names"]) == ["alpaca-a", "alpaca-b"]

    def test_no_duplicates_returns_empty_list(self, db_conn):
        sys.modules.pop("db.migrations", None)
        try:
            from db.migrations import (  # type: ignore[import]
                _find_duplicate_working_branches,
            )
        except ImportError as exc:  # pragma: no cover — TDD red path
            pytest.fail(
                f"S7 migration helper _find_duplicate_working_branches is missing: {exc}"
            )

        cur = db_conn.cursor()
        _insert_row(
            cur,
            agent_name="alpaca-a",
            github_repo="org/alpaca",
            working_branch="trinity/alpaca/a702560e",
            source_mode=0,
        )
        _insert_row(
            cur,
            agent_name="reader-a",
            github_repo="org/docs",
            working_branch="main",
            source_mode=1,
        )
        _insert_row(
            cur,
            agent_name="reader-b",
            github_repo="org/docs",
            working_branch="main",
            source_mode=1,
        )
        db_conn.commit()

        assert _find_duplicate_working_branches(cur) == []

    def test_migration_aborts_when_duplicates_exist(self, db_conn):
        """
        End-to-end: running the S7 migration against a DB with pre-existing
        duplicates must raise with a message that references the offending
        rows, instead of silently deleting them.
        """
        sys.modules.pop("db.migrations", None)
        try:
            from db.migrations import (  # type: ignore[import]
                _migrate_agent_git_config_branch_ownership,
            )
        except ImportError as exc:  # pragma: no cover — TDD red path
            pytest.fail(
                f"S7 migration function _migrate_agent_git_config_branch_ownership is missing: {exc}"
            )

        cur = db_conn.cursor()
        _insert_row(
            cur,
            agent_name="alpaca-a",
            github_repo="org/alpaca",
            working_branch="trinity/alpaca/a702560e",
            source_mode=0,
        )
        _insert_row(
            cur,
            agent_name="alpaca-b",
            github_repo="org/alpaca",
            working_branch="trinity/alpaca/a702560e",
            source_mode=0,
        )
        db_conn.commit()

        with pytest.raises(Exception) as exc_info:
            _migrate_agent_git_config_branch_ownership(cur, db_conn)

        msg = str(exc_info.value)
        assert "alpaca-a" in msg and "alpaca-b" in msg
        # The index must NOT exist after an aborted migration.
        indexes = [
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='agent_git_config'"
            ).fetchall()
        ]
        assert "idx_git_config_repo_branch_unique" not in indexes

    def test_migration_installs_index_on_clean_db(self, db_conn):
        sys.modules.pop("db.migrations", None)
        try:
            from db.migrations import (  # type: ignore[import]
                _migrate_agent_git_config_branch_ownership,
            )
        except ImportError as exc:  # pragma: no cover — TDD red path
            pytest.fail(
                f"S7 migration function _migrate_agent_git_config_branch_ownership is missing: {exc}"
            )

        cur = db_conn.cursor()
        _insert_row(
            cur,
            agent_name="alpaca-a",
            github_repo="org/alpaca",
            working_branch="trinity/alpaca/a702560e",
            source_mode=0,
        )
        _insert_row(
            cur,
            agent_name="reader-a",
            github_repo="org/docs",
            working_branch="main",
            source_mode=1,
        )
        _insert_row(
            cur,
            agent_name="reader-b",
            github_repo="org/docs",
            working_branch="main",
            source_mode=1,
        )
        db_conn.commit()

        _migrate_agent_git_config_branch_ownership(cur, db_conn)

        indexes = [
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='agent_git_config'"
            ).fetchall()
        ]
        assert "idx_git_config_repo_branch_unique" in indexes

        # Now the constraint must bite for source_mode=0.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_row(
                cur,
                agent_name="alpaca-b",
                github_repo="org/alpaca",
                working_branch="trinity/alpaca/a702560e",
                source_mode=0,
            )
            db_conn.commit()
