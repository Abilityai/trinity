"""#2216 — backup observability + knob plumbing.

Pins the properties that keep the operator surfaces honest:

- `GET /api/settings/retention` carries a `backup` block AND excludes
  `backup_retention_days` from the generic `windows` map — the generic
  `_ops_int` coerces garbage → 0 ("sweep disabled"), which for backups means
  "keep forever", so on one malformed row the two readers would disagree
  inside a single response (the two-readers-disagree defect the plan names).
  The ONE rendered number is the shared reader's 14.
- The write path is the validated ops-config route only (1..3650, 0 invalid;
  generic `PUT /api/settings/{key}` 422-blocks; `/ops/reset` skips).
- The alarm plumbing cannot be silenced or mis-scanned: `db-backup-` is a
  reserved id prefix, and the `_db-backup` sentinel is excluded from canary
  L-03 with service↔canary parity (the #1644 test's shape).
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

import database  # noqa: E402
import services.entitlement_service as _ENT  # noqa: E402
import services.db_backup_service as svc_mod  # noqa: E402

# Isolated file-load of routers/settings.py — same rationale as
# tests/unit/test_retention_floor.py (avoid routers/__init__ import pollution
# under pytest-randomly orderings).


def _load_isolated(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _BACKEND / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_RS = _load_isolated("backup_observability_settings_isolated", "routers/settings.py")

pytestmark = pytest.mark.unit


def _admin():
    u = MagicMock()
    u.role = "admin"
    u.connector_agent = None
    u.agent_name = None
    u.mcp_scope = None  # #2323: the admin gate allowlists `mcp_scope`; an absent field fails CLOSED (a `None` default would make it the privileged JWT value). None = interactive human.
    return u


def _call_retention(*, router_db_values=None):
    """Drive get_retention_status with a mocked router-side db + entitlement.
    The backup block goes through the REAL service reader against the REAL
    database facade — patch that separately per-test where needed."""
    values = router_db_values or {}
    db = MagicMock()
    db.get_setting_value.side_effect = (
        lambda key, default="0": values.get(key, default)
    )
    db.count_soft_deleted_agents_past_retention.return_value = 0
    db.count_soft_deleted_schedules_past_retention.return_value = 0
    ent = MagicMock()
    ent.is_entitled.return_value = False
    import services.retention_guard as _RG
    with patch.object(_RS, "db", db), \
         patch.object(_ENT, "entitlement_service", ent), \
         patch.object(_RG, "is_acknowledged", return_value=False), \
         patch.dict("os.environ", {
             "LOG_RETENTION_DAYS": "5", "AUDIT_LOG_RETENTION_DAYS": "365",
         }, clear=False):
        return asyncio.run(_RS.get_retention_status(current_user=_admin()))


# ---------------------------------------------------------------------------
# GET /api/settings/retention
# ---------------------------------------------------------------------------

class TestRetentionGet:

    def test_backup_block_present_with_scope_and_floor(self):
        res = _call_retention()
        b = res["backup"]
        assert b["scope"] == "same-disk", (
            "the same-disk boundary must be machine-readable, not just prose "
            "(BKUP-003)"
        )
        assert b["min_keep"] == 3
        assert isinstance(b["enabled"], bool)
        assert isinstance(b["retention_days"], int)
        assert "artifacts" in b and "count" in b["artifacts"]
        assert "last_status" in b and "stale" in b

    def test_windows_map_excludes_backup_retention_days(self):
        res = _call_retention()
        assert "backup_retention_days" not in res["windows"], (
            "the generic _ops_int coerces garbage → 0 = keep-forever for "
            "backups; the key must render ONLY through the service's inverted "
            "reader in the backup block"
        )
        # Every OTHER retention window still renders in the generic map.
        from services.settings_service import RETENTION_OPS_KEYS
        for key in RETENTION_OPS_KEYS:
            if key != "backup_retention_days":
                assert key in res["windows"]

    def test_malformed_row_renders_one_number_the_shared_readers_14(self, monkeypatch):
        """THE two-readers-disagree regression: with a malformed stored row,
        the only rendered retention number is the shared reader's 14 — the
        generic map (which would say 0) doesn't render the key at all."""
        real_get = database.db.get_setting_value

        def poisoned(key, default=None):
            if key == "backup_retention_days":
                return "garbage"
            return real_get(key, default)

        monkeypatch.setattr(database.db, "get_setting_value", poisoned)
        res = _call_retention(
            router_db_values={"backup_retention_days": "garbage"}
        )
        assert "backup_retention_days" not in res["windows"]
        assert res["backup"]["retention_days"] == 14

    def test_backup_block_failure_degrades_not_500(self):
        with patch.object(
            svc_mod, "build_backup_status_block",
            side_effect=RuntimeError("boom"),
        ):
            res = _call_retention()
        assert res["backup"] == {"error": "unavailable"}, (
            "a broken backup block must not take down the retention panel"
        )


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

class TestWritePath:

    def test_ops_validation_bounds(self):
        from config import validate_ops_setting
        assert validate_ops_setting("backup_retention_days", "14") == "14"
        assert validate_ops_setting("backup_retention_days", "1") == "1"
        assert validate_ops_setting("backup_retention_days", "3650") == "3650"
        for bad in ("0", "-1", "3651", "garbage", ""):
            with pytest.raises(ValueError):
                validate_ops_setting("backup_retention_days", bad)

    def test_zero_is_invalid_by_design(self):
        """`0` means "disable the sweep" on every other window; for backups it
        means keep-forever = the #1871 disk-fill trap. Disabling BACKUPS is
        DB_BACKUP_ENABLED=false, never a retention value."""
        from config import OPS_SETTINGS_VALIDATION
        kind, low, high = OPS_SETTINGS_VALIDATION["backup_retention_days"]
        assert (kind, low) == ("int", 1)
        assert high >= 3650

    def test_generic_put_422_blocks_the_key(self):
        from fastapi import HTTPException
        from database import SystemSettingUpdate
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _RS.update_setting(
                    key="backup_retention_days",
                    body=SystemSettingUpdate(value="7"),
                    request=MagicMock(),
                    current_user=_admin(),
                )
            )
        assert exc.value.status_code == 422
        assert "ops/config" in str(exc.value.detail)

    def test_key_is_a_retention_ops_key(self):
        """Membership is what buys /ops/reset skip + the generic-PUT block +
        boot logging (the #1644 lesson: a window missing from this tuple is
        invisible to three readers)."""
        from services.settings_service import (
            OPS_SETTINGS_DEFAULTS, RETENTION_OPS_KEYS,
        )
        assert "backup_retention_days" in RETENTION_OPS_KEYS
        assert OPS_SETTINGS_DEFAULTS["backup_retention_days"] == "14"

    def test_not_a_community_floor_key(self):
        """Floor-seeding means FEWER days — for backups that is the
        destructive direction (fewer recovery points on every fresh install)."""
        from services.settings_service import COMMUNITY_FRESH_INSTALL_SEED
        assert "backup_retention_days" not in COMMUNITY_FRESH_INSTALL_SEED


# ---------------------------------------------------------------------------
# Alarm plumbing parity (the #1644 test's shape)
# ---------------------------------------------------------------------------

class TestAlarmPlumbing:

    def test_id_prefix_is_reserved(self):
        """Unregistered, an agent could pre-create the id and — via
        create_item's on_conflict_do_nothing — silence its own backup-failure
        alarm."""
        from services.operator_queue_service import _RESERVED_ID_PREFIXES
        assert "db-backup-" in _RESERVED_ID_PREFIXES
        assert svc_mod.ALARM_ID_PREFIX == "db-backup-"

    def test_sentinel_matches_the_canary_exclusion(self):
        """canary/snapshot.py duplicates the sentinel as a literal (it must
        not import a service). If they drift, L-03 fires forever on every
        backup alarm."""
        from canary.snapshot import _DB_BACKUP_AGENT, _PLATFORM_ALARM_SENTINELS
        assert svc_mod.ALARM_AGENT_NAME == _DB_BACKUP_AGENT
        assert _DB_BACKUP_AGENT in _PLATFORM_ALARM_SENTINELS

    def test_sentinel_tuple_generalization_kept_retention_guard(self):
        """#2216 generalized the single != '_retention-guard' literal to a
        tuple — the generalization must not have dropped the original."""
        from canary.snapshot import (
            _PLATFORM_ALARM_SENTINELS, _RETENTION_GUARD_AGENT,
        )
        assert _RETENTION_GUARD_AGENT in _PLATFORM_ALARM_SENTINELS

    def test_l03_predicate_excludes_every_sentinel(self):
        from canary.snapshot import ORPHAN_SCAN_TABLES, _PLATFORM_ALARM_SENTINELS
        predicate = next(
            cond for table, _col, cond in ORPHAN_SCAN_TABLES
            if table == "operator_queue"
        )
        assert "NOT IN" in predicate
        for sentinel in _PLATFORM_ALARM_SENTINELS:
            assert f"'{sentinel}'" in predicate

    def test_sentinel_is_uncreatable_as_an_agent_name(self):
        """A real agent with this name would inherit the alarm into its
        owner's ACL and receive it in its queue file via the 5s sync loop."""
        from utils.helpers import sanitize_agent_name
        assert sanitize_agent_name(svc_mod.ALARM_AGENT_NAME) != svc_mod.ALARM_AGENT_NAME
