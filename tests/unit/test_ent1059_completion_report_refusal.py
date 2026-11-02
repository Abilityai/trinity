"""
A refused turn's completion report reads in plain words (trinity-enterprise#1059, re-review N1).

An admission policy closes a background turn SKIPPED with ``[usage_share] <sentence>``
on the row, and the terminal hands that text to the completion report. The
code is for machines (``_split_error_code`` reads it back into ``error_code``);
the person reading a Workspace thread, a Slack channel or a Telegram chat gets
the sentence. A ``[...]`` that does not name a known code, and anything on a
success report, is the agent's own text and is left alone.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import channel_completion_report as ccr  # noqa: E402
from services.execution_envelope import TaskExecutionErrorCode  # noqa: E402

pytestmark = pytest.mark.unit

SENTENCE = ("finance has used 15 of its 15 usage budget for the last 5 hours, so every "
            "door stops until the share is raised or the window frees at 2026-11-02 16:00 UTC.")
# The shape `_admission_policy_gate` stores on the row.
STORED = f"[{TaskExecutionErrorCode.USAGE_SHARE.value}] {SENTENCE}"


def test_the_workspace_report_shows_the_sentence_without_the_code():
    body = ccr._portal_body(executing_agent="finance", session_agent="finance",
                            status="skipped", summary_or_error=STORED)
    assert body == f"**Didn't finish** — skipped\n\n{SENTENCE}"


def test_the_channel_report_shows_the_sentence_without_the_code():
    text = ccr._summarize("skipped", STORED)
    assert text == f"⚠️ Task ended with status `skipped`\n\n{SENTENCE}"
    assert "[usage_share]" not in ccr._summarize("skipped", STORED, executing_agent="finance")


def test_an_unknown_bracket_is_the_agents_own_text():
    text = "[draft] the report is half written"
    assert ccr._portal_body(executing_agent="a", session_agent="a", status="failed",
                            summary_or_error=text).endswith(text)
    assert ccr._summarize("failed", text).endswith(text)


def test_a_success_report_is_never_rewritten():
    text = "[usage_share] is the column I renamed"
    assert ccr._summarize("success", text).endswith(text)
    assert ccr._portal_body(executing_agent="a", session_agent="a", status="success",
                            summary_or_error=text).endswith(text)
