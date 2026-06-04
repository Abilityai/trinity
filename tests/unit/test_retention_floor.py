"""
OSS retention floor tests (#1039).

Pins the community 5-day retention floor and the effective-retention read
surface (`GET /api/settings/retention`):

- The operator-tunable OPS retention windows default to the 5-day community
  floor (execution log/row, health-check, agent/schedule soft-delete).
- The audit-log window is EXEMPT — it is not an OPS default and keeps its
  365-day integrity floor.
- `GET /api/settings/retention` reports the effective windows + the active
  edition (community vs enterprise via the `retention` entitlement).

The OSS layer does NOT hard-clamp env/OPS values (the env is an unsupported
self-host escape hatch, #1039); the clamp lives in the enterprise `retention`
module. These tests therefore assert defaults + the read surface, not a clamp.
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest

from services.settings_service import (
    COMMUNITY_RETENTION_FLOOR_DAYS,
    OPS_SETTINGS_DEFAULTS,
    RETENTION_OPS_KEYS,
)

pytestmark = pytest.mark.unit


def test_community_floor_is_five_days():
    assert COMMUNITY_RETENTION_FLOOR_DAYS == 5


def test_operator_tunable_windows_default_to_floor():
    """Every operator-tunable OPS window ships the 5-day community default."""
    for key in RETENTION_OPS_KEYS:
        assert OPS_SETTINGS_DEFAULTS[key] == "5", (
            f"{key} default must be the 5-day community floor (#1039)"
        )


def test_audit_log_is_not_an_ops_retention_key():
    """Audit-log retention is exempt from the 5-day floor — it must not be an
    OPS default (it lives in audit_retention_service with a 365-day floor)."""
    assert "audit_log_retention_days" not in OPS_SETTINGS_DEFAULTS
    assert "audit_log_retention_days" not in RETENTION_OPS_KEYS


def _admin():
    u = MagicMock()
    u.role = "admin"
    return u


def _call_retention(*, entitled: bool, ops_values=None, env=None):
    """Drive routers.settings.get_retention_status with mocked db + entitlement."""
    from routers.settings import get_retention_status
    _SET = sys.modules[get_retention_status.__module__]

    ops_values = ops_values or {}
    db = MagicMock()
    db.get_setting_value.side_effect = (
        lambda key, default="0": ops_values.get(key, default)
    )
    ent = MagicMock()
    ent.is_entitled.return_value = entitled

    env = env or {}
    with patch.object(_SET, "db", db), \
         patch("services.entitlement_service.entitlement_service", ent), \
         patch.dict("os.environ", env, clear=False):
        return asyncio.run(get_retention_status(current_user=_admin()))


def test_read_surface_community_reports_floor_and_audit_exempt():
    res = _call_retention(entitled=False, env={
        "LOG_RETENTION_DAYS": "5",
        "AUDIT_LOG_RETENTION_DAYS": "365",
    })
    assert res["edition"] == "community"
    assert res["community_floor_days"] == 5
    w = res["windows"]
    assert w["log_retention_days"] == 5
    # OPS windows fall back to the 5-day defaults when unset in the DB
    for key in RETENTION_OPS_KEYS:
        assert w[key] == 5
    # audit exempt — stays at the 365 floor
    assert w["audit_log_retention_days"] == 365


def test_read_surface_enterprise_edition_when_entitled():
    res = _call_retention(entitled=True)
    assert res["edition"] == "enterprise"


def test_audit_window_floored_at_365_even_if_env_lower():
    """A sub-365 AUDIT_LOG_RETENTION_DAYS env is floored back to 365 (integrity
    floor — the audit_log_no_delete trigger refuses younger deletions)."""
    res = _call_retention(entitled=False, env={"AUDIT_LOG_RETENTION_DAYS": "30"})
    assert res["windows"]["audit_log_retention_days"] == 365


def test_read_surface_reflects_enterprise_set_ops_window():
    """When the OPS window has been raised (e.g. by the enterprise module's
    write-through), the read surface reports the live value, not the default."""
    res = _call_retention(
        entitled=True,
        ops_values={"execution_row_retention_days": "90"},
    )
    assert res["windows"]["execution_row_retention_days"] == 90
