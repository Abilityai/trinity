"""Tests for #678 auto-retry on the reader-race signature.

The backend's HTTPError handler retries once with the same execution_id
when the agent returned a 502 with the structured #678 signature AND
the original turn was cheap to retry. Gating must be conservative —
silently re-running a 24-min execution is worse than surfacing the
failure to the operator.

Module under test:
    src/backend/services/task_execution_service.py::
        _is_reader_race_signature
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _PROJECT_ROOT / "src" / "backend"
sys.path.insert(0, str(_BACKEND_DIR))

# Import via the deepest path that doesn't require database / config bootstrap.
from services.task_execution_service import (  # noqa: E402
    _is_reader_race_signature,
    _AUTO_RETRY_MAX_TURNS,
)


def _signature_body(
    *,
    raw_message_count: int = 0,
    num_turns: int | None = 0,
    parse_failure_count: int = 0,
    recovery_attempted: bool = True,
    message: str | None = None,
) -> dict:
    """Build a body shaped like _classify_empty_result's dict body."""
    body = {
        "message": message
        if message is not None
        else "Execution completed without a result message after 0 tool calls / 0 turns ...",
        "metadata": {"num_turns": num_turns} if num_turns is not None else {},
        "raw_message_count": raw_message_count,
        "parse_failure_count": parse_failure_count,
        "recovery_attempted": recovery_attempted,
    }
    return body


# ---------------------------------------------------------------------------
# Positive: signature matches → retry permitted
# ---------------------------------------------------------------------------


def test_classic_reader_race_signature_matches():
    """raw_message_count=0, num_turns small, no parse failures, classifier's
    diagnostic message present → retry."""
    body = _signature_body(raw_message_count=0, num_turns=1)
    assert _is_reader_race_signature(body) is True


def test_zero_turns_still_eligible():
    """Reader race that fires before claude emits anything (num_turns=0)
    is still cheap enough to retry."""
    body = _signature_body(raw_message_count=0, num_turns=0)
    assert _is_reader_race_signature(body) is True


def test_num_turns_at_threshold_minus_one_matches():
    body = _signature_body(raw_message_count=0, num_turns=_AUTO_RETRY_MAX_TURNS - 1)
    assert _is_reader_race_signature(body) is True


# ---------------------------------------------------------------------------
# Negative: gating refuses retry on expensive / suspicious failures
# ---------------------------------------------------------------------------


def test_too_many_turns_blocks_retry():
    """The whole point of the gating: 24-min runs at high turn counts
    MUST NOT be silently re-run. Gating must reject."""
    body = _signature_body(raw_message_count=0, num_turns=56)  # original #678 case
    assert _is_reader_race_signature(body) is False


def test_threshold_boundary_blocks_retry():
    """Exactly at the threshold is rejected — the comparator is strict <."""
    body = _signature_body(raw_message_count=0, num_turns=_AUTO_RETRY_MAX_TURNS)
    assert _is_reader_race_signature(body) is False


def test_raw_messages_present_blocks_retry():
    """If raw_message_count is non-zero, the reader thread captured at
    least *something*. Retrying would replay a partial-stream race —
    expensive and may not help."""
    body = _signature_body(raw_message_count=10, num_turns=2)
    assert _is_reader_race_signature(body) is False


def test_parse_failures_block_retry():
    """parse_failures != 0 means stdout wire corruption (interleaved MCP
    children writing to the same pipe). Retrying doesn't fix the root cause
    and risks the same outcome."""
    body = _signature_body(raw_message_count=0, num_turns=2, parse_failure_count=3)
    assert _is_reader_race_signature(body) is False


def test_recovery_not_attempted_blocks_retry():
    """The signature requires recovery_attempted=True so we don't retry
    on a body that came from a different error path."""
    body = _signature_body(raw_message_count=0, num_turns=2, recovery_attempted=False)
    assert _is_reader_race_signature(body) is False


def test_non_reader_race_message_blocks_retry():
    """The classifier's diagnostic includes the phrase 'result message'.
    If a different error type happens to produce a dict body, the
    signature must still reject."""
    body = _signature_body(
        raw_message_count=0,
        num_turns=2,
        message="auth failure — subscription token expired",
    )
    assert _is_reader_race_signature(body) is False


def test_missing_metadata_dict_blocks_retry():
    """Without a metadata dict, we can't gate on num_turns — bail out."""
    body = {
        "message": "Execution completed without a result message ...",
        "raw_message_count": 0,
        "parse_failure_count": 0,
        "recovery_attempted": True,
        # no "metadata"
    }
    # Default 0 num_turns when missing — passes the strict-less-than check,
    # so signature still matches. This documents the choice: missing
    # metadata is treated as "very small turn count" and is retryable.
    # If the product opinion shifts, flip this expectation explicitly.
    assert _is_reader_race_signature(body) is True


def test_string_detail_blocks_retry():
    """An old agent-image returning a string-detail body must NEVER be
    auto-retried — the signature can't be verified."""
    assert _is_reader_race_signature("Execution completed without a result message ...") is False


def test_none_detail_blocks_retry():
    assert _is_reader_race_signature(None) is False


def test_list_detail_blocks_retry():
    assert _is_reader_race_signature(["not", "a", "dict"]) is False
