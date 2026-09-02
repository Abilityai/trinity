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
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.settings_service import (
    COMMUNITY_FRESH_INSTALL_SEED,
    COMMUNITY_RETENTION_FLOOR_DAYS,
    OPS_SETTINGS_DEFAULTS,
    RETENTION_OPS_KEYS,
)
import services.entitlement_service as _ENT

# Load routers/settings.py in isolation (private module name) so it does NOT
# trigger routers/__init__ → routers.agents → services.agent_service. Another
# unit test (#612) loads services.agent_service under a fake sys.modules name,
# which breaks a plain `import routers.settings` under some pytest-randomly
# orderings (ImportError: cannot import name 'get_agents_by_prefix'). settings.py
# imports only models/database/dependencies/services.* — none of the polluted
# modules — so a direct file load is robust. Mirrors the conftest EntitlementCls
# pattern (spec_from_file_location to bypass a heavy package __init__).
_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


def _load_isolated(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _BACKEND / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# #1028: `routers/settings.py` is now the package `routers/settings/`. This
# isolated load points at the module that owns the handler under test —
# the split moved the code, not its behaviour.
_RS = _load_isolated("retention_settings_isolated", "routers/settings/retention.py")
get_retention_status = _RS.get_retention_status

pytestmark = pytest.mark.unit


def test_community_floor_is_five_days():
    assert COMMUNITY_RETENTION_FLOOR_DAYS == 5


def test_prune_time_defaults_are_never_the_community_floor():
    """#1638 regression guard — the inverse of what this file asserted before.

    `OPS_SETTINGS_DEFAULTS` is the fallback read at PRUNE time for an install
    with no `system_settings` row — the default state of every install that
    never touched retention. Setting it to the floor makes a default change
    retroactive and destructive: on the next boot `cleanup_service` hard-DELETEs
    everything outside the new, narrower window, silently.

    This test previously asserted `== "5"`, which is exactly how #1065 shipped
    green and cost a production instance ~3 months of execution history. The
    floor now reaches new installs via COMMUNITY_FRESH_INSTALL_SEED, which only
    writes to an empty database.
    """
    for key in RETENTION_OPS_KEYS:
        assert int(OPS_SETTINGS_DEFAULTS[key]) > COMMUNITY_RETENTION_FLOOR_DAYS, (
            f"{key}: the prune-time default must stay wider than the community "
            f"floor so an install with no row keeps its data (#1638). Apply the "
            f"floor via COMMUNITY_FRESH_INSTALL_SEED instead."
        )


def test_fresh_install_seed_applies_the_floor():
    """The floor still reaches new installs — via the seed, not the defaults."""
    for key, value in COMMUNITY_FRESH_INSTALL_SEED.items():
        assert key in RETENTION_OPS_KEYS
        assert int(value) == COMMUNITY_RETENTION_FLOOR_DAYS


def test_agent_soft_delete_is_exempt_from_the_floor():
    """#1638: purging a soft-deleted agent destroys its data volumes (#1581),
    so this is a recovery window, not a log window. It is exempt in every
    edition and must never be seeded down to the floor."""
    assert "agent_soft_delete_retention_days" not in COMMUNITY_FRESH_INSTALL_SEED
    assert int(OPS_SETTINGS_DEFAULTS["agent_soft_delete_retention_days"]) >= 180


def test_audit_log_is_not_an_ops_retention_key():
    """Audit-log retention is exempt from the 5-day floor — it must not be an
    OPS default (it lives in audit_retention_service with a 365-day floor)."""
    assert "audit_log_retention_days" not in OPS_SETTINGS_DEFAULTS
    assert "audit_log_retention_days" not in RETENTION_OPS_KEYS


def _admin():
    u = MagicMock()
    u.role = "admin"
    u.connector_agent = None  # #1310: not a connector principal
    u.agent_name = None  # ent#293: not an agent-scoped key
    # #2323: MagicMock auto-creates a truthy `.mcp_scope`, which the admin
    # gate's allowlist rejects — the same trap this helper already works around
    # for `connector_agent` and `agent_name`. None = an interactive human.
    u.mcp_scope = None
    return u


def _call_retention(
    *, entitled: bool, ops_values=None, env=None,
    agent_purge_count=0, schedule_purge_count=0, acked=False,
):
    """Drive routers.settings.get_retention_status with mocked db + entitlement.

    Pins LOG_/AUDIT_ env on every call so a polluted process env can't leak in.

    #1709: `agent_purge_count` / `schedule_purge_count` feed the guard's live
    re-evaluation (the `pending_acknowledgements` surface); `acked` stubs the
    guard's acknowledgement lookup so the single-use "acked ⇒ not pending"
    branch is exercisable. Defaults (0 / False) leave every sweep under
    threshold, so pre-#1709 tests see an empty pending list and are unaffected.
    """
    import services.retention_guard as _RG

    ops_values = ops_values or {}
    db = MagicMock()
    db.get_setting_value.side_effect = (
        lambda key, default="0": ops_values.get(key, default)
    )
    db.count_soft_deleted_agents_past_retention.return_value = agent_purge_count
    db.count_soft_deleted_schedules_past_retention.return_value = schedule_purge_count
    ent = MagicMock()
    ent.is_entitled.return_value = entitled

    full_env = {"LOG_RETENTION_DAYS": "5", "AUDIT_LOG_RETENTION_DAYS": "365"}
    full_env.update(env or {})
    with patch.object(_RS, "db", db), \
         patch.object(_ENT, "entitlement_service", ent), \
         patch.object(_RG, "is_acknowledged", return_value=acked), \
         patch.dict("os.environ", full_env, clear=False):
        return asyncio.run(get_retention_status(current_user=_admin()))


def test_read_surface_community_reports_wide_defaults_and_audit_exempt():
    """An install with no rows reports the WIDE code defaults (#1638).

    Pre-#1638 this asserted every window read back as 5 — i.e. that an install
    which had never opted in was already being pruned at the floor.
    """
    res = _call_retention(entitled=False, env={
        "LOG_RETENTION_DAYS": "5",
        "AUDIT_LOG_RETENTION_DAYS": "365",
    })
    assert res["edition"] == "community"
    assert res["community_floor_days"] == 5
    w = res["windows"]
    assert w["log_retention_days"] == 5
    # OPS windows fall back to the wide code defaults when unset in the DB —
    # an un-seeded install keeps its data.
    # #2216: backup_retention_days is deliberately EXCLUDED from the generic
    # windows map (its coercion is inverted — garbage must read as 14, never
    # as _ops_int's 0 = keep-forever); it renders only in the `backup` block.
    for key in RETENTION_OPS_KEYS:
        if key == "backup_retention_days":
            assert key not in w
            continue
        assert w[key] == int(OPS_SETTINGS_DEFAULTS[key])
        assert w[key] > COMMUNITY_RETENTION_FLOOR_DAYS
    # audit exempt — stays at the 365 floor
    assert w["audit_log_retention_days"] == 365


def test_read_surface_reports_source_per_window():
    """#1638: `code-default` vs `db-row` is the distinction that made the
    silent retroactive change invisible — the read surface must expose it."""
    res = _call_retention(
        entitled=False,
        ops_values={"execution_row_retention_days": "90"},
    )
    sources = res["sources"]
    assert sources["execution_row_retention_days"] == "db-row"
    assert sources["health_check_retention_days"] == "code-default"


def test_read_surface_does_not_advertise_a_nonexistent_env_hatch():
    """#1638: the surface used to claim `enterprise → env → community-default`,
    but no env layer exists for the OPS windows — so the one documented
    mitigation an operator could have reached for did nothing."""
    res = _call_retention(entitled=False)
    assert "env" not in res["precedence"].split("(")[0]
    assert "db-row" in res["precedence"]


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


# ---------------------------------------------------------------------------
# #1709 — pending-acknowledgement surface (the guard's in-product approval path)
# ---------------------------------------------------------------------------

def test_pending_lists_an_over_threshold_agent_purge():
    """When a sweep is over threshold and unacked, GET names it in
    `pending_acknowledgements` with the key, window, and candidate count — so the
    panel can offer an approve control (#1709)."""
    res = _call_retention(
        entitled=False,
        ops_values={"agent_soft_delete_retention_days": "180"},
        agent_purge_count=3,   # > FLOOR_AGENTS (0)
        acked=False,
    )
    pend = {p["key"]: p for p in res["pending_acknowledgements"]}
    assert "agent_soft_delete_retention_days" in pend
    item = pend["agent_soft_delete_retention_days"]
    assert item["candidate_count"] == 3
    assert item["window_days"] == 180
    assert item["floor"] == 0
    assert "volume" in item["label"].lower()  # names the irreversible cost


def test_acknowledged_sweep_drops_off_pending():
    """Single-use, no stale 'approved' state: once acked, the sweep is allowed and
    no longer appears as pending (#1709)."""
    res = _call_retention(
        entitled=False,
        ops_values={"agent_soft_delete_retention_days": "180"},
        agent_purge_count=3,
        acked=True,
    )
    keys = [p["key"] for p in res["pending_acknowledgements"]]
    assert "agent_soft_delete_retention_days" not in keys


def test_disabled_window_is_never_pending():
    """A 0-day (disabled) window prunes nothing, so it must not be pending even if
    a stale count came back non-zero (#1709)."""
    res = _call_retention(
        entitled=False,
        ops_values={"agent_soft_delete_retention_days": "0"},
        agent_purge_count=5,
        acked=False,
    )
    keys = [p["key"] for p in res["pending_acknowledgements"]]
    assert "agent_soft_delete_retention_days" not in keys


def test_nothing_pending_when_all_sweeps_under_threshold():
    """The honest empty state: no sweep over threshold ⇒ empty pending list."""
    res = _call_retention(
        entitled=False,
        ops_values={
            "agent_soft_delete_retention_days": "180",
            "schedule_soft_delete_retention_days": "30",
        },
        agent_purge_count=0,
        schedule_purge_count=0,
    )
    assert res["pending_acknowledgements"] == []


def test_over_threshold_schedule_purge_is_pending():
    """The schedule sweep (FLOOR_SCHEDULES=100) is also surfaced when it trips."""
    res = _call_retention(
        entitled=False,
        ops_values={"schedule_soft_delete_retention_days": "30"},
        schedule_purge_count=101,   # > FLOOR_SCHEDULES (100)
        acked=False,
    )
    keys = [p["key"] for p in res["pending_acknowledgements"]]
    assert "schedule_soft_delete_retention_days" in keys


def test_acknowledge_endpoint_is_callable_and_records_the_ack():
    """#1709 / #1310 regression: the ack endpoint called an UNIMPORTED
    `require_admin` and 500'd with NameError on every request — the guard's
    approval path was dead even for a valid admin caller. Drive it with a real
    admin and assert it reaches `record_acknowledgement` (no NameError), binds
    the window, and returns success.
    """
    from unittest.mock import AsyncMock
    from models import RetentionAcknowledge

    acknowledge = _RS.acknowledge_retention_prune

    body = RetentionAcknowledge(key="agent_soft_delete_retention_days", window_days=180)
    req = MagicMock()
    req.client = None
    req.url.path = "/api/settings/retention/acknowledge"
    req.state.request_id = None

    db = MagicMock()
    db.get_setting_value.side_effect = (
        lambda key, default="0": "180"
        if key == "agent_soft_delete_retention_days" else default
    )

    recorded = {}
    import services.retention_guard as _RG
    import dependencies as _DEP

    with patch.object(_RS, "db", db), \
         patch.object(_RS, "platform_audit_service", AsyncMock()), \
         patch.object(_DEP, "reject_agent_principal", lambda u: None), \
         patch.object(_RG, "record_acknowledgement",
                      lambda k, w: recorded.update(key=k, window=w)):
        res = asyncio.run(acknowledge(body=body, request=req, current_user=_admin()))

    assert res["success"] is True
    assert res["window_days"] == 180
    assert recorded == {"key": "agent_soft_delete_retention_days", "window": 180}


def test_acknowledge_rejects_a_window_mismatch_with_409():
    """The ack is bound to the window in force — a stale window is a named 409,
    not a silent approval of the wrong deletion."""
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from models import RetentionAcknowledge

    acknowledge = _RS.acknowledge_retention_prune
    body = RetentionAcknowledge(key="agent_soft_delete_retention_days", window_days=999)
    req = MagicMock(); req.client = None; req.url.path = "/x"; req.state.request_id = None
    db = MagicMock()
    db.get_setting_value.side_effect = lambda key, default="0": "180"

    import dependencies as _DEP
    with patch.object(_RS, "db", db), \
         patch.object(_RS, "platform_audit_service", AsyncMock()), \
         patch.object(_DEP, "reject_agent_principal", lambda u: None):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(acknowledge(body=body, request=req, current_user=_admin()))
    assert exc.value.status_code == 409
