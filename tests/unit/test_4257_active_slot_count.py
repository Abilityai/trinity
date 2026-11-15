"""#4257 — ``SlotService.active_slot_count`` reads the live meter.

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
    assert _service().active_slot_count("alpha") == 0


def test_it_counts_the_slots_actually_held():
    svc = _service()
    svc.redis.zadd(svc._slots_key("alpha"), {"e1": 1.0, "e2": 2.0})

    assert svc.active_slot_count("alpha") == 2


def test_it_is_scoped_to_one_agent():
    svc = _service()
    svc.redis.zadd(svc._slots_key("alpha"), {"e1": 1.0})
    svc.redis.zadd(svc._slots_key("beta"), {"e2": 1.0, "e3": 2.0})

    assert svc.active_slot_count("alpha") == 1
    assert svc.active_slot_count("beta") == 2


def test_a_released_slot_stops_counting():
    svc = _service()
    svc.redis.zadd(svc._slots_key("alpha"), {"e1": 1.0, "e2": 2.0})
    svc.redis.zrem(svc._slots_key("alpha"), "e1")

    assert svc.active_slot_count("alpha") == 1


def test_it_reads_the_same_key_acquire_writes():
    """Not a parallel meter: the key is the one `_slots_key` builds."""
    svc = _service()
    svc.redis.zadd("agent:slots:alpha", {"e1": 1.0})

    assert svc._slots_key("alpha") == "agent:slots:alpha"
    assert svc.active_slot_count("alpha") == 1
