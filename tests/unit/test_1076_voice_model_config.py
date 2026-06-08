"""Unit regression test for #1076 — VOICE_MODEL empty-coalesce.

Bug: `os.getenv("VOICE_MODEL", default)` returns the default ONLY when the
var is *unset*. Compose injected `VOICE_MODEL=${VOICE_MODEL:-}` and
`.env.example` shipped `VOICE_MODEL=`, both of which arrive as a *set-but-empty*
string that shadowed the default and sent `model=""` to Gemini Live
("model is required" → every voice path DOA on a stock deploy).

Fix (src/backend/config.py):
    VOICE_MODEL = os.getenv("VOICE_MODEL") or "models/gemini-3.1-flash-live-preview"

These tests load the REAL config.py module fresh under a controlled
environment and assert the resolved `VOICE_MODEL` for the three input
states: unset, set-but-empty, and a genuine override. No network, no Redis
(the tests/unit/ conftest overrides the parent's backend-dependent autouse
fixtures) — a valid dummy REDIS_URL is supplied solely to satisfy config.py's
import-time credential guard (Issue #589).

Lives under tests/unit/ so the CI unit job (`cd tests && pytest unit/`)
actually collects it — the #1076 regression guard must run where the bug
would regress.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# tests/unit/<this file> → parent=tests/unit, .parent=tests, .parent=repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_PATH = _REPO_ROOT / "src" / "backend" / "config.py"
_EXPECTED_DEFAULT = "models/gemini-3.1-flash-live-preview"


def _load_config_with_env(monkeypatch, env: dict[str, str | None]):
    """Exec src/backend/config.py fresh under `env` and return the module.

    Each call gets a uniquely-named throwaway module so we never touch the
    process-wide `sys.modules["config"]` other tests may rely on, and so the
    import-time `os.getenv` reads see exactly the env we set here.
    """
    # config.py's only hard import-time requirement is a credentialed REDIS_URL.
    monkeypatch.setenv("REDIS_URL", "redis://backend:devpassword@localhost:6379/0")
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location(
        f"_config_under_test_{abs(hash(frozenset(env.items())))}", str(_CONFIG_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_voice_model_unset_uses_default(monkeypatch):
    """Var entirely unset → built-in default (the os.getenv-default path)."""
    cfg = _load_config_with_env(monkeypatch, {"VOICE_MODEL": None})
    assert cfg.VOICE_MODEL == _EXPECTED_DEFAULT


def test_voice_model_empty_string_coalesces_to_default(monkeypatch):
    """THE #1076 bug: set-but-empty must NOT shadow the default."""
    cfg = _load_config_with_env(monkeypatch, {"VOICE_MODEL": ""})
    assert cfg.VOICE_MODEL == _EXPECTED_DEFAULT, (
        "Empty VOICE_MODEL shadowed the default — regression of #1076; "
        "this sends model=\"\" to Gemini Live and kills every voice path."
    )


def test_voice_model_override_is_respected(monkeypatch):
    """A genuine operator override must still flow through untouched."""
    cfg = _load_config_with_env(
        monkeypatch, {"VOICE_MODEL": "models/some-other-live-model"}
    )
    assert cfg.VOICE_MODEL == "models/some-other-live-model"


@pytest.mark.parametrize(
    "compose_file",
    ["docker-compose.yml", "docker-compose.prod.yml"],
)
def test_compose_default_is_non_empty(compose_file):
    """Defense-in-depth: compose must not inject an empty VOICE_MODEL.

    Guards against a future revert of the compose change re-introducing the
    `${VOICE_MODEL:-}` set-but-empty injection the code coalesce defends against.
    """
    text = (_REPO_ROOT / compose_file).read_text()
    voice_lines = [
        ln
        for ln in text.splitlines()
        if "VOICE_MODEL=${VOICE_MODEL:-" in ln and not ln.strip().startswith("#")
    ]
    assert voice_lines, f"{compose_file}: VOICE_MODEL injection line not found"
    for ln in voice_lines:
        assert "${VOICE_MODEL:-}" not in ln, (
            f"{compose_file}: VOICE_MODEL injects an empty default — "
            f"regression of #1076. Use ${{VOICE_MODEL:-{_EXPECTED_DEFAULT}}}."
        )
        assert _EXPECTED_DEFAULT in ln, (
            f"{compose_file}: compose default drifted from config.py "
            f"({_EXPECTED_DEFAULT})."
        )
