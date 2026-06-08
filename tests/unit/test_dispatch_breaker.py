"""Unit tests for the dispatch circuit breaker (#526, RELIABILITY-007).

Two things this file CAN'T cover (and why), plus where they live instead:

  * The Lua state machine (consecutive threshold, half-open probe, backoff,
    reset) needs a real Redis — fakeredis has no EVALSHA. Those live in
    tests/integration/test_dispatch_breaker.py (sibling-Redis), mirroring the
    transport-breaker unit/integration split (test_circuit_breaker.py).

  * The acquire()-gate + drain wiring is exercised in TestDispatchGate below
    by monkeypatching DispatchBreaker into capacity_manager.

What this file pins WITHOUT Redis Lua:
  * Transition value-object semantics (opened / closed / changed / noop).
  * record_outcome ROUTING (AUTH→failure, None→success, everything-else→noop)
    via mocked record_failure / record_success — no Redis needed for routing.
  * Fail-open: Lua unavailable (fakeredis) or client None → allow=True,
    record→noop, retry_after=0, to_dict closed-default. Never raises.
  * The dispatch gate raises CircuitOpen and does NOT enqueue (R-cases).

No sys.modules mutation here — dispatch_breaker imports cleanly via the package
(only pulls redis_breaker_util; no DB init), so tests/lint_sys_modules.py is
satisfied with a plain import.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Bootstrap src/backend on sys.path (mirrors test_capacity_manager.py).
_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

import fakeredis  # noqa: E402

from services.dispatch_breaker import (  # noqa: E402
    AUTH_ERROR_CODE,
    DISPATCH_FAILURE_THRESHOLD,
    DispatchBreaker,
    Transition,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Transition value object (pure — no Redis)
# ---------------------------------------------------------------------------


class TestTransition:
    def test_opened_only_on_fresh_open(self):
        assert Transition("closed", "open").opened is True
        # open→open (probe re-fail) is NOT a fresh open — no re-drain.
        assert Transition("open", "open").opened is False
        assert Transition("closed", "closed").opened is False

    def test_closed_only_on_recovery_from_open(self):
        assert Transition("open", "closed").closed is True
        # closed→closed (normal success) is not a recovery.
        assert Transition("closed", "closed").closed is False

    def test_changed_and_noop(self):
        assert Transition("closed", "open").changed is True
        assert Transition("closed", "closed").changed is False
        n = Transition.noop()
        assert (n.prior, n.new) == ("closed", "closed")
        assert n.opened is False and n.closed is False


# ---------------------------------------------------------------------------
# record_outcome routing (AUTH-only — D10). Mocks record_* so no Redis needed.
# ---------------------------------------------------------------------------


class TestRecordOutcomeRouting:
    def _breaker(self):
        b = DispatchBreaker("routing", redis_client=fakeredis.FakeStrictRedis(decode_responses=True))
        b.record_failure = MagicMock(return_value=Transition.noop())
        b.record_success = MagicMock(return_value=Transition.noop())
        return b

    def test_auth_string_routes_to_failure(self):
        b = self._breaker()
        b.record_outcome("auth")
        b.record_failure.assert_called_once()
        b.record_success.assert_not_called()

    def test_auth_enum_member_routes_to_failure(self):
        b = self._breaker()

        class _Code:
            value = "AUTH"  # uppercase — compared case-insensitively

        b.record_outcome(_Code())
        b.record_failure.assert_called_once()

    def test_none_routes_to_success(self):
        b = self._breaker()
        b.record_outcome(None)
        b.record_success.assert_called_once()
        b.record_failure.assert_not_called()

    @pytest.mark.parametrize(
        "code", ["timeout", "capacity", "network", "agent_error", "billing", "circuit_open"]
    )
    def test_non_auth_codes_are_ignored(self, code):
        """R2: non-AUTH outcomes must NOT touch the breaker (no failure, no reset)."""
        b = self._breaker()
        t = b.record_outcome(code)
        b.record_failure.assert_not_called()
        b.record_success.assert_not_called()
        assert (t.prior, t.new) == ("closed", "closed")  # noop

    def test_auth_error_code_constant_matches_taskexec(self):
        assert AUTH_ERROR_CODE == "auth"


# ---------------------------------------------------------------------------
# Fail-open (Lua unavailable OR client None) — never raises
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_fakeredis_no_lua_fails_open(self):
        """fakeredis has no EVALSHA → every Lua op fails open: allow=True,
        record→noop, retry_after=0, to_dict closed-default. Never raises."""
        b = DispatchBreaker("fo", redis_client=fakeredis.FakeStrictRedis(decode_responses=True))
        assert b.allow_dispatch() is True
        assert b.record_outcome("auth").changed is False
        # 3 AUTH should NOT open (Lua never ran) — proves fail-open is total.
        for _ in range(DISPATCH_FAILURE_THRESHOLD + 1):
            b.record_outcome("auth")
        assert b.allow_dispatch() is True
        assert b.retry_after_seconds() == 0
        assert b.to_dict() == {"state": "closed", "failure_count": 0, "retry_after_seconds": 0}

    def test_client_none_fails_open(self, monkeypatch):
        """Redis unreachable (get_breaker_redis → None) → allow=True, record→noop."""
        import services.dispatch_breaker as mod
        monkeypatch.setattr(mod, "get_breaker_redis", lambda: None)
        b = DispatchBreaker("none-client")  # redis_client=None → resolves via getter
        assert b.allow_dispatch() is True
        assert b.record_outcome("auth") == Transition.noop()
        assert b.record_failure("missed_heartbeat") == Transition.noop()
        assert b.retry_after_seconds() == 0
        assert b.to_dict()["state"] == "closed"


# ---------------------------------------------------------------------------
# Dispatch gate in CapacityManager.acquire (CircuitOpen + NO enqueue)
# ---------------------------------------------------------------------------


class _FakeRedisQueue:
    """Tracks LPUSH so we can assert no in-memory enqueue happened."""

    def __init__(self):
        self.lists: dict[str, list] = {}

    def lpush(self, key, *vals):
        self.lists.setdefault(key, [])
        for v in vals:
            self.lists[key].insert(0, v)
        return len(self.lists[key])

    def llen(self, key):
        return len(self.lists.get(key, []))

    def rpop(self, key):
        return self.lists.get(key, []).pop() if self.lists.get(key) else None

    def delete(self, key):
        self.lists.pop(key, None)
        return 1

    def exists(self, key):
        return int(key in self.lists)

    def set(self, *a, **k):
        return True


@pytest.fixture
def gated_capacity(monkeypatch):
    """CapacityManager whose DispatchBreaker is forced open, global flag on."""
    from unittest.mock import AsyncMock
    from services import capacity_manager as cm_module
    import config

    fake_redis = _FakeRedisQueue()
    monkeypatch.setattr(cm_module.redis, "from_url", lambda *_a, **_kw: fake_redis)

    slot_service = AsyncMock()
    slot_service.slots_prefix = "agent:slots:"
    slot_service.acquire_slot = AsyncMock(return_value=False)  # at capacity
    slot_service.register_on_release = lambda cb: None
    backlog_service = AsyncMock()
    backlog_service.enqueue = AsyncMock(return_value=True)

    # Force the breaker open.
    breaker = MagicMock()
    breaker.allow_dispatch.return_value = False
    breaker.retry_after_seconds.return_value = 42
    monkeypatch.setattr(cm_module, "DispatchBreaker", lambda _name: breaker)
    monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", True)

    cm = cm_module.CapacityManager(
        redis_url="redis://test",
        slot_service=slot_service,
        backlog_service=backlog_service,
    )
    return cm, fake_redis, backlog_service, slot_service


class TestDispatchGate:
    def test_acquire_raises_circuit_open_and_does_not_enqueue(self, gated_capacity):
        from services.capacity_manager import CircuitOpen, PersistentTaskPayload

        cm, fake_redis, backlog_service, slot_service = gated_capacity
        with pytest.raises(CircuitOpen) as exc:
            asyncio.run(cm.acquire(
                agent_name="dead",
                execution_id="e1",
                max_concurrent=3,
                overflow_policy="queue_persistent",
                breaker_enabled=True,
                overflow_payload=PersistentTaskPayload(
                    request=object(), effective_timeout=60, user_id=1, user_email="a@b.c",
                    subscription_id=None, x_source_agent=None, x_mcp_key_id=None,
                    x_mcp_key_name=None, triggered_by="manual", collaboration_activity_id=None,
                ),
            ))
        assert exc.value.agent_name == "dead"
        assert exc.value.retry_after_seconds == 42
        # NO enqueue: neither persistent backlog nor in-memory queue touched,
        # and the slot was never even attempted (gate is BEFORE acquire_slot).
        backlog_service.enqueue.assert_not_called()
        slot_service.acquire_slot.assert_not_called()
        assert all(len(v) == 0 for v in fake_redis.lists.values())

    def test_acquire_unchanged_when_breaker_enabled_false(self, gated_capacity):
        """R3: default breaker_enabled=False → gate skipped entirely (admitted)."""
        cm, _fr, _bl, slot_service = gated_capacity
        slot_service.acquire_slot = MagicMock(return_value=True)
        from unittest.mock import AsyncMock
        slot_service.acquire_slot = AsyncMock(return_value=True)
        result = asyncio.run(cm.acquire(
            agent_name="ok", execution_id="e2", max_concurrent=3, overflow_policy="reject",
        ))
        assert result.state == "admitted"


@pytest.fixture
def probe_capacity(monkeypatch):
    """CapacityManager whose DispatchBreaker is in the half-open PROBE window
    (allow_dispatch → True, state → 'open'), global flag on. Lets us pin the
    #526 F1 fix: a half-open probe is admitted ONLY into a free slot and is
    NEVER enqueued."""
    from unittest.mock import AsyncMock
    from services import capacity_manager as cm_module
    import config

    fake_redis = _FakeRedisQueue()
    monkeypatch.setattr(cm_module.redis, "from_url", lambda *_a, **_kw: fake_redis)

    slot_service = AsyncMock()
    slot_service.slots_prefix = "agent:slots:"
    slot_service.register_on_release = lambda cb: None
    backlog_service = AsyncMock()
    backlog_service.enqueue = AsyncMock(return_value=True)

    # Half-open probe granted, breaker still reads 'open'.
    breaker = MagicMock()
    breaker.allow_dispatch.return_value = True
    breaker.retry_after_seconds.return_value = 7
    breaker.to_dict.return_value = {
        "state": "open", "failure_count": 3, "retry_after_seconds": 7,
    }
    monkeypatch.setattr(cm_module, "DispatchBreaker", lambda _name: breaker)
    monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", True)

    cm = cm_module.CapacityManager(
        redis_url="redis://test",
        slot_service=slot_service,
        backlog_service=backlog_service,
    )
    return cm, fake_redis, backlog_service, slot_service


class TestHalfOpenProbeNoEnqueue:
    """#526 F1: the half-open probe must lead to a REAL dispatch (free slot) or
    fast-fail — it must never be spent on a backlog enqueue (which records no
    verdict and stalls the breaker's backoff)."""

    def test_probe_admitted_into_free_slot(self, probe_capacity):
        from unittest.mock import AsyncMock
        cm, _fr, backlog_service, slot_service = probe_capacity
        slot_service.acquire_slot = AsyncMock(return_value=True)  # slot free
        result = asyncio.run(cm.acquire(
            agent_name="recovering", execution_id="p1", max_concurrent=3,
            overflow_policy="queue_persistent", breaker_enabled=True,
            overflow_payload=_dummy_payload(),
        ))
        assert result.state == "admitted"
        slot_service.acquire_slot.assert_awaited_once()
        backlog_service.enqueue.assert_not_called()

    def test_probe_fast_fails_on_full_slots_without_enqueue(self, probe_capacity):
        from unittest.mock import AsyncMock
        from services.capacity_manager import CircuitOpen
        cm, fake_redis, backlog_service, slot_service = probe_capacity
        slot_service.acquire_slot = AsyncMock(return_value=False)  # at capacity
        with pytest.raises(CircuitOpen) as exc:
            asyncio.run(cm.acquire(
                agent_name="recovering", execution_id="p2", max_concurrent=3,
                overflow_policy="queue_persistent", breaker_enabled=True,
                overflow_payload=_dummy_payload(),
            ))
        assert exc.value.retry_after_seconds == 7
        # The probe was NOT enqueued anywhere — no verdict-less backlog row.
        backlog_service.enqueue.assert_not_called()
        assert all(len(v) == 0 for v in fake_redis.lists.values())


def _dummy_payload():
    from services.capacity_manager import PersistentTaskPayload
    return PersistentTaskPayload(
        request=object(), effective_timeout=60, user_id=1, user_email="a@b.c",
        subscription_id=None, x_source_agent=None, x_mcp_key_id=None,
        x_mcp_key_name=None, triggered_by="manual", collaboration_activity_id=None,
    )


# ---------------------------------------------------------------------------
# run_maintenance breaker-aware backstop (#526 Finding 1.2)
#
# The inline drain on a →open transition is fire-and-forget. If it's lost
# (task dropped, or fail_queued_for_agent threw), the 60s maintenance sweep
# re-fails the queued backlog for any agent whose breaker is still open.
# Only-open-agents, gated on the global flag, never raises.
# ---------------------------------------------------------------------------


@pytest.fixture
def plain_capacity(monkeypatch):
    """A CapacityManager wired to mocked slot/backlog services + a fake redis,
    with no breaker forced state — for exercising the maintenance backstop."""
    from unittest.mock import AsyncMock
    from services import capacity_manager as cm_module

    fake_redis = _FakeRedisQueue()
    monkeypatch.setattr(cm_module.redis, "from_url", lambda *_a, **_kw: fake_redis)

    slot_service = AsyncMock()
    slot_service.slots_prefix = "agent:slots:"
    slot_service.register_on_release = lambda cb: None
    backlog_service = AsyncMock()

    cm = cm_module.CapacityManager(
        redis_url="redis://test",
        slot_service=slot_service,
        backlog_service=backlog_service,
    )
    return cm, cm_module


class TestMaintenanceBackstop:
    def _patch_db(self, monkeypatch, queued_agents, failed_calls):
        # Replace the WHOLE `db` object that `_backstop_open_breaker_backlog`
        # resolves via its dynamic `from database import db`, rather than
        # setattr-ing methods onto the shared `database.db`. Another unit file
        # (test_904_agent_call_limiter) installs a stub `database.db` that lacks
        # these methods and never restores the attribute, so under randomized
        # order `setattr(database.db, "list_agents_with_queued", …)` raises
        # AttributeError. Swapping the object is immune to that pollution.
        import database
        from types import SimpleNamespace
        monkeypatch.setattr(
            database,
            "db",
            SimpleNamespace(
                list_agents_with_queued=lambda: list(queued_agents),
                fail_queued_for_agent=(
                    lambda name, reason="": (failed_calls.append((name, reason)) or 2)
                ),
            ),
        )

    def test_backstop_fails_only_open_breaker_agents(self, plain_capacity, monkeypatch):
        cm, _cm_module = plain_capacity
        import config
        import services.dispatch_breaker as db_mod

        monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", True)
        failed: list = []
        self._patch_db(monkeypatch, ["openA", "closedB"], failed)
        monkeypatch.setattr(
            db_mod,
            "get_dispatch_states_for",
            lambda names: {
                "openA": {"state": "open", "failure_count": 3, "retry_after_seconds": 5},
                "closedB": {"state": "closed", "failure_count": 0, "retry_after_seconds": 0},
            },
        )
        asyncio.run(cm._backstop_open_breaker_backlog())
        # Only the OPEN-breaker agent's backlog is failed.
        assert failed == [("openA", "circuit_open: maintenance backstop")]

    def test_backstop_noop_when_global_flag_off(self, plain_capacity, monkeypatch):
        cm, _cm_module = plain_capacity
        import config
        import database
        from types import SimpleNamespace

        monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", False)
        touched: list = []
        monkeypatch.setattr(
            database,
            "db",
            SimpleNamespace(list_agents_with_queued=lambda: touched.append("x") or []),
        )
        asyncio.run(cm._backstop_open_breaker_backlog())
        assert touched == []  # returned before touching the DB

    def test_backstop_never_raises(self, plain_capacity, monkeypatch):
        cm, _cm_module = plain_capacity
        import config
        import database
        from types import SimpleNamespace

        monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", True)

        def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(
            database, "db", SimpleNamespace(list_agents_with_queued=_boom)
        )
        # Must swallow the error — a thrown sweep would break run_maintenance
        # and falsely skip the canary drain-tick.
        asyncio.run(cm._backstop_open_breaker_backlog())


# ---------------------------------------------------------------------------
# Drain-on-open wiring in task_execution_service (#526 Finding 2)
#
# The single most important #526 path: breaker opens → backlog fails out +
# audit fires. Exercised here with the heavy collaborators mocked.
# ---------------------------------------------------------------------------


class TestDrainWiring:
    def test_fail_backlog_and_audit_drains_and_audits(self, monkeypatch):
        import services.task_execution_service as tse
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        # Swap the module-global `db` for a clean fake — `database.db` may be a
        # method-less stub left by test_904_agent_call_limiter under randomized
        # order, so setattr(tse.db, …) would raise AttributeError.
        fail_queued = MagicMock(return_value=3)
        monkeypatch.setattr(
            tse, "db", SimpleNamespace(fail_queued_for_agent=fail_queued)
        )
        cap = MagicMock()
        cap.clear_in_memory_queue = AsyncMock(return_value=3)
        monkeypatch.setattr(tse, "get_capacity_manager", lambda: cap)
        audit = AsyncMock()
        monkeypatch.setattr(tse.platform_audit_service, "log", audit)

        asyncio.run(tse._fail_backlog_and_audit("deadagent"))

        fail_queued.assert_called_once()
        args, kwargs = fail_queued.call_args
        assert args[0] == "deadagent"
        assert "circuit_open" in kwargs.get("reason", "")
        cap.clear_in_memory_queue.assert_awaited_once_with("deadagent")
        audit.assert_awaited_once()
        assert audit.call_args.kwargs["event_action"] == "circuit_breaker_open"

    def test_record_terminal_opened_spawns_drain(self, monkeypatch):
        import services.task_execution_service as tse
        from services.dispatch_breaker import Transition
        from unittest.mock import AsyncMock, MagicMock

        breaker = MagicMock()
        breaker.record_outcome.return_value = Transition("closed", "open")
        monkeypatch.setattr(tse, "DispatchBreaker", lambda _name: breaker)
        drain = AsyncMock()
        monkeypatch.setattr(tse, "_fail_backlog_and_audit", drain)
        tse._background_breaker_tasks.clear()

        async def _run():
            await tse._record_dispatch_terminal("deadagent", True, "auth")
            # The drain is fire-and-forget but a strong ref is held in the set —
            # awaiting it proves both the wiring AND that the task survived GC.
            await asyncio.gather(*list(tse._background_breaker_tasks))

        asyncio.run(_run())
        drain.assert_awaited_once_with("deadagent")

    # NOTE: the →closed recovery-audit and disabled-no-op routing of
    # _record_dispatch_terminal are covered by tests/unit/test_dispatch_breaker_wiring.py
    # (TestRecordDispatchTerminalRouting). Not duplicated here. This class owns the
    # drain *effect* (test_fail_backlog_and_audit_drains_and_audits) and the
    # open→drain wiring (test_record_terminal_opened_spawns_drain), which that file
    # deliberately defers to this work.
