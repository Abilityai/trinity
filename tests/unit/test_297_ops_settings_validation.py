"""Retention windows get a validated, audited, single write path (ent#297).

ent#297 is the root-cause issue for a class point-fixed five times: an
agent-scoped MCP key resolves to its owner *carrying the owner's role*, so on a
default admin-owned install every agent's `TRINITY_MCP_API_KEY` passed every
admin gate. **That root fix — `reject_agent_principal` inside `require_admin`
and `assert_admin` — is trinity#1890, not this file.**

This file covers the other half of the issue's ACs: the retention windows the
attack actually reached, which were writable through *two* endpoints with *zero*
validation and *no* audit trail.

The asymmetry that makes this worth stating precisely, because it inverts the
intuition:

* **Garbage fails safe.** Every reader coerces `max(int(raw), 0)` in a
  try/except returning 0, and 0 means "sweep disabled" — so `"abc"` widens
  retention to forever.
* **A small valid integer is the catastrophic input.** `{"execution_row_
  retention_days": "1"}` is well-typed, in range, and deletes the fleet's
  execution history on the next 5-minute cleanup cycle.

So validation here is explicitly NOT claimed to stop the attack — no range check
can distinguish a malicious `1` from an operator who genuinely wants a one-day
window. It buys a loud failure instead of a silent coercion, and it removes the
second unvalidated write path. The controls that stop the attack are the admin
gate (#1890) and the #1644 blast-radius guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


@pytest.fixture(scope="module")
def cfg():
    try:
        import config
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return config


# ---------------------------------------------------------------------------
# The value validator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "execution_log_retention_days",
    "execution_row_retention_days",
    "health_check_retention_days",
    "agent_soft_delete_retention_days",
    "schedule_soft_delete_retention_days",
    "agent_reports_retention_days",
    "operator_queue_retention_days",
    "agent_reminders_retention_days",
])
def test_garbage_is_rejected_rather_than_coerced_to_disabled(cfg, key):
    """The specific old behaviour: an unparseable value read back as 0, and 0 on
    a retention window means "sweep disabled" — a config typo silently turned
    off a sweep and nothing ever said so."""
    with pytest.raises(ValueError):
        cfg.validate_ops_setting(key, "abc")


def test_negative_is_rejected(cfg):
    with pytest.raises(ValueError):
        cfg.validate_ops_setting("execution_row_retention_days", "-1")


def test_absurdly_large_is_rejected(cfg):
    with pytest.raises(ValueError):
        cfg.validate_ops_setting("execution_row_retention_days", "99999999")


def test_the_upper_bound_matches_the_enterprise_contract(cfg):
    """There are TWO validated write paths to these values — this one and the
    enterprise `retention` module's `PUT /api/enterprise/retention/config`,
    whose `RetentionConfigUpdate` validates every window `ge=0, le=3650`.

    A wider OSS bound would let an admin store a window the managed panel then
    refuses to edit — a value its own GET surfaces and its own PUT rejects. The
    enterprise constant can't be imported (private submodule; OSS must build
    without it), so the alignment is pinned here by value. If the enterprise
    bound moves, this test is the thing that says so."""
    assert cfg._DAYS_MAX == 3650
    assert cfg.validate_ops_setting("execution_row_retention_days", "3650") == "3650"
    with pytest.raises(ValueError):
        cfg.validate_ops_setting("execution_row_retention_days", "3651")


def test_zero_is_accepted_because_it_means_disabled(cfg):
    """`0` is documented and meaningful on every retention window. A validator
    that rejected it would break the supported way to turn a sweep off."""
    assert cfg.validate_ops_setting("execution_row_retention_days", "0") == "0"


def test_a_short_window_is_still_accepted(cfg):
    """The honest boundary of this change: `1` is exactly the catastrophic input
    from the issue's PoC, and it is ACCEPTED, because an operator may legitimately
    want a one-day window and no range check can tell the two apart. The admin
    gate (#1890) and the #1644 blast-radius guard are what stop the attack."""
    assert cfg.validate_ops_setting("execution_row_retention_days", "1") == "1"


def test_the_community_floor_is_not_enforced_as_a_clamp(cfg):
    """#1039/#1638: the 5-day floor reaches new installs by SEEDING rows, and is
    an enterprise clamp on unentitled writes. OSS must not hard-clamp — silently
    rewriting an admin's explicit 3 to a 5 is the kind of invisible mutation
    #1638 was about."""
    assert cfg.validate_ops_setting("execution_log_retention_days", "3") == "3"


@pytest.mark.parametrize("value,ok", [
    ("true", True), ("false", True), ("TRUE", True),
    ("yes", False), ("1", False), ("", False),
])
def test_bool_settings_are_validated(cfg, value, ok):
    if ok:
        assert cfg.validate_ops_setting("ssh_access_enabled", value) in ("true", "false")
    else:
        with pytest.raises(ValueError):
            cfg.validate_ops_setting("ssh_access_enabled", value)


def test_percent_thresholds_are_range_checked(cfg):
    assert cfg.validate_ops_setting("ops_context_warning_threshold", "75") == "75"
    with pytest.raises(ValueError):
        cfg.validate_ops_setting("ops_context_warning_threshold", "101")


def test_unknown_keys_pass_through_untouched(cfg):
    """The endpoint already filters unknown keys into `ignored`; the validator
    must not second-guess that contract."""
    assert cfg.validate_ops_setting("some_future_key", "whatever") == "whatever"


# ---------------------------------------------------------------------------
# Parity — the durable guard
# ---------------------------------------------------------------------------

def test_every_retention_window_has_a_validation_spec(cfg):
    """The bug class this repo keeps repeating is a value set defined in one
    place and consumed in another that silently drifts (see learnings.md, the
    three-constant `triggered_by` entry). A new retention window added to
    RETENTION_OPS_KEYS without a spec here would be accepted unvalidated and
    nothing would say so."""
    from services.settings_service import RETENTION_OPS_KEYS

    missing = [k for k in RETENTION_OPS_KEYS if k not in cfg.OPS_SETTINGS_VALIDATION]
    assert not missing, f"retention windows with no validation spec: {missing}"


def test_every_ops_default_has_a_validation_spec(cfg):
    """Same guard, one level wider: any writable ops setting without a spec is a
    silently-unvalidated write path."""
    from services.settings_service import OPS_SETTINGS_DEFAULTS

    missing = [k for k in OPS_SETTINGS_DEFAULTS if k not in cfg.OPS_SETTINGS_VALIDATION]
    assert not missing, f"ops settings with no validation spec: {missing}"


def test_the_seeded_community_floor_values_all_validate(cfg):
    """A fresh install seeds these; if one failed its own validator the seed
    would be writing a value the API would refuse."""
    for key, value in cfg.COMMUNITY_FRESH_INSTALL_SEED.items():
        assert cfg.validate_ops_setting(key, value) == value


def test_the_code_defaults_all_validate(cfg):
    """Same for the prune-time fallbacks — a default outside its own declared
    range would mean the range is wrong, not the default."""
    from services.settings_service import OPS_SETTINGS_DEFAULTS

    for key, value in OPS_SETTINGS_DEFAULTS.items():
        cfg.validate_ops_setting(key, value)   # must not raise


# ---------------------------------------------------------------------------
# The generic catch-all no longer accepts retention windows
# ---------------------------------------------------------------------------

def _blocked_keys_in_generic_put() -> str:
    return (_BACKEND / "routers" / "settings.py").read_text()


def test_generic_put_routes_retention_windows_to_the_validated_endpoint():
    """AC #4. #1644 blocked the guard's ACK keys in this catch-all but left the
    WINDOWS falling through to a bare `db.set_setting` — a second, fully
    unvalidated write path to the values that drive irreversible deletion."""
    from services.settings_service import RETENTION_OPS_KEYS

    src = _blocked_keys_in_generic_put()
    assert "if key in RETENTION_OPS_KEYS:" in src, (
        "the generic PUT /api/settings/{key} must refuse retention windows"
    )
    # And the refusal must point somewhere real.
    assert "PUT /api/settings/ops/config" in src
    # 10 row-retention windows + #2216's backup_retention_days (a file-artifact
    # window that joins the tuple for the write-path protections).
    # ent#433 added two: subscription_headroom_retention_days (new probe-history
    # table) and subscription_failure_event_retention_days (which converted a
    # hardcoded 24h sweep into a real window).
    assert len(RETENTION_OPS_KEYS) == 11


def test_the_validated_endpoint_checks_before_it_writes():
    """All-or-nothing: a partial apply leaves some windows moved and some not,
    with no way to tell which from the response."""
    src = _blocked_keys_in_generic_put()
    marker = "# Validate EVERYTHING before writing ANYTHING."
    assert marker in src
    validate_at = src.index(marker)
    write_at = src.index("for key, value in to_write:")
    assert validate_at < write_at


def test_ops_config_writes_are_audited():
    """ent#297 lists the audit surface in its blast radius. Neither /ops/config
    nor the generic PUT's sibling /ops/reset logged anything, so the one route
    that could shrink a retention window was also the one that left no trace."""
    src = _blocked_keys_in_generic_put()
    assert 'event_action="ops_settings_change"' in src
    assert '"retention_windows_changed"' in src


# ---------------------------------------------------------------------------
# Behavioural — through the real handlers
# ---------------------------------------------------------------------------
#
# The assertions above this point are structural (source-text). They pin intent
# cheaply but would pass against code that never runs, so the contract is also
# driven through the actual handler functions below.

from dataclasses import dataclass          # noqa: E402
from typing import Optional                # noqa: E402


@dataclass
class _Admin:
    """Explicit human-admin principal. NOT a MagicMock: a bare MagicMock has a
    truthy `.agent_name`, so it reads as an agent key and would exercise the
    wrong branch (the trap recorded in #1816 and re-hit in ent#293)."""
    id: int = 1
    username: str = "admin"
    email: Optional[str] = "admin@example.com"
    role: str = "admin"
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None


class _Req:
    client = type("c", (), {"host": "127.0.0.1"})()
    url = type("u", (), {"path": "/api/settings/ops/config"})()
    state = type("s", (), {"request_id": "r1"})()
    headers: dict = {}


@pytest.fixture
def settings_router(monkeypatch):
    try:
        from routers import settings as mod
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    written = {}
    audited = []

    monkeypatch.setattr(mod.db, "set_setting",
                        lambda k, v, *a, **kw: written.update({k: v}) or True,
                        raising=False)

    async def _log(**kwargs):
        audited.append(kwargs)

    monkeypatch.setattr(mod.platform_audit_service, "log", _log, raising=False)
    return mod, written, audited


def test_handler_rejects_garbage_and_writes_nothing(settings_router):
    """All-or-nothing, driven for real: the good key in the same request must
    NOT land when a sibling fails validation."""
    import asyncio
    from fastapi import HTTPException
    from models import OpsSettingsUpdate

    mod, written, audited = settings_router

    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.update_ops_settings(
            body=OpsSettingsUpdate(settings={
                "ops_log_retention_days": "14",              # valid
                "execution_row_retention_days": "abc",       # invalid
            }),
            request=_Req(),
            current_user=_Admin(),
        ))

    assert exc.value.status_code == 422
    assert written == {}, "a rejected request must not partially apply"
    assert audited == []


def test_handler_accepts_a_valid_change_and_audits_it(settings_router):
    import asyncio
    from models import OpsSettingsUpdate

    mod, written, audited = settings_router

    result = asyncio.run(mod.update_ops_settings(
        body=OpsSettingsUpdate(settings={"execution_row_retention_days": "45"}),
        request=_Req(),
        current_user=_Admin(),
    ))

    assert result["success"] is True
    assert written == {"execution_row_retention_days": "45"}
    assert len(audited) == 1
    details = audited[0]["details"]
    assert details["settings"] == {"execution_row_retention_days": "45"}
    assert details["retention_windows_changed"] == ["execution_row_retention_days"]


def test_handler_still_ignores_unknown_keys(settings_router):
    """Pre-existing contract — unknown keys are reported, not rejected."""
    import asyncio
    from models import OpsSettingsUpdate

    mod, written, _ = settings_router

    result = asyncio.run(mod.update_ops_settings(
        body=OpsSettingsUpdate(settings={"not_an_ops_key": "x"}),
        request=_Req(),
        current_user=_Admin(),
    ))
    assert result["ignored"] == ["not_an_ops_key"]
    assert written == {}


def test_the_ssh_toggle_the_ui_actually_sends_still_works(settings_router):
    """The only place the UI PUTs /ops/config is the SSH toggle
    (`Settings.vue`, `ssh_access_enabled: 'true'|'false'`). Validation must not
    break the one live caller."""
    import asyncio
    from models import OpsSettingsUpdate

    mod, written, _ = settings_router

    for value in ("true", "false"):
        asyncio.run(mod.update_ops_settings(
            body=OpsSettingsUpdate(settings={"ssh_access_enabled": value}),
            request=_Req(),
            current_user=_Admin(),
        ))
        assert written["ssh_access_enabled"] == value


@pytest.mark.parametrize("key", [
    "execution_row_retention_days",
    "agent_soft_delete_retention_days",
])
def test_generic_put_refuses_a_retention_window_for_real(settings_router, key):
    """AC #4 driven through the handler, not grepped: the PoC's exact call."""
    import asyncio
    from fastapi import HTTPException
    from db_models import SystemSettingUpdate

    mod, written, _ = settings_router

    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.update_setting(
            key=key,
            body=SystemSettingUpdate(value="1"),
            request=_Req(),
            current_user=_Admin(),
        ))

    assert exc.value.status_code == 422
    assert "ops/config" in str(exc.value.detail)
    assert written == {}, "the blocked key must never reach db.set_setting"
