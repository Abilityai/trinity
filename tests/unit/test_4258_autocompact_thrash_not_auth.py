"""#4258 — a run that dies on autocompact thrashing is not an auth failure.

Claude Code ends a run with ``Autocompact is thrashing: the context refilled to
the limit within 3 turns of the previous compact, 3 times in a row`` when every
compact refills the context. The agent answers 503, and every 503 read as auth,
so SUB-003 walked the run across every subscription and the row was stored as
``[auth] …``. No credential changes the context window, so the sentence joins
the #3849 prompt-length carve-out (``PROMPT_LENGTH_MARKERS``) and every path
that already honours it: switch classification, chat, pull and the #1083
callback.

Shapes and helpers follow test_3984_3849_not_a_credential_failure.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from agent_server.models import ExecutionMetadata
from agent_server.services.headless_executor import (
    HeadlessRunContext,
    _finalize_headless_result,
)
from agent_server.services.stream_parser import process_stream_line

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.execution_classification import classify_switch_failure
from services.failure_classifier import is_auth_failure, is_prompt_too_long

pytestmark = pytest.mark.unit

# The CLI's own sentence, as quoted in #4258; Trinity does not author it.
_THRASHING = (
    "Autocompact is thrashing: the context refilled to the limit within 3 "
    "turns of the previous compact, 3 times in a row"
)
_THRASHING_EVENTS = [
    {"type": "result", "subtype": "success", "is_error": True, "num_turns": 140,
     "terminal_reason": "api_error", "total_cost_usd": 3.1, "duration_ms": 2038000,
     "result": _THRASHING,
     "usage": {"input_tokens": 1200, "output_tokens": 900}},
]
_STDERR_503 = (
    "Execution failed with no output (exit code 1): "
    f"Claude Code execution failed (exit code 1): {_THRASHING}"
)


def _resp(status: int, detail) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    body = {"detail": detail}
    resp.text = json.dumps(body)
    resp.json.return_value = body
    return resp


def _agent_answer(events) -> HTTPException:
    metadata = ExecutionMetadata()
    for event in events:
        process_stream_line(json.dumps(event), [], metadata, {}, [])
    ctx = HeadlessRunContext(
        cmd=["claude", "--print"], task_session_id="task-4258",
        task_start_iso="2026-11-04T10:00:00Z", effective_timeout=900,
        images=None, prompt="hi",
    )
    ctx.return_code = 1
    ctx.metadata = metadata
    ctx.verbose_output_lines = []
    with pytest.raises(HTTPException) as exc:
        _finalize_headless_result(ctx)
    return exc.value


@pytest.mark.parametrize("detail", [
    pytest.param("current", id="agent-answer"),
    pytest.param(_STDERR_503, id="stderr-line"),
    pytest.param(f"[auth] {_STDERR_503}", id="pull-row"),
])
def test_an_autocompact_thrash_never_switches(detail):
    if detail == "current":
        detail = _agent_answer(_THRASHING_EVENTS).detail
        assert _THRASHING in detail
    assert is_prompt_too_long(detail)
    assert not is_auth_failure(detail)
    assert classify_switch_failure(_resp(503, detail)) is None
    assert classify_switch_failure(_resp(503, {"message": detail, "metadata": {}})) is None


def test_an_auth_answer_quoting_the_sentence_is_still_auth():
    """#4123: the agent's own auth prefix wins over a context-limit sentence."""
    detail = f"Authentication failure: Not logged in. stderr: {_THRASHING}"
    assert not is_prompt_too_long(detail)
    assert classify_switch_failure(_resp(503, detail)) == "auth"


def test_chat_path_classifies_it_as_an_agent_error():
    from services.chat_execution_service import _classify_agent_http_failure

    assert _classify_agent_http_failure(503, _STDERR_503).value == "agent_error"


def test_pull_terminal_does_not_switch_or_count_it_as_auth():
    from services import pull_coordination_service as pcs

    assert pcs._switch_failure_kind("auth", f"[auth] {_STDERR_503}") is None

    mock_db = MagicMock()
    mock_db.get_execution.return_value = SimpleNamespace(
        id="exec-4258", agent_name="worker-a", status="running", triggered_by="schedule",
        duration_ms=None, cost=None, fan_out_id=None, loop_id=None,
        claude_session_id="dispatched_async",
    )
    mock_db.update_execution_status.return_value = True
    with (
        patch.object(pcs, "db", mock_db),
        patch.object(pcs, "event_dispatch_service", MagicMock()),
        patch.object(pcs, "activity_service", MagicMock()),
        patch.object(pcs, "subscription_auto_switch", MagicMock()),
        patch.object(pcs, "_spawn_breaker_verdict") as verdict,
    ):
        outcome = pcs.apply_task_result(
            "exec-4258", "tok", status="failed", content=_STDERR_503, error_code="auth",
        )

    assert outcome.kind == "applied"
    assert verdict.call_args.args[2] is False
    stored = mock_db.update_execution_status.call_args.kwargs["result"].error
    assert stored.startswith("[agent_error] ")


def test_async_callback_does_not_count_it_as_auth():
    from services.execution_envelope import terminal_from_callback_payload

    payload = SimpleNamespace(status="failed", error_code="auth", terminal_reason="auth",
                              error=_STDERR_503, response=None, metadata={},
                              execution_log=None, session_id=None, cost=None,
                              context_used=None, context_max=None)
    assert terminal_from_callback_payload(payload, "exec-1").error_code.value == "agent_error"
