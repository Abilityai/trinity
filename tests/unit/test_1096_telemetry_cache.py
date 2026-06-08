"""Unit tests for #1096 — non-blocking /api/telemetry/containers.

`container.stats(stream=False)` costs ~1-2s per container, so the aggregate
endpoint blocked for `ceil(N/pool) * ~1.5s` (~6s at 11 agents), pinning a
uvicorn worker and stalling the whole UI. The fix serves the payload from a
short-TTL, background-refreshed cache (stale-while-revalidate): the request
path NEVER awaits the Docker daemon.

These tests prove:
  - a fresh cache hit returns WITHOUT touching Docker (no blocking call);
  - a cold cache returns an instant, valid, empty payload (no inline compute)
    and schedules a background refresh that then populates the cache;
  - a stale cache serves the old payload immediately and schedules a refresh;
  - the original response contract is preserved (backward-compat);
  - the refresh aggregation is correct and is the ONLY place Docker is touched;
  - env parsing is defensive (a bad value can't crash the router at import).

True unit tests: the Docker seam is monkeypatched, no daemon / backend needed.

Issue: https://github.com/Abilityai/trinity/issues/1096
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


# Point the backend at an ephemeral SQLite file BEFORE any backend module
# imports (database.py tries to mkdir /data on import otherwise). The unit
# conftest already does this; belt-and-suspenders for direct invocation.
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


pytestmark = pytest.mark.unit


# Modules this file installs into sys.modules at collection time (the loader
# below registers the importlib-loaded router and briefly swaps stubs). Snapshot
# + restore them around every test so they can't leak into sibling test files —
# this is the sanctioned escape hatch recognized by tests/lint_sys_modules.py
# (precedent: tests/unit/test_telegram_webhook_backfill.py).
_STUBBED_MODULE_NAMES = [
    "routers.telemetry",
    "dependencies",
    "services.docker_service",
    "psutil",
    "fastapi",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot sys.modules before each test and restore after."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# ── Importlib-load routers/telemetry.py without dragging in routers/__init__ ──
#
# `from routers import telemetry` would import 50+ unrelated routers via
# routers/__init__.py. Load the file directly with scoped dependency stubs,
# mirroring tests/unit/test_monitoring_router_signatures.py.

def _make_psutil_stub():
    """telemetry.py imports psutil and primes cpu_percent() at module load.
    The host endpoint isn't under test here, so a minimal stub keeps this a
    dependency-light unit test (psutil need not be installed)."""
    stub = types.ModuleType("psutil")
    stub.cpu_percent = lambda interval=None: 0.0
    stub.cpu_count = lambda: 1
    stub.virtual_memory = lambda: types.SimpleNamespace(
        percent=0.0, used=0, total=1
    )
    stub.disk_usage = lambda _p: types.SimpleNamespace(percent=0.0, used=0, total=1)
    return stub


def _load_telemetry_router():
    _deps = types.ModuleType("dependencies")
    _deps.get_current_user = lambda: None

    _docker_svc = types.ModuleType("services.docker_service")
    _docker_svc.docker_client = object()          # truthy → not the 503 branch
    _docker_svc.list_all_agents_fast = MagicMock(return_value=[])

    # test_inject_assigned_credentials.py can overwrite sys.modules['fastapi']
    # with a Mock at collection time; evict briefly so @router.get() binds the
    # real decorator, then restore.
    _saved_fastapi = sys.modules.pop("fastapi", None)
    import fastapi as _real_fastapi  # noqa: PLC0415
    if _saved_fastapi is not None:
        sys.modules["fastapi"] = _saved_fastapi

    path = _BACKEND / "routers" / "telemetry.py"
    spec = importlib.util.spec_from_file_location("routers.telemetry", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["routers.telemetry"] = mod
    with patch.dict(sys.modules, {
        "dependencies": _deps,
        "services.docker_service": _docker_svc,
        "fastapi": _real_fastapi,
        "psutil": _make_psutil_stub(),
    }):
        spec.loader.exec_module(mod)
    return mod


telemetry = _load_telemetry_router()


# ── Helpers ───────────────────────────────────────────────────────────────

class _FakeAgent:
    """Stand-in for the dataclass returned by list_all_agents_fast()."""

    def __init__(self, name: str, status: str = "running"):
        self.name = name
        self.status = status


_ORIGINAL_FIELDS = {
    "running_count",
    "total_cpu_percent",
    "total_memory_mb",
    "containers",
    "timestamp",
}


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset the module-level cache + in-flight task around every test so
    cases don't leak state into one another."""
    telemetry._stats_cache.update(timestamp=0.0, data=None)
    telemetry._refresh_task = None
    yield
    telemetry._stats_cache.update(timestamp=0.0, data=None)
    telemetry._refresh_task = None


def _seed_fresh(payload: dict) -> None:
    telemetry._stats_cache.update(timestamp=time.monotonic(), data=payload)


def _seed_stale(payload: dict) -> None:
    telemetry._stats_cache.update(
        timestamp=time.monotonic() - (telemetry._CACHE_TTL + 100), data=payload
    )


# ── Fresh cache hit: NO Docker call, NO refresh scheduled ───────────────────

@pytest.mark.asyncio
async def test_fresh_cache_hit_does_not_touch_docker(monkeypatch):
    """A fresh cache must be returned verbatim with no blocking Docker work
    and without scheduling a refresh."""
    def _boom_single(name):  # pragma: no cover - must never run
        raise AssertionError("blocking Docker call on the request path!")

    def _boom_list():  # pragma: no cover - must never run
        raise AssertionError("agent listing on the request path!")

    monkeypatch.setattr(telemetry, "_get_single_container_stats_sync", _boom_single)
    monkeypatch.setattr(telemetry, "list_all_agents_fast", _boom_list)

    cached = {
        "running_count": 2,
        "total_cpu_percent": 15.0,
        "total_memory_mb": 150.0,
        "containers": [{"name": "a", "cpu": 10.0, "memory_mb": 100.0}],
        "timestamp": "2026-06-08T00:00:00Z",
    }
    _seed_fresh(cached)

    res = await telemetry.get_container_stats(current_user=None)

    assert res["cached"] is True
    assert res["stale"] is False
    assert res["running_count"] == 2
    assert res["cache_age_seconds"] is not None and res["cache_age_seconds"] < telemetry._CACHE_TTL
    assert _ORIGINAL_FIELDS <= set(res)          # backward-compatible contract
    assert telemetry._refresh_task is None       # fresh → no refresh scheduled


# ── Cold start: instant empty payload, NO inline compute, refresh fills cache ─

@pytest.mark.asyncio
async def test_cold_start_returns_instant_empty_then_refreshes(monkeypatch):
    """First-ever call (no cached data) must return an instant valid empty
    payload — proof it did NOT run the ~6s compute inline — then a background
    refresh populates the cache for the next poll."""
    monkeypatch.setattr(telemetry, "list_all_agents_fast", lambda: [_FakeAgent("a")])
    monkeypatch.setattr(
        telemetry, "_get_single_container_stats_sync",
        lambda name: {"name": name, "cpu": 5.0, "memory_mb": 100.0},
    )

    res = await telemetry.get_container_stats(current_user=None)

    # Returned the cold payload — could only happen if compute was NOT awaited.
    assert res["cached"] is False
    assert res["stale"] is True
    assert res["running_count"] == 0
    assert res["containers"] == []
    assert res["cache_age_seconds"] is None
    assert _ORIGINAL_FIELDS <= set(res)

    # A background refresh was scheduled; awaiting it fills the cache.
    assert telemetry._refresh_task is not None
    await telemetry._refresh_task
    assert telemetry._stats_cache["data"] is not None
    assert telemetry._stats_cache["data"]["running_count"] == 1
    assert telemetry._stats_cache["data"]["total_memory_mb"] == 100.0


# ── Stale cache: serve old data now, schedule refresh ───────────────────────

@pytest.mark.asyncio
async def test_stale_cache_serves_old_data_and_refreshes(monkeypatch):
    monkeypatch.setattr(telemetry, "list_all_agents_fast", lambda: [_FakeAgent("a")])
    monkeypatch.setattr(
        telemetry, "_get_single_container_stats_sync",
        lambda name: {"name": name, "cpu": 1.0, "memory_mb": 10.0},
    )

    stale = {
        "running_count": 2,
        "total_cpu_percent": 99.0,
        "total_memory_mb": 999.0,
        "containers": [{"name": "old", "cpu": 99.0, "memory_mb": 999.0}],
        "timestamp": "2026-06-08T00:00:00Z",
    }
    _seed_stale(stale)

    res = await telemetry.get_container_stats(current_user=None)

    # Served the STALE payload immediately (running_count 2, not the fresh 1).
    assert res["cached"] is True
    assert res["stale"] is True
    assert res["running_count"] == 2
    assert res["cache_age_seconds"] >= telemetry._CACHE_TTL
    assert telemetry._refresh_task is not None

    # The scheduled refresh recomputes to the new value.
    await telemetry._refresh_task
    assert telemetry._stats_cache["data"]["running_count"] == 1


# ── Docker unavailable → 503 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_docker_unavailable_returns_503(monkeypatch):
    monkeypatch.setattr(telemetry, "docker_client", None)
    with pytest.raises(HTTPException) as exc:
        await telemetry.get_container_stats(current_user=None)
    assert exc.value.status_code == 503


# ── Refresh aggregation correctness (the ONLY place Docker is touched) ──────

@pytest.mark.asyncio
async def test_compute_aggregates_only_running_and_sorts_by_cpu(monkeypatch):
    monkeypatch.setattr(
        telemetry, "list_all_agents_fast",
        lambda: [_FakeAgent("a"), _FakeAgent("b"), _FakeAgent("c", "exited")],
    )
    per = {
        "a": {"name": "a", "cpu": 5.0, "memory_mb": 50.0},
        "b": {"name": "b", "cpu": 10.0, "memory_mb": 100.0},
    }
    monkeypatch.setattr(telemetry, "_get_single_container_stats_sync", lambda n: per[n])

    payload = await telemetry._compute_container_stats()

    assert payload["running_count"] == 2                       # 'c' excluded (exited)
    assert payload["total_cpu_percent"] == 15.0
    assert payload["total_memory_mb"] == 150.0
    assert [c["name"] for c in payload["containers"]] == ["b", "a"]  # CPU desc


@pytest.mark.asyncio
async def test_compute_excludes_errored_containers_from_totals(monkeypatch):
    monkeypatch.setattr(
        telemetry, "list_all_agents_fast",
        lambda: [_FakeAgent("a"), _FakeAgent("b")],
    )
    per = {
        "a": {"name": "a", "cpu": 7.0, "memory_mb": 70.0},
        "b": {"name": "b", "cpu": 0, "memory_mb": 0, "error": "boom"},
    }
    monkeypatch.setattr(telemetry, "_get_single_container_stats_sync", lambda n: per[n])

    payload = await telemetry._compute_container_stats()

    assert payload["running_count"] == 2          # both running...
    assert len(payload["containers"]) == 2        # ...both listed...
    assert payload["total_cpu_percent"] == 7.0    # ...but errored one not summed
    assert payload["total_memory_mb"] == 70.0


@pytest.mark.asyncio
async def test_compute_skips_raw_exceptions_from_gather(monkeypatch):
    """If a per-container fetch RAISES (rather than returning an error-dict),
    asyncio.gather(return_exceptions=True) yields an Exception object in the
    results. It must be skipped from BOTH the totals and the container list —
    parity with the original's explicit `isinstance(result, Exception)` guard.
    `_get_single_container_stats_sync` normally swallows everything, so this
    pins the defensive branch against a future regression."""
    monkeypatch.setattr(
        telemetry, "list_all_agents_fast",
        lambda: [_FakeAgent("ok"), _FakeAgent("boom")],
    )

    def _maybe_raise(name):
        if name == "boom":
            raise RuntimeError("docker exploded mid-fetch")
        return {"name": name, "cpu": 3.0, "memory_mb": 30.0}

    monkeypatch.setattr(telemetry, "_get_single_container_stats_sync", _maybe_raise)

    payload = await telemetry._compute_container_stats()

    assert payload["running_count"] == 2                          # both agents running
    assert [c["name"] for c in payload["containers"]] == ["ok"]    # raised one excluded
    assert payload["total_cpu_percent"] == 3.0                     # excluded from totals
    assert payload["total_memory_mb"] == 30.0


@pytest.mark.asyncio
async def test_refresh_failure_leaves_previous_cache_intact(monkeypatch):
    """A Docker hiccup during refresh must not blow away good cached data nor
    raise — best-effort metrics."""
    good = {
        "running_count": 1,
        "total_cpu_percent": 1.0,
        "total_memory_mb": 1.0,
        "containers": [{"name": "a", "cpu": 1.0, "memory_mb": 1.0}],
        "timestamp": "2026-06-08T00:00:00Z",
    }
    _seed_fresh(good)

    def _boom():
        raise RuntimeError("docker boom")

    monkeypatch.setattr(telemetry, "list_all_agents_fast", _boom)

    await telemetry._refresh_cache()  # must not raise

    assert telemetry._stats_cache["data"] == good  # untouched


@pytest.mark.asyncio
async def test_concurrent_requests_schedule_single_refresh(monkeypatch):
    """Many simultaneous stale/cold requests must coalesce to ONE in-flight
    refresh task (no thundering herd of Docker sweeps)."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    def _list():
        calls["n"] += 1
        return [_FakeAgent("a")]

    monkeypatch.setattr(telemetry, "list_all_agents_fast", _list)
    monkeypatch.setattr(
        telemetry, "_get_single_container_stats_sync",
        lambda n: {"name": n, "cpu": 1.0, "memory_mb": 1.0},
    )

    # Cold cache for all callers.
    results = await asyncio.gather(
        *[telemetry.get_container_stats(current_user=None) for _ in range(8)]
    )

    assert all(r["stale"] is True for r in results)
    assert telemetry._refresh_task is not None
    await telemetry._refresh_task
    # Exactly one refresh ran despite 8 concurrent cold requests.
    assert calls["n"] == 1


# ── Defensive env parsing (runs at import; must never raise) ────────────────

def test_parse_float_env_is_defensive():
    assert telemetry._parse_float_env(None, 10.0) == 10.0
    assert telemetry._parse_float_env("not-a-number", 10.0) == 10.0
    assert telemetry._parse_float_env("", 10.0) == 10.0
    assert telemetry._parse_float_env("5.5", 10.0) == 5.5
    assert telemetry._parse_float_env("-3", 10.0, minimum=0.0) == 0.0   # clamped
    assert telemetry._parse_float_env("0", 10.0, minimum=0.0) == 0.0    # disables cache


def test_parse_pool_size_is_defensive():
    assert telemetry._parse_pool_size(None, 16) == 16
    assert telemetry._parse_pool_size("not-an-int", 16) == 16
    assert telemetry._parse_pool_size("", 16) == 16
    assert telemetry._parse_pool_size("8", 16) == 8
    assert telemetry._parse_pool_size("0", 16) == 1                       # floor
    assert telemetry._parse_pool_size("99999", 16) == telemetry._POOL_SIZE_MAX  # cap
