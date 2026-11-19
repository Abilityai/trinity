"""#4257 — ``SlotService.active_slot_counts`` reads the live meter.

The queue-stall monitor runs inside the synchronous maintenance tick and needs
one number: how many slots an agent is holding right now. It must be the SAME
ZSET ``acquire_slot`` admits against, or the monitor's push-agent verdict
(`capacity` vs `backlog_drain`) is a guess — which is exactly what the
hardcoded `slot_free=True` was.

Module under test: src/backend/services/slot_service.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import fakeredis
import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.slot_service import SlotService  # noqa: E402


def _service():
    svc = SlotService.__new__(SlotService)
    svc.redis = fakeredis.FakeRedis(decode_responses=True)
    svc.slots_prefix = "agent:slots:"
    svc.metadata_prefix = "agent:slot:"
    svc._on_release_callbacks = []
    return svc


def test_an_idle_agent_holds_nothing():
    assert _service().active_slot_counts(["alpha"])["alpha"] == 0


def test_it_counts_the_slots_actually_held():
    svc = _service()
    svc.redis.zadd(svc._slots_key("alpha"), {"e1": 1.0, "e2": 2.0})

    assert svc.active_slot_counts(["alpha"])["alpha"] == 2


def test_it_is_scoped_to_one_agent():
    svc = _service()
    svc.redis.zadd(svc._slots_key("alpha"), {"e1": 1.0})
    svc.redis.zadd(svc._slots_key("beta"), {"e2": 1.0, "e3": 2.0})

    assert svc.active_slot_counts(["alpha"])["alpha"] == 1
    assert svc.active_slot_counts(["beta"])["beta"] == 2


def test_a_released_slot_stops_counting():
    svc = _service()
    svc.redis.zadd(svc._slots_key("alpha"), {"e1": 1.0, "e2": 2.0})
    svc.redis.zrem(svc._slots_key("alpha"), "e1")

    assert svc.active_slot_counts(["alpha"])["alpha"] == 1


def test_it_reads_the_same_key_acquire_writes():
    """Not a parallel meter: the key is the one `_slots_key` builds."""
    svc = _service()
    svc.redis.zadd("agent:slots:alpha", {"e1": 1.0})

    assert svc._slots_key("alpha") == "agent:slots:alpha"
    assert svc.active_slot_counts(["alpha"])["alpha"] == 1


# ---------------------------------------------------------------------------
# #4635 G3: the monitor reads every push agent's meter in one round trip
# ---------------------------------------------------------------------------


class _CountingRedis:
    """Counts round trips: each command outside a pipeline, and each
    pipeline `execute`, is one."""

    def __init__(self, inner):
        self._inner = inner
        self.round_trips = 0

    def zcard(self, key):
        self.round_trips += 1
        return self._inner.zcard(key)

    def pipeline(self, transaction=True):
        outer = self
        pipe = self._inner.pipeline(transaction=transaction)

        class _Pipe:
            def zcard(self, key):
                pipe.zcard(key)
                return self

            def execute(self, raise_on_error=True):
                outer.round_trips += 1
                return pipe.execute(raise_on_error=raise_on_error)

        return _Pipe()


def test_many_agents_are_read_in_one_round_trip():
    svc = _service()
    svc.redis.zadd(svc._slots_key("alpha"), {"e1": 1.0, "e2": 2.0})
    svc.redis.zadd(svc._slots_key("beta"), {"e3": 1.0})
    counting = _CountingRedis(svc.redis)
    svc.redis = counting

    counts = svc.active_slot_counts(["alpha", "beta", "idle"])

    assert counts == {"alpha": 2, "beta": 1, "idle": 0}
    assert counting.round_trips == 1


def test_one_unreadable_key_costs_only_that_agent_its_reading():
    """A key holding the wrong type answers WRONGTYPE; the rest still read."""
    svc = _service()
    svc.redis.zadd(svc._slots_key("alpha"), {"e1": 1.0})
    svc.redis.set(svc._slots_key("broken"), "not-a-zset")

    counts = svc.active_slot_counts(["alpha", "broken"])

    assert counts == {"alpha": 1, "broken": None}


def test_no_agents_is_no_round_trip():
    svc = _service()
    counting = _CountingRedis(svc.redis)
    svc.redis = counting

    assert svc.active_slot_counts([]) == {}
    assert counting.round_trips == 0
