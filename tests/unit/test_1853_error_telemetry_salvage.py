"""Unit tests for #1853 — persist telemetry AND the transcript on a failing
``error_during_execution`` (502) / timeout (504) row.

Before #1853, an execution that ended ``error_during_execution`` was written to
``schedule_executions`` with status + error string only — no ``claude_session_id``,
no ``cost``, no ``execution_log`` — so the whole failure class was undiagnosable
after the fact. The fix mirrors the SUCCESS applier: the agent's structured 502/504
body carries ``metadata`` (cost/context/session_id) + ``execution_log`` (the raw
stream-json transcript), and ``apply_result``'s FAILED branch persists the
sanitized transcript + a #1741 tool_calls summary + the session id.

Coverage:
- Agent helper ``_execution_error_502_detail`` / ``_valid_session_id`` (incl. the
  FI-1 log-forging fallback guard).
- Agent call-site: ``_finalize_headless_result`` raises the structured 502 with an
  UNCHANGED message text (#1938) + transcript + session id.
- Reader-race regression (ENG#2): the new body does NOT trip
  ``_is_reader_race_signature`` (no false #678 auto-retry); ``_extract_agent_error``
  still surfaces ``detail["message"]`` so the resume-not-found self-heal is intact.
- ``_timeout_504_detail`` extension (FI-2).
- Backend FAILED branch: transcript persisted WITH the embedded credential
  redacted, tool_calls summary, validated session id, salvaged cost — and the
  won-gated close/emit path unchanged (#1578/#1804).
- Real-sqlite column-name proof (ENG#11).

Modules under test:
    docker/base-image/agent_server/services/headless_executor.py
    src/backend/services/task_execution_service.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# conftest.py preloads the real agent_server namespace package.
from agent_server.services import headless_executor as he  # noqa: E402
from agent_server.models import ExecutionMetadata  # noqa: E402
from fastapi import HTTPException  # noqa: E402

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Real (well-formed) session UUID and a value-shape credential (Anthropic key)
# that ``sanitize_execution_log`` redacts.
_UUID = "11111111-1111-1111-1111-111111111111"
_SECRET = "sk-ant-0123456789abcdef0123456789abcdef"

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Agent-side fixtures
# --------------------------------------------------------------------------
def _md(**over) -> ExecutionMetadata:
    base = dict(cost_usd=0.42, input_tokens=100, context_window=200000, num_turns=3)
    base.update(over)
    return ExecutionMetadata(**base)


def _ctx(
    metadata: ExecutionMetadata,
    *,
    raw_messages=None,
    claude_session_uuid: str = "",
    return_code: int = 0,
) -> "he.HeadlessRunContext":
    return he.HeadlessRunContext(
        cmd=["claude", "--print"],
        task_session_id="t-1853",
        task_start_iso="2026-01-01T00:00:00Z",
        effective_timeout=1200,
        images=None,
        prompt="hello",
        claude_session_uuid=claude_session_uuid,
        metadata=metadata,
        raw_messages=raw_messages if raw_messages is not None else [],
        return_code=return_code,
    )


# --------------------------------------------------------------------------
# _valid_session_id (FI-1)
# --------------------------------------------------------------------------
class TestValidSessionId:
    def test_accepts_uuid(self):
        assert he._valid_session_id(_UUID) == _UUID

    def test_accepts_uppercase_uuid(self):
        upper = _UUID.upper()
        assert he._valid_session_id(upper) == upper

    def test_rejects_newline_injection(self):
        # A resume_session_id from the /task body could carry a log-forging newline.
        assert he._valid_session_id("not-a-uuid\ninjected") is None

    def test_rejects_non_uuid_token(self):
        assert he._valid_session_id("abc123") is None

    def test_rejects_empty_and_none(self):
        assert he._valid_session_id("") is None
        assert he._valid_session_id(None) is None


# --------------------------------------------------------------------------
# _execution_error_502_detail
# --------------------------------------------------------------------------
class TestExecutionError502Body:
    def test_carries_transcript_and_metadata(self):
        transcript = [{"type": "assistant", "text": "partial"}]
        ctx = _ctx(_md(session_id=_UUID), raw_messages=transcript)
        detail = he._execution_error_502_detail(ctx, "Execution error: boom")

        assert detail["message"] == "Execution error: boom"
        assert detail["execution_log"] is transcript
        md = detail["metadata"]
        assert md["session_id"] == _UUID
        assert md["cost_usd"] == pytest.approx(0.42)
        assert md["context_window"] == 200000

    def test_session_id_falls_back_to_claude_session_uuid(self):
        ctx = _ctx(_md(session_id=None), claude_session_uuid=_UUID)
        detail = he._execution_error_502_detail(ctx, "Execution error: boom")
        assert detail["metadata"]["session_id"] == _UUID

    def test_malicious_fallback_session_id_dropped(self):
        # FI-1: ctx.claude_session_uuid can be an untrusted resume_session_id.
        ctx = _ctx(_md(session_id=None), claude_session_uuid="not-a-uuid\ninjected")
        detail = he._execution_error_502_detail(ctx, "Execution error: boom")
        assert detail["metadata"]["session_id"] is None

    def test_body_does_not_trip_reader_race_signature(self):
        # ENG#2: the structured body must NOT look like a #678 reader-race body
        # (which keys on `recovery_attempted`) — else it triggers a false
        # auto-retry in task_execution_service.
        from services.task_execution_service import _is_reader_race_signature

        ctx = _ctx(_md(session_id=_UUID), raw_messages=[{"type": "x"}])
        detail = he._execution_error_502_detail(ctx, "Execution error: boom")
        assert _is_reader_race_signature(detail) is False


# --------------------------------------------------------------------------
# Agent call-site (ENG#10)
# --------------------------------------------------------------------------
class TestFinalizeErrorBranch:
    def test_raises_structured_502_with_unchanged_message(self, monkeypatch):
        # _try_recover_completed_turn False → the branch raises the 502 (#1870
        # recovery declined). The message text must be byte-identical to the
        # pre-#1853 bare body (#1938).
        monkeypatch.setattr(he, "_try_recover_completed_turn", lambda ctx: False)
        transcript = [{"type": "assistant", "text": "partial"}]
        md = _md(
            error_type="execution_error",
            error_message="boom happened",
            session_id=_UUID,
        )
        ctx = _ctx(md, raw_messages=transcript, return_code=0)

        with pytest.raises(HTTPException) as ei:
            he._finalize_headless_result(ctx)

        exc = ei.value
        assert exc.status_code == 502
        assert isinstance(exc.detail, dict)
        assert exc.detail["message"] == "Execution error: boom happened"
        assert exc.detail["execution_log"] is transcript
        assert exc.detail["metadata"]["session_id"] == _UUID


# --------------------------------------------------------------------------
# _timeout_504_detail extension (FI-2)
# --------------------------------------------------------------------------
class TestTimeout504Extension:
    def test_carries_transcript_and_validated_session_id(self):
        transcript = [{"type": "assistant", "text": "before-timeout"}]
        ctx = _ctx(
            _md(session_id=None), claude_session_uuid=_UUID, raw_messages=transcript
        )
        detail = he._timeout_504_detail(ctx, "timed out", "max_duration")

        assert detail["execution_log"] is transcript
        assert detail["metadata"]["session_id"] == _UUID  # fallback + validated
        assert detail["termination_reason"] == "max_duration"

    def test_malicious_fallback_session_id_dropped(self):
        ctx = _ctx(_md(session_id=None), claude_session_uuid="bad\nid")
        detail = he._timeout_504_detail(ctx, "timed out", "max_duration")
        assert detail["metadata"]["session_id"] is None


# --------------------------------------------------------------------------
# Backend _extract_agent_error (3-tuple + resume-not-found preserved)
# --------------------------------------------------------------------------
def _resp(detail):
    r = MagicMock()
    r.json.return_value = {"detail": detail}
    r.text = json.dumps({"detail": detail})
    return r


class TestExtractAgentError:
    def test_returns_transcript_from_structured_body(self):
        from services.task_execution_service import _extract_agent_error

        transcript = [{"type": "assistant", "text": "x"}]
        body = {
            "message": "Execution error: boom",
            "metadata": {"session_id": _UUID},
            "execution_log": transcript,
        }
        msg, meta, exec_log = _extract_agent_error(_resp(body), "fallback")
        assert msg == "Execution error: boom"
        assert meta == {"session_id": _UUID}
        assert exec_log == transcript

    def test_resume_not_found_message_preserved(self):
        # #1673/#1849: the resume-not-found self-heal reads detail["message"];
        # the structured body carries it unchanged, so is_resume_not_found matches.
        from services.session_turn_service import is_resume_not_found
        from services.task_execution_service import _extract_agent_error

        body = {
            "message": "No conversation found with session ID abc",
            "metadata": {},
            "execution_log": [],
        }
        msg, _meta, _exec_log = _extract_agent_error(_resp(body), "fallback")
        assert msg == "No conversation found with session ID abc"
        assert is_resume_not_found(msg) is True

    def test_bare_string_body_yields_none_transcript(self):
        # Old-image graceful degrade: a bare-string detail carries no transcript.
        from services.task_execution_service import _extract_agent_error

        msg, meta, exec_log = _extract_agent_error(
            _resp("Execution error: boom"), "fallback"
        )
        assert msg == "Execution error: boom"
        assert meta == {}
        assert exec_log is None

    def test_none_response(self):
        from services.task_execution_service import _extract_agent_error

        assert _extract_agent_error(None, "fb") == ("fb", {}, None)


# --------------------------------------------------------------------------
# Backend apply_result FAILED branch salvage
# --------------------------------------------------------------------------
def _run_apply(
    envelope,
    *,
    cas_won=True,
    activity_id="act-1",
    breaker_enabled=False,
    release_slot=False,
):
    """Drive apply_result with mocked db/activity/capacity/breaker (test_1083 harness)."""
    import asyncio

    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.update_execution_status.return_value = cas_won

    mock_activity = MagicMock(complete_activity=AsyncMock())
    mock_capacity = MagicMock(release=AsyncMock())
    mock_record = AsyncMock()

    with (
        patch("services.task_execution_service.db", mock_db),
        patch(
            "services.task_execution_service.get_capacity_manager",
            return_value=mock_capacity,
        ),
        patch("services.task_execution_service.activity_service", mock_activity),
        patch("services.task_execution_service._record_dispatch_terminal", mock_record),
    ):
        svc = TaskExecutionService()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                svc.apply_result(
                    "test-agent",
                    envelope,
                    activity_id=activity_id,
                    breaker_enabled=breaker_enabled,
                    release_slot=release_slot,
                )
            )
        finally:
            loop.close()
    return result, (mock_db, mock_activity, mock_capacity, mock_record)


def _failed_envelope(**over):
    from services.task_execution_service import TerminalEnvelope, TaskExecutionStatus

    base = dict(
        execution_id="exec-1853",
        status=TaskExecutionStatus.FAILED,
        error="agent said no",
        error_code=None,
        metadata={"cost_usd": 0.02, "input_tokens": 50, "context_window": 200000},
    )
    base.update(over)
    return TerminalEnvelope(**base)


class TestFailedBranchSalvage:
    def test_transcript_and_session_persisted_with_redaction(self):
        from models import ActivityState

        transcript = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
                    ]
                },
            },
            {"type": "assistant", "text": "leaked " + _SECRET + " here"},
        ]
        env = _failed_envelope(execution_log=transcript, session_id=_UUID)
        _result, (mdb, mact, _mcap, _mrec) = _run_apply(env)

        kw = mdb.update_execution_status.call_args.kwargs
        # execution_log persisted AND the embedded secret redacted
        # (sanitize_execution_log fires — concern b).
        assert kw["execution_log"] is not None
        assert _SECRET not in kw["execution_log"]
        assert "***REDACTED***" in kw["execution_log"]
        # tool_calls is the #1741 SUMMARY, not a second copy of the transcript.
        assert json.loads(kw["tool_calls"]) == [
            {"type": "tool_use", "tool": "Bash", "input": {"command": "ls"}}
        ]
        assert kw["execution_log"] != kw["tool_calls"]
        # session id (validated agent-side) persisted; cost still salvaged.
        assert kw["claude_session_id"] == _UUID
        assert kw["cost"] == pytest.approx(0.02)
        # Won-gated close is unchanged: the FAILED activity still closes FAILED.
        assert (
            mact.complete_activity.await_args.kwargs["status"] == ActivityState.FAILED
        )

    def test_session_id_falls_back_to_metadata(self):
        env = _failed_envelope(
            execution_log=[{"type": "assistant", "text": "x"}],
            session_id=None,
            metadata={"cost_usd": 0.02, "session_id": _UUID},
        )
        _result, (mdb, *_rest) = _run_apply(env)
        assert (
            mdb.update_execution_status.call_args.kwargs["claude_session_id"] == _UUID
        )

    def test_bare_string_body_graceful_degrade(self):
        # Old image / no structured body → no transcript, no session id, no crash.
        env = _failed_envelope(
            execution_log=None, session_id=None, metadata={"cost_usd": 0.02}
        )
        _result, (mdb, *_rest) = _run_apply(env)
        kw = mdb.update_execution_status.call_args.kwargs
        assert kw["execution_log"] is None
        assert kw["tool_calls"] is None
        assert kw["claude_session_id"] is None
        assert kw["cost"] == pytest.approx(0.02)

    def test_lost_cas_skips_side_effects_even_with_transcript(self):
        # The new columns are added ABOVE the `won` gate; the gate itself is
        # untouched (#1578/#1804) — a lost CAS still fires no side effects.
        env = _failed_envelope(
            execution_log=[{"type": "assistant", "text": "x"}], session_id=_UUID
        )
        _result, (_mdb, mact, _mcap, mrec) = _run_apply(env, cas_won=False)
        from services.task_execution_service import TaskExecutionStatus

        assert _result.status == TaskExecutionStatus.FAILED
        mact.complete_activity.assert_not_awaited()
        mrec.assert_not_awaited()


# --------------------------------------------------------------------------
# Real-sqlite column-name proof (ENG#11)
# --------------------------------------------------------------------------
from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402,F401


@pytest.fixture
def tmp_db(db_backend, monkeypatch):
    for mod in ("db.connection", "db.schedules", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    return db_backend


def test_failed_row_persists_new_columns_real_sqlite(tmp_db):
    from db.schedules import ScheduleOperations
    from models import TaskExecutionStatus

    ops = ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by) "
        "VALUES ('e-1853', '__manual__', 'a', 'running', :sa, 'm', 'schedule')",
        sa=started,
    )

    won = ops.update_execution_status(
        execution_id="e-1853",
        status=TaskExecutionStatus.FAILED,
        error="Execution error: boom",
        cost=0.13,
        execution_log='[{"type":"assistant","text":"redacted"}]',
        tool_calls='[{"type":"tool_use","tool":"Bash","input":null}]',
        claude_session_id=_UUID,
    )
    assert won is True

    assert (
        _hscalar("SELECT status FROM schedule_executions WHERE id='e-1853'") == "failed"
    )
    assert (
        _hscalar("SELECT claude_session_id FROM schedule_executions WHERE id='e-1853'")
        == _UUID
    )
    assert (
        _hscalar("SELECT execution_log FROM schedule_executions WHERE id='e-1853'")
        == '[{"type":"assistant","text":"redacted"}]'
    )
    assert (
        _hscalar("SELECT tool_calls FROM schedule_executions WHERE id='e-1853'")
        == '[{"type":"tool_use","tool":"Bash","input":null}]'
    )
    assert _hscalar(
        "SELECT cost FROM schedule_executions WHERE id='e-1853'"
    ) == pytest.approx(0.13)
