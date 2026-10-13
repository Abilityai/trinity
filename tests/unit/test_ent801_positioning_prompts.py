"""abilityai/trinity-enterprise#801 — runtime prompts carry the ADR-0011 one-liner.

The platform prompt every agent receives each turn, its dead reference copy and
the system-agent seed must not describe Trinity with the retired taglines.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.platform_prompt_service import get_platform_system_prompt

REPO_ROOT = Path(__file__).resolve().parents[2]
META_PROMPT_PATH = REPO_ROOT / "config" / "trinity-meta-prompt" / "prompt.md"
SYSTEM_AGENT_SEED = REPO_ROOT / "config" / "agent-templates" / "trinity-system" / "CLAUDE.md"

ONE_LINER = "This agent runs on Trinity, the operating system for AI-native companies."
OLD_PHRASES = (
    "Deep Agent Orchestration Platform",
    "autonomous AI system capable of independent reasoning",
    "the Trinity autonomous agent platform",
)


def _surfaces():
    return {
        "platform_prompt": get_platform_system_prompt("claude-code"),
        "meta_prompt": META_PROMPT_PATH.read_text(encoding="utf-8"),
        "system_agent_seed": SYSTEM_AGENT_SEED.read_text(encoding="utf-8"),
    }


@pytest.mark.parametrize("surface", ["platform_prompt", "meta_prompt", "system_agent_seed"])
def test_old_taglines_absent(surface):
    text = _surfaces()[surface].lower()
    assert [p for p in OLD_PHRASES if p.lower() in text] == []


@pytest.mark.parametrize("surface", ["platform_prompt", "meta_prompt"])
def test_one_liner_present(surface):
    assert ONE_LINER in _surfaces()[surface]
