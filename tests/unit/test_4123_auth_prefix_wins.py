"""#4123: a real auth failure that mentions "prompt is too long" still walks.

#3849 taught every SUB-003 reader to skip a prompt-length rejection, keyed on
the API's sentence anywhere in the text (``is_prompt_too_long``). The agent's
auth answer quotes the stderr line that tripped it (``Authentication failure:
<first 300 chars>``), so when that one line also carries the sentence, the
turn read as a prompt-length rejection: no walk, and the row coded
``agent_error`` instead of ``auth``.

The agent's own ``Authentication failure:`` prefix is the stronger signal: the
agent only writes it after its own auth patterns matched. The agent-side detail
below comes from the real ``_finalize_headless_result``, through the stderr
auth-abort branch.
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
from agent_server.services.error_classifier import _is_auth_failure_message
from agent_server.services.headless_executor import (
    HeadlessRunContext,
    _finalize_headless_result,
)

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from services import failure_classifier
from services.execution_classification import classify_switch_failure
from services.failure_classifier import is_auth_failure, is_prompt_too_long

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]

# One stderr line holding both signals: an auth pattern and the sentence.
_STDERR_LINE = "Error: OAuth token expired (mcp-proxy: prompt is too long)"
# The issue's probe, verbatim shape.
_ISSUE_PROBE = "Authentication failure: Invalid API key (hook said: prompt is too long)"


def _agent_auth_detail() -> str:
    """The agent's 503 detail for one stderr line that holds both signals,
    driven the way ``read_stderr`` does it: the line goes into the
    transcript, the real auth matcher fires on it, and the abort is raised."""
    ctx = HeadlessRunContext(
        cmd=["claude", "--print"], task_session_id="task-4123",
        task_start_iso="2026-11-02T10:00:00Z", effective_timeout=900,
        images=None, prompt="hi",
    )
    ctx.return_code = 1
    ctx.metadata = ExecutionMetadata()
    ctx.verbose_output_lines.append(_STDERR_LINE)
    assert _is_auth_failure_message(_STDERR_LINE)
    ctx.auth_abort_reason.append(_STDERR_LINE)
    ctx.auth_abort_event.set()
    with pytest.raises(HTTPException) as exc:
        _finalize_headless_result(ctx)
    assert exc.value.status_code == 503
    return exc.value.detail


def _details():
    return [
        pytest.param(_agent_auth_detail, id="agent-finalizer"),
        pytest.param(lambda: _ISSUE_PROBE, id="issue-probe"),
    ]


def _resp(status: int, detail) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    body = {"detail": detail}
    resp.text = json.dumps(body)
    resp.json.return_value = body
    return resp


def test_the_fixture_really_carries_both_signals():
    """Guard the precondition: without the sentence in the detail this file
    would pass on the bug."""
    detail = _agent_auth_detail()
    assert detail.startswith(f"Authentication failure: {_STDERR_LINE}")
    assert "prompt is too long" in detail.lower()


@pytest.mark.parametrize("detail_fn", _details())
def test_the_auth_prefix_wins_over_the_prompt_length_marker(detail_fn):
    detail = detail_fn()
    assert not is_prompt_too_long(detail)
    assert is_auth_failure(detail)
    assert classify_switch_failure(_resp(503, detail)) == "auth"
    assert classify_switch_failure(_resp(503, {"message": detail, "metadata": {}})) == "auth"


def test_a_bare_prompt_length_rejection_is_still_not_auth():
    """No regression on #3849: the sentence without the agent's auth prefix."""
    for detail in (
        "Execution failed: Prompt is too long",
        (
            "Execution failed with no output (exit code 1): "
            "Claude Code execution failed (exit code 1): Prompt is too long"
        ),
    ):
        assert is_prompt_too_long(detail)
        assert classify_switch_failure(_resp(503, detail)) is None


@pytest.mark.parametrize("detail_fn", _details())
def test_task_path_switches_and_codes_auth(detail_fn):
    from test_792_subscription_retry import _resp as _agent_resp
    from test_792_subscription_retry import _run

    detail = detail_fn()
    legacy_switch = AsyncMock(return_value={"switched": False})
    with (
        patch("services.subscription_auto_switch.handle_subscription_failure", legacy_switch),
        patch("services.subscription_auto_switch.fallback_to_api_key", AsyncMock(return_value={"switched": False})),
    ):
        result, ctx = _run(responses=[_agent_resp(503, {"detail": detail})],
                           switch_result=None)

    assert ctx.switch.await_count + legacy_switch.await_count >= 1
    assert (result.error_code.value if result.error_code else None) == "auth"


@pytest.mark.asyncio
@pytest.mark.parametrize("detail_fn", _details())
async def test_chat_path_switches_and_codes_auth(detail_fn):
    from services.chat_execution_service import (
        ERROR_CODE_HEADER,
        ChatDispatchError,
        _apply_sub003_autoswitch,
        _classify_agent_http_failure,
    )

    detail = detail_fn()
    assert _classify_agent_http_failure(503, detail).value == "auth"
    switch = AsyncMock(return_value=None)
    fake = SimpleNamespace(handle_subscription_failure=switch, is_auth_failure=is_auth_failure)
    with patch.dict("sys.modules", {"services.subscription_auto_switch": fake}):
        with pytest.raises(ChatDispatchError) as exc:
            await _apply_sub003_autoswitch("agent1", detail, 503)
    switch.assert_awaited_once()
    assert exc.value.headers[ERROR_CODE_HEADER] == "auth"


@pytest.mark.parametrize("detail_fn", _details())
def test_pull_paths_keep_auth(detail_fn):
    """The refusal walk reads the worker's raw text; the terminal switch reads
    the stored ``[auth] ...`` row text; the recode reads the raw text."""
    from services.pull_coordination_service import (
        _recode_credential_independent_auth,
        _switch_failure_kind,
    )

    detail = detail_fn()
    assert _recode_credential_independent_auth("auth", detail) == "auth"
    assert _switch_failure_kind("auth", detail) == "auth"
    assert _switch_failure_kind("auth", f"[auth] {detail}") == "auth"


@pytest.mark.parametrize("detail_fn", _details())
def test_async_callback_keeps_auth(detail_fn):
    from services.execution_envelope import terminal_from_callback_payload

    payload = SimpleNamespace(status="failed", error_code="auth", terminal_reason="auth",
                              error=detail_fn(), response=None, metadata={},
                              execution_log=None, session_id=None, cost=None,
                              context_used=None, context_max=None)
    assert terminal_from_callback_payload(payload, "exec-1").error_code.value == "auth"


def test_the_auth_prefix_is_the_agents_own():
    """Pinned against the producer: if the agent's wording changes, this goes
    red instead of the precedence silently missing."""
    src = (_REPO / "docker/base-image/agent_server/services/headless_executor.py").read_text().lower()
    for prefix in failure_classifier.AGENT_AUTH_FAILURE_PREFIXES:
        assert f'detail=f"{prefix}' in src, prefix
