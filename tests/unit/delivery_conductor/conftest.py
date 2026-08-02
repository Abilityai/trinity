"""Shared paths for delivery-conductor template contract tests."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def template_root() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "config"
        / "agent-templates"
        / "delivery-conductor"
    )
