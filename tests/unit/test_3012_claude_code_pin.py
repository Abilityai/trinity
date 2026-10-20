"""#3012 — the base image pins Claude Code, and the pin satisfies the catalog.

`@anthropic-ai/claude-code@latest` in a RUN line is resolved once and then
served from Docker's layer cache by every rebuild, so a rebuilt image kept
2.1.278 while `claude-opus-5-5` needs >= 2.1.280. The pin makes a bump a real
rebuild; the catalog check makes a model that needs a newer CLI fail CI until
the pin moves with it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.model_catalog import MODEL_CATALOG

pytestmark = pytest.mark.unit

_DOCKERFILE = Path(__file__).resolve().parents[2] / "docker" / "base-image" / "Dockerfile"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def _pinned_version() -> str:
    match = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", _DOCKERFILE.read_text(), re.M)
    assert match, "docker/base-image/Dockerfile must declare ARG CLAUDE_CODE_VERSION"
    return match.group(1)


def test_claude_code_is_pinned_to_an_exact_version():
    text = _DOCKERFILE.read_text()
    assert _SEMVER.match(_pinned_version())
    assert '"@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"' in text
    assert "claude-code@latest" not in text


@pytest.mark.parametrize(
    "entry", [m for m in MODEL_CATALOG if m.min_claude_code], ids=lambda m: m.id
)
def test_pinned_version_satisfies_every_catalog_minimum(entry):
    assert _SEMVER.match(entry.min_claude_code)
    assert _version_tuple(_pinned_version()) >= _version_tuple(entry.min_claude_code), (
        f"{entry.id} needs Claude Code >= {entry.min_claude_code}; bump "
        f"CLAUDE_CODE_VERSION in docker/base-image/Dockerfile"
    )


def test_opus_5_5_declares_its_minimum():
    opus = next(m for m in MODEL_CATALOG if m.id == "claude-opus-5-5")
    assert opus.min_claude_code == "2.1.280"
