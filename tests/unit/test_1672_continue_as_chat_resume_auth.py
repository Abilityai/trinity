"""Unit tests for #1672 — "Continue as Chat" (EXEC-023) resume authorization.

The Execution Detail "Continue as Chat" button reopens a finished execution as an
interactive chat by sending its ``claude_session_id`` as ``resume_session_id`` to
``POST /api/agents/{name}/task``, which becomes ``claude --resume <id>`` (or the
Codex equivalent) inside the container — replaying that conversation.

Two defects guarded here:

  (A) IDOR. Execution rows are AGENT-scoped (``accessible_agent_names``), but a
      ``claude_session_id`` is a per-user secret — ``routers/sessions.py`` gates the
      Session tab on ``session.user_id`` and 404s to avoid leaking its existence. The
      ``/task`` resume path had NO ownership check, so on a *shared* agent one
      operator could read a peer's session id from the executions list and resume
      the peer's private conversation. The endpoint now authorizes via
      ``db.resume_session_belongs_to_user`` and 404s (enumeration-safe) on a foreign
      or unknown id.

  (B) Dispatch sentinels. #1083 writes ``'dispatched'`` / ``'dispatched_async'`` into
      ``claude_session_id`` before the real id lands, and permanently on a
      reaper-FAILED async row. Such a row IS owned by its triggerer (so it would pass
      the ownership gate), but resuming it runs ``--resume dispatched_async`` which
      cannot resolve. The endpoint rejects both sentinels up front with 400.

Test strategy:
  * ``TestResumeSessionBelongsToUser`` exercises the REAL DB accessor SQL against a
    throwaway SQLite pointed at by ``TRINITY_DB_PATH`` — not a mock. It proves the
    ownership predicate (agent + session id + owner) and that the accessor itself is
    sentinel-agnostic (the *caller* rejects sentinels).
  * ``TestTaskEndpointResumeGate`` calls the real ``execute_parallel_task`` coroutine
    with patched module globals, asserting: sentinel → 400, foreign/unknown → 404,
    and that an owned id / admin caller / agent-to-agent call pass the gate (proven
    by reaching a unique marker patched immediately downstream).
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _await(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# (A) Real DB accessor — resume_session_belongs_to_user
# ---------------------------------------------------------------------------

class TestResumeSessionBelongsToUser:
    """Exercise the actual accessor SQL against a throwaway SQLite engine."""

    @pytest.fixture()
    def ops(self, tmp_path, monkeypatch):
        # Point the engine at a throwaway DB and reset the per-URL engine cache so
        # this suite never touches a real /data/trinity.db.
        db_path = str(tmp_path / "resume_auth.db")
        monkeypatch.setenv("TRINITY_DB_PATH", db_path)
        monkeypatch.delenv("DATABASE_URL", raising=False)

        import db.engine as engine_mod
        engine_mod._engines.clear()
        engine = engine_mod.get_engine()

        from db.tables import metadata, schedule_executions
        # Create just the table under test.
        schedule_executions.create(bind=engine, checkfirst=True)

        from db.schedules import ScheduleOperations
        # The accessor uses only get_engine() + the table; user/agent ops are unused.
        ops = ScheduleOperations(user_ops=None, agent_ops=None)
        yield ops, engine, schedule_executions
        engine_mod._engines.clear()

    @staticmethod
    def _insert(engine, table, *, exec_id, agent, session_id, user_id):
        from sqlalchemy import insert
        with engine.begin() as conn:
            conn.execute(
                insert(table).values(
                    id=exec_id,
                    schedule_id="sched-1",
                    agent_name=agent,
                    status="success",
                    started_at="2026-07-17T00:00:00.000Z",
                    message="task",
                    triggered_by="manual",
                    claude_session_id=session_id,
                    source_user_id=user_id,
                )
            )

    def test_owned_session_returns_true(self, ops):
        ops_obj, engine, table = ops
        sid = str(uuid.uuid4())
        self._insert(engine, table, exec_id="e1", agent="agent-a", session_id=sid, user_id=7)
        assert ops_obj.resume_session_belongs_to_user("agent-a", sid, 7) is True

    def test_foreign_user_returns_false(self, ops):
        """The IDOR case: same agent + real session id, but a DIFFERENT owner."""
        ops_obj, engine, table = ops
        sid = str(uuid.uuid4())
        self._insert(engine, table, exec_id="e1", agent="agent-a", session_id=sid, user_id=7)
        # User 99 shares agent-a and can see the row, but does not own the session.
        assert ops_obj.resume_session_belongs_to_user("agent-a", sid, 99) is False

    def test_wrong_agent_returns_false(self, ops):
        """A session id must belong to a row on THIS agent, not just any agent."""
        ops_obj, engine, table = ops
        sid = str(uuid.uuid4())
        self._insert(engine, table, exec_id="e1", agent="agent-a", session_id=sid, user_id=7)
        assert ops_obj.resume_session_belongs_to_user("agent-b", sid, 7) is False

    def test_unknown_session_returns_false(self, ops):
        ops_obj, _engine, _table = ops
        assert ops_obj.resume_session_belongs_to_user("agent-a", str(uuid.uuid4()), 7) is False

    def test_accessor_is_sentinel_agnostic(self, ops):
        """A sentinel-valued row owned by the user matches — the accessor does NOT
        special-case sentinels; the endpoint rejects them before this call. This
        pins WHY the endpoint's sentinel check is load-bearing and not redundant."""
        ops_obj, engine, table = ops
        self._insert(
            engine, table, exec_id="e1", agent="agent-a",
            session_id="dispatched_async", user_id=7,
        )
        assert ops_obj.resume_session_belongs_to_user("agent-a", "dispatched_async", 7) is True


# ---------------------------------------------------------------------------
# (B) Endpoint gate — execute_parallel_task resume authorization
# ---------------------------------------------------------------------------

class _GatePassed(Exception):
    """Unique marker raised immediately downstream of the resume gate, so a test can
    assert the gate was PASSED (reached downstream code) vs. raised its own 400/404."""


class TestTaskEndpointResumeGate:
    @pytest.fixture()
    def call_endpoint(self):
        """Return a caller that invokes the real endpoint coroutine with a running
        container and the resume-gate's downstream patched to raise _GatePassed."""
        import routers.chat as chat_mod
        import services.dispatch_admission_service as dispatch_admission_service
        from models import ParallelTaskRequest

        def _call(*, resume_id, role="user", owns=False, x_source_agent=None,
                  user_agent_name="__from_source__"):
            # user_agent_name decouples the PRINCIPAL (agent-scoped key ⇒ set) from the
            # spoofable X-Source-Agent HEADER. Default: an agent-scoped self-call
            # (agent_name == x_source_agent). Pass user_agent_name=None to model a
            # REGULAR user who merely spoofs the header — the critical bypass case.
            req = ParallelTaskRequest(message="hi", resume_session_id=resume_id, async_mode=True)
            user = MagicMock()
            user.id = 7
            user.role = role
            user.agent_name = (
                x_source_agent if user_agent_name == "__from_source__" else user_agent_name
            )

            running = MagicMock()
            running.status = "running"

            # The resume gate + #1068 timeout normalization stay in the router
            # (execute_parallel_task); the dispatch orchestration moved to
            # chat_execution_service (#1483), whose first unconditional call is
            # dispatch_admission_service.begin_task_idempotency ->
            # idempotency_service.begin. Raising there marks "resume gate passed".
            with patch.object(chat_mod, "get_agent_container", return_value=running), \
                 patch.object(chat_mod.db, "resume_session_belongs_to_user", return_value=owns), \
                 patch.object(chat_mod.db, "get_execution_timeout", return_value=3600), \
                 patch.object(dispatch_admission_service.idempotency_service, "begin", side_effect=_GatePassed):
                return _await(
                    chat_mod.execute_parallel_task(
                        request=req,
                        name="agent-a",
                        current_user=user,
                        x_source_agent=x_source_agent,
                        x_via_mcp=None,
                        idempotency_key=None,
                    )
                )

        return _call

    def test_sentinel_dispatched_async_rejected_400(self, call_endpoint):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            call_endpoint(resume_id="dispatched_async", owns=True)
        assert ei.value.status_code == 400

    def test_sentinel_dispatched_rejected_400(self, call_endpoint):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            call_endpoint(resume_id="dispatched", owns=True)
        assert ei.value.status_code == 400

    def test_foreign_session_rejected_404(self, call_endpoint):
        """IDOR guard: a real-looking id the caller does NOT own → 404 (not 403), so
        session-id existence is never confirmed to an unauthorized caller."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            call_endpoint(resume_id=str(uuid.uuid4()), owns=False)
        assert ei.value.status_code == 404

    def test_owned_session_passes_gate(self, call_endpoint):
        """An owned id clears the gate and proceeds downstream (marker)."""
        with pytest.raises(_GatePassed):
            call_endpoint(resume_id=str(uuid.uuid4()), owns=True)

    def test_admin_bypasses_ownership(self, call_endpoint):
        """Admin passes the gate WITHOUT an owning row (owns=False) — but a sentinel
        still 400s even for admin (asserted separately)."""
        with pytest.raises(_GatePassed):
            call_endpoint(resume_id=str(uuid.uuid4()), role="admin", owns=False)

    def test_admin_still_rejected_on_sentinel(self, call_endpoint):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            call_endpoint(resume_id="dispatched_async", role="admin", owns=True)
        assert ei.value.status_code == 400

    def test_spoofed_x_source_agent_does_not_bypass_gate(self, call_endpoint):
        """CRITICAL (adversarial-review finding): a REGULAR user has agent_name=None,
        so the SELF-EXEC-001 spoof-guard never fires for them. If the gate were keyed
        on `not x_source_agent`, that user could set X-Source-Agent to ANY value and
        skip authorization entirely — resuming a peer's session on a shared agent. The
        gate ignores the header, so a foreign unowned id still 404s."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            call_endpoint(
                resume_id=str(uuid.uuid4()), owns=False,
                x_source_agent="agent-a", user_agent_name=None,  # regular user + spoof
            )
        assert ei.value.status_code == 404

    def test_spoofed_x_source_agent_sentinel_still_400(self, call_endpoint):
        """The sentinel reject also survives a spoofed X-Source-Agent header."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            call_endpoint(
                resume_id="dispatched_async", owns=True,
                x_source_agent="agent-a", user_agent_name=None,
            )
        assert ei.value.status_code == 400

    def test_agent_key_owned_resume_passes(self, call_endpoint):
        """A legit agent-scoped self-call (agent_name == x_source_agent, SELF-EXEC-001
        satisfied) resuming an OWNED session clears the gate — the agent principal is
        checked against its owner's id like any other."""
        with pytest.raises(_GatePassed):
            call_endpoint(resume_id=str(uuid.uuid4()), owns=True, x_source_agent="agent-a")

    def test_agent_key_foreign_resume_gated_404(self, call_endpoint):
        """Even a legit agent-scoped key cannot resume a session it does not own —
        no blanket agent exemption remains."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            call_endpoint(resume_id=str(uuid.uuid4()), owns=False, x_source_agent="agent-a")
        assert ei.value.status_code == 404
