"""
Settings → Retention rows for the two ent#478 metric knobs (trinity-enterprise#671).

`GET /api/settings/retention` already reported `metrics_retention_days` (it is a
retention window) but not `metrics_daily_point_cap`, which the panel's quota row
needs — value AND source, because a row whose value comes from the environment
is read-only. The cap is not a window, so it is reported in its own `quotas`
block and `sources` keeps meaning "per retention window".

The quota's value must be the number the write boundary ENFORCES
(`routers/metric_points._ops_int(key, 100000)`), not a re-derivation that
disagrees with it on a malformed row — pinned below by driving both readers with
the same resolver output.
"""
from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi", reason="backend venv required")

import services.entitlement_service as _ENT  # noqa: E402
import services.retention_guard as _RG  # noqa: E402
import services.settings_service as _SS  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"


def _load_isolated(name: str, relpath: str):
    # Same isolation as test_retention_floor.py: a direct file load avoids the
    # routers/__init__ chain a sibling test pollutes under some orderings.
    spec = importlib.util.spec_from_file_location(name, _BACKEND / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_RS = _load_isolated("retention_settings_ent671", "routers/settings/retention.py")
_MP = _load_isolated("metric_points_ent671", "routers/metric_points.py")


CAP = "metrics_daily_point_cap"
WINDOW = "metrics_retention_days"


def _admin():
    u = MagicMock()
    u.role = "admin"
    u.connector_agent = None
    u.agent_name = None
    u.mcp_scope = None  # #2323: an interactive human, not a truthy MagicMock
    return u


def _call(*, entitled=False, rows=None, env=None):
    """Drive the real handler with a mocked db + entitlement. `rows` are
    `system_settings` rows; both `db` bindings are patched because the ops
    values resolve through `settings_service.db`, not the router's."""
    rows = rows or {}
    db = MagicMock()
    db.get_setting_value.side_effect = lambda key, default=None: rows.get(key, default)
    db.count_soft_deleted_agents_past_retention.return_value = 0
    db.count_soft_deleted_schedules_past_retention.return_value = 0
    db.count_metric_points_candidates.return_value = 0
    ent = MagicMock()
    ent.is_entitled.return_value = entitled

    full_env = {"LOG_RETENTION_DAYS": "5", "AUDIT_LOG_RETENTION_DAYS": "365"}
    full_env.update(env or {})
    with patch.object(_RS, "db", db), \
         patch.object(_SS, "db", db), \
         patch.object(_ENT, "entitlement_service", ent), \
         patch.object(_RG, "is_acknowledged", return_value=False), \
         patch.object(_RS, "_backup_block", _no_backup), \
         patch.dict("os.environ", full_env, clear=False):
        return asyncio.run(_RS.get_retention_status(current_user=_admin()))


async def _no_backup():
    return {}


@pytest.fixture(autouse=True)
def _no_metric_env(monkeypatch):
    # A developer shell exporting either variable must not leak into a default.
    monkeypatch.delenv("METRICS_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("METRICS_DAILY_POINT_CAP", raising=False)


@pytest.mark.parametrize("entitled", [False, True])
def test_quota_is_reported_in_every_edition(entitled):
    """The read surface is every edition's; only the panel's rows are gated."""
    res = _call(entitled=entitled)
    assert res["quotas"] == {CAP: {"value": 100000, "source": "code-default"}}


def test_quota_is_not_a_window():
    """`windows`/`sources` mean retention windows — the cap is a write budget."""
    res = _call()
    assert CAP not in res["windows"]
    assert CAP not in res["sources"]
    assert WINDOW in res["windows"] and WINDOW in res["sources"]


def test_env_supplied_values_report_the_env_source():
    """The panel makes an env-sourced row read-only, so the source must be
    reported for both knobs when no row exists."""
    res = _call(env={"METRICS_DAILY_POINT_CAP": "42", "METRICS_RETENTION_DAYS": "30"})
    assert res["quotas"][CAP] == {"value": 42, "source": "env"}
    assert res["windows"][WINDOW] == 30
    assert res["sources"][WINDOW] == "env"


def test_a_stored_row_wins_over_env():
    res = _call(
        rows={CAP: "7", WINDOW: "400"},
        env={"METRICS_DAILY_POINT_CAP": "42", "METRICS_RETENTION_DAYS": "30"},
    )
    assert res["quotas"][CAP] == {"value": 7, "source": "db-row"}
    assert res["windows"][WINDOW] == 400
    assert res["sources"][WINDOW] == "db-row"


@pytest.mark.parametrize(
    "resolved",
    [
        ("100000", "default"),
        ("0", "db-row"),         # unlimited
        ("42", "env"),
        ("not-a-number", "db-row"),
        ("-1", "db-row"),        # reads as 0 in BOTH today — trinity#3089
    ],
)
def test_quota_value_is_what_the_write_boundary_enforces(resolved, monkeypatch):
    """Both readers get the same resolver output; the reported value must equal
    the enforced one. A reader that coerced garbage to 0 would advertise
    "unlimited" while the write path kept applying 100000."""
    monkeypatch.setattr(_SS.settings_service, "resolve_ops_setting", lambda key: resolved)
    reported = _call()["quotas"][CAP]["value"]
    enforced = _MP._ops_int(CAP, 100000)
    assert reported == enforced


# ---------------------------------------------------------------------------
# Call-site pin — the view must use the shared field module.
#
# The helpers' behaviour is proven by `src/frontend/tests/unit/retentionFields.spec.js`
# and the template wiring by `e2e/settings-retention-metric-rows.spec.js`. This
# pin covers the one gap between them: a view that re-grew a private copy of
# the field list or the save-body rule would leave the vitest spec green while
# the page ran different code (and the enterprise key-parity guard would be
# checking a file the page no longer reads).
# ---------------------------------------------------------------------------

_SETTINGS_VUE = _REPO / "src" / "frontend" / "src" / "views" / "Settings.vue"


def test_settings_view_uses_the_shared_retention_field_module():
    src = _SETTINGS_VUE.read_text(encoding="utf-8")
    assert re.search(r"from '\.\./utils/retentionFields'", src), (
        "Settings.vue no longer imports utils/retentionFields.js"
    )
    for helper in ("visibleRetentionFields(", "retentionSaveBody("):
        assert helper in src, f"Settings.vue no longer calls {helper}"
    assert not re.search(r"const RETENTION_FIELDS\s*=\s*\[", src), (
        "Settings.vue defines its own RETENTION_FIELDS again — the field list "
        "lives in utils/retentionFields.js (the list the vitest spec and the "
        "enterprise key-parity guard read)"
    )
