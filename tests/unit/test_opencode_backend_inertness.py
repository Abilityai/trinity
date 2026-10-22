"""Backend inertness for OpenCode-shaped failures (#2981, mirrors
test_codex_backend_inertness).

The #678 stdout reader-race auto-retry fires only on a Claude-specific 502 dict
body (recovery_attempted + raw_message_count==0 + parse_failure_count==0 +
num_turns<5 + "result message"). An OpenCode early exit or pipe drop carries a
plain string detail or a dict without those markers, so the signature check
must return False — no spurious same-execution_id retry.
"""

from __future__ import annotations

from services.task_execution_service import _is_reader_race_signature


def test_reader_race_inert_for_opencode_pipe_drop_string_detail():
    assert _is_reader_race_signature(
        "Agent subprocess closed before task could complete"
    ) is False
    assert _is_reader_race_signature(
        "OpenCode execution failed (exit code 1): stream closed"
    ) is False


def test_reader_race_inert_for_opencode_dict_without_recovery_marker():
    opencode_like = {
        "message": "OpenCode execution failed (exit code 1): no step_finish",
        "raw_message_count": 0,
        "parse_failure_count": 0,
        "metadata": {"num_turns": 1},
    }
    assert _is_reader_race_signature(opencode_like) is False


def test_reader_race_still_fires_for_genuine_claude_signature():
    claude_sig = {
        "recovery_attempted": True,
        "raw_message_count": 0,
        "parse_failure_count": 0,
        "metadata": {"num_turns": 1},
        "message": "No result message received from Claude Code",
    }
    assert _is_reader_race_signature(claude_sig) is True
