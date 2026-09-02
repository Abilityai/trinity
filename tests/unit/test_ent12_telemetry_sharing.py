"""Tier-2 opt-in fleet telemetry sharing (ent#12).

Unit-level coverage of the egress gate + the anonymized-aggregate payload, with
``db`` and ``httpx`` mocked (no real backend, no real egress). The end-to-end
gating/delivery was also proven live against the Postgres stack.

Locked behaviour (from the AC):
  * NEVER egresses without BOTH gates: config switch AND stored consent.
  * Payload is anonymized/coarse — no PII (no emails, agent names, content).
  * Opt-out (consent off) stops egress immediately.
  * Fail-open: a POST error never raises.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


@pytest.fixture
def tss():
    try:
        import services.telemetry_sharing_service as mod
    except ImportError:
        pytest.skip("backend venv required")
    # A db whose settings default to "off" and whose aggregates are coarse.
    store = {}
    mdb = MagicMock()
    mdb.get_setting_value.side_effect = lambda k, d=None: store.get(k, d)
    mdb.set_setting.side_effect = lambda k, v: store.update({k: v})
    mdb.get_fleet_execution_stats.return_value = {
        "total": 22, "success_count": 21, "failed_count": 1,
    }
    mdb.count_product_events_by_type.return_value = {"setup_started": 2}
    mdb.count_non_system_agents.return_value = 3
    with patch.object(mod, "db", mdb):
        yield mod, store


def _payload_blob(mod):
    return json.dumps(mod.build_aggregate_payload(window_days=30, backfill=True)).lower()


def test_payload_has_no_pii(tss):
    mod, _ = tss
    blob = _payload_blob(mod)
    for banned in ("email", "agent_name", "message", "content", "prompt", "password", "token", "@"):
        assert banned not in blob, f"payload leaked '{banned}'"


def test_payload_is_coarse_and_keyed(tss):
    mod, _ = tss
    pl = mod.build_aggregate_payload(window_days=30, backfill=True)
    assert pl["installation_id"]
    assert pl["counts"]["agents"] == 3
    assert pl["counts"]["executions_total"] == 22
    assert "activation_funnel" in pl
    assert set(pl["instance"]) == {"trinity_version", "edition", "platform", "python_version"}


def test_no_egress_when_consent_off(tss):
    mod, _store = tss
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod.httpx, "AsyncClient") as mclient:
        result = asyncio.run(mod.share_now(backfill=True))
    assert result is False
    mclient.assert_not_called()  # never even constructed a client


def test_no_egress_when_hard_disabled(tss):
    mod, store = tss
    store["telemetry_sharing_enabled"] = "true"  # consent ON...
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", False), \
         patch.object(mod.httpx, "AsyncClient") as mclient:
        result = asyncio.run(mod.share_now(backfill=True))  # ...but config OFF
    assert result is False
    mclient.assert_not_called()


def _fake_client(status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx), client


def test_egress_when_both_gates_on(tss):
    mod, store = tss
    store["telemetry_sharing_enabled"] = "true"
    fake_ac, client = _fake_client(200)
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod.httpx, "AsyncClient", fake_ac):
        result = asyncio.run(mod.share_now(backfill=True))
    assert result is True
    client.post.assert_awaited_once()
    # last-shared stamped
    assert store.get("telemetry_sharing_last_shared_at")
    # the posted body is the anonymized payload
    _, kwargs = client.post.call_args
    body = json.dumps(kwargs["json"]).lower()
    assert "@" not in body and "agent_name" not in body


def test_fail_open_on_post_error(tss):
    mod, store = tss
    store["telemetry_sharing_enabled"] = "true"
    fake_ac, client = _fake_client()
    client.post = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod.httpx, "AsyncClient", fake_ac):
        # must NOT raise
        result = asyncio.run(mod.share_now(backfill=True))
    assert result is False


def test_non_2xx_not_marked_shared(tss):
    mod, store = tss
    store["telemetry_sharing_enabled"] = "true"
    fake_ac, _client = _fake_client(404)
    with patch.object(mod, "TELEMETRY_SHARING_ENABLED", True), \
         patch.object(mod.httpx, "AsyncClient", fake_ac):
        result = asyncio.run(mod.share_now(backfill=True))
    assert result is False
    assert "telemetry_sharing_last_shared_at" not in store


def test_set_consent_roundtrip(tss):
    mod, store = tss
    st = mod.set_consent(True, backfill_days=7)
    assert st["enabled"] is True and st["consent_at"] and st["backfill_days"] == 7
    assert mod.is_consent_enabled() is True
    st = mod.set_consent(False)
    assert st["enabled"] is False
    assert mod.is_consent_enabled() is False


# --- review fixes (validation pass, 0.8.5) -----------------------------------

def test_consent_audit_uses_a_real_audit_event_type():
    """The consent handler audited with `AuditEventType.SETTINGS`, which does
    not exist — the AttributeError was swallowed by the best-effort except, so
    every consent flip of an egress channel went silently un-audited. Pin that
    the member referenced by the telemetry handler actually exists."""
    import re
    from pathlib import Path
    from services.platform_audit_service import AuditEventType

    # #1028: routers/settings.py is a package; the consent handler lives in
    # flags.py — read the whole package so a sub-module move cannot blank this.
    pkg = (Path(__file__).resolve().parents[2] / "src" / "backend" / "routers"
           / "settings")
    src = "\n".join(f.read_text() for f in sorted(pkg.glob("*.py")))
    handler = src[src.index("telemetry_sharing_consent") - 2000:
                  src.index("telemetry_sharing_consent") + 200]
    members = re.findall(r"AuditEventType\.([A-Z_]+)", handler)
    assert members, "consent handler must audit-log"
    for m in members:
        assert hasattr(AuditEventType, m), (
            f"AuditEventType.{m} does not exist — the audit call would raise "
            "AttributeError inside the best-effort except and never log"
        )


def test_generic_settings_put_blocks_telemetry_sharing_keys():
    """Consent is human-only via the dedicated route (reject_agent_principal +
    hard-disable 409 + audit). The generic PUT /api/settings/{key} must 422 the
    telemetry_sharing_* family or an admin-owned agent-scoped key can flip
    egress consent around all of that (trinity-ops-agent#232 class)."""
    from pathlib import Path

    # #1028: the generic catch-all lives in routers/settings/generic.py now.
    src = (Path(__file__).resolve().parents[2] / "src" / "backend" / "routers"
           / "settings" / "generic.py").read_text()
    assert 'key.startswith("telemetry_sharing_")' in src, (
        "generic settings PUT must block the telemetry_sharing_* key family"
    )
