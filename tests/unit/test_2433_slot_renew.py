"""#2433 — ``SlotService.renew_slot`` re-anchors a HELD slot's lease at now.

Moves the ZSET score and the metadata-hash TTL together (canary S-03
reconstructs ``initial = ttl_remaining + (read − score)``, so both must move),
uses the slot's OWN stored timeout, and is a no-op (False) for a slot that is
no longer held (``ZADD XX`` never resurrects a released/reclaimed member).

Module under test: src/backend/services/slot_service.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import fakeredis
import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.slot_service import SlotService, SLOT_TTL_BUFFER, DEFAULT_SLOT_TTL_SECONDS  # noqa: E402


def _service():
    svc = SlotService.__new__(SlotService)
    svc.redis = fakeredis.FakeRedis(decode_responses=True)
    svc.slots_prefix = "agent:slots:"
    svc.metadata_prefix = "agent:slot:"
    svc._on_release_callbacks = []
    return svc


def _acquire(svc, agent, eid, timeout, score):
    svc.redis.zadd(svc._slots_key(agent), {eid: score})
    key = svc._metadata_key(agent, eid)
    svc.redis.hset(key, mapping={"timeout_seconds": str(timeout), "started_at": "x"})
    svc.redis.expire(key, timeout + SLOT_TTL_BUFFER)


def test_renew_moves_score_and_hash_ttl_together():
    svc = _service()
    old_score = time.time() - 1000
    _acquire(svc, "agent-a", "exec-1", 900, old_score)
    key = svc._metadata_key("agent-a", "exec-1")
    svc.redis.expire(key, 5)  # nearly expired, as after a long park
    assert svc.renew_slot("agent-a", "exec-1") is True
    new_score = svc.redis.zscore(svc._slots_key("agent-a"), "exec-1")
    assert new_score > old_score + 900
    ttl = svc.redis.ttl(key)
    assert 900 + SLOT_TTL_BUFFER - 2 <= ttl <= 900 + SLOT_TTL_BUFFER
    # S-03 reconstruction: initial = ttl_remaining + (now − score) ≈ floor.
    assert abs((ttl + (time.time() - new_score)) - (900 + SLOT_TTL_BUFFER)) < 2


def test_renew_uses_the_slots_own_stored_timeout():
    svc = _service()
    _acquire(svc, "agent-a", "exec-long", 7200, time.time())
    assert svc.renew_slot("agent-a", "exec-long") is True
    assert svc.redis.ttl(svc._metadata_key("agent-a", "exec-long")) > 7200


def test_renew_falls_back_to_default_ttl_without_metadata():
    svc = _service()
    svc.redis.zadd(svc._slots_key("agent-a"), {"exec-nm": time.time() - 50})
    assert svc.renew_slot("agent-a", "exec-nm") is True
    # No hash existed; EXPIRE on a missing key is a no-op and must not raise.
    assert svc.redis.ttl(svc._metadata_key("agent-a", "exec-nm")) in (-1, -2)


def test_renew_never_resurrects_a_released_slot():
    svc = _service()
    assert svc.renew_slot("agent-a", "exec-gone") is False
    assert svc.redis.zcard(svc._slots_key("agent-a")) == 0


def test_renew_is_fail_open():
    svc = _service()

    class _Broken:
        def zadd(self, *a, **k):
            raise ConnectionError("down")

    svc.redis = _Broken()
    assert svc.renew_slot("agent-a", "exec-1") is False
