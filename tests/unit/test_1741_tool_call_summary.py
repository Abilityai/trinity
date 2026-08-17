"""`tool_calls` is a summary, not a second copy of the transcript (#1741).

`task_execution_service.apply_result` assigned `tool_calls_json =
execution_log_json` — the whole Claude Code stream-json transcript, verbatim,
into a column named `tool_calls`. Three consequences, all verified on a live
scheduled run before the fix:

1. Every consumer looks for `{"tool": …}` / `{"name": …}` entries, but tool uses
   are nested inside `assistant` events' `message.content`. So
   `get_agent_schedules_summary` counted **0 of 30** entries for a run that made
   4 real tool calls, and the execution-detail "Tool Calls" panel (which filters
   `type === 'tool_use'`) rendered empty. A confident wrong number, not a blank.
2. `prune_execution_logs` nulls only `execution_log`, so the copy outlived the
   transcript it duplicates by the gap between the log window (30d) and the row
   window (90d).
3. Every execution stored its transcript twice.

The chat path was already correct because `/api/chat` returns
`execution_log_simplified`; `/api/task` does not, which is why the summary is
derived backend-side (works on every agent image, no rebuild).
"""
from __future__ import annotations

import json

import pytest

from services.tool_call_summary import (
    extract_tool_calls,
    looks_like_raw_transcript,
    summarize_tool_calls_json,
)


def _raw_transcript() -> list:
    """The real shape observed on a live run: envelope events, with tool uses
    nested one level down inside assistant messages."""
    return [
        {"type": "system", "subtype": "init", "tools": ["Bash", "Read"], "cwd": "/home/developer"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me look that up."},
            {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://example.com"}},
        ]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "…"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__trinity__report", "input": {"title": "x"}},
        ]}},
        {"type": "rate_limit_event"},
        {"type": "result", "subtype": "success"},
    ]


# --- extraction --------------------------------------------------------------

def test_extracts_nested_tool_uses_the_old_scan_missed():
    calls = extract_tool_calls(_raw_transcript())
    assert [c["tool"] for c in calls] == ["WebFetch", "mcp__trinity__report"]
    assert all(c["type"] == "tool_use" for c in calls)

    # The old top-level scan is what read 0 — prove it on the same input.
    old_scan = sum(
        1 for e in _raw_transcript()
        if isinstance(e, dict) and (e.get("name") or e.get("tool"))
    )
    assert old_scan == 0


def test_output_is_shaped_for_both_consumers():
    """The analytics rollup counts `tool`/`name`; the execution-detail panel
    filters `type === 'tool_use'` and renders `tool` + `input`."""
    call = extract_tool_calls(_raw_transcript())[0]
    assert call["type"] == "tool_use"      # UI filter
    assert call["tool"] == "WebFetch"      # both
    assert call["input"] == {"url": "https://example.com"}


def test_already_simplified_entries_pass_through():
    """`/api/chat` sends `execution_log_simplified` already in this shape, and a
    future `/api/task` may too — it must not be mangled."""
    simplified = [
        {"type": "tool_use", "tool": "Bash", "input": {"cmd": "ls"},
         "success": True, "duration_ms": 12},
        {"type": "tool_result", "tool": "Bash", "output": "a b c"},
    ]
    calls = extract_tool_calls(simplified)
    assert [c["tool"] for c in calls] == ["Bash"]
    assert calls[0]["success"] is True and calls[0]["duration_ms"] == 12


def test_malformed_input_never_raises():
    """A bad log must not fail an already-billed execution."""
    for bad in (None, "not a list", 42, [None, "x", {"type": "assistant"}], [{"message": "nope"}]):
        assert extract_tool_calls(bad) == []


def test_tool_use_without_a_name_is_skipped():
    assert extract_tool_calls(
        [{"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}]
    ) == []


def test_call_count_is_bounded():
    from services.tool_call_summary import MAX_TOOL_CALLS
    huge = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": f"T{i}", "input": {}} for i in range(MAX_TOOL_CALLS + 50)
    ]}}]
    assert len(extract_tool_calls(huge)) == MAX_TOOL_CALLS


def test_large_inputs_are_truncated_not_dropped():
    from services.tool_call_summary import MAX_INPUT_CHARS
    big = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"cmd": "x" * (MAX_INPUT_CHARS * 2)}},
    ]}}
    call = extract_tool_calls([big])[0]
    assert call["tool"] == "Bash"
    assert "_truncated" in call["input"]
    assert len(json.dumps(call["input"])) < MAX_INPUT_CHARS + 200


# --- legacy detection + conversion ------------------------------------------

def test_raw_transcript_is_recognised():
    assert looks_like_raw_transcript(_raw_transcript()) is True


def test_a_summary_is_not_mistaken_for_a_transcript():
    summary = extract_tool_calls(_raw_transcript())
    assert looks_like_raw_transcript(summary) is False


def test_conversion_is_idempotent():
    raw_json = json.dumps(_raw_transcript())
    once = summarize_tool_calls_json(raw_json)
    twice = summarize_tool_calls_json(once)
    assert once == twice
    assert [c["tool"] for c in json.loads(once)] == ["WebFetch", "mcp__trinity__report"]


def test_conversion_shrinks_the_stored_blob():
    """The whole point: the column stopped being a transcript copy."""
    raw_json = json.dumps(_raw_transcript())
    assert len(summarize_tool_calls_json(raw_json)) < len(raw_json)


def test_transcript_with_no_tool_uses_becomes_none():
    raw = json.dumps([{"type": "system", "subtype": "init"}, {"type": "result"}])
    assert summarize_tool_calls_json(raw) is None


def test_unparseable_or_empty_input_is_safe():
    assert summarize_tool_calls_json(None) is None
    assert summarize_tool_calls_json("") is None
    assert summarize_tool_calls_json("{not json") is None


# --- retention coupling ------------------------------------------------------

def test_log_prune_nulls_tool_calls_too():
    """A copy of the transcript must not outlive the transcript."""
    import inspect
    from db.schedules import retention

    src = inspect.getsource(retention.ScheduleRetentionMixin.prune_execution_logs)
    assert "tool_calls=None" in src, (
        "prune_execution_logs must null tool_calls alongside execution_log — "
        "otherwise the derived copy survives the log-retention window (#1741)"
    )


def test_prune_predicate_reaches_rows_whose_log_was_already_nulled():
    """Gating on `execution_log IS NOT NULL` alone would permanently strand the
    `tool_calls` copies on rows a previous sweep already nulled — exactly the
    rows where the copy most obviously outlived what it duplicates."""
    import inspect
    from db.schedules import retention

    src = inspect.getsource(retention._execution_log_prune_predicate)
    assert "tool_calls" in src and "or_(" in src
