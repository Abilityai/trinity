"""Unit tests for #1410 — a scheduled slash-command referencing a missing skill
must not silently record as success.

When a scheduled/triggered `/foo` does not resolve to an installed skill, the
agent runtime (Claude Code) replies with a normal, successful assistant turn
("Unknown command: /foo"). Left as SUCCESS + $0 this blends into legitimate
skipped/$0 runs and a dead agent function stays invisible. `execute_task` now
detects that specific shape and finalizes the execution as FAILED
(``SKILL_NOT_FOUND``).

These are pure unit tests over the module-level detector — no backend needed.

Module under test:
    src/backend/services/task_execution_service.py::detect_unresolved_slash_command
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

from services.task_execution_service import (  # noqa: E402
    TaskExecutionErrorCode,
    detect_unresolved_slash_command,
)


# ---------------------------------------------------------------------------
# Positive: slash-command sent + runtime unknown-command reply -> offending cmd
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "message,response,expected",
    [
        ("/generate", "Unknown command: /generate", "/generate"),
        ("/generate weekly", "Unknown command: /generate", "/generate"),
        ("  /generate  ", "Unknown command: /generate", "/generate"),
        # case-insensitive + "slash" variant + leading whitespace in the reply
        ("/foo", "unknown command: /foo", "/foo"),
        ("/foo", "Unknown slash command: /foo", "/foo"),
        ("/foo", "\n  Unknown command: /foo\n", "/foo"),
    ],
)
def test_detects_unresolved_command(message, response, expected):
    assert detect_unresolved_slash_command(message, response) == expected


# ---------------------------------------------------------------------------
# Negative: must NOT misclassify legitimate turns
# ---------------------------------------------------------------------------

def test_non_slash_message_never_matches():
    """A normal prose turn that merely echoes the phrase is not a broken
    slash-command invocation — the gate is the SENT message being a command."""
    assert (
        detect_unresolved_slash_command(
            "explain the error 'Unknown command: /x'",
            "Unknown command: /x",
        )
        is None
    )


def test_phrase_mid_response_not_matched():
    """Anchored at the start — a slash-command whose skill produced real output
    that happens to contain the phrase later is a genuine success."""
    assert (
        detect_unresolved_slash_command(
            "/generate",
            "Report generated. Note: an earlier draft said 'Unknown command'.",
        )
        is None
    )


def test_legit_success_response_not_matched():
    assert detect_unresolved_slash_command("/generate", "Report generated OK.") is None


@pytest.mark.parametrize(
    "message,response",
    [
        (None, "Unknown command: /x"),
        ("/x", None),
        ("", ""),
        ("/x", ""),
    ],
)
def test_missing_inputs_return_none(message, response):
    assert detect_unresolved_slash_command(message, response) is None


def test_skill_not_found_error_code_exists():
    assert TaskExecutionErrorCode.SKILL_NOT_FOUND.value == "skill_not_found"
