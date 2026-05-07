"""
Unit tests for `_recover_response_from_jsonl` — the JSONL fallback recovery
that closes the stdout-pipe-race mid-tool-call gap left by Phase 5.1.

When a tool subprocess inherits Claude Code's stdout fd and wedges the
agent server's reader thread, the stream-json result event is lost. If
the wedge fires mid-tool-call (zero text emitted to stdout),
`response_parts` is empty and Phase 5.1's soft-recovery falls through
to a hard 502. The JSONL on disk usually contains the completed turn —
this helper reads it and synthesizes a soft-success response from
authoritative ground truth.
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

# Module under test
from agent_server.services.claude_code import (  # noqa: E402
    _recover_response_from_jsonl,
    _extract_compact_events_from_jsonl,
)
from agent_server.services import claude_code as _cc_module  # noqa: E402


# ---------------------------------------------------------------------------
# JSONL line builders — mirror Claude Code's actual on-disk shapes
# ---------------------------------------------------------------------------

def _user_input(text: str) -> str:
    """User input message — content is a STRING (boundary marker)."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": text},
    })


def _tool_result(output: str) -> str:
    """Tool result wrapped in a user message — content is a LIST of dicts."""
    return json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "abc", "content": output}
            ],
        },
    })


def _assistant_tool_use(name: str = "Bash") -> str:
    """Assistant message containing only a tool_use block (no text)."""
    return json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "abc", "name": name, "input": {}}
            ],
        },
    })


def _assistant_text(text: str) -> str:
    """Assistant message containing a text block."""
    return json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    })


def _system_init(session_id: str) -> str:
    return json.dumps({
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
    })


# ---------------------------------------------------------------------------
# Fixture — patches the projects dir constant to a tmp_path location
# ---------------------------------------------------------------------------

@pytest.fixture
def jsonl_dir(tmp_path, monkeypatch):
    """Redirect _JSONL_PROJECTS_DIR to tmp_path for the duration of the test."""
    target = tmp_path / "projects" / "-home-developer"
    target.mkdir(parents=True)
    monkeypatch.setattr(_cc_module, "_JSONL_PROJECTS_DIR", str(target))
    return target


def _write_jsonl(jsonl_dir: Path, session_id: str, lines: list[str]) -> Path:
    p = jsonl_dir / f"{session_id}.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


# ---------------------------------------------------------------------------
# Happy path — the user's actual failure pattern
# ---------------------------------------------------------------------------

def test_recovers_text_after_two_tool_calls_when_stdout_race_fires(jsonl_dir):
    """Reproduces the /session-context-pressure failure shape:
    user input "block" → assistant tool_use Bash → tool_result (32KB) →
    assistant tool_use Bash → tool_result (32KB) → assistant text reply.
    Stdout race truncates the stream-json mid-flight; JSONL has it all.
    """
    sid = "813e3dd3-d0be-4e35-a7ae-5cdb8870fb64"
    _write_jsonl(jsonl_dir, sid, [
        # Prior turn — should NOT be included in recovery
        _user_input("/session-context-pressure"),
        _assistant_text("Stage 1 done. Send block."),
        # Current turn — boundary is the next user_input
        _user_input("block"),
        _assistant_tool_use("Bash"),
        _tool_result("a" * 32000),
        _assistant_tool_use("Bash"),
        _tool_result("b" * 32000),
        _assistant_text("Stage 2 done. Send block again for stage 3."),
    ])

    recovered = _recover_response_from_jsonl(sid)

    assert recovered == "Stage 2 done. Send block again for stage 3."
    # Critically: prior turn's text "Stage 1 done." MUST NOT leak in.
    assert "Stage 1 done" not in recovered


def test_concatenates_multiple_text_blocks_after_boundary(jsonl_dir):
    """A single turn can emit multiple assistant.text blocks (e.g.,
    interleaved with thinking blocks). All must concatenate in order.
    """
    sid = "concat-test"
    _write_jsonl(jsonl_dir, sid, [
        _user_input("hello"),
        _assistant_text("Part one."),
        _assistant_text("Part two."),
    ])

    recovered = _recover_response_from_jsonl(sid)

    assert recovered == "Part one.\nPart two."


def test_uses_only_most_recent_user_boundary(jsonl_dir):
    """When the JSONL has multiple completed turns, recovery picks the
    LAST user-input boundary and walks forward only from there.
    """
    sid = "multi-turn"
    _write_jsonl(jsonl_dir, sid, [
        _user_input("first turn"),
        _assistant_text("first reply"),
        _user_input("second turn"),
        _assistant_text("second reply"),
        _user_input("third turn"),
        _assistant_text("third reply"),
    ])

    recovered = _recover_response_from_jsonl(sid)

    assert recovered == "third reply"
    assert "first reply" not in recovered
    assert "second reply" not in recovered


# ---------------------------------------------------------------------------
# Boundary handling — the user-input vs tool_result distinction
# ---------------------------------------------------------------------------

def test_tool_result_does_not_count_as_user_boundary(jsonl_dir):
    """tool_result entries have type=user but content is a LIST of dicts.
    They must NOT be treated as the boundary — otherwise we'd lose every
    assistant text block emitted before the final tool_result.
    """
    sid = "boundary"
    _write_jsonl(jsonl_dir, sid, [
        _user_input("block"),
        _assistant_tool_use("Bash"),
        _tool_result("output"),  # type=user, list content — NOT a boundary
        _assistant_text("real reply"),
    ])

    recovered = _recover_response_from_jsonl(sid)

    assert recovered == "real reply"


# ---------------------------------------------------------------------------
# None-returning edges — recovery must give up gracefully
# ---------------------------------------------------------------------------

def test_returns_none_when_session_id_missing(jsonl_dir):
    """No session_id (parser never saw system/init) → no JSONL to read."""
    assert _recover_response_from_jsonl(None) is None
    assert _recover_response_from_jsonl("") is None


def test_returns_none_when_jsonl_file_missing(jsonl_dir):
    """Different session UUID → file doesn't exist → return None."""
    assert _recover_response_from_jsonl("nonexistent-uuid") is None


def test_returns_none_when_no_user_input_boundary(jsonl_dir):
    """JSONL has only system/tool_result entries (shouldn't happen, but
    defend) → no boundary found → recovery refuses to guess."""
    sid = "no-boundary"
    _write_jsonl(jsonl_dir, sid, [
        _system_init(sid),
        _tool_result("orphan tool_result"),
        _assistant_text("text without preceding user input"),
    ])

    assert _recover_response_from_jsonl(sid) is None


def test_returns_none_when_no_text_after_boundary(jsonl_dir):
    """Wedge fired before Claude wrote any text — only tool_use and
    tool_result entries after the boundary. Genuinely incomplete turn,
    surface the original 502 instead of fabricating success.
    """
    sid = "no-text"
    _write_jsonl(jsonl_dir, sid, [
        _user_input("block"),
        _assistant_tool_use("Bash"),
        _tool_result("output"),
        # No assistant.text block — recovery gives up
    ])

    assert _recover_response_from_jsonl(sid) is None


def test_thinking_blocks_alone_do_not_qualify_as_recovery(jsonl_dir):
    """Thinking blocks are model-internal reasoning, never shown to user.
    A turn that emitted only a thinking block (no text) is still
    incomplete; recovery must not surface the thinking as the response.
    """
    sid = "thinking-only"
    thinking_msg = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "internal reasoning"}],
        },
    })
    _write_jsonl(jsonl_dir, sid, [
        _user_input("block"),
        thinking_msg,
    ])

    assert _recover_response_from_jsonl(sid) is None


# ---------------------------------------------------------------------------
# Robustness — malformed JSONL must not blow up recovery
# ---------------------------------------------------------------------------

def test_skips_malformed_lines(jsonl_dir):
    """Partial flush at the tail can leave an unparseable line. Recovery
    must skip it and return what it can from the surrounding good lines.
    """
    sid = "malformed"
    _write_jsonl(jsonl_dir, sid, [
        _user_input("block"),
        _assistant_text("good line"),
        '{"type": "assistant", "message": {"role"',  # truncated JSON
    ])

    recovered = _recover_response_from_jsonl(sid)

    assert recovered == "good line"


def test_skips_empty_lines(jsonl_dir):
    """Blank lines in the JSONL must not break parsing or alter results."""
    sid = "blanks"
    p = jsonl_dir / f"{sid}.jsonl"
    p.write_text(
        _user_input("block") + "\n\n"
        + _assistant_text("reply") + "\n\n"
    )

    assert _recover_response_from_jsonl(sid) == "reply"


# ===========================================================================
# _extract_compact_events_from_jsonl — fills in the stdout-stripped detail
# ===========================================================================

def _compact_boundary(
    pre_tokens: int = 175061,
    post_tokens: int = 5904,
    duration_ms: int = 73651,
    trigger: str = "auto",
    timestamp: str = "2026-05-04T13:01:56.959Z",
) -> str:
    """Compact_boundary record with the canonical JSONL shape (camelCase
    detail fields nested under compactMetadata) — the agent server's
    stdout stream-json strips this envelope, so we read it from disk."""
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
        "sessionId": "360b49c7-fb3a-49d5-a5bb-8c25378a1486",
    })


def test_compact_extract_canonical_jsonl_shape(jsonl_dir):
    """The canonical shape: real numeric fields under compactMetadata.
    Exact pattern from a captured JSONL — recovery must populate every
    field, not leave them None like the stdout parser does.
    """
    sid = "extract-1"
    _write_jsonl(jsonl_dir, sid, [
        _user_input("/heavy-task"),
        _assistant_tool_use("Bash"),
        _compact_boundary(),
        _assistant_text("done"),
    ])

    events = _extract_compact_events_from_jsonl(sid)

    assert len(events) == 1
    ev = events[0]
    assert ev.trigger == "auto"
    assert ev.pre_tokens == 175061
    assert ev.post_tokens == 5904
    assert ev.duration_ms == 73651
    assert ev.timestamp == "2026-05-04T13:01:56.959Z"


def test_compact_extract_filters_by_since_iso(jsonl_dir):
    """The JSONL accumulates compact_boundary records across every turn
    of the resumed session. Filtering by since_iso must scope the result
    to the current turn only.
    """
    sid = "extract-since"
    _write_jsonl(jsonl_dir, sid, [
        _user_input("turn one"),
        _compact_boundary(timestamp="2026-05-04T10:00:00.000Z", pre_tokens=170000),
        _user_input("turn two"),
        _compact_boundary(timestamp="2026-05-04T11:00:00.000Z", pre_tokens=160000),
        _user_input("turn three"),
        _compact_boundary(timestamp="2026-05-04T12:00:00.000Z", pre_tokens=150000),
    ])

    # Scope to events from turn three onwards.
    events = _extract_compact_events_from_jsonl(
        sid, since_iso="2026-05-04T11:30:00.000Z"
    )

    assert len(events) == 1
    assert events[0].pre_tokens == 150000


def test_compact_extract_includes_boundary_at_exact_since_iso(jsonl_dir):
    """since_iso uses >= comparison; an event at the exact start time
    must be included (turn-start anchor races with compact emission)."""
    sid = "extract-exact"
    _write_jsonl(jsonl_dir, sid, [
        _user_input("hello"),
        _compact_boundary(timestamp="2026-05-04T13:00:00.000Z"),
    ])

    events = _extract_compact_events_from_jsonl(
        sid, since_iso="2026-05-04T13:00:00.000Z"
    )

    assert len(events) == 1


def test_compact_extract_returns_empty_when_no_compact_records(jsonl_dir):
    """A turn with no compact event → empty list, not None or error."""
    sid = "extract-none"
    _write_jsonl(jsonl_dir, sid, [
        _user_input("hello"),
        _assistant_text("hi back"),
    ])

    assert _extract_compact_events_from_jsonl(sid) == []


def test_compact_extract_returns_empty_when_session_id_missing(jsonl_dir):
    assert _extract_compact_events_from_jsonl(None) == []
    assert _extract_compact_events_from_jsonl("") == []


def test_compact_extract_returns_empty_when_jsonl_missing(jsonl_dir):
    assert _extract_compact_events_from_jsonl("does-not-exist") == []


def test_compact_extract_handles_multiple_compacts_in_order(jsonl_dir):
    """A long turn can fire several compacts in a row. All must be
    captured in the order they appear in the JSONL."""
    sid = "extract-multi"
    _write_jsonl(jsonl_dir, sid, [
        _user_input("very long turn"),
        _compact_boundary(timestamp="t1", pre_tokens=170000, post_tokens=10000),
        _compact_boundary(timestamp="t2", pre_tokens=165000, post_tokens=8000),
        _compact_boundary(timestamp="t3", pre_tokens=160000, post_tokens=6000),
        _assistant_text("finally done"),
    ])

    events = _extract_compact_events_from_jsonl(sid)

    assert len(events) == 3
    assert [e.timestamp for e in events] == ["t1", "t2", "t3"]
    assert events[0].pre_tokens == 170000
    assert events[2].pre_tokens == 160000


def test_compact_extract_handles_missing_compact_metadata(jsonl_dir):
    """Defensive: if a future Claude Code version omits compactMetadata
    or sets it to null, we capture the event with None fields rather
    than crashing."""
    sid = "extract-missing-meta"
    line_no_meta = json.dumps({
        "type": "system",
        "subtype": "compact_boundary",
        "timestamp": "2026-05-04T13:00:00Z",
        # no compactMetadata
    })
    line_null_meta = json.dumps({
        "type": "system",
        "subtype": "compact_boundary",
        "timestamp": "2026-05-04T13:00:01Z",
        "compactMetadata": None,
    })
    _write_jsonl(jsonl_dir, sid, [line_no_meta, line_null_meta])

    events = _extract_compact_events_from_jsonl(sid)

    assert len(events) == 2
    for ev in events:
        assert ev.pre_tokens is None
        assert ev.trigger is None


def test_compact_extract_skips_malformed_lines(jsonl_dir):
    """Tail-truncated JSONLs must not abort the extract."""
    sid = "extract-malformed"
    _write_jsonl(jsonl_dir, sid, [
        _compact_boundary(timestamp="t1"),
        '{"type": "system", "subtype":',  # truncated
        _compact_boundary(timestamp="t2"),
    ])

    events = _extract_compact_events_from_jsonl(sid)

    assert len(events) == 2
    assert events[0].timestamp == "t1"
    assert events[1].timestamp == "t2"
