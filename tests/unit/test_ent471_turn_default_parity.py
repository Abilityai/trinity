"""The default turn cap is one value in four places (trinity-enterprise#471).

The value an agent actually runs with comes from the image's
`guardrails-baseline.json`. `_runtime_config.py` carries hand-maintained
fallbacks for when no config file loads, and the Settings → Guardrails panel
shows the default as its placeholder. Nothing tied them together, so a raise
landed in one and not the others: this pins all of them to the baseline and the
baseline to 200, inside the unchanged 1–500 bounds.

The agent server and the panel are read as source (AST / a literal match), not
imported: the agent server is its own image and the panel is a Vue file.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BASELINE = _REPO / "docker/base-image/hooks/guardrails-baseline.json"
_RUNTIME_CONFIG = _REPO / "docker/base-image/agent_server/services/_runtime_config.py"
_PANEL = _REPO / "src/frontend/src/components/GuardrailsPanel.vue"

DEFAULT = 200


def _baseline():
    return json.loads(_BASELINE.read_text())


def _module_constants(path: Path) -> dict:
    out = {}
    for node in ast.parse(path.read_text()).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)):
            out[node.targets[0].id] = node.value.value
    return out


def _panel_const(name: str) -> int:
    m = re.search(rf"^const {name} = (\d+)\s*$", _PANEL.read_text(), re.M)
    assert m, f"GuardrailsPanel.vue no longer declares `const {name} = <n>`"
    return int(m.group(1))


def test_the_baseline_default_is_200_for_chat_and_task():
    b = _baseline()
    assert (b["max_turns_chat"], b["max_turns_task"]) == (DEFAULT, DEFAULT)


def test_the_hardcoded_fallbacks_match_the_baseline():
    b, c = _baseline(), _module_constants(_RUNTIME_CONFIG)
    assert c["_DEFAULT_MAX_TURNS_CHAT"] == b["max_turns_chat"]
    assert c["_DEFAULT_MAX_TURNS_TASK"] == b["max_turns_task"]


def test_the_settings_placeholder_shows_the_baseline_default():
    b = _baseline()
    assert _panel_const("DEFAULT_TURNS") == b["max_turns_chat"] == b["max_turns_task"]


def test_the_bounds_are_unchanged_and_hold_the_default():
    assert (_panel_const("MIN_TURNS"), _panel_const("MAX_TURNS")) == (1, 500)
    assert 1 <= DEFAULT <= 500
