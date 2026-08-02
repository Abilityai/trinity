"""Static safety contract for the generic delivery-conductor template."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml


ALLOWED_CAPABILITIES = {"events", "reminders", "executions", "chat"}
ALLOWED_EFFECT_TOOL = "mcp__trinity__chat_with_agent"
FORBIDDEN_PRODUCT_LABELS = ("ar-trans", "ar_trans", "jira", "linear")
FORBIDDEN_CREDENTIAL_NAMES = re.compile(
    r"\b(?:github|gh(?:ub)?[_-]?(?:token|pat|key)|ghp_[a-z0-9]+|"
    r"deploy(?:ment)?[_-]?(?:token|key)|"
    r"(?:token|key)[_-]?(?:github|deploy(?:ment)?))\b",
    re.IGNORECASE,
)
FORBIDDEN_NETWORK_CLIENTS = re.compile(
    r"\b(?:requests|urllib|httpx|aiohttp|http\.client|socket|curl|wget)\b",
    re.IGNORECASE,
)
FORBIDDEN_SHELL_SUBPROCESSES = re.compile(
    r"\b(?:subprocess|os\.system|shell\s*=\s*true|(?:ba|z)?sh\s+-c)\b",
    re.IGNORECASE,
)


def _template_files(template_root: Path) -> list[Path]:
    return sorted(path for path in template_root.rglob("*") if path.is_file())


def _assert_at_most_one_proposed_effect(conductor: dict) -> None:
    proposed_effects = conductor.get("proposed_effects", [])

    assert isinstance(proposed_effects, list), "proposed effects must be a list"
    assert len(proposed_effects) <= 1, "at most one proposed effect is allowed"


def _assert_no_forbidden_surface(source: str) -> None:
    lowered = source.lower()

    assert not [label for label in FORBIDDEN_PRODUCT_LABELS if label in lowered]
    assert not FORBIDDEN_CREDENTIAL_NAMES.search(source)
    assert not FORBIDDEN_NETWORK_CLIENTS.search(source)
    assert not FORBIDDEN_SHELL_SUBPROCESSES.search(source)


def test_template_metadata_is_hidden_generic_and_tokenless(template_root: Path):
    """The bundled scaffold stays an unlisted, neutral source-mode template."""
    metadata = yaml.safe_load((template_root / "template.yaml").read_text())

    assert metadata["name"] == "delivery-conductor"
    assert metadata["hidden"] is True
    assert metadata["data_paths"] == ["data/**"]
    assert set(metadata["capabilities"]) == ALLOWED_CAPABILITIES
    assert not metadata.get("schedules")
    assert metadata["credentials"] == {}
    assert metadata["mcp_servers"] == []


def test_template_contract_has_one_named_effect_tool(template_root: Path):
    """A model turn may hand off no more than one action through one tool."""
    metadata = yaml.safe_load((template_root / "template.yaml").read_text())
    effect_tools = metadata["conductor"]["allowed_effect_tools"]
    instructions = (template_root / "CLAUDE.md").read_text()

    assert effect_tools == [ALLOWED_EFFECT_TOOL]
    _assert_at_most_one_proposed_effect(metadata["conductor"])
    assert f"Named allowed effect tool: `{ALLOWED_EFFECT_TOOL}`" in instructions
    assert "Run exactly one tick" in instructions
    assert "Record only the sanitized result" in instructions
    assert "Stop the turn" in instructions


def test_template_contract_rejects_multiple_proposed_effects(template_root: Path):
    """A future multi-effect contract must fail before an executor sees it."""
    metadata = yaml.safe_load((template_root / "template.yaml").read_text())
    conductor = dict(metadata["conductor"])
    conductor["proposed_effects"] = [{"action_key": "one"}, {"action_key": "two"}]

    with pytest.raises(AssertionError, match="at most one proposed effect"):
        _assert_at_most_one_proposed_effect(conductor)


def test_template_mcp_configuration_is_empty_and_valid_json(template_root: Path):
    """Platform capability injection needs no repository credential or client."""
    mcp_config = json.loads((template_root / ".mcp.json.template").read_text())

    assert mcp_config == {"mcpServers": {}}


def test_template_rejects_product_credentials_network_and_shell_escape_hatches(
    template_root: Path,
):
    """The generic scaffold never carries product or ambient execution authority."""
    source = "\n".join(path.read_text() for path in _template_files(template_root))

    _assert_no_forbidden_surface(source)


@pytest.mark.parametrize("source", ("GH_TOKEN", "http.client.HTTPConnection"))
def test_forbidden_surface_mutations_are_rejected(source: str):
    """GitHub token aliases and stdlib network clients stay outside the scaffold."""
    with pytest.raises(AssertionError):
        _assert_no_forbidden_surface(source)


def test_tick_launcher_is_executable_and_uses_the_local_library(template_root: Path):
    """The launcher stays in the workspace and invokes the future CLI module."""
    launcher = template_root / "bin" / "conductor-tick"
    source = launcher.read_text()

    assert launcher.stat().st_mode & 0o111
    assert "WORKSPACE_ROOT=/home/developer" in source
    assert "PYTHONPATH" in source
    assert "exec python -m delivery_conductor.cli" in source
