"""The two fleet fan-outs stop costing one agent round trip per poll (#4672).

- `GET /api/ops/fleet/status` (rank 3) asked every running agent for its
  session one after another, with no bound: one slow agent held the whole
  response, and the mobile admin polls it every 15 s.
- `GET /api/agents/context-stats` (rank 6) cached for 15 s against a 15 s poll,
  so the entry had always just expired; and only a fleet admin ever wrote it,
  so anyone else fanned out to their agents on every poll.

No database and no agents here: the seams are the agent client, the access
list and the per-agent fetch.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

from models import User  # noqa: E402


# ===========================================================================
# Rank 6 — context-stats
# ===========================================================================

def _person(name, role="user"):
    return User(id=abs(hash(name)) % 10_000 + 1, username=name, email=f"{name}@example.com", role=role)


@pytest.fixture
def stats(monkeypatch):
    from services.agent_service import stats as module

    module.invalidate_context_stats_cache()
    world = SimpleNamespace(access={}, fetched=[], module=module)

    def accessible(user):
        return [{"name": n, "status": "running"} for n in world.access.get(user.username, [])]

    async def fetch(agent, client, last_activity, active_cutoff):
        world.fetched.append(agent["name"])
        return {"name": agent["name"], "status": "running", "activityState": "idle",
                "contextPercent": 10, "contextUsed": 1, "contextMax": 10, "lastActivityTime": None}

    monkeypatch.setattr(module, "get_accessible_agents", accessible)
    monkeypatch.setattr(module, "_fetch_single_agent_context", fetch)
    monkeypatch.setattr(module.db, "get_latest_activity_for_agents", lambda names: {})
    world.read = lambda user: sorted(
        s["name"] for s in asyncio.run(module.get_agents_context_stats_logic(user))["agents"])
    yield world
    module.invalidate_context_stats_cache()


class TestContextStatsCache:
    def test_the_ttl_outlives_the_poll(self, stats):
        """The dashboard polls every 15 s. At a 15 s TTL the entry had always
        just expired when the next poll arrived."""
        assert stats.module._context_stats_cache["ttl"] > 15

    def test_a_non_admin_fans_out_once_not_on_every_poll(self, stats):
        alice = _person("alice")
        stats.access["alice"] = ["a1", "a2"]
        assert stats.read(alice) == ["a1", "a2"]
        assert stats.read(alice) == ["a1", "a2"]
        assert stats.read(alice) == ["a1", "a2"]
        assert sorted(stats.fetched) == ["a1", "a2"], "one fan-out, then the cache"

    def test_a_cache_hit_never_widens_access(self, stats):
        """Per-caller filtering still applies to cached data: a share revoked
        after the entry was written is gone from the very next answer."""
        alice = _person("alice")
        stats.access["alice"] = ["a1", "a2"]
        assert stats.read(alice) == ["a1", "a2"]
        stats.access["alice"] = ["a1"]
        assert stats.read(alice) == ["a1"]
        assert sorted(stats.fetched) == ["a1", "a2"]

    def test_one_callers_entry_is_never_another_callers_answer(self, stats):
        alice, bob = _person("alice"), _person("bob")
        stats.access.update(alice=["a1", "a2"], bob=["b1"])
        assert stats.read(alice) == ["a1", "a2"]
        assert stats.read(bob) == ["b1"]
        assert sorted(stats.fetched) == ["a1", "a2", "b1"], "bob read for himself"
        assert stats.read(alice) == ["a1", "a2"]
        assert stats.read(bob) == ["b1"]
        assert len(stats.fetched) == 3

    def test_an_agent_key_does_not_get_its_owners_entry(self, stats):
        """An agent-scoped key resolves to its owner but reaches fewer agents:
        the cache key is the principal, and the answer is filtered either way."""
        alice = _person("alice")
        key = User(id=alice.id, username="alice", email=alice.email, role="user",
                   agent_name="a1", mcp_scope="agent")
        stats.access["alice"] = ["a1", "a2"]
        assert stats.read(alice) == ["a1", "a2"]
        assert stats.module._caller_key(key) != stats.module._caller_key(alice)

    def test_the_fleet_entry_still_serves_everyone_filtered(self, stats, monkeypatch):
        admin, alice = _person("root", role="admin"), _person("alice")
        stats.access.update(root=["a1", "a2", "b1"], alice=["a2"])
        assert stats.read(admin) == ["a1", "a2", "b1"]
        assert stats.read(alice) == ["a2"]
        assert sorted(stats.fetched) == ["a1", "a2", "b1"], "alice was answered from the fleet entry"

    def test_a_non_admin_never_writes_the_fleet_entry(self, stats):
        """trinity-enterprise#872, unchanged: a narrower caller's subset must
        not become what the next admin sees."""
        alice, admin = _person("alice"), _person("root", role="admin")
        stats.access.update(alice=["a1"], root=["a1", "a2"])
        stats.read(alice)
        assert stats.module._context_stats_cache["data"] is None
        assert stats.read(admin) == ["a1", "a2"]

    def test_invalidation_drops_every_entry(self, stats):
        alice = _person("alice")
        stats.access["alice"] = ["a1"]
        stats.read(alice)
        stats.module.invalidate_context_stats_cache()
        stats.read(alice)
        assert stats.fetched == ["a1", "a1"]

    def test_an_expired_entry_is_read_again_and_stale_ones_are_dropped(self, stats, monkeypatch):
        alice, bob = _person("alice"), _person("bob")
        stats.access.update(alice=["a1"], bob=["b1"])
        clock = {"now": 1000.0}
        monkeypatch.setattr(stats.module.time, "monotonic", lambda: clock["now"])
        stats.read(alice)
        clock["now"] += stats.module._context_stats_cache["ttl"] + 1
        stats.read(bob)
        assert list(stats.module._context_stats_by_caller) == [stats.module._caller_key(bob)]
        stats.read(alice)
        assert stats.fetched == ["a1", "b1", "a1"]


# ===========================================================================
# Rank 3 — fleet status
# ===========================================================================

@pytest.fixture
def fleet(monkeypatch):
    from routers import ops

    world = SimpleNamespace(ops=ops, delay={}, owner_reads=[], single_owner_reads=[], in_flight=0, peak=0)

    def agent(name, status="running"):
        return SimpleNamespace(name=name, status=status, created=None)

    world.agents = [agent(f"agent-{i}") for i in range(6)] + [agent("stopped-1", "stopped")]

    class _Client:
        def __init__(self, name):
            self.name = name

        async def get_session(self):
            world.in_flight += 1
            world.peak = max(world.peak, world.in_flight)
            try:
                await asyncio.sleep(world.delay.get(self.name, 0.02))
            finally:
                world.in_flight -= 1
            if self.name == "agent-5":
                raise RuntimeError("agent error")
            return SimpleNamespace(context_tokens=10, context_window=100, context_percent=80.0)

    def owners(names):
        world.owner_reads.append(list(names))
        return {n: {"is_system": n == "agent-0"} for n in names}

    monkeypatch.setattr(ops, "list_all_agents_fast", lambda: world.agents)
    monkeypatch.setattr(ops, "get_agent_client", lambda name: _Client(name))
    monkeypatch.setattr(ops, "assert_admin", lambda *a, **k: None)
    monkeypatch.setattr(ops.db, "get_agent_owners", owners)
    monkeypatch.setattr(ops.db, "get_agent_owner",
                        lambda name: world.single_owner_reads.append(name))
    monkeypatch.setattr(ops, "FLEET_STATUS_AGENT_TIMEOUT_S", 0.15)
    world.read = lambda: asyncio.run(ops.get_fleet_status(request=None, current_user=_person("root", "admin")))
    return world


class TestFleetStatus:
    def test_agents_are_read_side_by_side(self, fleet):
        started = time.perf_counter()
        out = fleet.read()
        elapsed = time.perf_counter() - started
        assert fleet.peak > 1, "the sessions were awaited one after another"
        assert elapsed < 6 * 0.02, f"took {elapsed:.3f}s — the sum of every agent's answer"
        assert out["summary"] == {"total": 7, "running": 6, "stopped": 1, "high_context": 5}

    def test_one_slow_agent_cannot_hold_the_response(self, fleet):
        """AC: a bounded timeout. The wedged agent has no context in this
        response; every other agent has its own."""
        fleet.delay["agent-2"] = 5.0
        started = time.perf_counter()
        out = fleet.read()
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, f"took {elapsed:.2f}s"
        by_name = {a["name"]: a for a in out["agents"]}
        assert by_name["agent-2"]["context"] is None
        assert by_name["agent-5"]["context"] is None, "an erroring agent, as before"
        assert all(by_name[f"agent-{i}"]["context"]["context_percent"] == 80.0 for i in (0, 1, 3, 4))
        assert by_name["stopped-1"]["context"] is None
        assert out["summary"]["high_context"] == 4

    def test_concurrency_is_capped(self, fleet, monkeypatch):
        monkeypatch.setattr(fleet.ops, "FLEET_STATUS_CONCURRENCY", 2)
        fleet.read()
        assert fleet.peak == 2

    def test_owners_are_one_read_for_the_whole_fleet(self, fleet):
        out = fleet.read()
        assert fleet.owner_reads == [[a.name for a in fleet.agents]]
        assert fleet.single_owner_reads == []
        assert {a["name"] for a in out["agents"] if a["is_system"]} == {"agent-0"}
