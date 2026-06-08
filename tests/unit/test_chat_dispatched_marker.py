"""
Chat dispatched-marker tests (Issue #686)

Issue #686: `/api/chat` had no `mark_execution_dispatched()` defense, so
long-running executions (cold-start agents, ~5KB MCP prompts) were falsely
stamped FAILED by `cleanup_service.mark_no_session_executions_failed` after
60s — even though the agent was still healthy and processing.

The fix mirrors the defense already in `task_execution_service.py:401-410`
(commit 2798ca95, issue #279). It also closes a latent Chat-UI vulnerability
because `/api/chat` is the shared codepath for MCP sequential calls AND the
web Chat-UI.

Hybrid test strategy (per #686 autoplan UC6 + CC unit-test conventions):

- **DB-level tests (real SQLite)**: exercise `mark_no_session_executions_failed`
  directly with hand-crafted rows. Verifies the cleanup mechanism's
  exclude-dispatched filter still works AND that the NULL/'' regression path
  is still caught. These are the load-bearing tests for the fix's mechanism.
- **AST-level tests (parse `routers/chat.py` source)**: verify the marker call
  ordering, the try/except wrapper, the `claude_session_id` kwarg threading,
  and the failure-path `update_execution_status(FAILED)` survive. Mirrors the
  pattern at `tests/unit/test_backlog.py:828-895` (the `_run_async_task_with_persistence`
  rename regression guard). AST avoids the full FastAPI dep-injection chain
  while still catching the recurring bug class.
"""

from __future__ import annotations

import ast
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# tests/unit/conftest.py adds src/backend to sys.path and pre-installs the
# canonical `utils` package via _preload_backend_utils(), so no per-file
# sys.path / sys.modules manipulation is needed here. (Issue #762 lint
# forbids bare `sys.modules` mutations outside conftest.)
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


# ===========================================================================
# DB-level tests: cleanup query behavior
# ===========================================================================


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Spin up a fresh SQLite database with just `schedule_executions`.

    Pattern lifted from `tests/unit/test_backlog.py:66-156` — minimal schema,
    env-var redirected, modules evicted on teardown so the next test isn't
    poisoned.
    """
    db_path = tmp_path / "trinity-686.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY,
            schedule_id TEXT,
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms INTEGER,
            message TEXT NOT NULL DEFAULT '',
            response TEXT,
            error TEXT,
            triggered_by TEXT NOT NULL DEFAULT 'test',
            context_used INTEGER,
            context_max INTEGER,
            cost REAL,
            tool_calls TEXT,
            execution_log TEXT,
            source_user_id INTEGER,
            source_user_email TEXT,
            source_agent_name TEXT,
            source_mcp_key_id TEXT,
            source_mcp_key_name TEXT,
            claude_session_id TEXT,
            model_used TEXT,
            fan_out_id TEXT,
            subscription_id TEXT,
            queued_at TEXT,
            backlog_metadata TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    # Evict any cached backend DB modules so the schedule_ops fixture's
    # `from db.schedules import ScheduleOperations` re-imports against the
    # fresh TRINITY_DB_PATH set above. monkeypatch restores the prior
    # entries at fixture teardown, isolating the next test.
    for mod in ("db.connection", "db.schedules", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)

    yield db_path


@pytest.fixture
def schedule_ops(tmp_db):
    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


def _insert_running(tmp_db: Path, *, exec_id: str, claude_session_id, started_at: str):
    conn = sqlite3.connect(str(tmp_db))
    conn.execute(
        """
        INSERT INTO schedule_executions
          (id, agent_name, status, started_at, message, triggered_by, claude_session_id)
        VALUES (?, ?, 'running', ?, ?, 'mcp', ?)
        """,
        (exec_id, "test-agent", started_at, "msg", claude_session_id),
    )
    conn.commit()
    conn.close()


def _fetch(tmp_db: Path, exec_id: str):
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM schedule_executions WHERE id = ?", (exec_id,)
    ).fetchone()
    conn.close()
    return row


def _stale_started_at(seconds: int) -> str:
    """Format an ISO-Z timestamp `seconds` in the past so it sorts past
    the cleanup threshold computed by `db/schedules.py:1581`.
    """
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


class TestNoSessionCleanupFilter:
    """`mark_no_session_executions_failed` must exclude 'dispatched' rows and
    still catch genuinely-unmarked silent-launch failures.
    """

    pytestmark = pytest.mark.unit

    def test_dispatched_row_survives_no_session_cleanup(
        self, tmp_db, schedule_ops
    ):
        """REGRESSION (#686 mechanism): a 'running' row with
        claude_session_id='dispatched' is NOT matched by the cleanup
        filter, even after 60s+.

        This is the load-bearing assertion. After Change 1 in
        `routers/chat.py`, the chat path stamps 'dispatched' before
        calling the agent — the cleanup sweep's
        `claude_session_id IS NULL OR claude_session_id = ''` filter
        no longer matches, so long-running MCP/Chat-UI calls survive.
        """
        _insert_running(
            tmp_db,
            exec_id="exec-dispatched",
            claude_session_id="dispatched",
            started_at=_stale_started_at(seconds=120),
        )

        count = schedule_ops.mark_no_session_executions_failed(
            timeout_seconds=60
        )
        assert count == 0, (
            "dispatched rows must not match the no-session cleanup filter"
        )

        row = _fetch(tmp_db, "exec-dispatched")
        assert row["status"] == "running"
        assert row["error"] is None
        assert row["claude_session_id"] == "dispatched"

    def test_undispatched_row_still_caught_by_no_session_cleanup(
        self, tmp_db, schedule_ops
    ):
        """REGRESSION (cleanup safety net): a 'running' row with NULL
        claude_session_id is still flagged as a silent launch failure.

        Without this test, a future refactor that weakens the cleanup
        filter (or accidentally stamps 'dispatched' on every row) would
        silently break the 60s fast-fail safety net for genuinely-broken
        launches.
        """
        _insert_running(
            tmp_db,
            exec_id="exec-undispatched",
            claude_session_id=None,
            started_at=_stale_started_at(seconds=120),
        )

        count = schedule_ops.mark_no_session_executions_failed(
            timeout_seconds=60
        )
        assert count == 1, (
            "rows with NULL claude_session_id must still be marked FAILED"
        )

        row = _fetch(tmp_db, "exec-undispatched")
        assert row["status"] == "failed"
        assert row["error"] is not None
        assert "Silent launch failure" in row["error"]

    def test_empty_string_claude_session_id_still_caught(
        self, tmp_db, schedule_ops
    ):
        """Defensive: the cleanup filter is `IS NULL OR = ''`, so empty
        strings must also be caught. Locks in the existing SQL.
        """
        _insert_running(
            tmp_db,
            exec_id="exec-empty",
            claude_session_id="",
            started_at=_stale_started_at(seconds=120),
        )

        count = schedule_ops.mark_no_session_executions_failed(
            timeout_seconds=60
        )
        assert count == 1
        assert _fetch(tmp_db, "exec-empty")["status"] == "failed"


class TestMarkExecutionDispatched:
    """`mark_execution_dispatched` writes 'dispatched' only on a clean
    (status='running', claude_session_id IS NULL) row — idempotent + safe.
    """

    pytestmark = pytest.mark.unit

    def test_marks_running_row_with_null_session(self, tmp_db, schedule_ops):
        _insert_running(
            tmp_db,
            exec_id="exec-mark",
            claude_session_id=None,
            started_at=_stale_started_at(seconds=5),
        )

        updated = schedule_ops.mark_execution_dispatched("exec-mark")
        assert updated is True

        row = _fetch(tmp_db, "exec-mark")
        assert row["claude_session_id"] == "dispatched"
        assert row["status"] == "running"

    def test_no_op_when_already_dispatched(self, tmp_db, schedule_ops):
        """The UPDATE guards on `claude_session_id IS NULL` so calling
        mark twice doesn't clobber a real UUID written later.
        """
        _insert_running(
            tmp_db,
            exec_id="exec-twice",
            claude_session_id=None,
            started_at=_stale_started_at(seconds=1),
        )

        first = schedule_ops.mark_execution_dispatched("exec-twice")
        second = schedule_ops.mark_execution_dispatched("exec-twice")

        assert first is True
        assert second is False, "second mark must be a no-op"


# ===========================================================================
# AST-level tests: routers/chat.py structural verification
#
# Importing routers/chat.py at unit-test time would drag in FastAPI,
# the database singleton, capacity_manager, audit service, etc. The
# canonical analog at tests/unit/test_backlog.py:828-895 solves this by
# parsing the source file with `ast` and asserting the relevant call
# nodes exist where the fix landed them. The recurring bug class
# (silent dispatch-marker regression, #279 → #686) demands a guard at
# this layer — runtime mocks of the full router are too brittle to
# remain useful across refactors.
# ===========================================================================


class TestChatRouterSource:
    """Static analysis guards over `src/backend/routers/chat.py`."""

    pytestmark = pytest.mark.unit

    @pytest.fixture(scope="class")
    def chat_func(self):
        """Parse routers/chat.py and return the function carrying the
        dispatch/execute/finalize logic. After #1026 slice 3 this body lives in
        `_run_chat_and_finalize` (extracted from `chat_with_agent`); the #686 /
        #96 invariants are asserted against it."""
        chat_src = _BACKEND / "routers" / "chat.py"
        assert chat_src.exists(), f"routers/chat.py missing at {chat_src}"
        tree = ast.parse(chat_src.read_text(), filename=str(chat_src))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_run_chat_and_finalize"
            ):
                return node
        pytest.fail("_run_chat_and_finalize function not found in routers/chat.py")

    def _walk_calls_with_line(self, node):
        """Yield (lineno, attr_name, arg_repr) for every call expression
        inside the function body.
        """
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            # Match `obj.attr(...)` form (db.mark_execution_dispatched, etc.)
            if isinstance(func, ast.Attribute):
                yield child.lineno, func.attr, child
            # Match `name(...)` form (agent_post_with_retry, etc.)
            elif isinstance(func, ast.Name):
                yield child.lineno, func.id, child

    def test_marks_execution_dispatched_before_agent_call(self, chat_func):
        """Core ordering guarantee: `mark_execution_dispatched` appears
        BEFORE `agent_post_with_retry` in the chat_with_agent body.

        This is the structural check that catches the #686 regression
        class. Mirrors `task_execution_service.py:401-410`.
        """
        calls = list(self._walk_calls_with_line(chat_func))
        mark_lines = sorted(
            ln for ln, name, _ in calls if name == "mark_execution_dispatched"
        )
        post_lines = sorted(
            ln for ln, name, _ in calls if name == "agent_post_with_retry"
        )

        assert mark_lines, (
            "chat_with_agent must call db.mark_execution_dispatched(...) "
            "before agent_post_with_retry — see fix for #686. The defense "
            "is identical to services/task_execution_service.py:401-410."
        )
        assert post_lines, "agent_post_with_retry call not found"
        # First mark must come before first agent POST.
        assert mark_lines[0] < post_lines[0], (
            f"mark_execution_dispatched is at line {mark_lines[0]}, "
            f"agent_post_with_retry at {post_lines[0]} — marker must "
            f"precede the agent POST so cleanup_service doesn't false-fail "
            f"in-flight executions."
        )

    def test_dispatched_marker_wrapped_in_try_except(self, chat_func):
        """The marker call lives inside a `try / except / warning-log` block
        so a transient DB error never breaks chat.

        Mirrors `task_execution_service.py:406-410`. Without the wrapper, a
        flaky `mark_execution_dispatched` would 500 every chat call instead
        of degrading silently to the prior (vulnerable) behavior.
        """
        for try_node in ast.walk(chat_func):
            if not isinstance(try_node, ast.Try):
                continue
            # Does this try-block contain a mark_execution_dispatched call?
            contains_mark = any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr == "mark_execution_dispatched"
                for c in ast.walk(try_node)
            )
            if not contains_mark:
                continue
            # And the except clause logs a warning?
            for handler in try_node.handlers:
                for h_node in ast.walk(handler):
                    if (
                        isinstance(h_node, ast.Call)
                        and isinstance(h_node.func, ast.Attribute)
                        and h_node.func.attr == "warning"
                    ):
                        return  # pass
        pytest.fail(
            "mark_execution_dispatched must be wrapped in try/except with a "
            "logger.warning(...) on failure — mirrors task_execution_service.py:406-410"
        )

    def test_success_update_passes_real_claude_session_id(self, chat_func):
        """UC1: the SUCCESS-path `update_execution_status` call must pass a
        `claude_session_id=` kwarg so the real Claude UUID overwrites the
        'dispatched' sentinel.

        Without this, every chat-triggered row permanently shows
        claude_session_id='dispatched' — destroying observability and
        creating a future-bug surface for any code reading the column as a
        real session id.
        """
        # Find every update_execution_status call in the body.
        update_calls = [
            c
            for c in ast.walk(chat_func)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "update_execution_status"
        ]
        assert update_calls, "no update_execution_status calls found"

        success_calls = []
        for call in update_calls:
            for kw in call.keywords:
                if kw.arg == "status":
                    # Status is `TaskExecutionStatus.SUCCESS` — captured as
                    # an ast.Attribute node.
                    if (
                        isinstance(kw.value, ast.Attribute)
                        and kw.value.attr == "SUCCESS"
                    ):
                        success_calls.append(call)
        assert success_calls, (
            "no update_execution_status(status=TaskExecutionStatus.SUCCESS) "
            "call found in chat_with_agent"
        )

        for call in success_calls:
            kwarg_names = {kw.arg for kw in call.keywords}
            assert "claude_session_id" in kwarg_names, (
                f"update_execution_status SUCCESS call at line {call.lineno} "
                f"must pass claude_session_id=... — without it the row keeps "
                f"the 'dispatched' sentinel forever (#686 UC1)."
            )

    def test_failure_path_marks_execution_failed(self, chat_func):
        """Sanity: the httpx.HTTPError handler still calls
        `update_execution_status(status=TaskExecutionStatus.FAILED, ...)`.

        Regression guard against accidentally weakening the failure-path
        cleanup that the cleanup-service can't substitute for (because
        cleanup only catches NULL/'' rows, not dispatched-then-errored
        ones).
        """
        has_failed_update = False
        for call in ast.walk(chat_func):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "update_execution_status"
            ):
                continue
            for kw in call.keywords:
                if (
                    kw.arg == "status"
                    and isinstance(kw.value, ast.Attribute)
                    and kw.value.attr == "FAILED"
                ):
                    has_failed_update = True
                    break
        assert has_failed_update, (
            "chat_with_agent must call update_execution_status("
            "status=TaskExecutionStatus.FAILED) in its httpx error handler"
        )


# ===========================================================================
# Mirror-pattern guard: chat.py and task_execution_service.py stay in sync
# ===========================================================================


class TestMirrorPatternStillInSync:
    """The chat-router fix is a verbatim mirror of the
    `task_execution_service.py` defense. If one side rolls back the marker
    call, the other must signal a regression — that's the bug class behind
    both #279 and #686.
    """

    pytestmark = pytest.mark.unit

    def test_both_paths_call_mark_execution_dispatched(self):
        """Both `routers/chat.py` and `services/task_execution_service.py`
        must call `db.mark_execution_dispatched(...)`.

        UC3 follow-up will pull this into a shared `dispatch_to_agent`
        primitive — until then, the call must exist in BOTH places.
        """
        chat_src = (_BACKEND / "routers" / "chat.py").read_text()
        task_src = (
            _BACKEND / "services" / "task_execution_service.py"
        ).read_text()

        assert "mark_execution_dispatched" in chat_src, (
            "routers/chat.py is missing the mark_execution_dispatched call — "
            "#686 regression"
        )
        assert "mark_execution_dispatched" in task_src, (
            "services/task_execution_service.py is missing the "
            "mark_execution_dispatched call — #279 regression"
        )
