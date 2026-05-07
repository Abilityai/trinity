"""
Phase 1.3 / Appendix B regression tests: stream-json parser must capture the
real Claude Code session UUID from ``{"type": "system", "subtype": "init",
"session_id": ...}``, with the ``result`` event as a fallback.

Before the fix, the parser checked ``msg_type == "init"``, which never
matched (Claude Code emits ``type="system", subtype="init"``), so
``metadata.session_id`` stayed ``None`` and callers fell back to the Trinity
execution id with an ``EX-`` prefix — caching that broke ``--resume``.

Module under test:
    docker/base-image/agent_server/services/claude_code.py
        ::parse_stream_json_output  (batch parser)
        ::process_stream_line       (streaming parser)
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER_DIR = _PROJECT_ROOT / "docker" / "base-image" / "agent_server"

if "agent_server" not in sys.modules:
    _stub = types.ModuleType("agent_server")
    _stub.__path__ = [str(_AGENT_SERVER_DIR)]
    sys.modules["agent_server"] = _stub

from agent_server.models import ExecutionMetadata  # noqa: E402
from agent_server.services.claude_code import (  # noqa: E402
    parse_stream_json_output,
    process_stream_line,
)


_REAL_UUID = "3abcc2e4-c815-4a71-ae40-caf49cb9d71f"
_FALLBACK_UUID = "7f1b9d20-1234-5678-9abc-def012345678"


def _system_init_line(session_id: str = _REAL_UUID) -> str:
    """A well-formed system/init line as Claude Code actually emits it."""
    return json.dumps({
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "permissionMode": "bypassPermissions",
    })


def _result_line(session_id: str = _FALLBACK_UUID) -> str:
    return json.dumps({
        "type": "result",
        "session_id": session_id,
        "result": "done",
        "total_cost_usd": 0.0012,
        "duration_ms": 1234,
        "num_turns": 1,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })


# ---------------------------------------------------------------------------
# parse_stream_json_output — batch parser used after subprocess completes.
# ---------------------------------------------------------------------------

def test_batch_parser_captures_session_id_from_system_init():
    """The defining regression: type=system + subtype=init must populate
    metadata.session_id with the embedded UUID."""
    output = "\n".join([_system_init_line(), _result_line()])

    _, _, metadata = parse_stream_json_output(output)

    assert metadata.session_id == _REAL_UUID


def test_batch_parser_falls_back_to_result_session_id_when_init_missing():
    """Truncated streams may drop the init line. The result event also
    carries session_id and is the documented fallback."""
    output = _result_line(session_id=_FALLBACK_UUID)

    _, _, metadata = parse_stream_json_output(output)

    assert metadata.session_id == _FALLBACK_UUID


def test_batch_parser_prefers_init_over_result_when_both_present():
    """When both events arrive, init is authoritative — result.session_id
    must not overwrite a session id we already captured."""
    output = "\n".join([
        _system_init_line(session_id=_REAL_UUID),
        _result_line(session_id=_FALLBACK_UUID),
    ])

    _, _, metadata = parse_stream_json_output(output)

    assert metadata.session_id == _REAL_UUID


def test_batch_parser_ignores_legacy_bare_init_event():
    """Pre-fix code matched type=='init' (no system wrapper). That bare
    shape isn't what Claude Code emits and must not be honored — otherwise
    a malformed/test stream could spoof the session id."""
    bare_init = json.dumps({"type": "init", "session_id": "BOGUS-not-a-uuid"})
    output = "\n".join([bare_init, _result_line(session_id=_FALLBACK_UUID)])

    _, _, metadata = parse_stream_json_output(output)

    # Result event's UUID wins because the bare init was correctly ignored.
    assert metadata.session_id == _FALLBACK_UUID


# ---------------------------------------------------------------------------
# process_stream_line — streaming parser used during live subprocess output.
# ---------------------------------------------------------------------------

def test_streaming_parser_captures_session_id_from_system_init():
    metadata = ExecutionMetadata()
    response_parts: list[str] = []
    execution_log: list = []

    process_stream_line(
        _system_init_line(),
        execution_log,
        metadata,
        {},                # tool_start_times
        response_parts,    # response_parts (mutated in place)
    )

    assert metadata.session_id == _REAL_UUID


def test_streaming_parser_result_fallback_when_init_missed():
    """Init line lost (e.g. truncated reader) — result must populate session_id."""
    metadata = ExecutionMetadata()
    response_parts: list[str] = []
    execution_log: list = []

    process_stream_line(
        _result_line(session_id=_FALLBACK_UUID),
        execution_log,
        metadata,
        {},                # tool_start_times
        response_parts,    # response_parts
    )

    assert metadata.session_id == _FALLBACK_UUID


def test_permission_mode_validation_uses_system_subtype_init():
    """Phase 1.3 sibling fix: the permission-mode validation site inside
    execute_headless_task also matched the wrong shape (``type=='init'``) so
    ``permission_mode_validated`` never became True and the protective
    kill-on-misconfigured-permission-mode silently failed open.

    AST/source-level guard (the function itself spawns subprocesses and
    isn't suitable for a unit-test execution path)."""
    src = (
        Path(__file__).resolve().parents[2]
        / "docker" / "base-image" / "agent_server" / "services" / "claude_code.py"
    ).read_text()

    # The check must use system+init, not the legacy bare init shape.
    assert 'raw_msg.get("type") == "system"' in src
    assert 'raw_msg.get("subtype") == "init"' in src

    # The legacy mistaken pattern must be gone.
    assert 'raw_msg.get("type") == "init"' not in src, (
        "execute_headless_task must not check raw_msg.get('type') == 'init' — "
        "Claude Code emits type=system, subtype=init"
    )


def test_streaming_parser_init_wins_over_later_result():
    metadata = ExecutionMetadata()
    response_parts: list[str] = []
    execution_log: list = []

    process_stream_line(
        _system_init_line(session_id=_REAL_UUID),
        execution_log, metadata, {}, response_parts,
    )
    process_stream_line(
        _result_line(session_id=_FALLBACK_UUID),
        execution_log, metadata, {}, response_parts,
    )

    assert metadata.session_id == _REAL_UUID


# ---------------------------------------------------------------------------
# compact_boundary capture — Claude Code's auto-compact event mid-turn.
# ---------------------------------------------------------------------------

def _compact_boundary_line(
    pre_tokens: int = 170_325,
    post_tokens: int = 12_691,
    duration_ms: int = 110_361,
    trigger: str = "auto",
    timestamp: str = "2026-05-03T09:49:49.226Z",
) -> str:
    """A compact_boundary event as Claude Code emits it (real shape, taken
    from a captured JSONL on agent-testfix)."""
    return json.dumps({
        "type": "system",
        "subtype": "compact_boundary",
        "content": "Conversation compacted",
        "compactMetadata": {
            "trigger": trigger,
            "preTokens": pre_tokens,
            "postTokens": post_tokens,
            "durationMs": duration_ms,
        },
        "timestamp": timestamp,
        "sessionId": _REAL_UUID,
    })


def test_batch_parser_captures_single_compact_event():
    """compact_boundary lines must populate metadata.compact_events with the
    full pre/post/duration/trigger payload for downstream observability."""
    output = "\n".join([
        _system_init_line(),
        _compact_boundary_line(),
        _result_line(),
    ])

    _, _, metadata = parse_stream_json_output(output)

    assert len(metadata.compact_events) == 1
    ev = metadata.compact_events[0]
    assert ev.trigger == "auto"
    assert ev.pre_tokens == 170_325
    assert ev.post_tokens == 12_691
    assert ev.duration_ms == 110_361
    assert ev.timestamp == "2026-05-03T09:49:49.226Z"


def test_batch_parser_captures_multiple_compact_events_in_order():
    """A long heavy turn can fire more than one compact. All must land in
    compact_events in the order observed."""
    output = "\n".join([
        _system_init_line(),
        _compact_boundary_line(pre_tokens=170_325, post_tokens=12_691, timestamp="t1"),
        _compact_boundary_line(pre_tokens=169_341, post_tokens=9_763, timestamp="t2"),
        _result_line(),
    ])

    _, _, metadata = parse_stream_json_output(output)

    assert len(metadata.compact_events) == 2
    assert [e.timestamp for e in metadata.compact_events] == ["t1", "t2"]
    assert metadata.compact_events[0].pre_tokens == 170_325
    assert metadata.compact_events[1].pre_tokens == 169_341


def test_batch_parser_compact_events_empty_when_none_fired():
    """A normal turn with no compact must leave compact_events empty (not
    populated with a sentinel or null entry)."""
    output = "\n".join([_system_init_line(), _result_line()])

    _, _, metadata = parse_stream_json_output(output)

    assert metadata.compact_events == []


def test_streaming_parser_captures_compact_event():
    metadata = ExecutionMetadata()
    response_parts: list[str] = []
    execution_log: list = []

    process_stream_line(
        _compact_boundary_line(),
        execution_log, metadata, {}, response_parts,
    )

    assert len(metadata.compact_events) == 1
    assert metadata.compact_events[0].trigger == "auto"
    assert metadata.compact_events[0].pre_tokens == 170_325


def test_compact_event_round_trips_through_model_dump():
    """ExecutionMetadata.model_dump() must serialize compact_events so the
    HTTP response from the agent server carries them to the backend."""
    output = "\n".join([
        _system_init_line(),
        _compact_boundary_line(),
        _result_line(),
    ])

    _, _, metadata = parse_stream_json_output(output)
    dumped = metadata.model_dump()

    assert "compact_events" in dumped
    assert isinstance(dumped["compact_events"], list)
    assert len(dumped["compact_events"]) == 1
    assert dumped["compact_events"][0]["pre_tokens"] == 170_325
