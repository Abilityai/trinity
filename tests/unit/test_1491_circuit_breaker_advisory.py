"""#1491 — PUT /api/agents/{name}/circuit-breaker advisory when the global gate is off.

The dispatch breaker is two-tier gated (global ``DISPATCH_BREAKER_ENABLED`` AND
the per-agent toggle). Turning the per-agent toggle on while the global flag is
off saves the preference but does nothing; the PUT handler now returns a
non-fatal ``warning`` so the owner isn't caught by the "switch is on but nothing
happens" trap. The write always succeeds and the response is unchanged when the
global flag is on. Advisory only — no breaker-logic change.

Drives the handler directly with db + audit mocked, toggling
``config.DISPATCH_BREAKER_ENABLED`` (the handler imports it at call time).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _PROJECT_ROOT / "src" / "backend"
sys.path.insert(0, str(_BACKEND_DIR))

import config  # noqa: E402
import routers.agents as agents_mod  # noqa: E402
from models import CircuitBreakerConfigUpdate  # noqa: E402


def _call(enabled: bool, global_on: bool, monkeypatch) -> dict:
    monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", global_on, raising=False)
    monkeypatch.setattr(agents_mod.db, "set_circuit_breaker_enabled", MagicMock(), raising=False)
    monkeypatch.setattr(
        agents_mod.platform_audit_service, "log", AsyncMock(), raising=False
    )
    return asyncio.run(
        agents_mod.set_circuit_breaker_endpoint(
            agent_name="agent-1",
            body=CircuitBreakerConfigUpdate(enabled=enabled),
            current_user=MagicMock(),
        )
    )


def test_warning_when_enabling_with_global_off(monkeypatch):
    result = _call(enabled=True, global_on=False, monkeypatch=monkeypatch)
    assert result["enabled"] is True
    assert result["global_enabled"] is False
    assert "warning" in result and "DISPATCH_BREAKER_ENABLED" in result["warning"]


def test_no_warning_when_global_on(monkeypatch):
    result = _call(enabled=True, global_on=True, monkeypatch=monkeypatch)
    assert result["global_enabled"] is True
    assert "warning" not in result  # no behavior/response change when the gate is on


def test_no_warning_when_disabling(monkeypatch):
    # Turning the toggle OFF is never inert — no advisory even with global off.
    result = _call(enabled=False, global_on=False, monkeypatch=monkeypatch)
    assert result["enabled"] is False
    assert "warning" not in result


def test_write_always_succeeds(monkeypatch):
    # The stored preference is written regardless of the global flag.
    setter = MagicMock()
    monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", False, raising=False)
    monkeypatch.setattr(agents_mod.db, "set_circuit_breaker_enabled", setter, raising=False)
    monkeypatch.setattr(agents_mod.platform_audit_service, "log", AsyncMock(), raising=False)
    asyncio.run(
        agents_mod.set_circuit_breaker_endpoint(
            agent_name="agent-1",
            body=CircuitBreakerConfigUpdate(enabled=True),
            current_user=MagicMock(),
        )
    )
    setter.assert_called_once_with("agent-1", True)
