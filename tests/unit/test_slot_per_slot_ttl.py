"""
Issue #869 — cleanup watchdog falsely kills long-running executions whose
per-schedule ``timeout_seconds`` exceeds ``agent_ownership.execution_timeout_seconds``.

Root cause: ``_cleanup_stale_slots_for_agent`` used a single per-agent
cutoff (``agent_ownership.execution_timeout_seconds + SLOT_TTL_BUFFER``)
applied via ZREMRANGEBYSCORE. A slot acquired with a schedule-level
``timeout_seconds=7200`` would be reclaimed at ~65 min (3900s) because the
agent's ownership default was 3600s — even though the slot's own effective
TTL was 7500s (7200 + 300 buffer).

Fix: each slot's effective TTL is now read from its stored metadata HASH
(``timeout_seconds`` field, written at ``acquire_slot`` time). The per-agent
default only applies when metadata is absent.

Covered scenarios:

1. Slot with long schedule timeout (7200s) is NOT reclaimed at 65 min when
   metadata is present — the former false-positive kill case.
2. Same slot IS reclaimed after its actual deadline (7500s) passes.
3. Slot with no metadata falls back to the per-agent default TTL.
4. Slot with short agent-default timeout (3600s) is still reclaimed after
   3900s (backwards-compatible behaviour).
5. Mixed agents: one long-timeout slot (not stale), one short (stale) — only
   the short one is reclaimed.
6. WATCHDOG_HTTP_TIMEOUT is 15 s (increased from 5 s — #869 secondary fix).
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Backend on sys.path; docker is imported eagerly by services/__init__.py
# ---------------------------------------------------------------------------

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# Modules this test stubs into sys.modules (directly or via the
# importlib-loaded slot_service_direct / cleanup_service_direct helpers)
# — restored after each test so they don't leak into other test files in
# the same pytest session. Declaring this at module level (paired with
# the `_restore_sys_modules` fixture below) is the sanctioned
# snapshot/restore pattern recognised by tests/lint_sys_modules.py
# (precedent: tests/unit/test_telegram_webhook_backfill.py).
_STUBBED_MODULE_NAMES = [
    "config",
    "redis",
    "utils.helpers",
    "utils.credential_sanitizer",
    "database",
    "models",
    "services.capacity_manager",
    "slot_service_direct",
    "cleanup_service_direct",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {n: sys.modules.get(n) for n in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# ---------------------------------------------------------------------------
# FakeRedis — minimal surface area for SlotService._cleanup_stale_slots_for_agent
# ---------------------------------------------------------------------------

class _FakeRedis:
    """Sync Redis double covering ZSET and HASH operations used by SlotService."""

    def __init__(self):
        # key → {member: score}
        self.zsets: dict[str, dict[str, float]] = {}
        # key → {field: value}
        self.hashes: dict[str, dict[str, str]] = {}

    # ── ZSET helpers ──────────────────────────────────────────────────────

    def _zadd_raw(self, key: str, member: str, score: float) -> None:
        """Test setup helper."""
        self.zsets.setdefault(key, {})[member] = score

    def zrange(self, key: str, start: int, stop: int, withscores: bool = False):
        members = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        sliced = members[start:] if stop == -1 else members[start:stop + 1]
        if withscores:
            return [(m, s) for m, s in sliced]
        return [m for m, _ in sliced]

    def zrem(self, key: str, *members: str) -> int:
        removed = 0
        for m in members:
            if key in self.zsets and m in self.zsets[key]:
                del self.zsets[key][m]
                removed += 1
        return removed

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    # ── HASH helpers ──────────────────────────────────────────────────────

    def _hset_raw(self, key: str, **fields) -> None:
        """Test setup helper."""
        self.hashes.setdefault(key, {}).update(fields)

    def hget(self, key: str, field: str):
        return self.hashes.get(key, {}).get(field)

    def hset(self, key: str, mapping: dict | None = None, **kwargs) -> int:
        data = self.hashes.setdefault(key, {})
        if mapping:
            data.update(mapping)
        data.update(kwargs)
        return len(mapping or {}) + len(kwargs)

    def expire(self, key: str, ttl: int) -> int:
        return 1

    def delete(self, key: str) -> int:
        removed = 0
        if key in self.zsets:
            del self.zsets[key]
            removed += 1
        if key in self.hashes:
            del self.hashes[key]
            removed += 1
        return removed

    def scan(self, cursor: int, match: str = "*", count: int = 100):
        prefix = match.rstrip("*")
        keys = [k for k in self.zsets if k.startswith(prefix)]
        return 0, keys

    def zadd(self, key: str, mapping: dict) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zrangebyscore(self, key: str, min_score, max_score, withscores: bool = False):
        min_s = float("-inf") if min_score == "-inf" else float(min_score)
        max_s = float("+inf") if max_score == "+inf" else float(max_score)
        members = [
            (m, s) for m, s in self.zsets.get(key, {}).items()
            if min_s <= s <= max_s
        ]
        members.sort(key=lambda kv: kv[1])
        if withscores:
            return members
        return [m for m, _ in members]


# ---------------------------------------------------------------------------
# Fixture: SlotService backed by FakeRedis (no live broker needed)
# ---------------------------------------------------------------------------

def _load_slot_service_module():
    """Load slot_service.py directly, bypassing services/__init__.py.

    The services package __init__ imports docker_service eagerly, which
    requires the Docker SDK. Loading the file directly avoids that chain
    and lets us test SlotService in isolation.
    """
    import importlib.util

    slot_path = _BACKEND / "services" / "slot_service.py"

    # Provide the module-level dependencies slot_service.py needs.
    sys.modules.setdefault(
        "config",
        types.SimpleNamespace(REDIS_URL="redis://localhost:6379/0"),
    )
    # utils.helpers: only utc_now_iso is needed at module level
    if "utils.helpers" not in sys.modules:
        helpers_stub = types.ModuleType("utils.helpers")
        helpers_stub.utc_now_iso = lambda: "2026-01-01T00:00:00Z"
        sys.modules["utils.helpers"] = helpers_stub

    spec = importlib.util.spec_from_file_location("slot_service_direct", slot_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fake_redis():
    return _FakeRedis()


@pytest.fixture()
def slot_service(fake_redis):
    """SlotService instance backed by FakeRedis, loaded without docker deps."""
    mod = _load_slot_service_module()
    svc = mod.SlotService.__new__(mod.SlotService)
    svc.redis = fake_redis
    svc.slots_prefix = "agent:slots:"
    svc.metadata_prefix = "agent:slot:"
    svc._on_release_callbacks = []
    return svc


def _get_constants():
    mod = _load_slot_service_module()
    return mod.SLOT_TTL_BUFFER, mod.DEFAULT_SLOT_TTL_SECONDS


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slot_age(seconds: float) -> float:
    """Return the ZSET score (epoch) for a slot that is `seconds` old."""
    return time.time() - seconds


def _add_slot(fake_redis: _FakeRedis, agent: str, exec_id: str,
              age_seconds: float, slot_timeout: int | None = None) -> None:
    """Insert a slot into FakeRedis, optionally with stored timeout metadata."""
    slots_key = f"agent:slots:{agent}"
    meta_key = f"agent:slot:{agent}:{exec_id}"
    score = _slot_age(age_seconds)
    fake_redis._zadd_raw(slots_key, exec_id, score)
    if slot_timeout is not None:
        fake_redis._hset_raw(meta_key, timeout_seconds=str(slot_timeout))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPerSlotTTL:
    """``_cleanup_stale_slots_for_agent`` uses per-slot metadata TTL (#869)."""

    def test_long_schedule_timeout_not_reclaimed_at_65_min(self, slot_service, fake_redis):
        """
        Scenario: agent default = 3600s, schedule timeout = 7200s.
        Slot age = 3950s (just past agent-default+buffer = 3900s).
        OLD code: would reclaim → false kill.
        NEW code: reads stored timeout=7200, effective TTL=7500s → NOT stale.
        """
        _add_slot(fake_redis, "agent-a", "exec-1",
                  age_seconds=3950, slot_timeout=7200)

        stale = _run(slot_service._cleanup_stale_slots_for_agent(
            "agent-a", default_slot_ttl=3900  # agent default (3600+300)
        ))

        assert stale == [], (
            "Slot should not be reclaimed at 65 min when schedule timeout is 7200s"
        )
        # Slot still in Redis
        assert "exec-1" in fake_redis.zsets.get("agent:slots:agent-a", {})

    def test_long_schedule_timeout_reclaimed_after_deadline(self, slot_service, fake_redis):
        """
        Same agent, same schedule (7200s), but slot is 7600s old — past its
        effective TTL of 7500s (7200 + 300 buffer). Must be reclaimed.
        """
        _add_slot(fake_redis, "agent-a", "exec-1",
                  age_seconds=7600, slot_timeout=7200)

        stale = _run(slot_service._cleanup_stale_slots_for_agent(
            "agent-a", default_slot_ttl=7500
        ))

        assert stale == ["exec-1"]
        assert "exec-1" not in fake_redis.zsets.get("agent:slots:agent-a", {})

    def test_no_metadata_falls_back_to_default_ttl(self, slot_service, fake_redis):
        """
        Slot with no stored metadata falls back to default_slot_ttl.
        If age > default_slot_ttl → reclaim; otherwise → leave.
        """
        # Slot age 1500s, no metadata, default=1200s → stale
        _add_slot(fake_redis, "agent-b", "exec-old", age_seconds=1500, slot_timeout=None)
        # Slot age 500s, no metadata, default=1200s → not stale
        _add_slot(fake_redis, "agent-b", "exec-new", age_seconds=500, slot_timeout=None)

        stale = _run(slot_service._cleanup_stale_slots_for_agent(
            "agent-b", default_slot_ttl=1200
        ))

        assert stale == ["exec-old"]
        assert "exec-new" in fake_redis.zsets.get("agent:slots:agent-b", {})

    def test_agent_default_timeout_still_reclaimed_after_deadline(self, slot_service, fake_redis):
        """
        Standard case: slot acquired with agent-default timeout (3600s),
        slot age > 3900s → reclaim. Backwards-compatible behaviour.
        """
        _add_slot(fake_redis, "agent-c", "exec-1",
                  age_seconds=4000, slot_timeout=3600)

        stale = _run(slot_service._cleanup_stale_slots_for_agent(
            "agent-c", default_slot_ttl=3900
        ))

        assert stale == ["exec-1"]

    def test_agent_default_timeout_not_reclaimed_within_deadline(self, slot_service, fake_redis):
        """
        Slot with default timeout 3600s at age 3800s — within 3900s TTL. Not stale.
        """
        _add_slot(fake_redis, "agent-d", "exec-1",
                  age_seconds=3800, slot_timeout=3600)

        stale = _run(slot_service._cleanup_stale_slots_for_agent(
            "agent-d", default_slot_ttl=3900
        ))

        assert stale == []

    def test_mixed_slots_only_stale_one_reclaimed(self, slot_service, fake_redis):
        """
        Two slots on same agent:
        - exec-long: schedule timeout 7200s, age 3950s → NOT stale
        - exec-short: schedule timeout 3600s, age 3950s → stale (>3900s effective TTL)
        Only exec-short must be reclaimed.
        """
        _add_slot(fake_redis, "agent-e", "exec-long",
                  age_seconds=3950, slot_timeout=7200)
        _add_slot(fake_redis, "agent-e", "exec-short",
                  age_seconds=3950, slot_timeout=3600)

        stale = _run(slot_service._cleanup_stale_slots_for_agent(
            "agent-e", default_slot_ttl=3900
        ))

        assert stale == ["exec-short"]
        assert "exec-long" in fake_redis.zsets.get("agent:slots:agent-e", {})
        assert "exec-short" not in fake_redis.zsets.get("agent:slots:agent-e", {})

    def test_empty_agent_returns_empty_list(self, slot_service, fake_redis):
        """No slots → no reclaims, no crash."""
        stale = _run(slot_service._cleanup_stale_slots_for_agent(
            "agent-ghost", default_slot_ttl=1200
        ))
        assert stale == []

    def test_corrupt_metadata_timeout_falls_back_to_default(self, slot_service, fake_redis):
        """Garbage timeout_seconds in metadata → falls back to default_slot_ttl."""
        _add_slot(fake_redis, "agent-f", "exec-1",
                  age_seconds=1500, slot_timeout=None)
        # Corrupt the stored value
        fake_redis._hset_raw("agent:slot:agent-f:exec-1", timeout_seconds="not-a-number")

        # default_slot_ttl=1200, age=1500 → should reclaim
        stale = _run(slot_service._cleanup_stale_slots_for_agent(
            "agent-f", default_slot_ttl=1200
        ))
        assert stale == ["exec-1"]

    def test_cleanup_stale_slots_uses_per_slot_ttl_via_metadata(self, slot_service, fake_redis):
        """
        End-to-end: ``cleanup_stale_slots`` with agent_timeouts={"agent-g": 3600}
        must NOT reclaim a slot whose stored metadata says timeout=7200s even
        though the slot age (4000s) exceeds the agent-default TTL (3900s).
        """
        _add_slot(fake_redis, "agent-g", "exec-long",
                  age_seconds=4000, slot_timeout=7200)

        result = _run(slot_service.cleanup_stale_slots(
            agent_timeouts={"agent-g": 3600}
        ))

        # The long-timeout slot must NOT appear in the reclaimed dict.
        assert "agent-g" not in result, (
            "cleanup_stale_slots should not reclaim a slot whose per-slot "
            "timeout (7200s) has not yet elapsed"
        )


# ---------------------------------------------------------------------------
# WATCHDOG_HTTP_TIMEOUT secondary fix
# ---------------------------------------------------------------------------

def test_watchdog_http_timeout_increased():
    """
    #869 secondary fix: WATCHDOG_HTTP_TIMEOUT must be ≥ 15 s so agents
    under load are not declared unreachable by a 5 s HTTP timeout.
    """
    import importlib.util

    cleanup_path = _BACKEND / "services" / "cleanup_service.py"

    # Stub all transitive imports cleanup_service needs at module level.
    sys.modules.setdefault("config", types.SimpleNamespace(REDIS_URL="redis://localhost"))
    for mod_name in ["database", "models", "utils.helpers", "utils.credential_sanitizer",
                     "services.capacity_manager"]:
        if mod_name not in sys.modules:
            stub = MagicMock(name=mod_name)
            sys.modules[mod_name] = stub

    spec = importlib.util.spec_from_file_location("cleanup_service_direct", cleanup_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        timeout = mod.WATCHDOG_HTTP_TIMEOUT
    except Exception:
        # If direct load fails due to deep deps, grep the source directly
        import re
        src = cleanup_path.read_text()
        match = re.search(r"WATCHDOG_HTTP_TIMEOUT\s*=\s*([\d.]+)", src)
        assert match, "WATCHDOG_HTTP_TIMEOUT constant not found in cleanup_service.py"
        timeout = float(match.group(1))

    assert timeout >= 15.0, (
        f"WATCHDOG_HTTP_TIMEOUT should be ≥ 15 s to handle agents under load "
        f"(#869), got {timeout}"
    )
