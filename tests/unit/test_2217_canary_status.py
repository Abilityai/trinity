"""#2217 — canary run-state observability.

Nothing reported whether the harness was running. A disabled canary emits zero
violations — byte-for-byte identical to a clean fleet — which is the H-01 class
one level up, applied to the detector itself: H-01 catches a blind collector
*while a cycle runs*; it structurally cannot catch "no cycle is running at all"
(a dead loop emits nothing). `GET /api/canary/status` +
`CanaryService.get_run_status()` close that gap; a `canary_enabled` boolean
rides `GET /api/settings/feature-flags`.

These guards pin the run-state contract:
  - `disabled` short-circuits before Redis and NEVER reads as `stale`
    (default-OFF is the normal state for most installs and must not alarm);
  - the staleness threshold is `_max_failover_seconds + _MAX_CYCLE_LEASE_SECONDS`,
    provably above BOTH the leader-failover window AND a maxed-but-healthy cycle;
  - every read/parse failure fails OPEN to `unknown`, never `stale`;
  - `_read_last_cycle_for_status` distinguishes a Redis ERROR (redis_available
    False) from a MISSING cursor (True) — which `_read_prev_cycle_at`
    deliberately collapses;
  - the `/status` handler round-trips every model field, and the feature-flags
    handler carries `canary_enabled`.

## Why this file is under ``tests/unit/`` and not beside the canary suite

The big canary suite (`tests/test_canary_*.py`) runs in NO CI workflow — every
gating job runs ``cd tests && python -m pytest unit/`` (#1880/#2037, same as
``test_1881_canary_leader_lease.py`` / ``test_1897_canary_alert_delivery.py``).
A guard placed only beside that suite would never go red.

Imports are lazy, inside test bodies/helpers: ``services/__init__.py`` eagerly
imports ``docker_service`` and is a known pytest-randomly stub-leak target, so
each test seeds only what it needs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# A fixed "now" so age math is exact at the second — never `time.time()`.
FIXED_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime:
    """Stands in for the module-level `datetime` so `datetime.now(tz)` returns a
    fixed instant.

    `get_run_status` only calls `datetime.now`; `parse_iso_timestamp` uses
    utils.helpers' OWN datetime, so freezing here is surgical and leaves
    timestamp PARSING real.
    """

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self, tz=None) -> datetime:
        return self._fixed


def _cursor(offset_seconds: int, *, zulu: bool = True) -> str:
    """A cursor timestamp `offset_seconds` before FIXED_NOW (negative = future)."""
    ts = (FIXED_NOW - timedelta(seconds=offset_seconds)).isoformat()
    return ts.replace("+00:00", "Z") if zulu else ts


def _make_service(
    monkeypatch, *, enabled: bool, webhook: bool = False, interval: int = 300
):
    """A `CanaryService` with env set and `datetime.now` frozen to FIXED_NOW."""
    from services import canary_service as module

    monkeypatch.setenv("CANARY_ENABLED", "1" if enabled else "0")
    if webhook:
        monkeypatch.setenv("CANARY_SLACK_WEBHOOK_URL", "https://hooks.slack.com/x/y/z")
    else:
        monkeypatch.delenv("CANARY_SLACK_WEBHOOK_URL", raising=False)

    service = module.CanaryService(interval_seconds=interval)
    monkeypatch.setattr(module, "datetime", _FrozenDatetime(FIXED_NOW))
    return module, service


def _set_cursor(monkeypatch, service, value, ok: bool = True) -> None:
    """Drive `get_run_status` by controlling the (value, ok) the cursor read returns."""
    monkeypatch.setattr(service, "_read_last_cycle_for_status", lambda: (value, ok))


# ---------------------------------------------------------------------------
# get_run_status — status derivation
# ---------------------------------------------------------------------------


def test_disabled_never_reads_redis_and_never_alarms(monkeypatch):
    module, service = _make_service(monkeypatch, enabled=False)

    def _boom():
        raise AssertionError("Redis must not be consulted when disabled")

    monkeypatch.setattr(service, "_read_last_cycle_for_status", _boom)

    status = service.get_run_status()
    assert status["enabled"] is False
    assert status["status"] == "disabled"
    # The load-bearing invariant: default-OFF is NEVER an alarm.
    assert status["status"] != "stale"
    assert status["last_cycle_at"] is None
    assert status["seconds_since_last_cycle"] is None
    assert status["redis_available"] is None  # never consulted


def test_enabled_fresh_cursor_is_healthy(monkeypatch):
    module, service = _make_service(monkeypatch, enabled=True)
    _set_cursor(monkeypatch, service, _cursor(0))

    status = service.get_run_status()
    assert status["enabled"] is True
    assert status["status"] == "healthy"
    assert status["redis_available"] is True
    assert status["seconds_since_last_cycle"] == 0


def test_enabled_old_cursor_is_stale(monkeypatch):
    module, service = _make_service(monkeypatch, enabled=True)
    _set_cursor(monkeypatch, service, _cursor(service._stale_after_seconds() + 60))

    assert service.get_run_status()["status"] == "stale"


def test_failover_window_is_not_stale(monkeypatch):
    """A legitimate leader failover (~780s) must not read as stale."""
    module, service = _make_service(monkeypatch, enabled=True)
    _set_cursor(monkeypatch, service, _cursor(service._max_failover_seconds()))

    assert service.get_run_status()["status"] == "healthy"


def test_maxed_healthy_cycle_is_not_stale(monkeypatch):
    """A maxed-out-but-healthy cycle inflates the observed cursor age to
    `interval + cycle_duration` (up to 1199s at defaults) — still not stale."""
    from services.canary_service import _MAX_CYCLE_LEASE_SECONDS

    module, service = _make_service(monkeypatch, enabled=True)
    age = service.interval + _MAX_CYCLE_LEASE_SECONDS - 1  # 1199 at defaults
    _set_cursor(monkeypatch, service, _cursor(age))

    assert service.get_run_status()["status"] == "healthy"


def test_staleness_boundary_is_strict_gt(monkeypatch):
    module, service = _make_service(monkeypatch, enabled=True)
    bound = service._stale_after_seconds()

    _set_cursor(monkeypatch, service, _cursor(bound))
    assert service.get_run_status()["status"] == "healthy"  # age == bound → healthy

    _set_cursor(monkeypatch, service, _cursor(bound + 1))
    assert service.get_run_status()["status"] == "stale"  # age == bound + 1 → stale


def test_threshold_is_derived_from_the_file_constants(monkeypatch):
    from services.canary_service import _MAX_CYCLE_LEASE_SECONDS

    module, service = _make_service(monkeypatch, enabled=True)
    _set_cursor(monkeypatch, service, _cursor(0))

    status = service.get_run_status()
    assert (
        status["stale_after_seconds"]
        == service._max_failover_seconds() + _MAX_CYCLE_LEASE_SECONDS
    )
    assert status["interval_seconds"] == service.interval


def test_cursor_missing_but_redis_ok_is_unknown(monkeypatch):
    module, service = _make_service(monkeypatch, enabled=True)
    _set_cursor(monkeypatch, service, None, ok=True)

    status = service.get_run_status()
    assert status["status"] == "unknown"
    assert status["redis_available"] is True
    assert status["status"] != "stale"


def test_redis_error_is_unknown_not_stale(monkeypatch):
    module, service = _make_service(monkeypatch, enabled=True)
    _set_cursor(monkeypatch, service, None, ok=False)

    status = service.get_run_status()
    assert status["status"] == "unknown"
    assert status["redis_available"] is False
    assert status["status"] != "stale"


def test_unparseable_cursor_is_unknown(monkeypatch):
    module, service = _make_service(monkeypatch, enabled=True)
    _set_cursor(monkeypatch, service, "not-a-timestamp", ok=True)

    status = service.get_run_status()
    assert status["status"] == "unknown"
    assert status["last_cycle_at"] is None
    assert status["seconds_since_last_cycle"] is None


def test_future_cursor_is_clamped_not_negative(monkeypatch):
    """Cross-worker clock skew can future-date the cursor; the age clamps to 0."""
    module, service = _make_service(monkeypatch, enabled=True)
    _set_cursor(monkeypatch, service, _cursor(-5))  # 5s in the future

    status = service.get_run_status()
    assert status["status"] == "healthy"
    assert status["seconds_since_last_cycle"] == 0


def test_alert_sink_configured_reflects_env_when_enabled(monkeypatch):
    module, service = _make_service(monkeypatch, enabled=True, webhook=True)
    _set_cursor(monkeypatch, service, _cursor(0))

    assert service.get_run_status()["alert_sink_configured"] is True


def test_alert_sink_configured_reported_even_when_disabled(monkeypatch):
    """Read on EVERY path — an operator wiring up a canary wants the sink state
    before flipping it on."""
    module, service = _make_service(monkeypatch, enabled=False, webhook=True)

    status = service.get_run_status()
    assert status["status"] == "disabled"
    assert status["alert_sink_configured"] is True


def test_no_sink_reads_false(monkeypatch):
    module, service = _make_service(monkeypatch, enabled=True, webhook=False)
    _set_cursor(monkeypatch, service, _cursor(0))

    assert service.get_run_status()["alert_sink_configured"] is False


# ---------------------------------------------------------------------------
# _read_last_cycle_for_status — error vs missing, the contract _read_prev_cycle_at
# deliberately collapses
# ---------------------------------------------------------------------------


def test_read_helper_missing_value_is_ok_true(monkeypatch):
    from services import canary_service as module

    class _R:
        def get(self, key):
            return None

    monkeypatch.setattr(module.CanaryService, "_redis", staticmethod(lambda: _R()))
    assert module.CanaryService()._read_last_cycle_for_status() == (None, True)


def test_read_helper_error_is_ok_false(monkeypatch):
    from services import canary_service as module

    class _R:
        def get(self, key):
            raise RuntimeError("redis down")

    monkeypatch.setattr(module.CanaryService, "_redis", staticmethod(lambda: _R()))
    assert module.CanaryService()._read_last_cycle_for_status() == (None, False)


def test_read_helper_returns_value_and_ok(monkeypatch):
    from services import canary_service as module

    class _R:
        def get(self, key):
            return "2026-08-16T12:00:00Z"

    monkeypatch.setattr(module.CanaryService, "_redis", staticmethod(lambda: _R()))
    assert module.CanaryService()._read_last_cycle_for_status() == (
        "2026-08-16T12:00:00Z",
        True,
    )


# ---------------------------------------------------------------------------
# is_enabled — the public wrapper feeding the feature-flags surface
# ---------------------------------------------------------------------------


def test_is_enabled_reflects_env(monkeypatch):
    from services import canary_service as module

    monkeypatch.setenv("CANARY_ENABLED", "1")
    assert module.CanaryService.is_enabled() is True
    assert module.canary_service.is_enabled() is True

    monkeypatch.setenv("CANARY_ENABLED", "0")
    assert module.CanaryService.is_enabled() is False

    monkeypatch.delenv("CANARY_ENABLED", raising=False)
    assert module.CanaryService.is_enabled() is False


# ---------------------------------------------------------------------------
# Route + flag — the surface ships untested otherwise (#1880)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_handler_round_trips_every_field(monkeypatch):
    from routers import canary as router_module
    from models import CanaryStatusResponse

    fixed = {
        "enabled": True,
        "status": "healthy",
        "last_cycle_at": "2026-08-16T12:00:00Z",
        "seconds_since_last_cycle": 42,
        "interval_seconds": 300,
        "stale_after_seconds": 1680,
        "alert_sink_configured": True,
        "redis_available": True,
    }
    monkeypatch.setattr(
        router_module.canary_service, "get_run_status", lambda: dict(fixed)
    )

    # `_` (the require_admin dependency) is ignored by the handler.
    result = await router_module.get_canary_status(None)
    assert isinstance(result, CanaryStatusResponse)
    assert result.model_dump() == fixed


def _dependency_calls(dependant):
    """Every callable in a route's dependency tree (recursive), so a nested
    `require_admin → get_current_user` chain is reached without FastAPI's
    `get_flat_dependant` (known-drifty in this tree)."""
    calls = []
    for dep in dependant.dependencies:
        calls.append(dep.call)
        calls.extend(_dependency_calls(dep))
    return calls


def test_status_route_is_registered_and_admin_gated():
    """The `/status` handler test round-trips the model with auth bypassed
    (`None`), so it cannot catch a dropped `@router.get` or a dropped
    `Depends(require_admin)`. Assert the route is mounted at the literal
    `/api/canary/status` (a distinct root, so no Invariant #4 collision with
    `/violations/{violation_id}`) AND carries the admin gate — the one wiring
    fact the pure-handler test structurally can't see.
    """
    from routers import canary as router_module

    # Resolve `require_admin` from the router's OWN namespace, NOT a fresh
    # `from dependencies import require_admin`: under pytest-randomly a sibling
    # test can re-import `dependencies` under a second module identity, and the
    # route captured the object bound in `routers.canary` at decoration time —
    # a freshly-imported one is a DIFFERENT function object (the module-identity
    # gotcha), which false-fails this guard. `router_module.require_admin` is
    # exactly what the decorator saw, and stays internally consistent with
    # `router_module.router` even if the router module itself is reloaded.
    require_admin = router_module.require_admin

    route = next(
        (
            r
            for r in router_module.router.routes
            if getattr(r, "path", None) == "/api/canary/status"
            and "GET" in getattr(r, "methods", set())
        ),
        None,
    )
    assert route is not None, "GET /api/canary/status is not registered"
    assert require_admin in _dependency_calls(
        route.dependant
    ), "GET /api/canary/status lost its require_admin gate"


def test_feature_flags_carries_canary_enabled(monkeypatch):
    """The feature-flags handler exposes `canary_enabled == is_enabled()`.

    Stubs the DB-backed settings/entitlement/a2a services so this stays a pure
    handler test (no DB), yet still catches a dropped or misnamed key.
    """
    import asyncio
    from types import SimpleNamespace

    from routers import settings as settings_module

    # Stub the module-level settings/telemetry/db surfaces the handler reads.
    stub_settings = SimpleNamespace(
        is_brain_orb_enabled=lambda: False,
        is_session_tab_enabled=lambda: False,
        is_workspace_enabled=lambda: False,
        is_brain_orb_voice_enabled=lambda: False,
        is_brain_orb_write_enabled=lambda: False,
        get_elevenlabs_api_key=lambda: None,
        get_platform_default_model=lambda: "model",
        get_anthropic_api_key=lambda: None,
        # #2380 — install provenance. Hand-rolled stub, so every field the
        # handler reads has to be present or the whole endpoint AttributeErrors
        # and this test goes red for a reason that has nothing to do with the
        # canary. Values are irrelevant here; `test_2380_install_provenance.py`
        # owns their contract.
        get_install_source=lambda: "unknown",
        is_marketplace_install=lambda: False,
        get_install_tls_posture=lambda: "unconfigured",
    )
    monkeypatch.setattr(settings_module, "settings_service", stub_settings)
    monkeypatch.setattr(
        settings_module,
        "telemetry_sharing_service",
        SimpleNamespace(
            is_consent_enabled=lambda: False,
            # ent#437: the flags document now spreads the four consent bools.
            public_flags=lambda: {"telemetry_sharing_enabled": False},
        ),
    )
    monkeypatch.setattr(settings_module.db, "has_any_subscription", lambda: False)

    # Function-local imports inside the handler — stub the DB-touching ones.
    import services.a2a_outbound_service as a2a_module
    from services.entitlement_service import entitlement_service

    monkeypatch.setattr(a2a_module, "is_outbound_enabled", lambda: False)
    monkeypatch.setattr(entitlement_service, "list_entitled_features", lambda: [])

    monkeypatch.setenv("CANARY_ENABLED", "1")
    from services.canary_service import canary_service

    flags = asyncio.run(settings_module.get_public_feature_flags(current_user=None))
    assert "canary_enabled" in flags
    assert flags["canary_enabled"] == canary_service.is_enabled() is True

    monkeypatch.setenv("CANARY_ENABLED", "0")
    flags_off = asyncio.run(settings_module.get_public_feature_flags(current_user=None))
    assert flags_off["canary_enabled"] is False
