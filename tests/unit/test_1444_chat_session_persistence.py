"""#1444 — async `/task` with save_to_session must persist a chat session.

Fast unit/regression guards for the in-process `/task` persistence path
(`routers/chat.py::_persist_chat_session` +
`_run_async_task_with_persistence`). These are the CI guard the slow,
`requires_agent` integration tests in
`tests/test_dynamic_thinking_status.py::TestAsyncModeSessionPersistence` never
provided — *that* is why #1444 shipped: the persistence contract was only
covered by live-stack tests that never gate CI.

Root cause (triaged three ways: direct `_persist_chat_session`, the full wrapper
with a stubbed `execute_task`, and the full wrapper with the REAL `execute_task`
+ real terminal-CAS row write): on stock config the in-process persistence path
is **correct** — it creates a `chat_sessions` row + user & assistant
`chat_messages` whenever `execute_task` returns a SUCCESS result. The #1083
fire-and-forget callback path (which does NOT persist) is structurally
unreachable by a manual `/task`: `ASYNC_DISPATCH_ELIGIBLE_TRIGGERS` is a
hardcoded `frozenset({"schedule", "webhook"})`.

These tests pin that contract, plus the #1444 hardening:
  * fail-loud — a persistence error logs at ERROR (with a stack trace) and never
    leaks user content into the log message, never re-raises past a billed turn;
  * Security F2 — a caller-supplied `chat_session_id` belonging to another user
    is NOT written into (IDOR); the write falls through to the caller's own
    session.

Real-SQLite (and PostgreSQL when TEST_POSTGRES_URL is set) via db_harness (#300).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401  (db_backend is a pytest fixture)

pytestmark = pytest.mark.unit


# Sentinels that must NEVER appear in a fail-loud log record (Security F3).
_SENSITIVE_EMAIL = "victim-pii@example.com"
_SENSITIVE_MSG = "SENSITIVE_USER_MESSAGE_should_not_be_logged"
_SENSITIVE_RESP = "SENSITIVE_ASSISTANT_RESPONSE_should_not_be_logged"


@pytest.fixture
def chat_mod(db_backend, monkeypatch):
    """Import routers.chat against a fresh real-DB backend.

    Pops the DB module cache so `database` / `db.chat` rebind to the engine the
    db_harness just repointed at the throwaway backend.
    """
    for mod in ("db.connection", "db.chat", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    import routers.chat as chat  # noqa: E402

    return chat


def _make_request(chat_mod, **overrides):
    from models import ParallelTaskRequest

    base = dict(
        message="What is 2+2? Reply with just the number.",
        async_mode=True,
        save_to_session=True,
        user_message="What is 2+2?",
        create_new_session=True,
    )
    base.update(overrides)
    return ParallelTaskRequest(**base)


class _SuccessResult:
    """Minimal stand-in for TaskExecutionResult on the SUCCESS path."""

    def __init__(self, response="4", execution_id="exec-1"):
        from models import TaskExecutionStatus

        self.status = TaskExecutionStatus.SUCCESS
        self.response = response
        self.execution_id = execution_id
        self.cost = 0.01
        self.context_used = 100
        self.context_max = 200000
        self.error = None


class _FailedResult:
    def __init__(self, execution_id="exec-1"):
        from models import TaskExecutionStatus

        self.status = TaskExecutionStatus.FAILED
        self.response = ""
        self.execution_id = execution_id
        self.cost = None
        self.context_used = None
        self.context_max = None
        self.error = "agent unavailable"


def _count(table: str) -> int:
    from db.engine import get_engine
    from sqlalchemy import text

    with get_engine().connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0


# ---------------------------------------------------------------------------
# Fold 2 — the CI guard: the async wrapper persists on a SUCCESS result.
# ---------------------------------------------------------------------------

def test_async_wrapper_persists_session_on_success(chat_mod, monkeypatch):
    """`_run_async_task_with_persistence` with a SUCCESS `execute_task` creates
    one chat_sessions row + a user AND an assistant chat_messages row.

    This is the exact contract the failing integration test asserts, driven at
    unit speed with a stubbed execute_task — the guard that would have caught
    #1444 in CI."""

    class _Svc:
        async def execute_task(self, **kwargs):
            return _SuccessResult(execution_id=kwargs.get("execution_id") or "exec-1")

    monkeypatch.setattr(chat_mod, "get_task_execution_service", lambda: _Svc())

    asyncio.run(chat_mod._run_async_task_with_persistence(
        agent_name="agent-a",
        request=_make_request(chat_mod),
        execution_id="exec-1",
        collaboration_activity_id=None,
        x_source_agent=None,
        user_id=7,
        user_email="owner@example.com",
    ))

    from db.engine import get_engine
    from sqlalchemy import text

    assert _count("chat_sessions") == 1
    with get_engine().connect() as conn:
        roles = [
            r[0]
            for r in conn.execute(
                text("SELECT role FROM chat_messages ORDER BY timestamp")
            ).all()
        ]
    assert "user" in roles
    assert "assistant" in roles


def test_persist_returns_session_id_on_success(chat_mod):
    """Direct `_persist_chat_session` on a SUCCESS result returns the new
    session id and writes both messages (the primitive Fold 2 builds on)."""
    sid = asyncio.run(chat_mod._persist_chat_session(
        agent_name="agent-a",
        request=_make_request(chat_mod),
        result=_SuccessResult(),
        user_id=7,
        user_email="owner@example.com",
    ))
    assert sid is not None
    assert _count("chat_sessions") == 1
    assert _count("chat_messages") == 2


def test_no_persist_on_non_success(chat_mod):
    """A non-SUCCESS terminal (FAILED/CANCELLED) must NOT write an empty
    assistant message — persistence is guarded on SUCCESS."""
    sid = asyncio.run(chat_mod._persist_chat_session(
        agent_name="agent-a",
        request=_make_request(chat_mod),
        result=_FailedResult(),
        user_id=7,
        user_email="owner@example.com",
    ))
    assert sid is None
    assert _count("chat_sessions") == 0
    assert _count("chat_messages") == 0


# ---------------------------------------------------------------------------
# Fail-loud — a persistence error is visible, safe, and non-fatal.
# ---------------------------------------------------------------------------

def test_persist_failure_is_loud_safe_and_non_fatal(chat_mod, monkeypatch, caplog):
    """A raising DB writer must: return None (not raise past a billed turn),
    log at ERROR, and NEVER leak user_message / user_email / response into the
    log message (Security F3)."""

    def _boom(*a, **k):
        raise RuntimeError("simulated db write failure")

    # create_new_session=True → create_new_chat_session runs first.
    monkeypatch.setattr(chat_mod.db, "create_new_chat_session", _boom, raising=False)

    req = _make_request(
        chat_mod, user_message=_SENSITIVE_MSG
    )

    with caplog.at_level(logging.ERROR, logger="routers.chat"):
        sid = asyncio.run(chat_mod._persist_chat_session(
            agent_name="agent-a",
            request=req,
            result=_SuccessResult(response=_SENSITIVE_RESP),
            user_id=7,
            user_email=_SENSITIVE_EMAIL,
        ))

    assert sid is None  # non-fatal: swallowed after logging

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a persistence failure must log at ERROR (fail-loud)"

    # The message string carries agent + execution_id + exc type only.
    msg = errors[0].getMessage()
    assert "agent-a" in msg
    assert "exec-1" in msg
    assert "RuntimeError" in msg

    # No user content anywhere in the emitted records (message OR traceback).
    joined = "\n".join(
        r.getMessage() + "\n" + (logging.Formatter().formatException(r.exc_info) if r.exc_info else "")
        for r in errors
    )
    assert _SENSITIVE_EMAIL not in joined
    assert _SENSITIVE_MSG not in joined
    assert _SENSITIVE_RESP not in joined


def test_dbapi_error_params_not_leaked_by_fail_loud_log(chat_mod, monkeypatch, caplog):
    """A REAL SQLAlchemy statement error that binds user content must NOT leak
    that content through the fail-loud `exc_info=True` log.

    The sibling test above uses a param-less RuntimeError, which cannot exercise
    SQLAlchemy's `[parameters: [...]]` tail — the realistic failure is a DBAPIError
    from the `add_chat_message` INSERT (which binds the message body as a
    parameter). Without `hide_parameters=True` on the engine (db/engine.py), that
    tail is appended to the exception str and `exc_info=True` writes it verbatim
    to the ERROR log (proven leak). This guards that engine config end-to-end."""
    from db.engine import get_engine
    from sqlalchemy import text

    def _boom(**kwargs):
        # A genuine engine statement error that binds the sensitive body as a
        # parameter (unknown table → OperationalError, params still attached).
        with get_engine().connect() as conn:
            conn.execute(
                text("SELECT * FROM __no_such_table__ WHERE body = :b"),
                {"b": _SENSITIVE_MSG},
            )

    monkeypatch.setattr(chat_mod.db, "add_chat_message", _boom, raising=False)

    with caplog.at_level(logging.ERROR, logger="routers.chat"):
        sid = asyncio.run(chat_mod._persist_chat_session(
            agent_name="agent-a",
            request=_make_request(chat_mod, user_message=_SENSITIVE_MSG),
            result=_SuccessResult(response=_SENSITIVE_RESP),
            user_id=7,
            user_email=_SENSITIVE_EMAIL,
        ))

    assert sid is None  # non-fatal
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a persistence failure must log at ERROR (fail-loud)"

    # The full emitted record — message AND the exc_info traceback (which is
    # where the DBAPIError's parameter tail would surface) — carries no content.
    joined = "\n".join(
        r.getMessage() + "\n" + (logging.Formatter().formatException(r.exc_info) if r.exc_info else "")
        for r in errors
    )
    assert _SENSITIVE_MSG not in joined
    assert _SENSITIVE_RESP not in joined
    assert _SENSITIVE_EMAIL not in joined


# ---------------------------------------------------------------------------
# Security F2 — a foreign chat_session_id must not be written into (IDOR).
# ---------------------------------------------------------------------------

def test_foreign_chat_session_id_falls_through_to_own_session(chat_mod):
    """A caller-supplied chat_session_id owned by a DIFFERENT user must not be
    appended into — the write falls through to get_or_create for the caller.

    Without the owner-gate this is an IDOR: user B forges user A's session id
    and injects a message into A's history."""
    # Seed a session owned by user A (id=1).
    session_a = chat_mod.db.create_new_chat_session(
        agent_name="agent-a", user_id=1, user_email="alice@example.com",
    )

    # User B (id=2) submits a task referencing A's session id.
    req = _make_request(
        chat_mod,
        create_new_session=False,
        chat_session_id=session_a.id,
    )
    sid = asyncio.run(chat_mod._persist_chat_session(
        agent_name="agent-a",
        request=req,
        result=_SuccessResult(),
        user_id=2,
        user_email="bob@example.com",
    ))

    assert sid is not None
    assert sid != session_a.id, "must NOT write into another user's session (IDOR)"

    # A's session got no new messages; B's own session did.
    from db.engine import get_engine
    from sqlalchemy import text

    with get_engine().connect() as conn:
        a_msgs = conn.execute(
            text("SELECT COUNT(*) FROM chat_messages WHERE session_id=:s"),
            {"s": session_a.id},
        ).scalar()
        b_user = conn.execute(
            text("SELECT user_id FROM chat_sessions WHERE id=:s"), {"s": sid}
        ).scalar()
    assert a_msgs == 0
    assert b_user == 2
