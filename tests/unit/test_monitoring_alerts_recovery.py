"""
MonitoringAlertService recovery tests.

These unit tests keep recovery handling away from the live notification DB:
the service-level database dependency is replaced with a small in-memory fake.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
_croniter_stub = ModuleType("croniter")
_croniter_stub.croniter = object
sys.modules.setdefault("croniter", _croniter_stub)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _install_services_namespace():
    services_pkg = ModuleType("services")
    services_pkg.__path__ = [str(_BACKEND / "services")]
    sys.modules["services"] = services_pkg


pytestmark = pytest.mark.unit


class _FakeDB:
    def __init__(self, pending=None):
        self.pending = pending or []
        self.dismissed = []
        self.created = []
        self.cooldowns_cleaned = []
        self.cooldowns_set = []

    def cleanup_alert_cooldowns(self, agent_name=None):
        self.cooldowns_cleaned.append(agent_name)

    def list_notifications(self, **kwargs):
        self.list_kwargs = kwargs
        return self.pending

    def dismiss_notification(self, notification_id, dismissed_by):
        self.dismissed.append((notification_id, dismissed_by))
        return SimpleNamespace(id=notification_id)

    def is_in_alert_cooldown(self, agent_name, condition, cooldown_seconds):
        return False

    def set_alert_cooldown(self, agent_name, condition):
        self.cooldowns_set.append((agent_name, condition))

    def create_notification(self, agent_name, data):
        self.created.append((agent_name, data))
        return SimpleNamespace(
            id=f"notif-{len(self.created)}",
            agent_name=agent_name,
            notification_type=data.notification_type,
            priority=data.priority,
            title=data.title,
            created_at="2026-04-18T10:00:00.000000Z",
        )


@pytest.fixture
def alerts_module(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://user:pass@localhost:6379")
    for modname in list(sys.modules):
        if modname == "services" or modname == "services.monitoring_alerts":
            sys.modules.pop(modname, None)
    _install_services_namespace()
    import services.monitoring_alerts as monitoring_alerts  # noqa: WPS433

    return monitoring_alerts


@pytest.mark.asyncio
async def test_recovery_dismisses_prior_health_alerts_and_keeps_evidence(
    alerts_module,
    monkeypatch,
):
    pending_alert = SimpleNamespace(
        id="old-health-alert",
        notification_type="alert",
    )
    pending_info = SimpleNamespace(
        id="old-health-info",
        notification_type="info",
    )
    fake_db = _FakeDB(pending=[pending_alert, pending_info])
    monkeypatch.setattr(alerts_module, "db", fake_db)

    service = alerts_module.MonitoringAlertService()
    service._broadcast_alert = AsyncMock()

    notification_id = await service._send_recovery_alert(
        agent_name="alpha",
        prev=alerts_module.AgentHealthStatus.UNHEALTHY,
        curr=alerts_module.AgentHealthStatus.HEALTHY,
        details={"latency_ms": 12.5},
    )

    assert notification_id == "notif-1"
    assert fake_db.cooldowns_cleaned == ["alpha"]
    assert fake_db.list_kwargs == {
        "agent_name": "alpha",
        "status": "pending",
        "category": "health",
        "limit": 100,
    }
    assert fake_db.dismissed == [("old-health-alert", "system:recovered")]
    _, data = fake_db.created[0]
    assert data.notification_type == "info"
    assert data.category == "health"
    assert data.metadata["resolution_state"] == "recovered_unacknowledged"
    assert data.metadata["latency_ms"] == 12.5
    evidence = data.metadata["recovery_evidence"]
    assert evidence["source"] == "monitoring_alert_service"
    assert evidence["verified_status"] == "healthy"
    assert evidence["recovered_from_status"] == "unhealthy"
    assert evidence["recovered_alert_ids"] == ["old-health-alert"]
    service._broadcast_alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_degradation_alert_marks_active_failure(
    alerts_module,
    monkeypatch,
):
    fake_db = _FakeDB()
    monkeypatch.setattr(alerts_module, "db", fake_db)

    service = alerts_module.MonitoringAlertService()
    service._broadcast_alert = AsyncMock()

    notification_id = await service._send_degradation_alert(
        agent_name="alpha",
        prev=alerts_module.AgentHealthStatus.HEALTHY,
        curr=alerts_module.AgentHealthStatus.UNHEALTHY,
        issues=["network unreachable"],
    )

    assert notification_id == "notif-1"
    assert fake_db.cooldowns_set == [("alpha", "status:unhealthy")]
    _, data = fake_db.created[0]
    assert data.notification_type == "alert"
    assert data.category == "health"
    assert data.metadata["resolution_state"] == "active_failure"
    assert data.metadata["issues"] == ["network unreachable"]
    service._broadcast_alert.assert_awaited_once()
