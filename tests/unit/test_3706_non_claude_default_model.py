"""A Codex or Gemini agent is never handed the platform default model.

The platform default (`platform_default_model`) is a Claude model id. Filling it
in for every turn gave a Codex agent `-m claude-sonnet-4-6`, which OpenAI
answers with a 404, so every turn failed. A non-Claude agent with no explicit
model runs on its own (`AGENT_RUNTIME_MODEL` from the template, or the CLI
default).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ent403_workspace_model import _platform_default, svc  # noqa: E402,F401

DEFAULT = "claude-sonnet-4-6"


def _runtime(monkeypatch, runtime):
    import services.docker_service as ds
    monkeypatch.setattr(ds, "get_agent_runtime", lambda name: runtime)


def _no_override(monkeypatch):
    import database
    monkeypatch.setattr(database.db, "get_public_channel_model", lambda a: None, raising=False)


@pytest.mark.parametrize("runtime", ["codex", "gemini-cli"])
def test_workspace_turn_on_a_non_claude_agent_gets_no_default(svc, monkeypatch, runtime):
    _platform_default(monkeypatch, DEFAULT)
    _no_override(monkeypatch)
    _runtime(monkeypatch, runtime)
    assert svc.resolve_turn_model("codexing", None) is None


def test_workspace_turn_on_a_claude_agent_keeps_the_default(svc, monkeypatch):
    _platform_default(monkeypatch, DEFAULT)
    _no_override(monkeypatch)
    _runtime(monkeypatch, "claude-code")
    assert svc.resolve_turn_model("cornelius", None) == DEFAULT


@pytest.mark.parametrize("runtime,expected", [
    ("codex", None), ("gemini-cli", None), ("claude-code", DEFAULT),
])
def test_execute_task_default_follows_the_runtime(monkeypatch, runtime, expected):
    from services import task_execution_service as tes
    monkeypatch.setattr(tes, "_resolve_agent_runtime", lambda name: runtime)
    monkeypatch.setattr(tes.settings_service, "get_platform_default_model", lambda: DEFAULT)
    assert tes._default_model_for("agent-x") == expected
