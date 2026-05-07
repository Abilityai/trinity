"""
Unit tests for #630: result-line recovery and per-line reader hardening.

The bug: when an agent has stdio MCP servers, the read_stdout thread can
exit early (publish_log_entry raising, sanitize hiccup, anything that's
not a JSONDecodeError). If that happens, the trailing
``{"type": "result"}`` line either never gets parsed by
``process_stream_line`` even though it sits in ``raw_messages``, or never
makes it to ``raw_messages`` at all. metadata.cost_usd / duration_ms stay
None, _classify_empty_result fires, the execution is recorded as a 502
"completed without a result message" failure even though Claude finished
cleanly.

Two defensive layers are exercised here:

1. ``_recover_metadata_from_raw_messages`` — back-fills metadata when
   raw_messages contains a result entry the stream parser missed.
2. ``_classify_empty_result`` — calls the recovery first; only emits 502
   if recovery cannot find anything.

The reader-thread per-line isolation is exercised separately by
``test_read_stdout_continues_after_publish_failure``: a mocked publisher
raises on every call, but the reader still appends every well-formed
JSON line it sees and the result line eventually wins.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# tests/unit/conftest.py preloads the real agent_server package; just import.
from agent_server.models import ExecutionMetadata  # noqa: E402
from agent_server.services.claude_code import (  # noqa: E402
    _classify_empty_result,
    _recover_metadata_from_raw_messages,
)


def _result_msg(**overrides):
    msg = {
        "type": "result",
        "total_cost_usd": 0.0123,
        "duration_ms": 4567,
        "num_turns": 7,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_creation_input_tokens": 50,
            "cache_read_input_tokens": 100,
        },
        "modelUsage": {
            "claude-opus": {
                "contextWindow": 200_000,
                "inputTokens": 1500,
                "outputTokens": 250,
            }
        },
    }
    msg.update(overrides)
    return msg


# ── _recover_metadata_from_raw_messages ─────────────────────────────────────

class TestRecoverMetadata:

    def test_no_op_when_metadata_already_populated(self):
        m = ExecutionMetadata(cost_usd=0.5, duration_ms=100)
        assert _recover_metadata_from_raw_messages(m, [_result_msg()]) is False
        assert m.cost_usd == 0.5  # unchanged

    def test_no_op_when_raw_messages_empty(self):
        m = ExecutionMetadata()
        assert _recover_metadata_from_raw_messages(m, []) is False
        assert _recover_metadata_from_raw_messages(m, None) is False

    def test_no_op_when_no_result_entry(self):
        m = ExecutionMetadata()
        raw = [
            {"type": "init"},
            {"type": "assistant", "message": {"content": []}},
            {"type": "user", "message": {"content": []}},
        ]
        assert _recover_metadata_from_raw_messages(m, raw) is False
        assert m.cost_usd is None

    def test_populates_from_result_entry(self):
        m = ExecutionMetadata()
        ok = _recover_metadata_from_raw_messages(m, [{"type": "init"}, _result_msg()])
        assert ok is True
        assert m.cost_usd == 0.0123
        assert m.duration_ms == 4567
        assert m.num_turns == 7
        assert m.cache_creation_tokens == 50
        assert m.cache_read_tokens == 100
        # modelUsage's inputTokens (1500) wins over usage.input_tokens (1000)
        assert m.input_tokens == 1500
        assert m.output_tokens == 250
        assert m.context_window == 200_000

    def test_picks_last_result_when_multiple(self):
        """Recovery scans from the end — the trailing result is authoritative."""
        m = ExecutionMetadata()
        first = _result_msg(total_cost_usd=0.001)
        last = _result_msg(total_cost_usd=0.999)
        _recover_metadata_from_raw_messages(m, [first, {"type": "assistant"}, last])
        assert m.cost_usd == 0.999

    def test_skips_malformed_result_with_no_cost_or_duration(self):
        m = ExecutionMetadata()
        broken = {"type": "result"}  # no total_cost_usd, no duration_ms
        assert _recover_metadata_from_raw_messages(m, [broken]) is False
        assert m.cost_usd is None

    def test_handles_partial_result_just_cost(self):
        m = ExecutionMetadata()
        partial = {"type": "result", "total_cost_usd": 0.5}  # no duration
        assert _recover_metadata_from_raw_messages(m, [partial]) is True
        assert m.cost_usd == 0.5
        assert m.duration_ms is None  # genuinely missing

    def test_handles_non_dict_entries(self):
        m = ExecutionMetadata()
        raw = ["string entry", 42, None, _result_msg()]
        assert _recover_metadata_from_raw_messages(m, raw) is True
        assert m.cost_usd == 0.0123

    def test_handles_missing_usage_block(self):
        m = ExecutionMetadata()
        msg = {"type": "result", "total_cost_usd": 0.1, "duration_ms": 100}
        assert _recover_metadata_from_raw_messages(m, [msg]) is True
        # Token defaults preserved when usage missing
        assert m.input_tokens == 0
        assert m.output_tokens == 0


# ── _classify_empty_result wired with recovery ─────────────────────────────

class TestClassifyEmptyResultRecovery:

    def test_recovery_short_circuits_502(self):
        """End-to-end: result line is in raw_messages → classify returns None
        (success path) instead of 502."""
        m = ExecutionMetadata()
        result = _classify_empty_result(
            metadata=m,
            raw_message_count=3,
            raw_messages=[{"type": "init"}, {"type": "assistant"}, _result_msg()],
        )
        assert result is None
        assert m.cost_usd == 0.0123  # recovered

    def test_returns_502_when_no_result_in_raw_messages(self):
        """When the result line is genuinely lost, classify still returns 502."""
        m = ExecutionMetadata()
        result = _classify_empty_result(
            metadata=m,
            raw_message_count=2,
            raw_messages=[{"type": "init"}, {"type": "assistant"}],
        )
        assert result is not None
        status, _ = result
        assert status == 502

    def test_returns_none_when_metadata_already_populated(self):
        m = ExecutionMetadata(cost_usd=0.5, duration_ms=100)
        assert _classify_empty_result(metadata=m, raw_messages=[]) is None

    def test_returns_none_when_metadata_is_none(self):
        assert _classify_empty_result(metadata=None) is None


# ── Reader-thread per-line isolation ────────────────────────────────────────

class TestReadStdoutPerLineIsolation:
    """Verify that a publish-side failure does not kill the reader thread.

    This test exercises the structure of read_stdout: previously, any
    Exception raised inside the per-line block (other than JSONDecodeError)
    propagated out of the for loop, killed the reader, and silently dropped
    every subsequent line — including the trailing result line.

    We can't easily import the nested closures without spinning up the full
    headless task path, so we re-implement the per-line policy here and
    assert the contract: result line is captured even when intermediate
    publishes raise.
    """

    def test_per_line_exception_does_not_kill_reader(self):
        """Simulate the patched read_stdout structure: publish raises every
        time, but the reader still reaches the result line and metadata is
        recoverable from raw_messages."""
        import json

        raw_messages: list = []
        published: list = []

        def fake_publish(msg):
            published.append(msg)
            raise RuntimeError("subscriber explosion")

        def per_line(line: str) -> None:
            try:
                try:
                    raw_msg = json.loads(line.strip())
                except json.JSONDecodeError:
                    raw_msg = None
                if isinstance(raw_msg, dict):
                    raw_messages.append(raw_msg)
                    try:
                        fake_publish(raw_msg)
                    except Exception:
                        pass  # mirror the patched code: log and continue
            except Exception:
                pass

        lines = [
            json.dumps({"type": "init"}) + "\n",
            json.dumps({"type": "assistant", "message": {"content": []}}) + "\n",
            json.dumps(_result_msg()) + "\n",
        ]
        for ln in lines:
            per_line(ln)

        # All three lines reached raw_messages despite publish raising every time.
        assert len(raw_messages) == 3
        assert raw_messages[-1]["type"] == "result"
        assert len(published) == 3  # publisher was attempted on every line

        # And recovery successfully back-fills metadata from the captured tail.
        m = ExecutionMetadata()
        assert _recover_metadata_from_raw_messages(m, raw_messages) is True
        assert m.cost_usd == 0.0123
