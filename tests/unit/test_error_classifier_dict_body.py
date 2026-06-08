"""Tests for the #678 structured-dict-body return shape of
``_classify_empty_result``.

Prior to #678 the function returned ``(int, str)``. Backend extracted
``error_data["detail"]`` as a plain string and wrote it to the failure
row's ``error`` column. All telemetry was lost.

After #678 the function returns ``(int, dict)`` where the dict carries:
- ``message`` — the existing diagnostic string (sanitized)
- ``metadata`` — partial ExecutionMetadata.model_dump() (sanitized)
- ``raw_message_count`` — int, what the reader thread captured
- ``parse_failure_count`` — int, wire-corruption count
- ``recovery_attempted`` — bool, signal for the backend auto-retry gate

The backend's HTTPError handler salvages cost / context / model_name
from ``metadata`` onto the failure row. Sanitization happens in the
classifier so the body can never leak credentials.

Module under test:
    docker/base-image/agent_server/services/error_classifier.py
"""
from __future__ import annotations

import pytest

from agent_server.models import ExecutionMetadata
from agent_server.services.error_classifier import _classify_empty_result


# ---------------------------------------------------------------------------
# Body shape & contents
# ---------------------------------------------------------------------------


def test_returns_dict_body_with_required_keys():
    meta = ExecutionMetadata(tool_count=2, num_turns=3)
    result = _classify_empty_result(meta, raw_message_count=5)

    assert result is not None
    status_code, body = result
    assert status_code == 502
    assert isinstance(body, dict)
    for required in ("message", "metadata", "raw_message_count", "parse_failure_count", "recovery_attempted"):
        assert required in body, f"#678 body missing required key '{required}'"


def test_message_field_carries_diagnostic_string():
    meta = ExecutionMetadata(tool_count=2, num_turns=3)
    result = _classify_empty_result(meta, raw_message_count=5)
    _, body = result

    msg = body["message"]
    assert isinstance(msg, str)
    assert "2 tool calls" in msg
    assert "3 turns" in msg


def test_metadata_field_carries_partial_telemetry():
    """The whole point of #678: even on failure, cost / tokens / model_name
    survive into the body so the backend can persist them onto the row."""
    meta = ExecutionMetadata(
        tool_count=2,
        num_turns=3,
        input_tokens=150,
        cache_read_tokens=2000,
        model_name="claude-sonnet-4-5",
    )
    result = _classify_empty_result(meta, raw_message_count=5)
    _, body = result

    body_meta = body["metadata"]
    assert isinstance(body_meta, dict)
    assert body_meta.get("input_tokens") == 150
    assert body_meta.get("cache_read_tokens") == 2000
    assert body_meta.get("model_name") == "claude-sonnet-4-5"


def test_raw_message_count_passes_through():
    meta = ExecutionMetadata()
    result = _classify_empty_result(meta, raw_message_count=42)
    _, body = result
    assert body["raw_message_count"] == 42


def test_parse_failure_count_passes_through():
    meta = ExecutionMetadata()
    result = _classify_empty_result(meta, raw_message_count=0, parse_failure_count=7)
    _, body = result
    assert body["parse_failure_count"] == 7


def test_recovery_attempted_is_true():
    """The flag is the auto-retry gate's signal — must be True on every
    structured emit so the backend knows the agent did try recovery."""
    meta = ExecutionMetadata()
    result = _classify_empty_result(meta, raw_message_count=0)
    _, body = result
    assert body["recovery_attempted"] is True


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def test_metadata_error_message_is_sanitized():
    """ExecutionMetadata.error_message is populated directly from claude
    output (stream_parser.py:281+) and can carry leaked tokens. The
    dict body MUST sanitize before returning so credentials don't end up
    on the failure row."""
    meta = ExecutionMetadata(
        error_type="auth",
        # Realistic token leak shape that the credential sanitizer should
        # redact. The exact replacement string doesn't matter; we just
        # require the raw value to be gone.
        error_message="sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-zZ_-9876543210x",
    )
    result = _classify_empty_result(meta, raw_message_count=0)
    _, body = result

    sanitized_error = body["metadata"].get("error_message") or ""
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in sanitized_error, (
        "raw API key leaked through to the dict body"
    )


def test_message_field_is_sanitized():
    """The diagnostic message is built from format-strings; tests
    that #640's parse_failure_sample inclusion goes through sanitize_text
    if it ever carries leaked content."""
    # parse_failure_sample contains a malformed stdout line. If that line
    # happened to contain a token, sanitization must strip it.
    meta = ExecutionMetadata()
    leaked_line = "{\"key\": \"sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOP-zZ_-987654321xyz\""
    result = _classify_empty_result(
        meta,
        raw_message_count=0,
        parse_failure_count=1,
        parse_failure_sample=leaked_line,
    )
    _, body = result
    msg = body["message"]
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in msg
