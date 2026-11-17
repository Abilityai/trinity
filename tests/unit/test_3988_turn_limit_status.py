"""#3988 — a turn-limit failure tells a sync /task caller it hit the turn limit.

The agent answers a turn that stopped at its cap with ``422 Task exceeded turn
limit: …``. The push path left the result's ``error_code`` None, and
``_map_task_failure`` sent every uncoded failure to **503 "The agent may be
unavailable"** with no ``X-Trinity-Error-Code`` — a wait-and-retry answer to a
failure every retry repeats. The pull path lost it too: the worker reports
``max_turns`` and the sink stores ``[max_turns] text``, but ``_split_error_code``
only strips a prefix that names a ``TaskExecutionErrorCode``, and ``max_turns``
was not one (MESSAGE_ENVELOPE_SCHEMA.md §4 already listed it as a
contract-required addition).

Error-code assertions compare ``.value``: the str-Enum members all compare
equal under ``==`` (learnings #1085).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from agent_server.models import ExecutionMetadata
from agent_server.services.headless_executor import (
    HeadlessRunContext,
    _finalize_headless_result,
)
from agent_server.services.pull_worker import _failed_result_body_from_http
from agent_server.services.result_callback import _envelope_from_http_exception
from agent_server.services.stream_parser import process_stream_line

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.execution_envelope import TaskExecutionErrorCode

pytestmark = pytest.mark.unit


def _turn_limit_detail(
    cap: int = 2, cli_text: str | None = None, source: str = "guardrail"
) -> str:
    """The agent's own 422 detail, produced by the real executor.

    ``source`` is the cap that stopped the turn: the agent words the remedy
    differently when the caller's per-task ``max_turns`` bound (#2328).
    """
    errors = [cli_text or f"Reached maximum number of turns ({cap})"]
    metadata = ExecutionMetadata()
    process_stream_line(json.dumps({
        "type": "result", "subtype": "error_max_turns", "is_error": True,
        "num_turns": cap, "terminal_reason": "max_turns", "errors": errors,
        "total_cost_usd": 0.1, "duration_ms": 9000, "result": "",
    }), [], metadata, {}, [])
    ctx = HeadlessRunContext(
        cmd=["claude", "--print"], task_session_id="task-3988",
        task_start_iso="2026-10-27T11:00:00Z", effective_timeout=900,
        images=None, prompt="hi", max_turns_source=source,
    )
    ctx.return_code = 1
    ctx.metadata = metadata
    ctx.verbose_output_lines = []
    with pytest.raises(HTTPException) as exc:
        _finalize_headless_result(ctx)
    assert exc.value.status_code == 422
    return exc.value.detail


def _result(error: str, code):
    return SimpleNamespace(status="failed", error=error, error_code=code)


# --------------------------------------------------------------------------- #
# The code exists and the pull sink's prefix resolves to it
# --------------------------------------------------------------------------- #


def test_max_turns_is_a_task_execution_error_code():
    assert TaskExecutionErrorCode("max_turns").value == "max_turns"


def test_the_pull_sinks_max_turns_prefix_round_trips():
    """The pull worker's real failed body → the sink's ``[code] text`` row →
    ``_split_error_code`` gives back the code and the bare text."""
    from services.task_execution_service import _split_error_code

    detail = _turn_limit_detail()
    body = _failed_result_body_from_http("tok", HTTPException(422, detail=detail))
    assert body["error_code"] == "max_turns"

    code, text = _split_error_code(f"[{body['error_code']}] {detail}")
    assert code is not None and code.value == "max_turns"
    assert text == detail


def test_the_callback_maps_a_max_turns_terminal_to_the_code():
    """#1083: the callback labels the agent's 422 by terminal_reason only."""
    from services.execution_envelope import terminal_from_callback_payload

    env = _envelope_from_http_exception(HTTPException(422, detail=_turn_limit_detail()))
    assert env["error_code"] is None and env["terminal_reason"] == "max_turns"
    payload = SimpleNamespace(status="failed", error_code=env["error_code"],
                              terminal_reason=env["terminal_reason"], error=env.get("error"),
                              response=None, metadata={}, execution_log=None, session_id=None,
                              cost=None, context_used=None, context_max=None)
    assert terminal_from_callback_payload(payload, "exec-1").error_code.value == "max_turns"

    payload.terminal_reason = "error"
    assert terminal_from_callback_payload(payload, "exec-1").error_code is None


# --------------------------------------------------------------------------- #
# Push path — execute_task codes the agent's 422
# --------------------------------------------------------------------------- #


def _run_task(status: int, detail):
    from test_792_subscription_retry import _resp, _run

    with patch("services.subscription_auto_switch.handle_subscription_failure", AsyncMock()) as legacy:
        result, ctx = _run(responses=[_resp(status, {"detail": detail})],
                           switch_result={"switched": True, "new_subscription": "sub-b"})
    return result, ctx, legacy


_SOURCES = pytest.mark.parametrize("source", ["guardrail", "per_task"])


def _assert_wording(detail: str, source: str) -> None:
    """The parametrize really produced the wording it names."""
    assert ("The caller's per-task max_turns" in detail) is (source == "per_task")


@_SOURCES
@pytest.mark.parametrize("cap", [2, 401])
def test_execute_task_codes_the_agents_turn_limit(cap, source):
    detail = _turn_limit_detail(cap, source=source)
    _assert_wording(detail, source)
    result, ctx, legacy = _run_task(422, detail)
    assert result.error_code is not None and result.error_code.value == "max_turns"
    assert ctx.agent_call_count == 1
    ctx.switch.assert_not_awaited()
    legacy.assert_not_awaited()


def test_an_agent_422_that_is_not_a_turn_limit_stays_uncoded():
    """A FastAPI validation 422 from the agent server is not a turn limit."""
    validation = [{"type": "missing", "loc": ["body", "message"], "msg": "Field required"}]
    result, _, _ = _run_task(422, validation)
    assert result.error_code is None


# --------------------------------------------------------------------------- #
# _map_task_failure — what the caller receives
# --------------------------------------------------------------------------- #


def _mapped(result):
    from services.chat_execution_service import ChatDispatchError, _map_task_failure

    with patch("services.chat_execution_service.idempotency_service") as idem_svc:
        with pytest.raises(ChatDispatchError) as exc:
            _map_task_failure("agent1", result, idem=MagicMock())
    # #3993: a turn limit completes the claim with its 422 (a replay must not
    # run it again); every other failure releases it so a retry can run.
    turn_limit = getattr(result.error_code, "value", None) == "max_turns"
    (idem_svc.complete if turn_limit else idem_svc.fail).assert_called_once()
    (idem_svc.fail if turn_limit else idem_svc.complete).assert_not_called()
    return exc.value


@_SOURCES
def test_a_turn_limit_answers_422_with_the_code(source):
    from services.chat_execution_service import ERROR_CODE_HEADER

    detail = _turn_limit_detail(source=source)
    _assert_wording(detail, source)
    err = _mapped(_result(detail, TaskExecutionErrorCode.MAX_TURNS))
    assert err.status_code == 422
    assert err.headers[ERROR_CODE_HEADER] == "max_turns"
    assert err.detail == detail


@pytest.mark.parametrize("cli_text", [
    "Reached maximum number of turns (2) after the tool call timed out",
    "Reached maximum number of turns (2); agent at capacity",
])
def test_the_code_beats_prose_that_mentions_a_timeout_or_capacity(cli_text):
    """The CLI's text is interpolated raw. Before the code was checked first, a
    "timed out" would answer 504 — which the MCP client reads as its own wait
    running out — and "at capacity" would answer 429."""
    err = _mapped(_result(_turn_limit_detail(cli_text=cli_text), TaskExecutionErrorCode.MAX_TURNS))
    assert err.status_code == 422


@pytest.mark.parametrize("error,code,status,header", [
    ("Execution timed out after 900s", TaskExecutionErrorCode.TIMEOUT, 504, "timeout"),
    ("Agent 'agent1' is at capacity", None, 429, "capacity"),
    ("Model unsupported: x", TaskExecutionErrorCode.MODEL_UNSUPPORTED, 400, "model_unsupported"),
    ("Authentication failure: x", TaskExecutionErrorCode.AUTH, 503, "auth"),
    ("Task execution failed (exit code 1): x", None, 503, None),
])
def test_other_failures_keep_their_answer(error, code, status, header):
    from services.chat_execution_service import ERROR_CODE_HEADER

    err = _mapped(_result(error, code))
    assert err.status_code == status
    assert (err.headers or {}).get(ERROR_CODE_HEADER) == header


def test_a_queued_turn_rebuilt_from_a_pull_row_answers_422():
    """The backlog-reconstruct path builds its result from the row; a pull row
    carries ``[max_turns]``, so the caller gets the same answer."""
    from services.task_execution_service import _split_error_code

    detail = _turn_limit_detail()
    code, text = _split_error_code(f"[max_turns] {detail}")
    assert _mapped(_result(text, code)).status_code == 422
