"""#3012 — a model Claude Code refuses is not an auth failure.

Setting the platform default to ``claude-opus-5-5`` on an image carrying Claude
Code 2.1.278 failed every run with exit 1, zero tokens and
``[claude-code:unrecognized_model]`` on stderr. The agent server's zero-token
heuristic answered 503, the backend read 503 as auth, and SUB-003 rotated the
agent through every subscription and onto the platform API key while the
Workspace told people to "send that again".

Fixtures below are the CLI's real stream-json events, captured from
``claude -p --model <id> --output-format stream-json --verbose`` (ids trimmed).

Every error-code assertion compares ``.value``: ``TaskExecutionErrorCode`` is a
``@dataclass`` str-Enum whose members all compare equal (#1085), so
``== TaskExecutionErrorCode.MODEL_UNSUPPORTED`` would pass for AUTH too.
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
from agent_server.services import error_classifier as agent_classifier
from agent_server.services.headless_executor import (
    HeadlessRunContext,
    _finalize_headless_result,
)
from agent_server.services.pull_worker import _failed_result_body_from_http
from agent_server.services.result_callback import _envelope_from_http_exception
from agent_server.services.stream_parser import process_stream_line

from services import failure_classifier

# Sibling imports (the unit dir is not implicitly importable) — the #792 execute_task
# harness and the ent#403 portal-turn harness, reused so neither mock stack forks.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from services.execution_envelope import TaskExecutionErrorCode
from services.failure_classifier import is_auth_failure, is_model_rejection
from test_ent403_workspace_model import (  # noqa: F401 — `svc` is a fixture
    AGENT,
    EMAIL,
    OPUS,
    SESSION,
    _failed_turn,
    svc,
)
from test_ent403_workspace_model import _run as _portal_run

pytestmark = pytest.mark.unit

# Claude Code 2.1.278, --model claude-opus-5-5
_TOO_OLD_TEXT = (
    "API Error: 400 Claude Code 2.1.278 does not support this model; version "
    "2.1.280 or newer is required. Run 'claude update', or update the Claude "
    "desktop app, then try again."
)
_TOO_OLD_EVENTS = [
    {"type": "assistant", "error": "invalid_request", "is_api_error_message": True,
     "api_error": "claude_code_version_too_old",
     "api_error_code": "claude_code_version_too_old",
     "message": {"model": "<synthetic>", "role": "assistant",
                 "content": [{"type": "text", "text": _TOO_OLD_TEXT}],
                 "usage": {"input_tokens": 0, "output_tokens": 0}}},
    {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
     "terminal_reason": "api_error", "api_error_status": 400,
     "api_error_code": "claude_code_version_too_old", "total_cost_usd": 0,
     "duration_ms": 734, "result": _TOO_OLD_TEXT},
]
_TOO_OLD_STDERR = '[claude-code:unrecognized_model] {"model":"claude-opus-5-5","query_source":"sdk"}'

# Claude Code 2.1.281, --model claude-nonexistent-9
_UNKNOWN_TEXT = (
    "There's an issue with the selected model (claude-nonexistent-9). It may not "
    "exist or you may not have access to it. Run --model to pick a different model."
)
_UNKNOWN_EVENTS = [
    {"type": "assistant", "error": "model_not_found", "is_api_error_message": True,
     "message": {"model": "<synthetic>", "role": "assistant",
                 "content": [{"type": "text", "text": _UNKNOWN_TEXT}],
                 "usage": {"input_tokens": 0, "output_tokens": 0}}},
    {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
     "terminal_reason": "api_error", "api_error_status": 404, "total_cost_usd": 0,
     "result": _UNKNOWN_TEXT},
]

# What an agent image older than #3012 sends the backend for the same run.
_OLD_IMAGE_503 = (
    "Execution failed with no output (exit code 1): " + _TOO_OLD_STDERR
)


def _parsed(events) -> ExecutionMetadata:
    metadata = ExecutionMetadata()
    for event in events:
        process_stream_line(json.dumps(event), [], metadata, {}, [])
    return metadata


def _ctx(metadata: ExecutionMetadata, *, return_code=1, stderr=_TOO_OLD_STDERR):
    ctx = HeadlessRunContext(
        cmd=["claude", "--print"], task_session_id="task-3012",
        task_start_iso="2026-09-24T16:12:00Z", effective_timeout=900,
        images=None, prompt="hi",
    )
    ctx.return_code = return_code
    ctx.metadata = metadata
    ctx.verbose_output_lines = [stderr] if stderr else []
    return ctx


# --------------------------------------------------------------------------- #
# Agent server
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("events,text", [
    (_TOO_OLD_EVENTS, _TOO_OLD_TEXT),
    (_UNKNOWN_EVENTS, _UNKNOWN_TEXT),
])
def test_finalize_answers_400_with_the_runtime_sentence(events, text):
    with pytest.raises(HTTPException) as exc:
        _finalize_headless_result(_ctx(_parsed(events)))
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "model_unsupported"
    assert exc.value.detail["message"] == f"Model unsupported: {text}"


def test_finalize_catches_the_shape_on_a_clean_exit_too():
    """Keyed on the stream parser's record, not the exit code."""
    with pytest.raises(HTTPException) as exc:
        _finalize_headless_result(_ctx(_parsed(_UNKNOWN_EVENTS), return_code=0, stderr=""))
    assert exc.value.status_code == 400


@pytest.mark.parametrize("spent", [
    {"cost_usd": 0.42}, {"input_tokens": 1200}, {"output_tokens": 30},
])
def test_a_turn_that_did_work_is_never_a_model_rejection(spent):
    """A subagent's refused model, or a marker logged before a late kill, must not
    turn a turn that reached the API into `model_unsupported`."""
    metadata = _parsed(_UNKNOWN_EVENTS)
    for field, value in spent.items():
        setattr(metadata, field, value)
    assert agent_classifier._model_rejection_message(metadata, _TOO_OLD_STDERR) is None


def test_finalize_falls_back_to_the_stderr_marker():
    """No assistant text reached the parser: the stderr marker still wins over
    the zero-token 503."""
    with pytest.raises(HTTPException) as exc:
        _finalize_headless_result(_ctx(ExecutionMetadata()))
    assert exc.value.status_code == 400
    assert "[claude-code:unrecognized_model]" in exc.value.detail["message"]


def test_finalize_zero_token_exit_without_the_marker_is_still_503():
    """Negative control: the #285 heuristic is untouched for everything else."""
    with pytest.raises(HTTPException) as exc:
        _finalize_headless_result(_ctx(ExecutionMetadata(), stderr="something else"))
    assert exc.value.status_code == 503


def test_async_callback_and_pull_bodies_carry_the_code():
    exc = HTTPException(400, detail=agent_classifier._model_rejection_detail(_TOO_OLD_TEXT))
    assert _envelope_from_http_exception(exc)["error_code"] == "model_unsupported"
    assert _failed_result_body_from_http("tok", exc)["error_code"] == "model_unsupported"


# --------------------------------------------------------------------------- #
# Backend classifier — the fallback for agents still on an older image
# --------------------------------------------------------------------------- #


def test_backend_markers_match_what_the_agent_and_cli_emit():
    """Contract: every backend marker is a literal the producer actually emits."""
    emitted = " ".join([
        agent_classifier._model_rejection_detail("x")["message"],
        _TOO_OLD_STDERR,
        _TOO_OLD_TEXT,
    ]).lower()
    for marker in failure_classifier.MODEL_REJECTION_MARKERS:
        assert marker in emitted, marker
    assert agent_classifier._UNRECOGNIZED_MODEL_MARKER in failure_classifier.MODEL_REJECTION_MARKERS
    assert agent_classifier._VERSION_TOO_OLD_TEXT in failure_classifier.MODEL_REJECTION_MARKERS


@pytest.mark.parametrize("msg", [
    _OLD_IMAGE_503,
    f"Model unsupported: {_TOO_OLD_TEXT}",
    f"Model unsupported: {_UNKNOWN_TEXT}",
])
def test_model_rejection_is_never_auth(msg):
    assert is_model_rejection(msg)
    # Even with an auth word riding along, the model rejection wins.
    assert not is_auth_failure(msg + " unauthorized")


@pytest.mark.parametrize("msg", [
    "Authentication failed: invalid model access token",
    "Credit balance is too low",
    "Model access error: not available on your subscription",
])
def test_real_auth_text_still_classifies_as_auth(msg):
    assert not is_model_rejection(msg)


def test_classify_switch_failure_ignores_an_old_image_503():
    """AC4: a 503 carrying `unrecognized_model` classifies as non-auth."""
    import httpx

    from services.execution_classification import classify_switch_failure

    old = httpx.Response(503, json={"detail": _OLD_IMAGE_503})
    new = httpx.Response(400, json={"detail": {"message": f"Model unsupported: {_TOO_OLD_TEXT}",
                                               "error_code": "model_unsupported"}})
    plain_503 = httpx.Response(503, json={"detail": "service unavailable"})
    assert classify_switch_failure(old) is None
    assert classify_switch_failure(new) is None
    assert classify_switch_failure(plain_503) == "auth"


def test_sync_task_answers_400_for_a_refused_model():
    """Same status as /chat, so no caller reads it as 'agent unavailable, retry'."""
    from services.chat_execution_service import ChatDispatchError, _map_task_failure

    result = SimpleNamespace(status="failed", error=f"Model unsupported: {_TOO_OLD_TEXT}",
                             error_code=TaskExecutionErrorCode.MODEL_UNSUPPORTED)
    with patch("services.chat_execution_service.idempotency_service"):
        with pytest.raises(ChatDispatchError) as exc:
            _map_task_failure("agent1", result, idem=None)
    assert exc.value.status_code == 400


# --------------------------------------------------------------------------- #
# Backend — end to end through execute_task (AC2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status,detail", [
    (503, _OLD_IMAGE_503),
    (400, {"message": f"Model unsupported: {_TOO_OLD_TEXT}", "error_code": "model_unsupported"}),
])
def test_task_path_never_switches_or_falls_back(status, detail):
    from test_792_subscription_retry import _resp, _run

    from services.task_execution_service import TaskExecutionStatus

    fallback = AsyncMock(return_value={"switched": True})
    with patch("services.subscription_auto_switch.fallback_to_api_key", fallback):
        result, ctx = _run(responses=[_resp(status, {"detail": detail})],
                           switch_result={"switched": True, "new_subscription": "sub-b"})

    assert result.status == TaskExecutionStatus.FAILED
    assert ctx.agent_call_count == 1
    ctx.switch.assert_not_awaited()
    fallback.assert_not_awaited()
    assert result.error_code.value == "model_unsupported"


@pytest.mark.asyncio
async def test_chat_path_never_switches():
    from services.chat_execution_service import (
        ERROR_CODE_HEADER,
        ChatDispatchError,
        _apply_sub003_autoswitch,
    )

    switch = AsyncMock(return_value={"new_subscription": "sub-b"})
    fake = SimpleNamespace(handle_subscription_failure=switch, is_auth_failure=is_auth_failure)
    with patch.dict("sys.modules", {"services.subscription_auto_switch": fake}):
        with pytest.raises(ChatDispatchError) as exc:
            await _apply_sub003_autoswitch("agent1", _OLD_IMAGE_503, 503)
    switch.assert_not_awaited()
    assert exc.value.status_code == 400
    assert exc.value.headers == {ERROR_CODE_HEADER: "model_unsupported"}


def test_old_image_async_callback_does_not_count_as_auth():
    """An old image labels the rejection error_code "auth" on the #1083
    callback, which would trip the AUTH dispatch breaker."""
    from services.execution_envelope import terminal_from_callback_payload

    payload = SimpleNamespace(status="failed", error_code="auth", terminal_reason="auth",
                              error=_OLD_IMAGE_503, response=None, metadata={},
                              execution_log=None, session_id=None, cost=None,
                              context_used=None, context_max=None)
    env = terminal_from_callback_payload(payload, "exec-1")
    assert env.error_code.value == "model_unsupported"

    payload.error = "token expired"
    assert terminal_from_callback_payload(payload, "exec-1").error_code.value == "auth"


def test_old_image_pull_terminal_does_not_switch():
    from services.pull_coordination_service import _switch_failure_kind

    assert _switch_failure_kind("auth", f"[auth] {_OLD_IMAGE_503}") is None
    assert _switch_failure_kind("auth", "token expired") == "auth"


def test_pull_endpoint_accepts_the_new_code():
    from models import PullTaskResultRequest

    req = PullTaskResultRequest(claim_token="t", status="failed", error_code="model_unsupported")
    assert req.error_code == "model_unsupported"


# --------------------------------------------------------------------------- #
# Workspace copy (AC3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", ["MODEL_UNSUPPORTED", TaskExecutionErrorCode.MODEL_UNSUPPORTED])
def test_portal_names_the_model_problem_when_the_agent_default_is_refused(svc, monkeypatch, code):
    """The agent's own default was refused: no usage-limit copy, no "send it
    again", no internal version numbers to an external client."""
    from client_portal.service import ClientPortalError

    _failed_turn(svc, monkeypatch, code=code, error=f"Model unsupported: {_TOO_OLD_TEXT}")
    with pytest.raises(ClientPortalError) as exc:
        _portal_run(svc.portal_chat(AGENT, "hi", EMAIL, session_id=SESSION))

    assert exc.value.category == "model_unsupported"
    assert exc.value.category in svc.PORTAL_FAILURE_CATEGORIES
    assert exc.value.retryable is False
    assert "model" in exc.value.detail.lower()
    assert "usage limit" not in exc.value.detail
    assert "2.1.2" not in exc.value.detail


def test_portal_keeps_the_self_heal_when_the_client_chose_the_model(svc, monkeypatch):
    """`invalid_model` clears the stored choice; answering `model_unsupported`
    here would loop the person into the same refused model forever."""
    from client_portal.service import ClientPortalError

    _failed_turn(svc, monkeypatch, code="MODEL_UNSUPPORTED")
    with pytest.raises(ClientPortalError) as exc:
        _portal_run(svc.portal_chat(AGENT, "hi", EMAIL, session_id=SESSION, model=OPUS))

    assert exc.value.category == "invalid_model"
