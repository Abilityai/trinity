"""#3984 / #3849 — two agent failures no credential can fix are not auth failures.

SUB-003 reads an agent failure as a credential fault and walks the turn across
every subscription and onto the platform API key, recording an auth refusal
against each one tried (the pool is skip-listed for ~2h). Two answers the agent
gives fell into that walk although switching credentials cannot change them:

- #3984 — the turn-limit 422. The agent answers ``Task exceeded turn limit:
  <CLI text>``, and the CLI text carries the turn count. At a cap of 401 or 403
  the bare ``"401"`` / ``"403"`` entries of ``AUTH_INDICATORS`` matched, so the
  body read as auth.
- #3849 — the API's "Prompt is too long" rejection. Zero tokens and exit 1, so
  the agent answers 503, and every 503 read as auth.

Both join the #3012 (model rejection) and #3470 (container fault) carve-outs.
The agent-side fixtures run through the real ``_finalize_headless_result`` so
the asserted text is what the agent actually sends.

Error-code assertions compare ``.value`` (see test_3012_model_rejection.py:
the str-Enum members all compare equal).
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
from agent_server.services.stream_parser import process_stream_line

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from services import failure_classifier
from services.execution_classification import classify_switch_failure
from services.failure_classifier import (
    is_auth_failure,
    is_prompt_too_long,
    is_turn_limit,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]


def _parsed(events) -> ExecutionMetadata:
    metadata = ExecutionMetadata()
    for event in events:
        process_stream_line(json.dumps(event), [], metadata, {}, [])
    return metadata


def _ctx(metadata: ExecutionMetadata, *, return_code=1, stderr=""):
    ctx = HeadlessRunContext(
        cmd=["claude", "--print"], task_session_id="task-3984",
        task_start_iso="2026-10-27T10:00:00Z", effective_timeout=900,
        images=None, prompt="hi",
    )
    ctx.return_code = return_code
    ctx.metadata = metadata
    ctx.verbose_output_lines = [stderr] if stderr else []
    return ctx


def _agent_answer(events, *, return_code=1) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        _finalize_headless_result(_ctx(_parsed(events), return_code=return_code))
    return exc.value


def _resp(status: int, detail) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    body = {"detail": detail}
    resp.text = json.dumps(body)
    resp.json.return_value = body
    return resp


# --------------------------------------------------------------------------- #
# Fixtures — the CLI's result events
# --------------------------------------------------------------------------- #


def _max_turns_events(turns: int, *, errors=None):
    """Claude Code stopping at its turn cap. ``errors`` None → the CLI gave no
    text, so the agent falls back to ``Task stopped after N turns``."""
    result = {"type": "result", "subtype": "error_max_turns", "is_error": True,
              "num_turns": turns, "terminal_reason": "max_turns",
              "total_cost_usd": 0.42, "duration_ms": 61000, "result": ""}
    if errors is not None:
        result["errors"] = errors
    return [result]


# The API's own sentence; Trinity does not author it.
_PROMPT_TOO_LONG = "Prompt is too long"
_PROMPT_TOO_LONG_EVENTS = [
    {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
     "terminal_reason": "api_error", "total_cost_usd": 0, "duration_ms": 412,
     "result": _PROMPT_TOO_LONG,
     "usage": {"input_tokens": 0, "output_tokens": 0}},
]
# What an agent image older than #3340 sent: the stream's reason was not read,
# so the stderr line was the detail.
_OLD_IMAGE_PROMPT_503 = (
    "Execution failed with no output (exit code 1): "
    "Claude Code execution failed (exit code 1): Prompt is too long"
)


def _turn_limit_answers():
    """Every shape of the agent's turn-limit 422 at caps 401 and 403."""
    out = []
    for cap in (401, 403):
        for errors in ([f"Reached maximum number of turns ({cap})"], None):
            out.append(pytest.param(cap, errors, id=f"cap{cap}-{'cli' if errors else 'fallback'}"))
    return out


# --------------------------------------------------------------------------- #
# #3984 — the turn-limit 422
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cap,errors", _turn_limit_answers())
def test_the_agents_turn_limit_answer_at_cap_401_or_403_never_switches(cap, errors):
    answer = _agent_answer(_max_turns_events(cap, errors=errors))
    assert answer.status_code == 422
    assert str(cap) in answer.detail  # the number that tripped "401"/"403"

    assert is_turn_limit(answer.detail)
    assert not is_auth_failure(answer.detail)
    assert classify_switch_failure(_resp(422, answer.detail)) is None


def test_the_turn_limit_marker_is_the_agents_own_prefix():
    """Pinned against the producer, the #3012 / #3470 shape: if the agent's
    wording changes, this goes red instead of the carve-out silently missing."""
    src = (_REPO / "docker/base-image/agent_server/services/headless_executor.py").read_text().lower()
    for marker in failure_classifier.TURN_LIMIT_MARKERS:
        assert marker in src, marker


# --------------------------------------------------------------------------- #
# #3849 — the prompt-length rejection
# --------------------------------------------------------------------------- #


def test_the_agent_answers_a_prompt_length_rejection_503_naming_the_cause():
    answer = _agent_answer(_PROMPT_TOO_LONG_EVENTS)
    assert answer.status_code == 503
    assert _PROMPT_TOO_LONG in answer.detail


@pytest.mark.parametrize("detail", [
    pytest.param("current", id="current-image"),
    pytest.param(_OLD_IMAGE_PROMPT_503, id="pre-3340-image"),
])
def test_a_prompt_length_503_never_switches(detail):
    if detail == "current":
        detail = _agent_answer(_PROMPT_TOO_LONG_EVENTS).detail
    assert is_prompt_too_long(detail)
    assert not is_auth_failure(detail)
    assert classify_switch_failure(_resp(503, detail)) is None
    # structured #678 body too
    assert classify_switch_failure(_resp(503, {"message": detail, "metadata": {}})) is None


# --------------------------------------------------------------------------- #
# No regression — a real credential failure still switches
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status,detail,kind", [
    (503, "service unavailable", "auth"),
    (503, "Authentication failure: Not logged in · Please run /login.", "auth"),
    (401, "nope", "auth"),
    (403, "nope", "auth"),
    (500, "OAuth token expired", "auth"),
    (500, "HTTP 401 from upstream", "auth"),
    (429, "rate limited", "rate_limit"),
    (422, "Task exceeded turn limit: Reached maximum number of turns (20). x", None),
])
def test_genuine_failures_keep_their_classification(status, detail, kind):
    assert classify_switch_failure(_resp(status, detail)) == kind


def test_a_turn_limit_mention_inside_an_auth_failure_is_still_auth():
    """The carve-out keys on the agent's prefix, not the words anywhere."""
    msg = "Authentication failure: token expired while the task exceeded turn limit"
    assert is_auth_failure(msg)


# --------------------------------------------------------------------------- #
# Backend — end to end through each path that can switch
# --------------------------------------------------------------------------- #


def _turn_limit_detail(cap: int) -> str:
    return _agent_answer(
        _max_turns_events(cap, errors=[f"Reached maximum number of turns ({cap})"])
    ).detail


@pytest.mark.parametrize("status,detail_fn,code", [
    (422, lambda: _turn_limit_detail(401), "agent_error"),
    (422, lambda: _turn_limit_detail(403), "agent_error"),
    (503, lambda: _agent_answer(_PROMPT_TOO_LONG_EVENTS).detail, "agent_error"),
    (503, lambda: _OLD_IMAGE_PROMPT_503, "agent_error"),
], ids=["turn-cap-401", "turn-cap-403", "prompt-length", "prompt-length-old-image"])
def test_task_path_runs_once_and_never_switches(status, detail_fn, code):
    """Neither the pre-raise walk nor the except handler's switch fires, the
    agent is called once, and the failure is not coded AUTH (which the dispatch
    breaker counts)."""
    from test_792_subscription_retry import _resp as _agent_resp
    from test_792_subscription_retry import _run

    from services.task_execution_service import TaskExecutionStatus

    detail = detail_fn()
    legacy_switch = AsyncMock(return_value={"switched": True, "new_subscription": "sub-b"})
    fallback = AsyncMock(return_value={"switched": True})
    with (
        patch("services.subscription_auto_switch.handle_subscription_failure", legacy_switch),
        patch("services.subscription_auto_switch.fallback_to_api_key", fallback),
    ):
        result, ctx = _run(responses=[_agent_resp(status, {"detail": detail})],
                           switch_result={"switched": True, "new_subscription": "sub-b"})

    assert result.status == TaskExecutionStatus.FAILED
    assert ctx.agent_call_count == 1
    ctx.switch.assert_not_awaited()
    legacy_switch.assert_not_awaited()
    fallback.assert_not_awaited()
    assert result.error_code is None or result.error_code.value == code
    assert result.error_code is None or result.error_code.value != "auth"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,detail_fn", [
    (422, lambda: _turn_limit_detail(401)),
    (503, lambda: _agent_answer(_PROMPT_TOO_LONG_EVENTS).detail),
], ids=["turn-cap-401", "prompt-length"])
async def test_chat_path_never_switches(status, detail_fn):
    from services.chat_execution_service import (
        ERROR_CODE_HEADER,
        ChatDispatchError,
        _apply_sub003_autoswitch,
    )

    detail = detail_fn()
    switch = AsyncMock(return_value={"new_subscription": "sub-b"})
    fake = SimpleNamespace(handle_subscription_failure=switch, is_auth_failure=is_auth_failure)
    with patch.dict("sys.modules", {"services.subscription_auto_switch": fake}):
        with pytest.raises(ChatDispatchError) as exc:
            await _apply_sub003_autoswitch("agent1", detail, status)
    switch.assert_not_awaited()
    assert exc.value.headers[ERROR_CODE_HEADER] == "agent_error"


def test_chat_path_classifies_a_prompt_length_503_as_an_agent_error():
    from services.chat_execution_service import _classify_agent_http_failure

    assert _classify_agent_http_failure(503, _OLD_IMAGE_PROMPT_503).value == "agent_error"
    assert _classify_agent_http_failure(503, "service unavailable").value == "auth"


def test_pull_terminal_does_not_switch():
    """A pull worker reports the agent's 503 as ``auth``; the text overrides it."""
    from services.pull_coordination_service import _switch_failure_kind

    assert _switch_failure_kind("auth", f"[auth] {_OLD_IMAGE_PROMPT_503}") is None
    assert _switch_failure_kind("auth", "token expired") == "auth"


def test_async_callback_does_not_count_a_prompt_length_503_as_auth():
    """The #1083 callback labels the agent's 503 ``auth``, which the dispatch
    breaker counts."""
    from services.execution_envelope import terminal_from_callback_payload

    payload = SimpleNamespace(status="failed", error_code="auth", terminal_reason="auth",
                              error=_OLD_IMAGE_PROMPT_503, response=None, metadata={},
                              execution_log=None, session_id=None, cost=None,
                              context_used=None, context_max=None)
    assert terminal_from_callback_payload(payload, "exec-1").error_code.value == "agent_error"

    payload.error = "token expired"
    assert terminal_from_callback_payload(payload, "exec-1").error_code.value == "auth"


# --------------------------------------------------------------------------- #
# Mirror
# --------------------------------------------------------------------------- #


def test_the_scheduler_mirror_carries_the_new_markers():
    """Byte identity is pinned by test_904's TestBackendSchedulerParity; this
    names the new lists so a stale mirror reads as this issue."""
    mirror = (_REPO / "src/scheduler/failure_classifier.py").read_text()
    assert "TURN_LIMIT_MARKERS" in mirror
    assert "PROMPT_LENGTH_MARKERS" in mirror
