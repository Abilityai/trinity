"""ent#463 — operator-intake Settings surface.

Unit-level coverage of the durable admin-only opt-in that ent#463 adds beside
the first-run welcome-form producer. Locked behaviour:

  * Status is honest across the three axes the panel renders (hard_disabled,
    already_submitted+submitted_at, enabled+consent_at). A legacy install with
    the marker but no timestamp reports submitted_at=None (not a lie).
  * At-most-once is preserved — a second submit is a no-op that does NOT
    re-fire the hosted POST and does NOT overwrite submitted_at.
  * Opt-out is durable but does NOT roll back the submitted marker (AC #5).
  * OPERATOR_INTAKE_ENABLED / DO_NOT_TRACK win over the Settings control
    (AC #6) — a fresh-install submit attempt raises SettingsIntakeResult
    .HARD_DISABLED rather than silently succeeding.
  * The generic PUT /api/settings/{key} catch-all refuses operator_intake_*
    keys (mirror of the ent#12 telemetry_sharing_* guard).
  * The dedicated PUT endpoint is admin AND human-only, and audit-logs a
    distinct action so a Settings-driven consent is distinguishable from a
    first-run one.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# Service-layer coverage (state machine + delegation)
# ---------------------------------------------------------------------------


@pytest.fixture
def ois_env():
    """A cleanroom operator_intake_service module with `db` mocked over an
    in-memory settings store. Yields (module, store) so tests can assert reads
    and writes without a real database."""
    try:
        import services.operator_intake_service as mod
    except ImportError:
        pytest.skip("backend venv required")

    store: dict = {}

    def _get(key, default=None):
        return store.get(key, default)

    def _set(key, value):
        store[key] = value

    mdb = MagicMock()
    mdb.get_setting_value.side_effect = _get
    mdb.set_setting.side_effect = _set

    with patch.object(mod, "db", mdb):
        yield mod, store


def test_status_fresh_install(ois_env):
    mod, _store = ois_env
    with patch.object(mod, "OPERATOR_INTAKE_ENABLED", True):
        status = mod.get_status()
    assert status["enabled"] is False
    assert status["hard_disabled"] is False
    assert status["already_submitted"] is False
    assert status["submitted_at"] is None
    assert status["consent_at"] is None
    assert "intake_url" in status


def test_status_hard_disabled_wins(ois_env):
    mod, store = ois_env
    # Even with consent recorded, the config kill switch dominates the panel.
    store["operator_intake_consent_enabled"] = "true"
    with patch.object(mod, "OPERATOR_INTAKE_ENABLED", False):
        status = mod.get_status()
    assert status["hard_disabled"] is True
    assert status["enabled"] is True  # consent state is reported honestly...
    # ...and the panel uses `hard_disabled` to gate the toggle regardless.


def test_status_legacy_marker_no_timestamp(ois_env):
    """A pre-ent#463 install has the marker but no `submitted_at`. Must render
    as 'already submitted, date unknown' rather than lying with e.g. utc_now."""
    mod, store = ois_env
    store["operator_intake_submitted"] = "true"
    with patch.object(mod, "OPERATOR_INTAKE_ENABLED", True):
        status = mod.get_status()
    assert status["already_submitted"] is True
    assert status["submitted_at"] is None


def test_set_consent_writes_flag_and_timestamp(ois_env):
    mod, store = ois_env
    with patch.object(mod, "OPERATOR_INTAKE_ENABLED", True):
        status = mod.set_consent(True)
    assert store["operator_intake_consent_enabled"] == "true"
    assert store["operator_intake_consent_at"]  # any non-empty ISO string
    assert status["enabled"] is True


def test_set_consent_off_does_not_rollback_submitted_marker(ois_env):
    """AC #5 — opting out is durable but the record has already been sent; we
    do NOT roll back the at-most-once marker (would open a re-send race if
    anything ever adds a resubmit path)."""
    mod, store = ois_env
    store["operator_intake_submitted"] = "true"
    store["operator_intake_submitted_at"] = "2026-05-01T00:00:00Z"
    with patch.object(mod, "OPERATOR_INTAKE_ENABLED", True):
        mod.set_consent(False)
    assert store["operator_intake_consent_enabled"] == "false"
    assert store["operator_intake_submitted"] == "true"  # UNCHANGED
    assert store["operator_intake_submitted_at"] == "2026-05-01T00:00:00Z"


def test_submit_from_settings_fires_once_and_records_timestamp(ois_env):
    """AC #3 — Settings path reuses the existing submit path and records the
    submitted_at timestamp so the panel can render honest state."""
    mod, store = ois_env
    called = {"n": 0}

    async def _fake_submit(**kwargs):
        called["n"] += 1
        # Mirror what the real submit does: claim marker + timestamp.
        mod.db.set_setting("operator_intake_submitted", "true")
        mod.db.set_setting("operator_intake_submitted_at", "2026-08-27T00:00:00Z")

    with patch.object(mod, "OPERATOR_INTAKE_ENABLED", True), \
         patch.object(mod, "submit_operator_intake", _fake_submit):
        outcome = asyncio.run(
            mod.submit_from_settings(email="op@example.com", company="Acme")
        )
    assert outcome == mod.SettingsIntakeResult.SUBMITTED
    assert called["n"] == 1
    assert store["operator_intake_submitted"] == "true"
    assert store["operator_intake_submitted_at"] == "2026-08-27T00:00:00Z"


def test_submit_from_settings_second_call_is_noop(ois_env):
    """AC #4 — no-op on resubmit. Second call must NOT fire, must NOT overwrite
    the recorded submitted_at, and must report `already_submitted`."""
    mod, store = ois_env
    store["operator_intake_submitted"] = "true"
    store["operator_intake_submitted_at"] = "2026-01-01T00:00:00Z"
    called = {"n": 0}

    async def _fake_submit(**kwargs):
        called["n"] += 1

    with patch.object(mod, "OPERATOR_INTAKE_ENABLED", True), \
         patch.object(mod, "submit_operator_intake", _fake_submit):
        outcome = asyncio.run(
            mod.submit_from_settings(email="op@example.com")
        )
    assert outcome == mod.SettingsIntakeResult.ALREADY_SUBMITTED
    assert called["n"] == 0
    assert store["operator_intake_submitted_at"] == "2026-01-01T00:00:00Z"


def test_submit_from_settings_hard_disabled_wins(ois_env):
    """AC #6 — the env kill switch overrides the Settings control."""
    mod, _store = ois_env
    with patch.object(mod, "OPERATOR_INTAKE_ENABLED", False):
        outcome = asyncio.run(mod.submit_from_settings(email="op@example.com"))
    assert outcome == mod.SettingsIntakeResult.HARD_DISABLED


def test_submit_from_settings_missing_email(ois_env):
    """Nothing to submit — the endpoint returns MISSING_EMAIL rather than firing
    an empty intake or crashing on strip()."""
    mod, _store = ois_env
    with patch.object(mod, "OPERATOR_INTAKE_ENABLED", True):
        outcome = asyncio.run(mod.submit_from_settings(email="   "))
    assert outcome == mod.SettingsIntakeResult.MISSING_EMAIL


# ---------------------------------------------------------------------------
# Router-layer coverage (auth + audit + catch-all guard)
# ---------------------------------------------------------------------------


@pytest.fixture
def router_env():
    """Provide the settings router with its collaborators mocked."""
    try:
        from routers import settings as router_mod
        import services.operator_intake_service as ois_mod
    except ImportError:
        pytest.skip("backend venv required")

    # DB with an in-memory settings store; both modules see the same one.
    store: dict = {}

    mdb = MagicMock()
    mdb.get_setting_value.side_effect = lambda k, d=None: store.get(k, d)
    mdb.set_setting.side_effect = lambda k, v: store.update({k: v})

    # Audit is best-effort — we let the real object accept the call but log
    # nothing, and inspect what would have been persisted through a spy.
    audit = MagicMock()
    audit.log = AsyncMock()

    with patch.object(router_mod, "db", mdb), \
         patch.object(ois_mod, "db", mdb), \
         patch.object(router_mod, "platform_audit_service", audit), \
         patch.object(ois_mod, "OPERATOR_INTAKE_ENABLED", True):
        yield router_mod, ois_mod, store, audit


def _admin_user():
    """Real admin User — needed because `assert_admin` reads `connector_agent`
    and `mcp_scope`, and a `MagicMock` returns truthy child mocks for
    unset attributes, which would trip `_reject_connector_principal` by
    accident."""
    from models import User
    return User(
        id=1,
        username="admin",
        email="admin@example.com",
        role="admin",
        agent_name=None,
        connector_agent=None,
        mcp_scope=None,  # JWT branch — interactive human
    )


def _agent_user():
    """Agent-scoped principal that RESOLVES to an admin owner (trinity-ops-
    agent#232). Must be rejected by the human-only gate even though `role` is
    'admin', because the resolved role is the owner's, not the caller's."""
    from models import User
    return User(
        id=1,
        username="admin",
        email="admin@example.com",
        role="admin",
        agent_name="some-agent",
        connector_agent=None,
        mcp_scope="agent",
    )


def test_put_rejects_agent_principal(router_env):
    """AC #7 — reject_agent_principal on top of assert_admin. An agent-scoped
    key resolving to an admin owner (the default admin-owned install shape)
    must NOT be able to flip consent or submit contact info."""
    router_mod, _ois_mod, _store, _audit = router_env
    from fastapi import HTTPException
    from models import OperatorIntakeUpdate

    body = OperatorIntakeUpdate(enabled=True, email="op@example.com")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(router_mod.set_operator_intake(body, _agent_user()))
    assert ei.value.status_code == 403


def test_put_hard_disabled_returns_409_when_submitting(router_env):
    """AC #6 — enabling with an email while OPERATOR_INTAKE_ENABLED=false must
    NOT silently accept. Pure consent flips (no email, or opt-out) may still
    be recorded — the operator has an honest way to see the toggle is refused
    only when they'd actually cause an egress attempt."""
    router_mod, ois_mod, _store, _audit = router_env
    from fastapi import HTTPException
    from models import OperatorIntakeUpdate

    with patch.object(ois_mod, "OPERATOR_INTAKE_ENABLED", False):
        body = OperatorIntakeUpdate(enabled=True, email="op@example.com")
        with pytest.raises(HTTPException) as ei:
            asyncio.run(router_mod.set_operator_intake(body, _admin_user()))
    assert ei.value.status_code == 409


def test_put_fires_submit_once_and_audits(router_env):
    """AC #3 + AC #8 — Settings PUT converges on submit_operator_intake AND
    writes a distinct audit action so the two producers are separable in the
    log."""
    router_mod, ois_mod, store, audit = router_env
    from models import OperatorIntakeUpdate

    submitted = {"n": 0, "email_seen": None}

    async def _fake(*, email, **_kw):
        submitted["n"] += 1
        submitted["email_seen"] = email
        # Mirror what the real submit does: claim marker + timestamp.
        store["operator_intake_submitted"] = "true"
        store["operator_intake_submitted_at"] = "2026-08-27T00:00:00Z"

    with patch.object(ois_mod, "submit_operator_intake", _fake):
        body = OperatorIntakeUpdate(enabled=True, email="op@example.com", company="Acme")
        result = asyncio.run(router_mod.set_operator_intake(body, _admin_user()))

    assert submitted["n"] == 1
    assert submitted["email_seen"] == "op@example.com"
    assert result["already_submitted"] is True
    assert result["submit_outcome"] == ois_mod.SettingsIntakeResult.SUBMITTED

    # Audit must have fired with the distinct action and MUST NOT log the email.
    audit.log.assert_awaited_once()
    kwargs = audit.log.await_args.kwargs
    assert kwargs["event_action"] == "operator_intake_consent"
    details = kwargs["details"]
    assert "email" not in details
    # We're allowed to record a bool that AN email was present, just never
    # the value.
    assert details.get("has_email") is True


def test_put_second_call_is_noop_no_resend(router_env):
    """AC #4 — second submit is a no-op that doesn't fire the hosted POST."""
    router_mod, ois_mod, store, _audit = router_env
    from models import OperatorIntakeUpdate

    store["operator_intake_submitted"] = "true"
    store["operator_intake_submitted_at"] = "2026-01-01T00:00:00Z"

    submitted = {"n": 0}

    async def _fake(**_kw):
        submitted["n"] += 1

    with patch.object(ois_mod, "submit_operator_intake", _fake):
        body = OperatorIntakeUpdate(enabled=True, email="op@example.com")
        result = asyncio.run(router_mod.set_operator_intake(body, _admin_user()))

    # No hosted POST was fired for the resubmit. The submit_outcome is not
    # present because the router's submit_intent gate short-circuited — this
    # is the exact no-op AC #4 demands.
    assert submitted["n"] == 0
    # Original submitted_at preserved — the record is not re-stamped.
    assert store["operator_intake_submitted_at"] == "2026-01-01T00:00:00Z"
    # And a durable consent flip was still recorded on the way through.
    assert result["enabled"] is True


def test_generic_put_catch_all_refuses_operator_intake_keys(router_env):
    """A raw PUT /api/settings/operator_intake_consent_enabled would otherwise
    write consent directly, bypassing the human-only gate and the audit. The
    catch-all must refuse the whole key family with 422 + pointer, matching
    the ent#12 telemetry_sharing_* guard shape."""
    router_mod, _ois_mod, _store, _audit = router_env
    from fastapi import HTTPException
    from db_models import SystemSettingUpdate

    body = SystemSettingUpdate(value="true")
    fake_request = MagicMock()  # unused inside the early-return guard path
    for key in (
        "operator_intake_consent_enabled",
        "operator_intake_submitted",
        "operator_intake_submitted_at",
        "operator_intake_consent_at",
    ):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                router_mod.update_setting(key, body, fake_request, _admin_user())
            )
        assert ei.value.status_code == 422
        assert "operator-intake" in ei.value.detail
