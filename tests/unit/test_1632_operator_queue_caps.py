"""Regression + security tests for #1632 — operator-queue ingestion caps.

The operator-queue **create path** (the agent-authored `~/.trinity/operator-queue.json`
sync-ingestion boundary in `OperatorQueueSyncService._sync_agent`) previously
accepted unbounded agent input with no per-agent ingestion cap. #1402 makes this
queue the approval channel for irreversible actions, so a compromised /
prompt-injected agent that floods plausible "approve this" items causes operator
fatigue → reflexive approval. These tests pin the security-complete bound:

  - a DB-measured pending-DEPTH cap (primary, Redis-independent),
  - a per-agent + fleet RATE cap (burst smoothing, fail-open),
  - total field-hygiene clamp (truncate-with-marker), inside the #1525 try/except,
  - the reserved-platform-id guard (an agent can't suppress its own alarm),
  - the leader lock (one syncing worker under `--workers 2`),
  - the aggregated flood alert (one per episode), and
  - the platform exemption (direct DB creates bypass every cap).

Pure/mocked — no backend or DB (the DB belt is tested with a stubbed engine).
"""
import json
import sys
import os
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src",
    "backend",
)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import services.operator_queue_service as oqs  # noqa: E402
from services.operator_queue_service import (  # noqa: E402
    OperatorQueueSyncService,
    _clamp_ingested_item,
    _truncate_with_marker,
    _valid_execution_id,
    _TRUNC_MARKER,
    _OPTIONS_DROPPED_MARKER,
    OPERATOR_QUEUE_TITLE_MAX,
    OPERATOR_QUEUE_QUESTION_MAX,
    OPERATOR_QUEUE_CONTEXT_MAX_BYTES,
    OPERATOR_QUEUE_MAX_PENDING_PER_AGENT,
)
from services.rate_limiter import RateLimitResult  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Wiring helpers (mirror test_1525_operator_queue_quarantine.py)
# ---------------------------------------------------------------------------

def _allowed():
    return RateLimitResult(True, 10, 0, 60)


def _denied():
    return RateLimitResult(False, 0, 60, 60)


def _fake_db(pending=0, exists=False, create_side_effect=None):
    db = MagicMock()
    db.operator_queue_item_exists.return_value = exists
    db.count_operator_queue_pending_for_agent.return_value = pending
    if create_side_effect is not None:
        db.create_operator_queue_item.side_effect = create_side_effect
    db.mark_operator_queue_acknowledged.return_value = False
    db.get_operator_queue_responded_for_agent.return_value = []
    db.get_operator_queue_terminal_for_agent.return_value = []
    return db


def _wire(monkeypatch, db, requests, rate=None):
    """Wire a service instance to a fake db + fake agent file. `rate` optionally
    stubs rate_limiter.check (default: allow all)."""
    monkeypatch.setattr(oqs, "db", db)
    client = MagicMock()
    client.read_file = AsyncMock(return_value={
        "success": True,
        "content": json.dumps({"requests": list(requests)}),
    })
    monkeypatch.setattr(oqs, "AgentClient", lambda name: client)
    if rate is None:
        rate = lambda *a, **k: _allowed()  # noqa: E731
    monkeypatch.setattr(oqs.rate_limiter, "check", rate)
    svc = OperatorQueueSyncService()
    svc._write_responses_to_agent = AsyncMock()
    return svc


def _created_agent_items(db):
    """Creates that are NOT the platform flood alert."""
    return [
        c.args[1]
        for c in db.create_operator_queue_item.call_args_list
        if not str(c.args[1].get("id", "")).startswith("queue-flood-")
    ]


def _flood_alerts(db):
    return [
        c.args[1]
        for c in db.create_operator_queue_item.call_args_list
        if str(c.args[1].get("id", "")).startswith("queue-flood-")
    ]


def _pending(n, start=0):
    return [
        {"id": f"req-{i}", "status": "pending", "title": "t", "question": "q"}
        for i in range(start, start + n)
    ]


# ---------------------------------------------------------------------------
# 1. Depth cap (primary bound)
# ---------------------------------------------------------------------------

class TestDepthCap:
    def test_admits_up_to_cap_then_holds_and_alerts(self, monkeypatch):
        # 40 unique pending items, 20 already pending, cap 25 → exactly 5 admitted.
        db = _fake_db(pending=20)
        svc = _wire(monkeypatch, db, _pending(40))

        asyncio.run(svc._sync_agent("a"))

        assert len(_created_agent_items(db)) == 5
        assert len(_flood_alerts(db)) == 1  # one aggregated summary alert

    def test_at_cap_admits_zero_no_infinite_drip(self, monkeypatch):
        # Already at the cap (25 pending). Two cycles must admit ZERO more items
        # (the deferred-not-dropped / per-cycle DoS class) and emit at most one
        # alert (cooldown).
        db = _fake_db(pending=OPERATOR_QUEUE_MAX_PENDING_PER_AGENT)
        svc = _wire(monkeypatch, db, _pending(30))

        asyncio.run(svc._sync_agent("a"))
        asyncio.run(svc._sync_agent("a"))

        assert _created_agent_items(db) == []            # never drips past the cap
        assert len(_flood_alerts(db)) == 1               # one alert/episode (cooldown)

    def test_depth_break_bounds_exists_probes(self, monkeypatch):
        # C1: the depth cap must STOP the scan (break), not keep probing exists()
        # for every item in a huge file.
        db = _fake_db(pending=OPERATOR_QUEUE_MAX_PENDING_PER_AGENT)
        svc = _wire(monkeypatch, db, _pending(500))

        asyncio.run(svc._sync_agent("a"))

        # At the cap the very first admittable item breaks — exists() is probed
        # at most once (for that first item), never 500×.
        assert db.operator_queue_item_exists.call_count <= 1


# ---------------------------------------------------------------------------
# 2. Rate cap (burst smoothing) + short-circuit
# ---------------------------------------------------------------------------

class TestRateCap:
    def test_allow_then_deny_bounds_admits_and_breaks(self, monkeypatch):
        # Allow the first 4 check() calls (2 items × per-agent+fleet), deny the
        # 5th (item 3's per-agent check) → 2 admitted, then break.
        calls = {"n": 0}

        def _rate(*a, **k):
            calls["n"] += 1
            return _allowed() if calls["n"] <= 4 else _denied()

        db = _fake_db(pending=0)
        svc = _wire(monkeypatch, db, _pending(10), rate=_rate)

        asyncio.run(svc._sync_agent("a"))

        assert len(_created_agent_items(db)) == 2
        # break bounds the check calls — never 2×10.
        assert calls["n"] == 5
        assert len(_flood_alerts(db)) == 1

    def test_per_agent_deny_short_circuits_fleet_check(self, monkeypatch):
        # A per-agent denial on the first item must short-circuit (fleet check
        # not called) and break immediately.
        keys = []

        def _rate(key, *a, **k):
            keys.append(key)
            return _denied()  # per-agent denies first item

        db = _fake_db(pending=0)
        svc = _wire(monkeypatch, db, _pending(3), rate=_rate)

        asyncio.run(svc._sync_agent("a"))

        assert _created_agent_items(db) == []
        assert keys == ["operator_queue_create:a"]  # fleet key never reached


class TestRealInProcessLimiter:
    def test_real_limiter_caps_at_limit(self, monkeypatch):
        # Exercise the REAL rate_limiter (no stub) via its in-process fallback —
        # catches a wrong key / call-site a stub can't. Force Redis-unavailable.
        from services import rate_limiter

        monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
        rate_limiter.clear_inprocess()
        monkeypatch.setattr(oqs, "OPERATOR_QUEUE_CREATE_RATE_LIMIT", 3)
        monkeypatch.setattr(oqs, "OPERATOR_QUEUE_FLEET_CREATE_RATE_LIMIT", 1000)

        db = _fake_db(pending=0)
        # NOTE: use the real limiter (do not pass a `rate` stub).
        monkeypatch.setattr(oqs, "db", db)
        client = MagicMock()
        client.read_file = AsyncMock(return_value={
            "success": True,
            "content": json.dumps({"requests": _pending(5)}),
        })
        monkeypatch.setattr(oqs, "AgentClient", lambda name: client)
        svc = OperatorQueueSyncService()
        svc._write_responses_to_agent = AsyncMock()

        asyncio.run(svc._sync_agent("a"))
        rate_limiter.clear_inprocess()

        assert len(_created_agent_items(db)) == 3  # exactly the per-agent limit


# ---------------------------------------------------------------------------
# 3. Total field-hygiene clamp (pure)
# ---------------------------------------------------------------------------

class TestClamp:
    def test_long_title_and_question_truncated_with_marker(self):
        out = _clamp_ingested_item({
            "id": "x",
            "title": "T" * (OPERATOR_QUEUE_TITLE_MAX + 100),
            "question": "Q" * (OPERATOR_QUEUE_QUESTION_MAX + 100),
        })
        assert len(out["title"]) == OPERATOR_QUEUE_TITLE_MAX
        assert out["title"].endswith(_TRUNC_MARKER)
        assert len(out["question"]) == OPERATOR_QUEUE_QUESTION_MAX
        assert out["question"].endswith(_TRUNC_MARKER)

    def test_context_over_cap_becomes_marker_preserving_valid_execution_id(self):
        out = _clamp_ingested_item({
            "id": "x",
            "context": {"execution_id": "exec-123", "blob": "z" * 20000},
        })
        assert out["context"]["_truncated"] is True
        assert out["context"]["execution_id"] == "exec-123"  # short, id-shaped → kept
        assert out["context"]["_original_bytes"] > OPERATOR_QUEUE_CONTEXT_MAX_BYTES

    def test_context_marker_drops_oversize_execution_id(self):
        # A 100 KB execution_id must NOT be copied verbatim into the marker (that
        # would defeat the context cap, C4).
        out = _clamp_ingested_item({
            "id": "x",
            "context": {"execution_id": "E" * 100_000},
        })
        assert out["context"]["_truncated"] is True
        assert out["context"]["execution_id"] is None

    def test_non_dict_context_becomes_empty_dict_no_raise(self):
        for bad in ("a string", ["a", "list"], 42, 3.14):
            out = _clamp_ingested_item({"id": "x", "context": bad})
            assert out["context"] == {}

    def test_non_serializable_context_becomes_safe_marker(self):
        out = _clamp_ingested_item({"id": "x", "context": {"s": {1, 2, 3}}})
        assert out["context"]["_truncated"] is True  # a set is not JSON-serializable

    def test_options_over_cap_dropped_with_marker(self):
        out = _clamp_ingested_item({
            "id": "x",
            "options": ["opt" + "y" * 5000 for _ in range(5)],
        })
        assert out["options"] == [_OPTIONS_DROPPED_MARKER]

    def test_unknown_priority_collapses_to_medium(self):
        assert _clamp_ingested_item({"id": "x", "priority": "URGENT!!!"})["priority"] == "medium"
        assert _clamp_ingested_item({"id": "x"})["priority"] == "medium"

    def test_valid_critical_priority_untouched(self):
        assert _clamp_ingested_item({"id": "x", "priority": "critical"})["priority"] == "critical"

    def test_created_at_normalized_to_ingest_time(self):
        out = _clamp_ingested_item({"id": "x", "created_at": "2999-01-01T00:00:00Z"})
        assert out["created_at"] != "2999-01-01T00:00:00Z"  # sort-pin defeated

    def test_expires_at_honored(self):
        out = _clamp_ingested_item({"id": "x", "expires_at": "2030-01-01T00:00:00Z"})
        assert out["expires_at"] == "2030-01-01T00:00:00Z"

    def test_under_cap_values_pass_through(self):
        item = {"id": "x", "title": "short", "question": "q", "priority": "high",
                "context": {"execution_id": "e1"}, "options": ["a", "b"]}
        out = _clamp_ingested_item(item)
        assert out["title"] == "short"
        assert out["question"] == "q"
        assert out["priority"] == "high"
        assert out["context"] == {"execution_id": "e1"}
        assert out["options"] == ["a", "b"]

    def test_clamp_does_not_mutate_input(self):
        item = {"id": "x", "title": "T" * 5000}
        original = dict(item)
        _clamp_ingested_item(item)
        assert item == original  # returns a NEW dict

    def test_clamp_never_raises_on_hostile_shapes(self):
        for bad in (
            {"id": "x", "title": 123, "question": None, "context": None, "options": None},
            {"id": "x", "options": {"not": "a list"}},
            {"id": "x", "context": [1, 2, 3], "priority": None},
            {},  # no fields at all
        ):
            _clamp_ingested_item(bad)  # must not raise


class TestClampBoundaries:
    def test_title_exactly_at_cap_no_marker(self):
        exact = "T" * OPERATOR_QUEUE_TITLE_MAX
        out = _clamp_ingested_item({"id": "x", "title": exact})
        assert out["title"] == exact  # untouched

    def test_title_one_over_cap_gets_marker_len_equals_cap(self):
        out = _clamp_ingested_item({"id": "x", "title": "T" * (OPERATOR_QUEUE_TITLE_MAX + 1)})
        assert len(out["title"]) == OPERATOR_QUEUE_TITLE_MAX
        assert out["title"].endswith(_TRUNC_MARKER)

    def test_question_exactly_at_cap_no_marker(self):
        exact = "Q" * OPERATOR_QUEUE_QUESTION_MAX
        out = _clamp_ingested_item({"id": "x", "question": exact})
        assert out["question"] == exact

    def test_context_exactly_at_cap_no_marker(self):
        pad = OPERATOR_QUEUE_CONTEXT_MAX_BYTES - len(json.dumps({"data": ""}).encode("utf-8"))
        ctx = {"data": "x" * pad}
        assert len(json.dumps(ctx).encode("utf-8")) == OPERATOR_QUEUE_CONTEXT_MAX_BYTES
        out = _clamp_ingested_item({"id": "x", "context": ctx})
        assert out["context"] == ctx  # exactly at cap → untouched

    def test_context_one_over_cap_gets_marker(self):
        pad = OPERATOR_QUEUE_CONTEXT_MAX_BYTES - len(json.dumps({"data": ""}).encode("utf-8"))
        ctx = {"data": "x" * (pad + 1)}
        out = _clamp_ingested_item({"id": "x", "context": ctx})
        assert out["context"].get("_truncated") is True


class TestHelpers:
    def test_truncate_result_never_exceeds_max(self):
        assert _truncate_with_marker("abc", 10) == "abc"
        assert len(_truncate_with_marker("a" * 100, 20)) == 20

    def test_valid_execution_id(self):
        assert _valid_execution_id("exec-123") == "exec-123"
        assert _valid_execution_id("E" * 500) is None       # too long
        assert _valid_execution_id("has spaces") is None    # not id-shaped
        assert _valid_execution_id(12345) is None            # not a string
        assert _valid_execution_id("") is None


# ---------------------------------------------------------------------------
# 4. Reserved-id guard + malformed-id reject
# ---------------------------------------------------------------------------

class TestReservedAndMalformedIds:
    def test_reserved_prefix_ids_are_never_created(self, monkeypatch):
        db = _fake_db(pending=0)
        reqs = [
            {"id": "poison-abc", "status": "pending"},
            {"id": "queue-flood-a-2026", "status": "pending"},
            {"id": "val_x_123", "status": "pending"},
            {"id": "cb-dormant-a-1", "status": "pending"},
            {"id": "req-ok", "status": "pending", "title": "t"},
        ]
        svc = _wire(monkeypatch, db, reqs)

        asyncio.run(svc._sync_agent("a"))

        created = {i["id"] for i in _created_agent_items(db)}
        assert created == {"req-ok"}                # only the legit item
        assert _flood_alerts(db) == []             # reserved rejects don't alert

    def test_malformed_ids_rejected_and_alert(self, monkeypatch):
        db = _fake_db(pending=0)
        reqs = [
            {"id": "ok-1", "status": "pending", "title": "t"},
            {"id": "bad id with spaces", "status": "pending"},
            {"id": "z" * 300, "status": "pending"},  # over OPERATOR_QUEUE_ID_MAX
        ]
        svc = _wire(monkeypatch, db, reqs)

        asyncio.run(svc._sync_agent("a"))

        created = {i["id"] for i in _created_agent_items(db)}
        assert created == {"ok-1"}
        assert len(_flood_alerts(db)) == 1          # malformed → held → alert


# ---------------------------------------------------------------------------
# 5. Platform exemption + flood alert robustness
# ---------------------------------------------------------------------------

class TestExemptionAndFloodAlert:
    def test_flood_alert_is_direct_create_not_rate_limited_or_clamped(self, monkeypatch):
        db = MagicMock()
        monkeypatch.setattr(oqs, "db", db)
        rate = MagicMock()
        monkeypatch.setattr(oqs.rate_limiter, "check", rate)
        svc = OperatorQueueSyncService()

        asyncio.run(svc._maybe_emit_flood_alert("a", held=7))

        assert db.create_operator_queue_item.call_count == 1
        item = db.create_operator_queue_item.call_args.args[1]
        assert item["id"].startswith("queue-flood-a-")   # un-guessable id (C2)
        assert item["type"] == "alert"
        assert item["priority"] == "high"
        assert "7 request(s)" in item["question"]        # not clamped
        rate.assert_not_called()                          # exempt from the rate cap

    def test_flood_alert_cooldown_emits_once_per_episode(self, monkeypatch):
        db = MagicMock()
        monkeypatch.setattr(oqs, "db", db)
        monkeypatch.setattr(oqs.rate_limiter, "check", MagicMock())
        svc = OperatorQueueSyncService()

        asyncio.run(svc._maybe_emit_flood_alert("a", held=1))
        asyncio.run(svc._maybe_emit_flood_alert("a", held=1))  # within cooldown

        assert db.create_operator_queue_item.call_count == 1

    def test_first_alert_emits_when_monotonic_below_cooldown(self, monkeypatch):
        # Regression: on a freshly-booted process `time.monotonic()` can be
        # smaller than the cooldown window. A `0.0` "never alerted" sentinel made
        # `now - 0.0 < COOLDOWN` true and suppressed the FIRST-ever alert for the
        # first COOLDOWN seconds of uptime. The `None` sentinel must always emit
        # the first alert regardless of monotonic's absolute value.
        db = MagicMock()
        monkeypatch.setattr(oqs, "db", db)
        monkeypatch.setattr(oqs.rate_limiter, "check", MagicMock())
        # Simulate a fresh runner: monotonic well below COOLDOWN (300s).
        monkeypatch.setattr(oqs.time, "monotonic", lambda: 5.0)
        svc = OperatorQueueSyncService()

        asyncio.run(svc._maybe_emit_flood_alert("a", held=1))
        assert db.create_operator_queue_item.call_count == 1   # emits despite low monotonic
        asyncio.run(svc._maybe_emit_flood_alert("a", held=1))  # still within cooldown
        assert db.create_operator_queue_item.call_count == 1   # cooldown still holds

    def test_flood_alert_create_failure_does_not_kill_sync(self, monkeypatch):
        # A create raise inside the alert path must be swallowed.
        db = _fake_db(pending=OPERATOR_QUEUE_MAX_PENDING_PER_AGENT,
                      create_side_effect=Exception("db down"))
        svc = _wire(monkeypatch, db, _pending(5))
        # Must not raise (held>0 → alert attempt raises → swallowed).
        asyncio.run(svc._sync_agent("a"))

    def test_oversize_file_skipped_with_alert(self, monkeypatch):
        db = _fake_db(pending=0)
        monkeypatch.setattr(oqs, "db", db)
        monkeypatch.setattr(oqs, "OPERATOR_QUEUE_MAX_FILE_BYTES", 100)
        big = json.dumps({"requests": _pending(50)})
        assert len(big) > 100
        client = MagicMock()
        client.read_file = AsyncMock(return_value={"success": True, "content": big})
        monkeypatch.setattr(oqs, "AgentClient", lambda name: client)
        monkeypatch.setattr(oqs.rate_limiter, "check", lambda *a, **k: _allowed())
        svc = OperatorQueueSyncService()
        svc._write_responses_to_agent = AsyncMock()

        asyncio.run(svc._sync_agent("a"))

        assert _created_agent_items(db) == []       # never parsed / ingested
        assert len(_flood_alerts(db)) == 1


# ---------------------------------------------------------------------------
# 5b. Platform producer — validation_service direct create (#1632)
# ---------------------------------------------------------------------------
# `_notify_operator_on_failure` was converted to a DIRECT db.create_operator_queue_item
# (#1632): it's a *platform* alert, so it must bypass the agent-authored sync-ingestion
# caps (routing it through the agent file would flow it through `_sync_agent` and cap it),
# it uses the reserved `val_` id prefix an agent cannot pre-create (so an agent can't
# suppress its own validation alarm), and it stays best-effort. The conversion also fixed a
# latent bug: the method used to `.append` to a bare list (`[]`) while the sync loop expects
# `{"requests": [...]}`, so the notification was never actually ingested.

def _validation_service():
    """A ValidationService without __init__ (skips the TaskExecutionService() build)."""
    from services.validation_service import ValidationService
    return ValidationService.__new__(ValidationService)


def _validation_result():
    from services.validation_service import ValidationResult, ValidationStatus
    return ValidationResult(
        status=ValidationStatus.FAIL,
        summary="checks failed",
        items=[{"check": "c", "result": "fail", "evidence": "e"}],
        recommendation="retry",
    )


class TestValidationServiceDirectCreate:
    def test_notifies_via_direct_db_create_with_reserved_val_id(self, monkeypatch):
        # The platform exemption's concrete producer: one DIRECT create (never an
        # agent-file write → never rate/depth-capped), reserved `val_` id, alert.
        import services.validation_service as vs
        db = MagicMock()
        monkeypatch.setattr(vs, "db", db)
        svc = _validation_service()

        asyncio.run(svc._notify_operator_on_failure("exec-1", "agent-a", _validation_result()))

        assert db.create_operator_queue_item.call_count == 1
        agent_arg, item = db.create_operator_queue_item.call_args.args
        assert agent_arg == "agent-a"
        assert item["id"].startswith("val_")           # reserved prefix (agent can't pre-create)
        assert item["type"] == "alert"
        assert item["priority"] == "high"
        assert item["context"]["execution_id"] == "exec-1"

    def test_notify_is_best_effort_swallows_create_failure(self, monkeypatch):
        # Validation must not fail because the operator-queue create failed.
        import services.validation_service as vs
        db = MagicMock()
        db.create_operator_queue_item.side_effect = Exception("db down")
        monkeypatch.setattr(vs, "db", db)
        svc = _validation_service()

        asyncio.run(svc._notify_operator_on_failure("exec-1", "agent-a", _validation_result()))  # must not raise


# ---------------------------------------------------------------------------
# 6. Leader lock (multi-worker)
# ---------------------------------------------------------------------------

class FakeRedis:
    """Minimal SET NX EX / GET / EXPIRE / DELETE (mirror test_1464)."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def expire(self, key, ttl):
        return key in self.store

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


class TestLeaderLock:
    def test_only_one_worker_becomes_leader(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(oqs, "get_breaker_redis", lambda: fake)
        a, b = OperatorQueueSyncService(), OperatorQueueSyncService()
        assert a._try_acquire_leadership() is True
        assert b._try_acquire_leadership() is False
        assert fake.get(oqs._LEADER_KEY) == a._worker_id

    def test_redis_down_fails_open_to_leader(self, monkeypatch):
        monkeypatch.setattr(oqs, "get_breaker_redis", lambda: None)
        assert OperatorQueueSyncService()._try_acquire_leadership() is True

    def test_release_only_deletes_own_lease(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(oqs, "get_breaker_redis", lambda: fake)
        a, b = OperatorQueueSyncService(), OperatorQueueSyncService()
        assert a._try_acquire_leadership() is True
        b._release_leadership()                       # b doesn't hold it
        assert fake.get(oqs._LEADER_KEY) == a._worker_id

    def test_non_leader_poll_cycle_is_a_no_op(self, monkeypatch):
        svc = OperatorQueueSyncService()
        monkeypatch.setattr(svc, "_try_acquire_leadership", lambda: False)
        svc._sync_agent = AsyncMock()
        svc._is_leader = True  # was leader, now yields

        asyncio.run(svc._poll_cycle())

        assert svc._sync_agent.await_count == 0        # no sync work when not leader

    def test_leader_ttl_has_headroom_over_worst_case_cycle(self):
        # #1632 F1: the lease is refreshed once per cycle, so it must outlast one
        # worst-case cycle + the inter-cycle sleep, or leadership flaps under a
        # slow-writing agent and the flood alert double-emits. A _sync_agent can
        # take a fast read + write_file(timeout=10.0) ≈ 10s, then the loop sleeps
        # poll_interval (5s default) → ~15s between refreshes. The bare
        # poll_interval*3 (15s) had zero headroom; the 30s floor gives 2×.
        assert OperatorQueueSyncService(poll_interval=5)._leader_ttl() >= 30
        # A larger poll interval keeps the *3 rule (still dwarfs the 10s write).
        assert OperatorQueueSyncService(poll_interval=20)._leader_ttl() == 60


# ---------------------------------------------------------------------------
# 7. DB layer — belt + count helper
# ---------------------------------------------------------------------------

# #1631: create_item mints a platform uuid `id`, inserts, then re-reads the
# surviving row's id (on conflict that is the pre-existing row's uuid, not the
# one this call minted). The stub returns a fixed sentinel so a belt-passing
# create returns that re-read id.
_STUB_ROW_ID = "__row_uuid__"


class _FakeConn:
    def __init__(self, scalar=0):
        self._scalar = scalar

    def execute(self, stmt):
        return self

    def scalar(self):
        return self._scalar

    def first(self):
        # #1631: the create re-read (`select(id).where(...)`) returns the row.
        return (_STUB_ROW_ID,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeEngine:
    def __init__(self, scalar=0):
        self._scalar = scalar

    def begin(self):
        return _FakeConn(self._scalar)

    def connect(self):
        return _FakeConn(self._scalar)


def _ops():
    from db.operator_queue import OperatorQueueOperations
    return OperatorQueueOperations.__new__(OperatorQueueOperations)


def _patch_engine(monkeypatch, engine):
    """Patch get_engine/make_insert where OperatorQueueOperations methods actually
    resolve them — the method's __globals__ (the db.operator_queue module dict) —
    NOT the string path "db.operator_queue.get_engine".

    A prior test in the full-suite run may have left a stand-in in
    sys.modules["db.operator_queue"] that still re-exports the *real* class, so a
    string-path monkeypatch lands on the stand-in while the real method keeps
    reading the original module dict (the module-identity gotcha) → the real
    engine runs and the stub is ignored. Patching __globals__ targets exactly the
    dict the running method looks names up in, so it is immune to that swap."""
    g = _ops().create_item.__globals__
    monkeypatch.setitem(g, "get_engine", lambda: engine)
    monkeypatch.setitem(g, "make_insert", lambda t: MagicMock())


class TestDbBelt:
    def test_belt_rejects_oversize_question(self):
        ops = _ops()
        with pytest.raises(ValueError, match="'question' exceeds"):
            ops.create_item("agent", {"id": "x", "question": "q" * 20_000})

    def test_belt_rejects_oversize_title(self):
        ops = _ops()
        with pytest.raises(ValueError, match="'title' exceeds"):
            ops.create_item("agent", {"id": "x", "title": "t" * 5_000})

    def test_belt_rejects_oversize_id(self):
        ops = _ops()
        with pytest.raises(ValueError, match="'id' exceeds"):
            ops.create_item("agent", {"id": "z" * 600})

    def test_belt_rejects_oversize_context(self):
        ops = _ops()
        with pytest.raises(ValueError, match="'context' exceeds"):
            ops.create_item("agent", {"id": "x", "context": {"blob": "z" * 70_000}})

    def test_belt_passes_a_small_platform_item(self, monkeypatch):
        # A validation_service-style ~200-char item must pass the belt.
        _patch_engine(monkeypatch, _FakeEngine())
        ops = _ops()
        rid = ops.create_item("agent", {
            "id": "val_exec_20260717",
            "type": "alert",
            "question": "Execution validation failed: " + "detail " * 20,
        })
        # #1631: the belt passed and the create proceeded, so create_item returns
        # the re-read row's platform uuid (not the agent's request_id).
        assert rid == _STUB_ROW_ID

    def test_missing_id_still_raises_valueerror(self):
        # #1525 belt preserved.
        ops = _ops()
        with pytest.raises(ValueError, match="missing a required 'id'"):
            ops.create_item("agent", {"title": "t"})

    def test_non_dict_context_does_not_crash_create(self, monkeypatch):
        # The pre-existing execution_id `.get` crash on a non-dict context.
        _patch_engine(monkeypatch, _FakeEngine())
        ops = _ops()
        rid = ops.create_item("agent", {"id": "x", "context": "not-a-dict"})
        # No AttributeError on the non-dict context; #1631 returns the row uuid.
        assert rid == _STUB_ROW_ID

    def test_count_pending_returns_int(self, monkeypatch):
        _patch_engine(monkeypatch, _FakeEngine(scalar=7))
        ops = _ops()
        assert ops.count_pending_for_agent("agent") == 7

    def test_count_pending_none_scalar_is_zero(self, monkeypatch):
        _patch_engine(monkeypatch, _FakeEngine(scalar=None))
        ops = _ops()
        assert ops.count_pending_for_agent("agent") == 0
