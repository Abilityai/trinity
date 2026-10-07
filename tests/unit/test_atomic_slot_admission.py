"""Atomic admission regression; opt-in real Redis via TEST_SLOT_REDIS_URL.

Regression for #3305.

Use a disposable Redis database. Only unique test keys are removed.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import multiprocessing
import os
import sys
from types import SimpleNamespace
import uuid

import fakeredis
import pytest
import redis
from services.slot_service import SlotService

@pytest.fixture
def slots(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis, "from_url", lambda *a, **kw: client)
    return SlotService("redis://unused")


def test_duplicate_cannot_authorize_dispatch_or_replace_metadata(slots):
    assert asyncio.run(slots.acquire_slot("test-agent", "first", 3, "original", 900))
    before = slots.redis.hgetall("agent:slot:test-agent:first")
    score = slots.redis.zscore("agent:slots:test-agent", "first")
    assert not asyncio.run(slots.acquire_slot("test-agent", "first", 3, "replacement", 60))
    assert slots.redis.hgetall("agent:slot:test-agent:first") == before
    assert slots.redis.zscore("agent:slots:test-agent", "first") == score
    assert slots.redis.ttl("agent:slot:test-agent:first") > 1190


def test_wrong_type_metadata_rejects_without_partial_admission(slots):
    key = "agent:slot:test-agent:first"
    slots.redis.set(key, "unexpected", ex=600)
    with pytest.raises(redis.ResponseError):
        asyncio.run(slots.acquire_slot("test-agent", "first", 3))
    assert slots.redis.zscore("agent:slots:test-agent", "first") is None
    assert slots.redis.get(key) == "unexpected"
    assert 590 < slots.redis.ttl(key) <= 600


@pytest.mark.parametrize("committed", [False, True])
def test_exec_connection_loss_never_authorizes_dispatch(slots, monkeypatch, committed):
    original = redis.client.Pipeline.execute
    def lost_response(pipe, *args, **kwargs):
        if committed:
            original(pipe, *args, **kwargs)
        raise redis.ConnectionError("test connection lost during EXEC")
    monkeypatch.setattr(redis.client.Pipeline, "execute", lost_response)
    with pytest.raises(redis.ConnectionError):
        asyncio.run(slots.acquire_slot("test-agent", "first", 3))
    assert (slots.redis.zscore("agent:slots:test-agent", "first") is not None) == committed
    assert bool(slots.redis.exists("agent:slot:test-agent:first")) == committed


def test_metadata_capacity_release_and_sentinel_handoff(slots):
    assert asyncio.run(slots.acquire_slot("test-agent", "drain-sentinel", 1, "x" * 150, 60))
    metadata = slots.redis.hgetall("agent:slot:test-agent:drain-sentinel")
    assert metadata["message_preview"] == "x" * 100
    assert metadata["timeout_seconds"] == "60"
    assert metadata["slot_number"] == "1"
    assert metadata["started_at"]
    assert 350 < slots.redis.ttl("agent:slot:test-agent:drain-sentinel") <= 360
    assert not asyncio.run(slots.acquire_slot("test-agent", "rejected", 1))
    assert not slots.redis.exists("agent:slot:test-agent:rejected")
    asyncio.run(slots.release_slot("test-agent", "drain-sentinel"))
    assert asyncio.run(slots.acquire_slot("test-agent", "claimed-row", 1))
    assert slots.redis.zrange("agent:slots:test-agent", 0, -1) == ["claimed-row"]
    assert not slots.redis.exists("agent:slot:test-agent:drain-sentinel")


@pytest.mark.asyncio
async def test_backlog_lost_sentinel_handoff_requeues_without_over_admission(slots, monkeypatch):
    from services import backlog_service
    row = {"id": "claimed-row", "message": "queued work", "status": "queued"}
    def claim(_agent):
        row["status"] = "claimed"
        return row
    def requeue(execution_id):
        assert execution_id == row["id"]
        row["status"] = "queued"
        return True
    monkeypatch.setitem(sys.modules, "database", SimpleNamespace(db=SimpleNamespace(
        get_queued_count=lambda _agent: 1,
        get_execution_timeout=lambda _agent: 900,
        claim_next_queued=claim,
        release_claim_to_queued=requeue,
    )))
    monkeypatch.setitem(sys.modules, "services.settings_service", SimpleNamespace(
        get_effective_max_parallel_tasks=lambda _agent: 1))
    monkeypatch.setitem(sys.modules, "services.pull_pilot", SimpleNamespace(
        is_pull_pilot_agent=lambda _agent: False))
    monkeypatch.setattr(backlog_service, "get_slot_service", lambda: slots)
    release = slots.release_slot
    async def competing_release(agent, execution_id):
        await release(agent, execution_id)
        assert await slots.acquire_slot(agent, "competing-request", 1)
    monkeypatch.setattr(slots, "release_slot", competing_release)
    assert not await backlog_service.BacklogService().drain_next("test-agent")
    assert row["status"] == "queued"
    assert slots.redis.zrange("agent:slots:test-agent", 0, -1) == ["competing-request"]
    assert not slots.redis.exists("agent:slot:test-agent:claimed-row")


@pytest.mark.parametrize("same_id,limit", [(False, 1), (False, 3), (True, 3)])
def test_independent_clients_never_over_admit(monkeypatch, same_id, limit):
    server = fakeredis.FakeServer()
    monkeypatch.setattr(redis, "from_url", lambda *a, **kw: fakeredis.FakeRedis(
        server=server, decode_responses=True))
    barrier = threading.Barrier(6)
    local = threading.local()
    original = redis.Redis.zcard
    def rendezvous(self, key):
        value = original(self, key)
        if not getattr(local, "count_read", False):
            local.count_read = True
            barrier.wait(timeout=10)
        return value
    monkeypatch.setattr(redis.Redis, "zcard", rendezvous)
    monkeypatch.setattr(redis.client.Pipeline, "zcard", rendezvous)
    def acquire(n):
        ident = "duplicate" if same_id else f"execution-{n}"
        return asyncio.run(SlotService("redis://unused").acquire_slot("contended", ident, limit))
    with ThreadPoolExecutor(max_workers=6) as pool:
        admitted = list(pool.map(acquire, range(6)))
    assert sum(admitted) == (1 if same_id else limit)
    # Avoid the admission rendezvous when inspecting the final state.
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    assert len(client.zrange("agent:slots:contended", 0, -1)) == sum(admitted)


def _contend(url, agent, execution, limit, barrier, output):
    # Force a legal old-code interleaving after actual Redis reads, not fake
    # counts. WATCH retries must re-read, so pause only the first observation.
    original = redis.Redis.zcard
    first = True
    def rendezvous(self, key):
        nonlocal first
        value = original(self, key)
        if first:
            first = False
            barrier.wait(timeout=10)
        return value
    redis.Redis.zcard = rendezvous
    redis.client.Pipeline.zcard = rendezvous
    service = SlotService(url)
    output.put(asyncio.run(service.acquire_slot(agent, execution, limit, execution, 900)))


@pytest.mark.parametrize("same_id,limit", [(False, 1), (False, 3), (True, 3)])
def test_concurrent_processes_never_over_admit(same_id, limit):
    url = os.environ.get("TEST_SLOT_REDIS_URL")
    if not url:
        pytest.skip("set TEST_SLOT_REDIS_URL to disposable Redis for process contention")
    client = redis.Redis.from_url(url, decode_responses=True)
    agent = "slot-test-" + uuid.uuid4().hex
    context = multiprocessing.get_context("spawn")
    count = 6
    barrier, output = context.Barrier(count), context.Queue()
    ids = ["duplicate" if same_id else f"execution-{n}" for n in range(count)]
    children = [context.Process(target=_contend, args=(url, agent, ident, limit, barrier, output)) for ident in ids]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(20)
            assert child.exitcode == 0
        admitted = [output.get(timeout=2) for _ in children]
        expected = 1 if same_id else limit
        assert sum(admitted) == expected
        assert client.zcard(f"agent:slots:{agent}") == expected
        for ident in set(ids):
            present = client.zscore(f"agent:slots:{agent}", ident) is not None
            assert bool(client.exists(f"agent:slot:{agent}:{ident}")) == present
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
        client.delete(f"agent:slots:{agent}", *(f"agent:slot:{agent}:{ident}" for ident in set(ids)))
        client.close()
