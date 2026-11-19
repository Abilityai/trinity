"""An OpenCode agent gets its own platform default model, never the Claude one.

The fleet default (`platform_default_model`) is a bare Claude id, which OpenCode
refuses with "missing a provider prefix". OpenCode reads
`platform_default_model_opencode` (a `provider/model` id) instead, and with that
unset no model is sent, so the agent's own default applies (#2981, ported from
Abilityai/lilu#652).
"""
from __future__ import annotations

import types

import pytest

CLAUDE_DEFAULT = "claude-sonnet-4-6"


def _settings(monkeypatch, rows):
    import services.settings_service as ss

    monkeypatch.setattr(
        ss, "db",
        types.SimpleNamespace(get_setting_value=lambda key, default=None: rows.get(key, default)),
    )
    monkeypatch.setattr(ss, "_platform_model_cache", None)
    monkeypatch.setattr(ss, "_platform_model_cache_ts", 0.0)
    return ss.SettingsService()


def test_platform_default_is_runtime_aware_for_opencode(monkeypatch):
    svc = _settings(monkeypatch, {
        "platform_default_model": "claude-opus-5",
        "platform_default_model_opencode": "ollama/gemma4:12b",
    })
    assert svc.get_platform_default_model() == "claude-opus-5"
    assert svc.get_platform_default_model("claude-code") == "claude-opus-5"
    assert svc.get_platform_default_model(runtime="opencode") == "ollama/gemma4:12b"


@pytest.mark.parametrize("stored", [None, "", "   "])
def test_opencode_has_no_code_default(monkeypatch, stored):
    """Unset or blank → None: never the Claude fleet default, never a hardcoded id."""
    rows = {"platform_default_model": "claude-opus-5"}
    if stored is not None:
        rows["platform_default_model_opencode"] = stored
    svc = _settings(monkeypatch, rows)
    assert svc.get_platform_default_model(runtime="opencode") is None


def test_opencode_write_applies_without_cache_invalidation(monkeypatch):
    rows = {}
    svc = _settings(monkeypatch, rows)
    assert svc.get_platform_default_model(runtime="opencode") is None
    rows["platform_default_model_opencode"] = "ollama/qwen3.6:27b"
    assert svc.get_platform_default_model(runtime="opencode") == "ollama/qwen3.6:27b"


def _dispatch(monkeypatch, runtime, opencode_default):
    from services import task_execution_service as tes

    monkeypatch.setattr(tes, "_resolve_agent_runtime", lambda name: runtime)
    monkeypatch.setattr(
        tes.settings_service, "get_platform_default_model",
        lambda runtime=None: opencode_default if runtime == "opencode" else CLAUDE_DEFAULT,
    )
    return tes


def test_execute_task_default_on_opencode_is_its_own_default(monkeypatch):
    tes = _dispatch(monkeypatch, "opencode", "ollama/gemma4:12b")
    assert tes._default_model_for("oc") == "ollama/gemma4:12b"


def test_execute_task_on_an_unset_opencode_agent_sends_no_model(monkeypatch):
    """The regression: this returned the Claude id, so every backend-dispatched
    turn on an OpenCode agent without `runtime.model` was refused."""
    tes = _dispatch(monkeypatch, "opencode", None)
    assert tes._default_model_for("oc") is None


@pytest.mark.parametrize("runtime,expected", [
    ("claude-code", CLAUDE_DEFAULT), ("codex", None), ("gemini-cli", None),
])
def test_other_runtimes_are_unchanged(monkeypatch, runtime, expected):
    tes = _dispatch(monkeypatch, runtime, "ollama/gemma4:12b")
    assert tes._default_model_for("agent-x") == expected
