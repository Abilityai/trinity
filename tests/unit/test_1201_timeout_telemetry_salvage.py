"""Unit tests for #1201: agent-side timeout (504) must carry execution
telemetry so the backend can salvage cost/context onto the FAILED row.

Before #1201, the 504 raised on an agent-side budget timeout / stall-kill
(`headless_executor.py`) carried only ``{message, termination_reason}``. The
cost / context / tool-call telemetry parsed before the kill (``ctx.metadata``)
was discarded, so the backend wrote a bare FAILED row — zero cost accounting
for exactly the long, tool-heavy runs that reach the timeout.

Fix: the 504 detail now includes ``metadata`` = sanitized
``ExecutionMetadata.model_dump()`` (the #678 structured-error shape). The
backend's existing HTTPError salvage branch reads ``detail["metadata"]`` and
persists cost / context_used / context_max onto the FAILED row — unchanged.

These tests pin the agent-side contract (`_timeout_504_detail`): the body
carries the telemetry in the exact shape the backend salvage consumes.

Module under test:
    docker/base-image/agent_server/services/headless_executor.py
"""
from __future__ import annotations

import pytest

# conftest.py preloads the real agent_server namespace package.
from agent_server.services import headless_executor as he  # noqa: E402
from agent_server.models import ExecutionMetadata  # noqa: E402


def _ctx(metadata: ExecutionMetadata) -> "he.HeadlessRunContext":
    return he.HeadlessRunContext(
        cmd=["claude", "--print"],
        task_session_id="t-1201",
        task_start_iso="2026-01-01T00:00:00Z",
        effective_timeout=2400,
        images=None,
        prompt="hello",
        metadata=metadata,
    )


def _populated_metadata() -> ExecutionMetadata:
    return ExecutionMetadata(
        cost_usd=0.4242,
        input_tokens=1234,
        output_tokens=99,
        cache_read_tokens=500,
        cache_creation_tokens=100,
        context_window=200000,
        tool_count=7,
        num_turns=12,
    )


def test_max_duration_504_carries_metadata():
    ctx = _ctx(_populated_metadata())
    detail = he._timeout_504_detail(
        ctx, "Task execution timed out after 2400 seconds", "max_duration"
    )

    assert detail["termination_reason"] == "max_duration"
    assert detail["message"] == "Task execution timed out after 2400 seconds"
    assert "stalled_tool" not in detail            # only set for stall kills

    md = detail["metadata"]
    assert md["cost_usd"] == pytest.approx(0.4242)  # cost accounting preserved
    assert md["input_tokens"] == 1234
    assert md["context_window"] == 200000
    assert md["tool_count"] == 7


def test_stall_504_carries_metadata_and_tool():
    ctx = _ctx(_populated_metadata())
    detail = he._timeout_504_detail(
        ctx,
        "Killed: tool 'mcp__x__hang' produced no output for 300s (stall watchdog)",
        "stall_no_output",
        stalled_tool="mcp__x__hang",
    )

    assert detail["termination_reason"] == "stall_no_output"
    assert detail["stalled_tool"] == "mcp__x__hang"
    assert detail["metadata"]["cost_usd"] == pytest.approx(0.4242)


def test_metadata_shape_matches_backend_salvage_contract():
    """The metadata dict must carry every key the backend HTTPError salvage
    reads (task_execution_service.py): cost_usd + the token fields
    _compute_context_used consumes + context_window. Guards against an
    ExecutionMetadata rename silently breaking salvage."""
    detail = he._timeout_504_detail(_ctx(_populated_metadata()), "m", "max_duration")
    md = detail["metadata"]
    for key in (
        "cost_usd",
        "input_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "context_window",
    ):
        assert key in md, f"backend salvage reads metadata['{key}']"

    # Mirror _compute_context_used: cache_read + cache_creation wins when > 0.
    expected_context_used = md["cache_read_tokens"] + md["cache_creation_tokens"]
    assert expected_context_used == 600


def test_empty_metadata_still_well_formed():
    """A timeout before any telemetry parsed (default ExecutionMetadata) still
    produces a valid body — salvage just yields null cost / None context."""
    detail = he._timeout_504_detail(_ctx(ExecutionMetadata()), "m", "max_duration")
    assert detail["metadata"]["cost_usd"] is None
    assert detail["metadata"]["input_tokens"] == 0
    assert detail["metadata"]["context_window"] == 200000  # model default
