"""Metric retention / cap knobs, now that something enforces them (ent#478).

REPLACES `test_ent477_settings_knobs.py`. Every assertion in that file inverts
by design: ent#477 ruled the knobs must NOT exist until an enforcer shipped
(a control that changes a number nothing reads is a dishonest affordance,
Product Quality Bar 4), so it asserted their absence and recorded two facts for
this issue — that `get_ops_setting` had no env tier, and that `0` means
"disabled" on every sibling window. This issue ships the enforcer, so the
knobs are minted, the env tier is BUILT, and `enforced` flips to True. The
replacement is deliberate and named in the commit; nothing was quietly deleted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("sqlalchemy", reason="backend venv required")

import config  # noqa: E402

_KNOB_NAMES = ("metrics_retention_days", "metrics_daily_point_cap")

# The frozen contract. These numbers appear in places that must agree: the
# defaults, the requirement, the guide, and the `policy` block of
# `GET /api/agents/{name}/metrics/definitions`.
RETENTION_DAYS = 365
DAILY_POINT_CAP = 100000


# ---------------------------------------------------------------------------
# The knobs exist, with defaults, validation and descriptions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("knob", _KNOB_NAMES)
def test_the_knob_is_an_ops_setting_with_a_default(knob):
    """Bar 1: zero-config works. An install that never touches Settings keeps a
    year of points and a cap that only a runaway writer reaches."""
    assert config.OPS_SETTINGS_DEFAULTS[knob]


def test_the_defaults_are_the_published_contract():
    assert config.OPS_SETTINGS_DEFAULTS["metrics_retention_days"] == str(
        RETENTION_DAYS)
    assert config.OPS_SETTINGS_DEFAULTS["metrics_daily_point_cap"] == str(
        DAILY_POINT_CAP)


@pytest.mark.parametrize("knob", _KNOB_NAMES)
def test_the_knob_is_validated(knob):
    """A validated key has exactly ONE write path (the ent#297 rule,
    generalised here) — so validation is not advisory."""
    kind, low, high = config.OPS_SETTINGS_VALIDATION[knob]
    assert kind == "int"
    assert low == 0, "0 is meaningful on both: disabled / unlimited"
    assert high > 0


def test_the_window_bounds_follow_the_sibling_convention():
    assert config.OPS_SETTINGS_VALIDATION["metrics_retention_days"] == (
        "int", 0, 3650)


def test_the_cap_bounds_allow_lifting_it_without_a_huge_number():
    """TD-3: `0` = unlimited, the `ops_cost_limit_daily_usd` convention."""
    _kind, low, high = config.OPS_SETTINGS_VALIDATION["metrics_daily_point_cap"]
    assert low == 0 and high == 10_000_000


@pytest.mark.parametrize("knob", _KNOB_NAMES)
def test_the_knob_description_advertises_its_default(knob):
    """The #1638 rule: an operator reading Settings must see the number that is
    in force if they change nothing."""
    from services import settings_service

    description = settings_service.OPS_SETTINGS_DESCRIPTIONS[knob]
    assert "default:" in description
    assert str(RETENTION_DAYS if "retention" in knob else DAILY_POINT_CAP) in (
        description)


def test_the_window_is_a_retention_key_and_the_cap_is_not():
    """Membership means "is a retention window" and REQUIRES a `_guard_allows`
    sweep site (`test_1771a_retention_edges` asserts the set equality). The cap
    is a write budget, not a window — registering it would demand a sweep that
    cannot exist."""
    assert "metrics_retention_days" in config.RETENTION_OPS_KEYS
    assert "metrics_daily_point_cap" not in config.RETENTION_OPS_KEYS


def test_the_window_is_not_a_community_floor_key():
    """The 5-day fresh-install floor would gut a feature whose entire point is
    a year of history (the ent#433 reasoning)."""
    assert "metrics_retention_days" not in config.COMMUNITY_FRESH_INSTALL_SEED


# ---------------------------------------------------------------------------
# The env tier (built here — ent#477 recorded its absence as a fact)
# ---------------------------------------------------------------------------

def test_both_knobs_are_env_backed():
    # #2806 opted a third key in: the inter-agent chain-depth cap.
    assert config.ENV_BACKED_OPS_KEYS == {
        "metrics_retention_days": "METRICS_RETENTION_DAYS",
        "metrics_daily_point_cap": "METRICS_DAILY_POINT_CAP",
        "inter_agent_max_chain_depth": "INTER_AGENT_MAX_CHAIN_DEPTH",
    }


def test_the_env_tier_is_opt_in_per_key(monkeypatch):
    """TD-2: making every ops key env-backed would change precedence for ~20
    keys the retention endpoint documents as env-less — inert today, but a
    policy change nobody asked for."""
    monkeypatch.setenv("EXECUTION_ROW_RETENTION_DAYS", "1")
    assert config.resolve_ops_default("execution_row_retention_days") == "90"


def test_env_supplies_the_value_when_no_row_exists(monkeypatch):
    monkeypatch.setenv("METRICS_RETENTION_DAYS", "30")
    assert config.resolve_ops_default("metrics_retention_days") == "30"


def test_an_invalid_env_value_is_ignored_rather_than_obeyed(monkeypatch):
    """An operator who typed a word gets the built-in default and a warning,
    not a 500 on the first write of the day — and emphatically not `0`, which
    for a window means "keep forever" and for a cap means "unlimited"."""
    monkeypatch.setattr(config, "_env_ops_warned", set())
    monkeypatch.setenv("METRICS_RETENTION_DAYS", "not-a-number")
    assert config.resolve_ops_default("metrics_retention_days") == "365"

    monkeypatch.setenv("METRICS_DAILY_POINT_CAP", "-5")
    assert config.resolve_ops_default("metrics_daily_point_cap") == "100000"


def test_an_out_of_range_env_value_is_ignored(monkeypatch):
    monkeypatch.setattr(config, "_env_ops_warned", set())
    monkeypatch.setenv("METRICS_RETENTION_DAYS", "99999")
    assert config.resolve_ops_default("metrics_retention_days") == "365"


def test_the_env_is_not_read_for_a_key_that_has_no_variable(monkeypatch):
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "1")
    assert config.env_ops_value("backup_retention_days") is None


def test_a_stored_row_still_wins_over_the_environment(monkeypatch):
    """The precedence the endpoint documents: row -> env -> default. An
    operator's saved choice must not be silently overridden by a variable in
    the compose file."""
    from services.settings_service import settings_service

    monkeypatch.setenv("METRICS_RETENTION_DAYS", "30")
    monkeypatch.setattr(settings_service, "get_setting",
                        lambda key, default=None: "7")
    assert settings_service.resolve_ops_setting("metrics_retention_days") == (
        "7", "db-row")


def test_the_resolver_names_the_tier_it_used(monkeypatch):
    """Bar 4, honest status: "365 because nobody configured it" and "365
    because someone chose it" are different promises."""
    from services.settings_service import settings_service

    monkeypatch.setattr(settings_service, "get_setting",
                        lambda key, default=None: None)
    monkeypatch.delenv("METRICS_RETENTION_DAYS", raising=False)
    assert settings_service.resolve_ops_setting("metrics_retention_days") == (
        "365", "default")

    monkeypatch.setenv("METRICS_RETENTION_DAYS", "30")
    assert settings_service.resolve_ops_setting("metrics_retention_days") == (
        "30", "env")


def test_get_ops_setting_now_routes_through_the_resolver(monkeypatch):
    """ent#477 recorded "no env tier" as a FACT with a tripwire so that this
    change could not happen silently. It happens here, deliberately."""
    from services.settings_service import settings_service

    monkeypatch.setattr(settings_service, "get_setting",
                        lambda key, default=None: None)
    monkeypatch.setenv("METRICS_DAILY_POINT_CAP", "42")
    assert settings_service.get_ops_setting("metrics_daily_point_cap", int) == 42


def test_the_boot_seeder_leaves_an_env_backed_window_to_the_environment(monkeypatch):
    """S11: one semantic, not two. Seeding the env value into a row would
    freeze it — a later change to `METRICS_RETENTION_DAYS` would then be
    ignored with nothing on screen to explain why."""
    import database

    monkeypatch.setenv("METRICS_RETENTION_DAYS", "30")
    seeded = dict(database._retention_window_seed_values())
    assert "metrics_retention_days" not in seeded
    assert "agent_reports_retention_days" in seeded, "the others still seed"


def test_the_boot_seeder_writes_the_row_when_the_environment_is_silent(monkeypatch):
    import database

    monkeypatch.delenv("METRICS_RETENTION_DAYS", raising=False)
    seeded = dict(database._retention_window_seed_values())
    assert seeded["metrics_retention_days"] == "365"


# ---------------------------------------------------------------------------
# Write paths (E4)
# ---------------------------------------------------------------------------

def test_the_generic_put_refuses_every_validated_ops_key():
    """E4: `metrics_daily_point_cap` was reachable through the unvalidated
    catch-all, where garbage stores verbatim and then 500s the reader that
    calls `int()` on it. The refusal is now the property (every key with
    validation), not a hand-maintained subset."""
    source = (_BACKEND / "routers" / "settings" / "generic.py").read_text()
    assert "if key in OPS_SETTINGS_VALIDATION:" in source


@pytest.mark.parametrize("key", _KNOB_NAMES)
def test_the_generic_put_actually_refuses_the_metric_knobs(key):
    """I5: the guard above reads the source; this one CALLS the route. A
    refusal that only exists as a matched string is a refusal nobody has seen
    happen — and this branch is the whole reason the knobs have a validated
    range at all."""
    import asyncio
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from database import SystemSettingUpdate
    from routers.settings import generic as mod

    admin = MagicMock()
    admin.role = "admin"
    admin.connector_agent = None   # #1310: not a connector principal
    admin.agent_name = None        # ent#293: not an agent-scoped key
    admin.mcp_scope = None         # #2323: an interactive human

    req = MagicMock()
    req.client = None
    req.url.path = f"/api/settings/{key}"
    req.state.request_id = None

    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.update_setting(
            key=key,
            body=SystemSettingUpdate(value="not-a-number"),
            request=req,
            current_user=admin,
        ))

    assert exc.value.status_code == 422
    # Named, and it points at the route that DOES validate — a bare refusal
    # leaves the operator with a control they cannot reach.
    assert "/api/settings/ops/config" in exc.value.detail


def test_the_ack_gated_sweeps_include_the_metric_points_one():
    """TD-12: for this window a refusal is the EXPECTED path after a
    narrowing, and the guard's acknowledgements are single-use — without an
    approve control the only unblock would be the alarm's link."""
    source = (_BACKEND / "routers" / "settings" / "retention.py").read_text()
    assert '"metrics_retention_days",' in source
    assert "FLOOR_METRIC_POINTS" in source
    assert "count_metric_points_candidates" in source


def test_the_retention_endpoint_no_longer_claims_there_is_no_env_layer():
    """Bar 4: the endpoint whose job is to say where a window came from must
    not describe a precedence the platform no longer has."""
    source = (_BACKEND / "routers" / "settings" / "retention.py").read_text()
    assert "ENV_BACKED_OPS_KEYS" in source
    assert "db-row → env → code-default" in source


# ---------------------------------------------------------------------------
# The published contract, now enforced
# ---------------------------------------------------------------------------

def test_the_definitions_policy_reads_the_live_values():
    """A consumer that hard-codes 365 is wrong the first time an operator
    changes it — so the block is resolved, not restated."""
    from routers.agent_files import _metric_policy

    policy = _metric_policy()
    assert policy["enforced"] is True
    assert policy["retention_days"] == RETENTION_DAYS
    assert policy["daily_point_cap"] == DAILY_POINT_CAP
    assert policy["retention_days_source"] in ("db-row", "env", "default")
    assert policy["daily_point_cap_source"] in ("db-row", "env", "default")


def test_the_policy_follows_a_changed_setting(monkeypatch):
    from routers.agent_files import _metric_policy
    from services.settings_service import settings_service

    monkeypatch.setattr(settings_service, "resolve_ops_setting",
                        lambda key: ("7", "db-row"))
    policy = _metric_policy()
    assert policy["retention_days"] == 7 and policy["daily_point_cap"] == 7
    assert policy["retention_days_source"] == "db-row"


def test_zero_still_means_disabled_on_every_sibling_window():
    """The reason the window's bounds are 0-3650 rather than 1-3650 — carried
    over from the file this one replaces, because it is still the reason."""
    for key in sorted(config.RETENTION_OPS_KEYS):
        rule = config.OPS_SETTINGS_VALIDATION.get(key)
        if key == "backup_retention_days":
            continue  # #2216: 0 is the disk-fill trap here, documented
        if isinstance(rule, tuple) and len(rule) >= 2 and isinstance(rule[1], int):
            assert rule[1] == 0, (
                f"{key} has a lower bound of {rule[1]}; every retention window "
                f"treats 0 as 'disabled'"
            )
