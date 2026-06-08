"""Unit tests for the #307 heartbeat alert methods on MonitoringAlertService.

``alert_heartbeat_lost`` / ``alert_heartbeat_recovered`` mirror the existing
``alert_container_stopped`` pattern: cooldown gate -> create_notification ->
set_alert_cooldown -> broadcast. These verify the gate and the notification
shape without a real DB or WebSocket.

Loads services/monitoring_alerts.py directly via importlib with a MagicMock
``database.db`` stub — the import-isolation pattern from
tests/unit/test_monitoring_dormant_skip.py. All sys.modules writes go through
monkeypatch.setitem (lint-approved; see tests/lint_sys_modules.py).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


@pytest.fixture
def alerts(monkeypatch):
    """Load monitoring_alerts with a MagicMock ``database.db``; return (mod, db)."""
    fake_db_module = types.ModuleType("database")
    fake_db_module.db = MagicMock()
    monkeypatch.setitem(sys.modules, "database", fake_db_module)

    spec = importlib.util.spec_from_file_location(
        "monitoring_alerts_under_test",
        str(_BACKEND / "services" / "monitoring_alerts.py"),
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "monitoring_alerts_under_test", module)
    spec.loader.exec_module(module)
    return module, fake_db_module.db


def _service(module):
    svc = module.MonitoringAlertService()
    svc._broadcast_alert = AsyncMock()
    return svc


def test_heartbeat_lost_creates_notification(alerts):
    module, db = alerts
    db.is_in_alert_cooldown.return_value = False
    db.create_notification.return_value = MagicMock(id="notif-lost")
    svc = _service(module)

    result = asyncio.run(svc.alert_heartbeat_lost("agent-a", 3))

    assert result == "notif-lost"
    db.create_notification.assert_called_once()
    data = db.create_notification.call_args.kwargs["data"]
    assert "heartbeat lost" in data.title.lower()
    assert data.category == "health"
    assert data.metadata["missed_beats"] == 3
    db.set_alert_cooldown.assert_called_once_with("agent-a", "heartbeat_lost")
    svc._broadcast_alert.assert_awaited_once()


def test_heartbeat_lost_suppressed_during_cooldown(alerts):
    module, db = alerts
    db.is_in_alert_cooldown.return_value = True
    svc = _service(module)

    result = asyncio.run(svc.alert_heartbeat_lost("agent-a", 3))

    assert result is None
    db.create_notification.assert_not_called()
    db.set_alert_cooldown.assert_not_called()
    svc._broadcast_alert.assert_not_awaited()


def test_heartbeat_recovered_creates_notification(alerts):
    module, db = alerts
    db.create_notification.return_value = MagicMock(id="notif-recovered")
    svc = _service(module)

    result = asyncio.run(svc.alert_heartbeat_recovered("agent-a"))

    assert result == "notif-recovered"
    db.create_notification.assert_called_once()
    data = db.create_notification.call_args.kwargs["data"]
    assert "recovered" in data.title.lower()
    assert data.category == "health"
    db.set_alert_cooldown.assert_called_once_with("agent-a", "heartbeat_recovered")
    svc._broadcast_alert.assert_awaited_once()
