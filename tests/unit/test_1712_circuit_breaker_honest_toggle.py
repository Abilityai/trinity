"""#1712 — the per-agent circuit-breaker toggle must tell the honest truth.

The dispatch breaker is two-tier: the per-agent `circuit_breaker_enabled` opt-in
AND the platform-wide `DISPATCH_BREAKER_ENABLED` master switch must BOTH be on.
The endpoint already returns `config.global_enabled` (GET) and a `warning` (PUT)
when a toggle is inert. The residual (#1712) was the frontend: it reported
success and said nothing. `ReliabilityPanel.vue` is the honest owner-facing
control; these static guards fail if it loses the honesty again.

Static, like the #1709 UI-caller guard — a passing endpoint contract can't catch
a UI that ignores it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_FE = Path(__file__).resolve().parents[2] / "src" / "frontend" / "src"
_PANEL = _FE / "components" / "ReliabilityPanel.vue"
_SETTINGS = _FE / "components" / "settings" / "SettingsPanel.vue"


@pytest.fixture(scope="module")
def panel_src() -> str:
    assert _PANEL.exists(), f"ReliabilityPanel.vue not found at {_PANEL}"
    return _PANEL.read_text(encoding="utf-8")


def test_panel_consumes_global_enabled_not_inferred(panel_src):
    """AC: the frontend consumes `config.global_enabled` from the endpoint rather
    than inferring the state."""
    assert "global_enabled" in panel_src, (
        "ReliabilityPanel must read config.global_enabled from the endpoint — "
        "otherwise it can't tell the owner the breaker is globally disabled (#1712)."
    )


def test_panel_surfaces_the_endpoint_warning(panel_src):
    """AC: it does not silently accept and report success — the PUT's `warning`
    (inert-while-global-off) is surfaced as the outcome."""
    assert "warning" in panel_src, (
        "ReliabilityPanel must surface the PUT response's `warning` field so a "
        "save made while the global flag is off isn't reported as plain success."
    )


def test_panel_names_the_reason_and_remedy(panel_src):
    """AC: the UI names the reason (the global flag) and the remedy (an admin sets
    it) — not only a disabled control."""
    assert "DISPATCH_BREAKER_ENABLED" in panel_src, (
        "ReliabilityPanel must name DISPATCH_BREAKER_ENABLED (the reason) and that "
        "an admin sets it (the remedy) — a disabled toggle with no explanation is "
        "the #1712 complaint."
    )
    assert "admin" in panel_src.lower()


def test_panel_calls_the_circuit_breaker_endpoint(panel_src):
    """The toggle drives the real endpoint (both GET to read state and PUT to set)."""
    assert "/circuit-breaker" in panel_src


def test_panel_is_wired_into_the_settings_tab():
    """A component nobody renders is as inert as the bug it fixes — the panel must
    be mounted in the agent Settings tab."""
    settings = _SETTINGS.read_text(encoding="utf-8")
    assert "ReliabilityPanel" in settings, (
        "SettingsPanel.vue must render ReliabilityPanel — otherwise the honest "
        "circuit-breaker toggle has no home (#1712)."
    )
