"""Retention / cap contract — ent#477 (R7, re-scoped by ruling T2).

The plan originally minted `metrics_retention_days` and
`metrics_daily_point_cap` as Settings knobs in this PR. The orchestrator ruled
**defer to ent#478**, so this file asserts the *opposite* of what R7 was
first written to assert: the knobs are **absent**, and the contract they will
implement is **written down**.

Why the absence is worth a test rather than nothing at all:

  * a retention window in `RETENTION_OPS_KEYS` with no `_guard_allows` sweep
    site fails `test_1771a_retention_edges` AND the private retention module's
    mirror of that set — so adding the key "early" breaks two suites in a way
    whose cause is three files away;
  * a Settings control that changes a number nothing reads is a **dishonest
    affordance** (Product Quality Bar 4). An operator who sets
    `metrics_retention_days = 30` would reasonably believe points are being
    pruned at 30 days. Nothing prunes anything until ent#478 ships the sweep;
  * the *names* and *defaults* still have to be fixed now, because ent#478,
    ent#482 and ent#483 all build against them. Publishing them as a documented
    contract (and in the definitions response, flagged `enforced: False`) gives
    those issues something to target without minting the lie.

Two facts recorded here for ent#478's benefit (E2, verified against the code):
`get_ops_setting` has **no env tier** today — it resolves DB row → default —
so the frozen schema's "`system_settings` → env → default" has to be BUILT with
the sweep; and `0` means "disabled" on every sibling window, which fixes the
bounds at `0–3650`, not `1–3650`.
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

# The frozen contract. These exact numbers appear in three places that must
# agree: the requirement, the guide, and the `policy` block of
# `GET /api/agents/{name}/metrics/definitions`.
RETENTION_DAYS = 365
DAILY_POINT_CAP = 100000


# ---------------------------------------------------------------------------
# No knob is minted here (T2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("knob", _KNOB_NAMES)
def test_the_knob_is_not_in_the_ops_defaults(knob):
    assert knob not in config.OPS_SETTINGS_DEFAULTS, (
        f"{knob} must not be minted until ent#478 ships the enforcement that "
        f"makes it mean something (T2, Product Quality Bar 4)"
    )


@pytest.mark.parametrize("knob", _KNOB_NAMES)
def test_the_knob_is_not_validated_as_an_ops_setting(knob):
    assert knob not in config.OPS_SETTINGS_VALIDATION


@pytest.mark.parametrize("knob", _KNOB_NAMES)
def test_the_knob_is_not_a_retention_window(knob):
    """P7 — `RETENTION_OPS_KEYS` membership REQUIRES a `_guard_allows` sweep
    site; `test_1771a_retention_edges` asserts it and the private retention
    module mirrors the set. Registering the key without its sweeper turns two
    unrelated suites red."""
    assert knob not in config.RETENTION_OPS_KEYS


@pytest.mark.parametrize("knob", _KNOB_NAMES)
def test_the_knob_has_no_settings_description(knob):
    from services import settings_service

    assert knob not in settings_service.OPS_SETTINGS_DESCRIPTIONS


def test_no_metrics_knob_leaked_in_under_another_spelling():
    """The absence has to hold for the whole family, not two exact strings —
    `metric_retention_days` (singular) would be just as dishonest."""
    for name in (*config.OPS_SETTINGS_DEFAULTS, *config.RETENTION_OPS_KEYS):
        assert not name.startswith("metric"), (
            f"{name} minted a metrics knob ahead of its enforcer"
        )


# ---------------------------------------------------------------------------
# The contract IS written down (the half that ships)
# ---------------------------------------------------------------------------

def test_the_definitions_response_publishes_the_contract_as_unenforced():
    """`enforced: False` is the honest-status half (Bar 4): a consumer reads
    the numbers from the platform AND is told nothing acts on them yet."""
    source = (_BACKEND / "routers" / "agent_files.py").read_text()
    assert f'"retention_days": {RETENTION_DAYS}' in source
    assert f'"daily_point_cap": {DAILY_POINT_CAP}' in source
    assert '"enforced": False' in source


def test_the_requirement_records_the_names_defaults_and_owner():
    req = (_REPO / "docs" / "memory" / "requirements"
           / "lifecycle-observability.md").read_text(encoding="utf-8")
    assert "metrics_retention_days" in req
    assert str(RETENTION_DAYS) in req
    assert "metrics_daily_point_cap" in req
    assert str(DAILY_POINT_CAP) in req
    assert "478" in req, "the requirement must name who implements the contract"


def test_the_requirement_records_the_two_facts_ent478_needs():
    """E2 — without these written down, ent#478 inherits a false premise from
    the frozen schema's own wording."""
    req = (_REPO / "docs" / "memory" / "requirements"
           / "lifecycle-observability.md").read_text(encoding="utf-8")
    assert "no env tier" in req, (
        "`get_ops_setting` resolves DB row -> default today; the env tier the "
        "frozen schema names does not exist and must be built with the sweep"
    )
    assert "0-3650" in req or "0–3650" in req, (
        "0 = disabled on every sibling window, so the bounds are 0-3650"
    )


def test_the_guide_tells_an_author_the_same_numbers():
    guide = (_REPO / "docs" / "TRINITY_COMPATIBLE_AGENT_GUIDE.md").read_text(
        encoding="utf-8")
    assert str(RETENTION_DAYS) in guide
    assert "100,000" in guide or str(DAILY_POINT_CAP) in guide


# ---------------------------------------------------------------------------
# The two recorded facts, verified against the code they describe
# ---------------------------------------------------------------------------

def test_get_ops_setting_still_has_no_env_tier():
    """Recorded as a FACT, not an assumption — if someone adds the env tier
    before ent#478, this goes red and the requirement's note becomes stale.

    `SettingsService.get_ops_setting` resolves `get_setting(key, default)`:
    a `system_settings` row, else the `OPS_SETTINGS_DEFAULTS` entry. There is
    no third tier to fall through to."""
    import inspect

    from services.settings_service import SettingsService

    source = inspect.getsource(SettingsService.get_ops_setting)
    assert "os.environ" not in source and "getenv" not in source


def test_zero_means_disabled_on_the_sibling_windows():
    """The reason the bounds are 0-3650 rather than 1-3650."""
    for key in sorted(config.RETENTION_OPS_KEYS):
        rule = config.OPS_SETTINGS_VALIDATION.get(key)
        if isinstance(rule, tuple) and len(rule) >= 2 and isinstance(rule[0], int):
            assert rule[0] == 0, (
                f"{key} has a lower bound of {rule[0]}; every retention window "
                f"treats 0 as 'disabled'"
            )
