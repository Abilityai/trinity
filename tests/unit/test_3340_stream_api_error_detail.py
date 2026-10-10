"""#3340 — an API error the stream parser recorded is the failure detail.

Claude Code reported ``API Error: 400 Output blocked by content filtering
policy`` in its stream, exited 1 with zero tokens and nothing on stderr. The
stream parser stored the sentence on ``metadata.error_message``; the failure
path built its detail from stderr only, so the person read "no diagnostic
output. Most likely causes: OOM kill …" and was sent to raise a memory limit.

The assistant event below follows the CLI's real API-error shape captured for
#3012 (``is_api_error_message`` + the sentence as the text block); the
``error: "unknown"`` value and the sentence are the ones the agent log printed
for the reported run. The headless tests drive ``process_stream_line`` →
``_finalize_headless_result``; the chat tests drive the real
``execute_claude_code`` against a fake ``claude`` on PATH.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest
from fastapi import HTTPException

from agent_server.models import ExecutionMetadata
from agent_server.services import claude_code, execution_env, jsonl_recovery
from agent_server.services.error_classifier import _diagnose_exit_failure
from agent_server.services.headless_executor import (
    HeadlessRunContext,
    _finalize_headless_result,
)
from agent_server.services.stream_parser import _NO_ERROR_DETAIL, process_stream_line
from agent_server.state import agent_state

from services.failure_classifier import is_auth_failure, is_model_rejection

_API_ERROR = "API Error: 400 Output blocked by content filtering policy"


def _api_error_events(text: str = _API_ERROR, error: str = "unknown") -> list:
    return [
        {"type": "assistant", "error": error, "is_api_error_message": True,
         "message": {"model": "<synthetic>", "role": "assistant",
                     "content": [{"type": "text", "text": text}],
                     "usage": {"input_tokens": 0, "output_tokens": 0}}},
        {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
         "terminal_reason": "api_error", "api_error_status": 400,
         "total_cost_usd": 0, "duration_ms": 412, "result": text},
    ]


# What the zero-token branch answered before #3340 when nothing said why — and
# must still answer when nothing does.
_GUESS = (
    "Process failed with exit code 1 and no diagnostic output. "
    "Most likely causes: OOM kill (raise the agent's memory limit), "
    "schedule timeout (extend timeout_seconds), or container restart. "
    "Check the agent container logs."
)
_NO_REASON_DETAIL = f"Execution failed with no output (exit code 1): {_GUESS}"


@pytest.fixture(autouse=True)
def subscription_env(monkeypatch):
    """The reported install: a subscription token and no API key — the branch
    of ``_diagnose_exit_failure`` that returns the OOM guess."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "placeholder-token")


def _parsed(events) -> ExecutionMetadata:
    metadata = ExecutionMetadata()
    for event in events:
        process_stream_line(json.dumps(event), [], metadata, {}, [])
    return metadata


def _ctx(metadata: ExecutionMetadata, *, return_code: int = 1, stderr: str = ""):
    ctx = HeadlessRunContext(
        cmd=["claude", "--print"], task_session_id="task-3340",
        task_start_iso="2026-10-09T10:00:00Z", effective_timeout=900,
        images=None, prompt="hi",
    )
    ctx.return_code = return_code
    ctx.metadata = metadata
    ctx.verbose_output_lines = [stderr] if stderr else []
    return ctx


def _finalize(ctx) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        _finalize_headless_result(ctx)
    return exc.value


# --------------------------------------------------------------------------- #
# Headless: the stream's reason is the detail
# --------------------------------------------------------------------------- #


def test_zero_token_exit_reports_the_api_error_not_the_oom_guess():
    err = _finalize(_ctx(_parsed(_api_error_events())))
    assert err.status_code == 503
    assert err.detail == f"Execution failed: {_API_ERROR}"
    assert "OOM" not in err.detail
    assert "no diagnostic output" not in err.detail


def test_the_api_error_wins_over_unrelated_stderr():
    err = _finalize(_ctx(
        _parsed(_api_error_events()),
        stderr="(node:41) DeprecationWarning: punycode is deprecated",
    ))
    assert err.status_code == 503
    assert err.detail == f"Execution failed: {_API_ERROR}"


def test_a_turn_that_spent_tokens_reports_the_api_error_too():
    """The generic failure after the zero-token branch."""
    metadata = _parsed(_api_error_events())
    metadata.input_tokens = 1800
    metadata.output_tokens = 240
    err = _finalize(_ctx(metadata))
    assert err.status_code == 500
    assert err.detail == f"Task execution failed (exit code 1): {_API_ERROR}"


def test_a_credential_in_the_api_error_is_redacted():
    from agent_server.utils import credential_sanitizer

    # Named for its shape, not `secret`: CodeQL's sensitive-name heuristic
    # treats a local called `secret` as a taint source, and this value flows
    # through ExecutionMetadata into every telemetry log in the agent server —
    # 40 clear-text-logging false positives on the PR that added this test.
    sk_shaped = "sk-ant-api03-" + "a1B2" * 12
    assert sk_shaped not in credential_sanitizer.sanitize_text(sk_shaped)  # precondition
    err = _finalize(_ctx(_parsed(_api_error_events(f"API Error: 400 bad header {sk_shaped}"))))
    assert "API Error: 400 bad header" in err.detail
    assert sk_shaped not in err.detail


# --------------------------------------------------------------------------- #
# Headless: nothing said why → today's text, unchanged
# --------------------------------------------------------------------------- #


def test_no_stream_error_and_no_stderr_keeps_the_guess():
    err = _finalize(_ctx(ExecutionMetadata()))
    assert err.status_code == 503
    assert err.detail == _NO_REASON_DETAIL


def test_no_stream_error_keeps_stderr_as_the_detail():
    err = _finalize(_ctx(ExecutionMetadata(), stderr="npm ERR! something else"))
    assert err.status_code == 503
    assert err.detail == (
        "Execution failed with no output (exit code 1): npm ERR! something else"
    )


@pytest.mark.parametrize("message", [
    _NO_ERROR_DETAIL,
    f"{_NO_ERROR_DETAIL} (diagnostic: result_type=user stop_reason=null)",
])
def test_the_no_detail_placeholder_is_not_a_reason(message):
    """An ``is_error`` result that named no cause leaves the guess in place."""
    metadata = ExecutionMetadata()
    metadata.error_type = "execution_error"
    metadata.error_message = message
    assert _finalize(_ctx(metadata)).detail == _NO_REASON_DETAIL


def test_a_billing_error_keeps_its_own_wording():
    metadata = _parsed(_api_error_events("Credit balance is too low", error="billing_error"))
    err = _finalize(_ctx(metadata))
    assert err.status_code == 503
    assert "Credit balance is too low. To resolve: (1) add credits" in err.detail


# --------------------------------------------------------------------------- #
# Headless: auth still routes through the auth branch
# --------------------------------------------------------------------------- #

_AUTH_SUFFIX = ". Check subscription token or API key configuration."


def test_stream_auth_signal_is_unchanged():
    metadata = _parsed(_api_error_events(
        "Not logged in · Please run /login", error="authentication_failed"
    ))
    err = _finalize(_ctx(metadata))
    assert err.status_code == 503
    assert err.detail == (
        "Authentication failure: Not logged in · Please run /login" + _AUTH_SUFFIX
    )


def test_stderr_auth_abort_is_unchanged_when_the_stream_also_carried_an_error():
    ctx = _ctx(_parsed(_api_error_events()))
    ctx.auth_abort_event.set()
    ctx.auth_abort_reason.append("OAuth token has expired")
    err = _finalize(ctx)
    assert err.status_code == 503
    assert err.detail == "Authentication failure: OAuth token has expired" + _AUTH_SUFFIX


def test_stderr_auth_pattern_still_wins_over_a_stream_error():
    err = _finalize(_ctx(_parsed(_api_error_events()), stderr="Error: invalid token"))
    assert err.status_code == 503
    assert err.detail.startswith("Authentication failure: ")
    assert not err.detail.startswith("Execution failed")


def test_auth_wording_in_a_stream_error_never_reaches_the_new_text():
    """#904: SUB-003 substring-matches auth wording, so an API error that reads
    as auth is answered by the auth branch, never as ``Execution failed: …``."""
    text = "API Error: 401 Invalid token"
    err = _finalize(_ctx(_parsed(_api_error_events(text))))
    assert err.status_code == 503
    assert err.detail == f"Authentication failure: {text}" + _AUTH_SUFFIX


# --------------------------------------------------------------------------- #
# What the backend's text classifier makes of the new detail
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("detail", [
    f"Execution failed: {_API_ERROR}",
    f"Task execution failed (exit code 1): {_API_ERROR}",
    f"Claude Code execution failed (exit code 1): {_API_ERROR}",
])
def test_backend_text_classifier_does_not_read_the_new_detail_as_auth(detail):
    assert is_auth_failure(detail) is False
    assert is_model_rejection(detail) is False


# --------------------------------------------------------------------------- #
# Shared diagnosis (the chat path's only source when stderr is empty)
# --------------------------------------------------------------------------- #


def test_diagnose_returns_the_stream_error():
    assert _diagnose_exit_failure(1, _parsed(_api_error_events())) == _API_ERROR


def test_diagnose_without_a_stream_error_keeps_the_guess():
    assert _diagnose_exit_failure(1, ExecutionMetadata()) == _GUESS
    assert _diagnose_exit_failure(1, None) == _GUESS


# --------------------------------------------------------------------------- #
# Chat path — the real execute_claude_code against a fake `claude`
# --------------------------------------------------------------------------- #

# Prints SHIM_STREAM (a JSON list of stream-json lines), SHIM_STDERR on stderr,
# and exits 1.
_SHIM = r"""#!__PYTHON__
import json, os, sys

sys.stdin.read()
for line in json.loads(os.environ["SHIM_STREAM"]):
    sys.stdout.write(json.dumps(line) + "\n")
    sys.stdout.flush()
sys.stderr.write(os.environ.get("SHIM_STDERR", ""))
sys.exit(1)
"""

_SHIM_KEYS = ("SHIM_STREAM", "SHIM_STDERR")
_CHAT_SID = "33400000-0000-4000-8000-000000000001"


@pytest.fixture
def shim(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(_SHIM.replace("__PYTHON__", sys.executable))
    exe.chmod(0o755)

    old_path = execution_env.INITIAL_ENV.get("PATH", os.environ.get("PATH", ""))
    monkeypatch.setitem(
        execution_env.INITIAL_ENV, "PATH", str(bin_dir) + os.pathsep + old_path
    )
    monkeypatch.setitem(execution_env.INITIAL_ENV, "HOME", str(home))
    for key in _SHIM_KEYS:
        execution_env.INITIAL_ENV.pop(key, None)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TRINITY_CHAT_SESSION_FILE", str(tmp_path / "marker.json"))
    monkeypatch.setattr(jsonl_recovery, "_JSONL_PROJECTS_DIR", str(projects))

    monkeypatch.setattr(agent_state, "claude_code_available", True)
    monkeypatch.setattr(agent_state, "current_model", None)
    monkeypatch.setattr(agent_state, "session_started", False)
    monkeypatch.setattr(agent_state, "session_context_tokens", 0)
    monkeypatch.setattr(agent_state, "session_total_cost", 0.0)
    monkeypatch.setattr(agent_state, "session_total_output_tokens", 0)
    monkeypatch.setattr(agent_state, "conversation_history", [])
    monkeypatch.setattr(
        agent_state, "session_activity", agent_state._create_empty_activity()
    )
    monkeypatch.setattr(agent_state, "tool_outputs", {})
    monkeypatch.setattr(agent_state, "chat_session_id", None)
    monkeypatch.setattr(agent_state, "chat_session_model", None)
    monkeypatch.setattr(agent_state, "chat_session_generation", 0)
    yield
    for key in _SHIM_KEYS:
        execution_env.INITIAL_ENV.pop(key, None)


async def _chat(stream: list, stderr: str = "") -> HTTPException:
    execution_env.INITIAL_ENV["SHIM_STREAM"] = json.dumps(
        [{"type": "system", "subtype": "init", "session_id": _CHAT_SID}] + stream
    )
    execution_env.INITIAL_ENV["SHIM_STDERR"] = stderr
    with pytest.raises(HTTPException) as exc:
        await claude_code.execute_claude_code(
            "hello", model="claude-sentinel-3340",
            execution_id=f"exec-{uuid.uuid4().hex[:12]}",
        )
    return exc.value


@pytest.mark.asyncio
@pytest.mark.parametrize("stderr", ["", "(node:41) DeprecationWarning: punycode\n"])
async def test_chat_exit_1_reports_the_api_error(shim, stderr):
    err = await _chat(_api_error_events(), stderr)
    assert err.status_code == 500
    assert err.detail == f"Claude Code execution failed (exit code 1): {_API_ERROR}"


@pytest.mark.asyncio
async def test_chat_exit_1_with_no_reason_keeps_the_guess(shim):
    err = await _chat([])
    assert err.status_code == 500
    assert err.detail == f"Claude Code execution failed (exit code 1): {_GUESS}"
