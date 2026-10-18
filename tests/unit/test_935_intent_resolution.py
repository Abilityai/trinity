"""Tier-0 intent resolution (trinity-enterprise#935, checkpoint A1).

`resolve_intent` decides what a run was asked to do before any check grades
it: a `/playbook args` invocation names a skill (the contract A2's
`playbook_outputs` reads), anything else is a plain message. The unresolved
case reuses #1410's detector rather than a second copy of its regex.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.tier0_evaluator import resolve_intent  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("message, ref, has_args", [
    ("/report-weekly", "report-weekly", False),
    ("/report-weekly --since 7d", "report-weekly", True),
    ("   /canon-publish\nwith a body", "canon-publish", True),
    ("/trinity:onboard now", "trinity:onboard", True),
])
def test_a_slash_command_names_its_playbook(message, ref, has_args):
    intent = resolve_intent(message)
    assert intent["kind"] == "playbook"
    assert intent["ref"] == ref
    assert intent["has_args"] is has_args
    assert intent["unresolved"] is False


@pytest.mark.parametrize("message", [
    "Summarise yesterday's leads",
    "/home/developer/notes.md is out of date, fix it",   # a path, not a skill
    "/",
    "/ report",
    "",
    None,
])
def test_anything_else_is_a_message(message):
    intent = resolve_intent(message)
    assert intent["kind"] == "message"
    assert intent["ref"] is None
    assert intent["unresolved"] is False


def test_an_unknown_command_reply_marks_the_playbook_unresolved():
    """#1410: a missing skill comes back as a successful $0 turn. Tier 0 must
    say the playbook did not resolve, not grade the reply as its output."""
    intent = resolve_intent("/gone-skill x", response="Unknown command: /gone-skill")
    assert intent == {"kind": "playbook", "ref": "gone-skill", "has_args": True,
                      "unresolved": True}


def test_the_phrase_in_a_prose_turn_is_not_an_unresolved_playbook():
    intent = resolve_intent("what does 'Unknown command' mean?",
                            response="Unknown command: /x is what Claude Code prints")
    assert intent["kind"] == "message" and intent["unresolved"] is False


def test_the_reference_is_bounded():
    intent = resolve_intent("/" + "a" * 500)
    assert intent["kind"] == "message"      # no real skill name is 500 chars
