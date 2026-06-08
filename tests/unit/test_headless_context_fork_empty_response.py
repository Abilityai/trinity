"""
Issue #160 — headless tasks invoking skills with `context: fork` in the
frontmatter fail with HTTP 500 "Task returned empty response".

The parent claude process exits cleanly (return_code == 0) with a populated
`result` line (so `_classify_empty_result` returns None — `cost_usd` and
`duration_ms` are set), but the fork's output goes to a sub-context that
never reaches the parent stdout, leaving `response_parts` empty.

Pre-#160: the empty-string check at the bottom of
`_finalize_headless_result` raised HTTP 500. This silently failed every
scheduled invocation of any fork-style skill (the issue report
documented 8 consecutive daily failures on one agent).

Post-#160: when `return_code == 0` and `cost_usd` is set (parent claude
explicitly reported completion), we synthesize a placeholder response so
the caller gets 200 instead of an opaque error. Real plumbing failures
are already caught by `_classify_empty_result` and never reach this
branch — see tests/unit/test_empty_result_classification.py for the
distinction.

Module under test:
    docker/base-image/agent_server/services/headless_executor.py
    ::_finalize_headless_result
"""
from __future__ import annotations

from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER_DIR = _PROJECT_ROOT / "docker" / "base-image" / "agent_server"

# tests/unit/conftest.py:_preload_real_agent_server() already registers
# docker/base-image/agent_server as a namespace package in sys.modules,
# so plain `from agent_server.<sub> import <X>` works here without any
# importlib gymnastics.

from agent_server.models import ExecutionMetadata  # noqa: E402
from agent_server.services.headless_executor import (  # noqa: E402
    HeadlessRunContext,
    _finalize_headless_result,
)


def _make_ctx(
    *,
    return_code: int,
    cost_usd,
    duration_ms,
    response_parts=None,
    raw_messages=None,
) -> HeadlessRunContext:
    """Build a HeadlessRunContext in the post-subprocess state."""
    ctx = HeadlessRunContext(
        cmd=["claude", "--print"],
        task_session_id="task-test-1",
        task_start_iso="2026-05-12T00:00:00Z",
        effective_timeout=900,
        images=None,
        prompt="dummy",
    )
    ctx.return_code = return_code
    ctx.metadata = ExecutionMetadata(
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        tool_count=0,
        num_turns=1,
    )
    ctx.metadata.session_id = "session-abc"
    if response_parts is not None:
        ctx.response_parts = list(response_parts)
    if raw_messages is not None:
        ctx.raw_messages = list(raw_messages)
    return ctx


# ---------------------------------------------------------------------------
# The #160 happy path: fork skill exits cleanly with empty parent stream.
# ---------------------------------------------------------------------------


def test_clean_exit_with_empty_parent_stream_returns_placeholder():
    """`context: fork` skill: return_code=0, cost/duration set, empty parts.

    Must NOT raise — must return a placeholder response so the caller
    sees 200, not the pre-#160 opaque "Task returned empty response" 500.
    """
    ctx = _make_ctx(
        return_code=0,
        cost_usd=0.0123,
        duration_ms=4200,
        response_parts=[],  # parent stream emitted no assistant text
        raw_messages=[
            {"type": "init", "session_id": "session-abc"},
            {"type": "result", "cost_usd": 0.0123, "duration_ms": 4200},
        ],
    )

    response_text, raw_messages, metadata, session_id = (
        _finalize_headless_result(ctx)
    )

    # Caller receives a non-empty string so the upstream "empty response"
    # check (e.g. backend task_execution_service) doesn't fail the task.
    assert response_text  # non-empty
    assert "context: fork" in response_text.lower() or "no direct output" in response_text.lower(), (
        f"placeholder should hint at the fork cause; got: {response_text!r}"
    )
    # Metadata preserved as-is for cost/usage accounting.
    assert metadata.cost_usd == 0.0123
    assert metadata.duration_ms == 4200
    assert session_id == "session-abc"


def test_clean_exit_with_real_output_unchanged():
    """Sanity check: when parent stream DID produce output, the placeholder
    branch must not fire — return the real text."""
    ctx = _make_ctx(
        return_code=0,
        cost_usd=0.05,
        duration_ms=8000,
        response_parts=["Hello,", "world."],
        raw_messages=[{"type": "result", "cost_usd": 0.05, "duration_ms": 8000}],
    )

    response_text, _, _, _ = _finalize_headless_result(ctx)

    assert response_text == "Hello,\nworld."


# ---------------------------------------------------------------------------
# Guards: scenarios where we still want a 500.
# ---------------------------------------------------------------------------


def test_nonzero_exit_with_empty_parts_still_fails():
    """If the subprocess exited non-zero, we should NOT synthesize a
    placeholder — that path is owned by the earlier exit-code branch in
    _finalize_headless_result and surfaces an honest error to the caller.
    """
    from fastapi import HTTPException

    ctx = _make_ctx(
        return_code=2,  # subprocess crashed
        cost_usd=None,
        duration_ms=None,
        response_parts=[],
        raw_messages=[],
    )
    # `verbose_output_lines` empty too so the diagnose path falls through
    # to its generic message.

    with pytest.raises(HTTPException) as exc_info:
        _finalize_headless_result(ctx)

    # The point is we do NOT return 200 with a placeholder. Multiple
    # earlier branches own non-zero exit (nonzero-exit 500, auth-fallback
    # 503, empty-result classifier 502); any non-2xx is acceptable as
    # long as the placeholder doesn't fire.
    assert 500 <= exc_info.value.status_code < 600


def test_clean_exit_but_missing_cost_falls_to_empty_result_classifier():
    """return_code == 0 but cost_usd is None means the result line never
    arrived — that's the #520 lost-result-line case which must surface as
    502, not a placeholder 200.

    Note: `_classify_empty_result` also attempts metadata recovery from
    raw_messages first; if no recovery is possible AND no response_parts
    exist AND no JSONL recovery, it raises 502.
    """
    from fastapi import HTTPException

    ctx = _make_ctx(
        return_code=0,
        cost_usd=None,  # the defining "lost result" condition
        duration_ms=None,
        response_parts=[],
        raw_messages=[],
    )

    with pytest.raises(HTTPException) as exc_info:
        _finalize_headless_result(ctx)

    # #520 owns this case — it raises 502 with a diagnostic detail.
    # #678: detail is now a structured dict ({"message", "metadata",
    # "raw_message_count", "parse_failure_count", "recovery_attempted"})
    # carrying salvage telemetry — read the human-readable text out of
    # detail["message"]. See tests/unit/test_empty_result_classification.py
    # for the canonical pattern.
    assert exc_info.value.status_code == 502
    assert isinstance(exc_info.value.detail, dict), "#678: detail is a dict"
    message = exc_info.value.detail["message"]
    assert "result message" in message.lower() or "stdout" in message.lower()


# ---------------------------------------------------------------------------
# Regression-pin: source-level signature of the new branch.
# ---------------------------------------------------------------------------


def test_finalize_source_contains_fork_branch():
    """If someone deletes the fork-aware branch, this catches it."""
    src = (
        _AGENT_SERVER_DIR / "services" / "headless_executor.py"
    ).read_text()
    # Branch references the issue number AND its guard condition.
    assert "#160" in src
    assert "context: fork" in src.lower() or "context:fork" in src.lower()
    # The guard predicate must stay coupled to a clean exit + populated
    # cost so the placeholder can't fire on a genuinely broken run.
    assert "return_code == 0" in src
    assert "cost_usd is not None" in src
