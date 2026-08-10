"""ent#372 — S-03 must reconstruct a slot's initial TTL from ONE instant.

S-03 rebuilds a slot's initial TTL as ``ttl + age``. ``ttl`` comes from the
per-slot Redis pipeline in ``_collect_redis_slot_state``, which runs LAST in
``collect_snapshot()``; ``age`` used to come from ``Snapshot.snapshot_time``,
stamped FIRST, before the docker and roster collectors. Everything elapsing in
between was ticked off ``ttl`` without being added to ``age``, so the
reconstruction landed short by exactly the collector's elapsed time.

Measured on eu2: ``docker=1.0-1.4s``, ``roster=0-0.5s``, always ≥1s — so the
1-second tolerance never covered it, and S-03 fired ``below_floor`` on every
live slot on every cycle (208 critical violations / 85 cycles / 24h, 9 agents,
100% ``kind=below_floor``). The deficit tracked cycle work rather than the
timeout value, which is the tell: it was collector elapsed time, not the slot.

The suite never saw it because every S-03 fixture wrote a score/TTL pair that
was consistent *by construction* — the two reads were the same instant. These
tests deliberately drive the two apart.

The clock is faked (no ``sleep``), so the floor boundary is exact rather than
absorbed by a timing margin (learnings 2026-08-03 / #1909).
"""
from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest


# A literal epoch, so every assertion below is arithmetic rather than a sample
# of the host clock.
T0_UNIX = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc).timestamp()

BUFFER = 300  # services/slot_service.py SLOT_TTL_BUFFER


# ---------------------------------------------------------------------------
# Fakes — a clock the collectors advance, and the slice of Redis S-03 reads
# ---------------------------------------------------------------------------


class _Clock:
    """Wall clock the stubbed collectors advance, in place of real elapsed time."""

    def __init__(self, now: float):
        self.now = now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def time(self) -> float:
        return self.now


class _FakePipeline:
    """Buffers like the real pipeline; every value is computed AT execute().

    Deliberately not eager: the TTL a real Redis returns is evaluated when the
    command reaches the server, which is the whole subject of this issue.
    """

    def __init__(self, parent: "_FakeRedis"):
        self._parent = parent
        self._queued: list = []

    def ttl(self, key: str) -> "_FakePipeline":
        self._queued.append(("ttl", (key,)))
        return self

    def hget(self, key: str, field: str) -> "_FakePipeline":
        self._queued.append(("hget", (key, field)))
        return self

    def execute(self) -> list:
        out = [getattr(self._parent, op)(*args) for op, args in self._queued]
        self._queued.clear()
        return out


class _FakeRedis:
    """Slot ZSET + metadata HASH, with TTLs that decay against the fake clock."""

    def __init__(self, clock: _Clock):
        self.clock = clock
        self.zsets: dict[str, dict[str, float]] = {}
        # eid -> {"expires_at": float|None, "exists": bool, "timeout": int|None}
        self.slots: dict[str, dict] = {}

    def add_slot(
        self,
        agent: str,
        eid: str,
        *,
        score: float,
        expires_at: float | None,
        timeout: int | None,
        exists: bool = True,
    ) -> None:
        self.zsets.setdefault(f"agent:slots:{agent}", {})[eid] = score
        self.slots[f"agent:slot:{agent}:{eid}"] = {
            "expires_at": expires_at,
            "exists": exists,
            "timeout": timeout,
        }

    # --- read surface the canary collector uses ---------------------------

    def zrange(self, key: str, start: int, end: int, withscores: bool = False):
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        return list(items) if withscores else [m for m, _ in items]

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def ttl(self, key: str) -> int:
        slot = self.slots.get(key)
        if slot is None or not slot["exists"]:
            return -2  # key does not exist
        if slot["expires_at"] is None:
            return -1  # exists, no expiry
        return int(slot["expires_at"] - self.clock.time())

    def hget(self, key: str, field: str):
        slot = self.slots.get(key)
        if slot is None or not slot["exists"] or field != "timeout_seconds":
            return None
        return None if slot["timeout"] is None else str(slot["timeout"])

    def get(self, key: str):
        # B-02's `canary:drain_tick_at`. Answered rather than raised: the
        # failure label is `redis.*`, and S-03's source gate matches on that
        # prefix — an unimplemented method here would skip the very check
        # these tests assert on, silently.
        return None

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    def scan(self, cursor: int = 0, match: str = "", count: int = 200):
        return 0, list(self.zsets.keys())


# ---------------------------------------------------------------------------
# Harness — one agent, one slot, a stubbed slow docker collector
# ---------------------------------------------------------------------------


def _collect(
    monkeypatch,
    *,
    docker_elapsed: float,
    agent_cap: int = 3600,
    stored_timeout: int | None = 1200,
    expire_seconds: int | None = None,
    no_expiry: bool = False,
    hash_exists: bool = True,
    slot_age: float = 0.0,
):
    """Run the real `collect_snapshot()` against fakes and return the snapshot.

    `docker_elapsed` is the wall time the pre-Redis collectors burn — the
    interval that used to be silently subtracted from every slot's
    reconstructed TTL. `expire_seconds` defaults to the correct
    `stored_timeout + 300` EXPIRE that `acquire_slot` writes; `no_expiry`
    models the `-1` case (HASH present, EXPIRE never set).
    """
    import canary.snapshot as snapshot_mod

    clock = _Clock(T0_UNIX)
    redis = _FakeRedis(clock)

    if expire_seconds is None and stored_timeout is not None:
        expire_seconds = stored_timeout + BUFFER

    acquired_at = T0_UNIX - slot_age
    redis.add_slot(
        "a1",
        "e1",
        score=acquired_at,
        expires_at=(
            None if (no_expiry or expire_seconds is None) else acquired_at + expire_seconds
        ),
        timeout=stored_timeout,
        exists=hash_exists,
    )

    # The canary reads the clock through the module, so both the snapshot
    # stamp and the per-slot read time move with the fake one.
    monkeypatch.setattr(snapshot_mod, "time", types.SimpleNamespace(time=clock.time))
    monkeypatch.setattr(
        snapshot_mod,
        "utc_now_iso",
        lambda: datetime.fromtimestamp(clock.time(), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    )

    order: list[str] = []

    def _fake_zombies():
        order.append("docker")
        clock.advance(docker_elapsed)  # docker + roster elapsed, in one step
        return {"counts": {}, "pids": {}, "started_at": {}, "names": set(), "unavailable": []}

    def _fake_roster():
        order.append("roster")
        return [
            {
                "agent_name": "a1",
                "is_system": 0,
                "max_parallel_tasks": 3,
                "execution_timeout_seconds": agent_cap,
            }
        ]

    real_slot_state = snapshot_mod._collect_redis_slot_state

    def _traced_slot_state(known_agents):
        order.append("redis")
        return real_slot_state(known_agents)

    monkeypatch.setattr(snapshot_mod, "_collect_zombie_counts", _fake_zombies)
    monkeypatch.setattr(snapshot_mod, "_collect_known_agents", _fake_roster)
    monkeypatch.setattr(snapshot_mod, "_collect_redis_slot_state", _traced_slot_state)

    import services.slot_service as slot_service_mod

    monkeypatch.setattr(
        slot_service_mod,
        "get_slot_service",
        lambda: types.SimpleNamespace(
            redis=redis, slots_prefix="agent:slots:", metadata_prefix="agent:slot:"
        ),
    )

    snap = snapshot_mod.collect_snapshot()
    return snap, order, clock


def _check(snap):
    from canary.invariants import s03_slot_ttl_floor as s03

    return s03.check(snap)


def _snapshot_unix(snap) -> float:
    return datetime.fromisoformat(snap.snapshot_time.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docker_elapsed", [1.0, 1.9, 9.0])
def test_slow_pre_redis_collectors_do_not_fire_on_a_healthy_slot(
    monkeypatch, docker_elapsed
):
    """THE ent#372 regression, at eu2's measured elapsed times and its tail.

    A correctly-acquired slot (EXPIRE = timeout + 300) must stay silent no
    matter how long the docker and roster collectors took before the TTL was
    read.
    """
    snap, _order, _clock = _collect(monkeypatch, docker_elapsed=docker_elapsed)

    assert _check(snap) == []


def test_the_fixture_really_reproduces_the_old_deficit(monkeypatch):
    """Proof the case above is the live bug and not a vacuous green.

    Recomputes S-03's OLD formula (`age` from `snapshot_time`) by hand: on this
    exact snapshot it lands under the floor, i.e. the pre-fix check fires. The
    suite never caught this because every other fixture writes a score/TTL pair
    sampled at one instant.
    """
    snap, _order, _clock = _collect(monkeypatch, docker_elapsed=9.0)
    agent = snap.agents[0]

    ttl = agent.slot_ttls["e1"]
    score = agent.slot_scores["e1"]
    floor = agent.slot_timeouts["e1"] + BUFFER

    old_age = _snapshot_unix(snap) - score          # pre-fix: two instants
    new_age = agent.slot_ttl_read_at["e1"] - score  # post-fix: one instant

    assert ttl + old_age < floor - 1   # the pre-fix check FIRES
    assert ttl + new_age >= floor - 1  # the post-fix check does not
    assert agent.slot_ttl_read_at["e1"] - _snapshot_unix(snap) == pytest.approx(9.0)


def test_read_time_is_recorded_for_every_slot_that_has_a_ttl(monkeypatch):
    """The pair is all-or-nothing — a TTL without its read time is unjudgeable."""
    snap, _order, _clock = _collect(monkeypatch, docker_elapsed=2.0)
    agent = snap.agents[0]

    assert set(agent.slot_ttl_read_at) == set(agent.slot_ttls)


# ---------------------------------------------------------------------------
# What must still fire
# ---------------------------------------------------------------------------


def test_a_genuinely_short_ttl_still_fires_under_a_slow_collector(monkeypatch):
    """Unbiasing the reconstruction must not blind the arm.

    EXPIRE 600 against a stored timeout of 1200 (floor 1500) — the EXPIRE and
    the HSET disagreeing, which is what `below_floor` means since ent#336.
    """
    snap, _order, _clock = _collect(
        monkeypatch, docker_elapsed=9.0, stored_timeout=1200, expire_seconds=600
    )

    violations = _check(snap)

    assert len(violations) == 1
    obs = violations[0].observed_state
    assert violations[0].severity == "critical"
    assert obs["kind"] == "below_floor"
    assert obs["floor_seconds"] == 1500
    assert obs["floor_source"] == "stored"
    # Reconstructed from the read instant: EXPIRE was 600, and the 9s of
    # collector time is added back rather than lost.
    assert obs["initial_ttl_seconds"] == 600
    assert obs["age_seconds"] == 9


def test_metadata_hash_missing_still_fires(monkeypatch):
    """`-2` arm — load-bearing #226 coverage, independent of the floor."""
    snap, _order, _clock = _collect(
        monkeypatch, docker_elapsed=9.0, hash_exists=False
    )

    violations = _check(snap)

    assert len(violations) == 1
    assert violations[0].observed_state["kind"] == "missing"
    assert violations[0].observed_state["redis_ttl_seconds"] == -2


def test_slot_without_expiry_still_fires(monkeypatch):
    """`-1` arm — also independent of the floor and of collector elapsed time."""
    snap, _order, _clock = _collect(
        monkeypatch, docker_elapsed=9.0, no_expiry=True, stored_timeout=1200
    )

    violations = _check(snap)

    assert len(violations) == 1
    assert violations[0].observed_state["kind"] == "no_expiry"
    assert violations[0].observed_state["redis_ttl_seconds"] == -1


# ---------------------------------------------------------------------------
# Neighbouring contracts this fix must not disturb
# ---------------------------------------------------------------------------


def test_ent336_shorter_schedule_timeout_stays_silent(monkeypatch):
    """ent#336 stays fixed: the floor is the slot's own timeout, not the cap.

    Agent cap 3600 (old floor 3900), schedule timeout 2700 → EXPIRE 3000.
    """
    snap, _order, _clock = _collect(
        monkeypatch, docker_elapsed=1.9, agent_cap=3600, stored_timeout=2700
    )

    assert snap.agents[0].slot_timeouts == {"e1": 2700}
    assert _check(snap) == []


def test_unobservable_stored_timeout_still_skips_the_floor_arm(monkeypatch):
    """ent#336's other half: no stored timeout ⇒ skip, never the agent cap."""
    snap, _order, _clock = _collect(
        monkeypatch,
        docker_elapsed=1.9,
        agent_cap=3600,
        stored_timeout=None,
        expire_seconds=3000,
    )

    assert snap.agents[0].slot_timeouts == {}
    assert _check(snap) == []


def test_natural_decay_still_does_not_fire(monkeypatch):
    """#913 regression — an aged slot reconstructs back to exactly its floor."""
    snap, _order, _clock = _collect(
        monkeypatch, docker_elapsed=1.9, stored_timeout=1200, slot_age=600
    )

    assert snap.agents[0].slot_ttls["e1"] < 1500  # decayed below the raw floor
    assert _check(snap) == []


def test_collector_order_is_unchanged(monkeypatch):
    """#1813 — docker runs before the roster read, Redis stays after it.

    The fix records a timestamp; it must not have "solved" the skew by moving
    a collector. H-01 depends on docker having run on the roster-failure path.
    """
    snap, order, _clock = _collect(monkeypatch, docker_elapsed=1.0)

    assert order == ["docker", "roster", "redis"]
    assert snap.collector_ran("docker")
    assert snap.collector_ran("redis")


# ---------------------------------------------------------------------------
# Hand-built snapshots — the fallback that must NOT exist
# ---------------------------------------------------------------------------


def _hand_built(*, ttl: int, stored_timeout: int | None, with_read_time: bool):
    from canary.snapshot import AgentSnapshot, Snapshot

    return Snapshot(
        snapshot_time="2026-08-10T12:00:00Z",
        agents=[
            AgentSnapshot(
                name="a1",
                is_system=False,
                max_parallel=3,
                execution_timeout_seconds=3600,
                slot_ids={"e1"},
                slot_scores={"e1": T0_UNIX},
                slot_ttls={"e1": ttl},
                slot_ttl_read_at=({"e1": T0_UNIX} if with_read_time else {}),
                slot_timeouts=({} if stored_timeout is None else {"e1": stored_timeout}),
            )
        ],
    )


def test_missing_read_time_skips_the_floor_arm_rather_than_using_snapshot_time():
    """No read time ⇒ unjudgeable, so skip. Falling back IS the bug.

    Unreachable at runtime (the collector writes the TTL and its read time in
    one try-block), so this only governs hand-built snapshots — but the stance
    matters: a `snapshot_time` fallback would quietly reinstate ent#372 on any
    future path that populated one field and not the other.
    """
    snap = _hand_built(ttl=600, stored_timeout=1200, with_read_time=False)

    assert _check(snap) == []


def test_missing_read_time_does_not_disarm_the_sentinels():
    """`-2` / `-1` never depended on the age reconstruction, and still don't."""
    snap = _hand_built(ttl=-2, stored_timeout=None, with_read_time=False)

    violations = _check(snap)

    assert len(violations) == 1
    assert violations[0].observed_state["kind"] == "missing"


def test_read_time_present_judges_the_same_slot():
    """Control for the two above — with the read time it is judgeable, and short."""
    snap = _hand_built(ttl=600, stored_timeout=1200, with_read_time=True)

    violations = _check(snap)

    assert len(violations) == 1
    assert violations[0].observed_state["kind"] == "below_floor"
