"""
trinity-enterprise#620 — the agent server's `get_input_summary` and the
frontend's `summariseToolInput` (a port) must agree, case by case.

The heartbeat line is summarised in the container; the chat's own turn is
summarised in the browser from the same raw frame. The Work card renders
both, so the two must produce the same text for the same call. The cases
live in `tests/fixtures/tool_input_summary.json`; the vitest twin is
`src/frontend/tests/unit/workActivity.spec.js`.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO / "tests" / "fixtures" / "tool_input_summary.json"
_HELPERS = _REPO / "docker" / "base-image" / "agent_server" / "utils" / "helpers.py"


def _load_helpers():
    spec = importlib.util.spec_from_file_location("agent_server_helpers_620", _HELPERS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # stdlib-only module
    return mod


CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[f"{c['tool']}:{c['summary'][:20]}" for c in CASES])
def test_agent_summary_matches_the_fixture(case):
    helpers = _load_helpers()
    assert helpers.get_input_summary(case["tool"], case["input"]) == case["summary"]


def test_the_fixture_covers_every_tool_the_agent_names():
    """A new branch in `get_input_summary` without a fixture case is a branch
    the JS port can silently diverge on."""
    src = _HELPERS.read_text(encoding="utf-8")
    body = src[src.index("def get_input_summary"):]
    body = body[: body.index("\ndef ")] if "\ndef " in body else body
    import re
    named = set(re.findall(r'tool == "([A-Za-z]+)"', body))
    covered = {c["tool"] for c in CASES}
    assert named <= covered, f"fixture is missing {sorted(named - covered)}"
