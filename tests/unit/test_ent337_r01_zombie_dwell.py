"""ent#337 — canary R-01 fires only on a zombie that PERSISTS past the dwell.

R-01 used to page **critical** on any single positive sample. A zombie between
child-exit and the parent's ``wait()`` is a normal transient, so it fired on the
sampling window rather than on the #407 leak: 6 critical pages in ~13h on eu2,
every one ``zombie_count: 1`` and every one self-resolved by the next cycle.

These tests live under ``tests/unit/`` deliberately. The pre-existing R-01 tests
are in ``tests/test_canary_invariants.py``, which **no CI workflow executes** —
``backend-unit-test.yml`` runs ``cd tests && python -m pytest unit/``. A guard
placed beside them would never go red (the same finding
``test_1880_canary_alert_parity.py`` records).

## Clock discipline

Every instant here is an integer offset from one literal ``T0``, and R-01 reads
its clock from ``snapshot.snapshot_time`` rather than ``time.time()``. Neither
side ever samples a real clock, so the dwell boundary is asserted exactly
(``dwell-1`` → silent, ``dwell`` → fires) instead of with a margin. Building a
boundary fixture from the test's own ``datetime.now()`` races the
implementation's — see ``docs/memory/learnings.md`` 2026-08-03 (#1909), where
that pattern produced a flake whose rate was a property of the machine.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import pytest


T0 = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)


def _iso(offset_seconds: int) -> str:
    """T0 + offset as an ISO-Z string. The ONLY source of time in this module."""
    return (
        (T0 + timedelta(seconds=offset_seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )


class RecordingRedis:
    """HASH-only Redis double that also records the commands it received.

    Recording matters for one property that is invisible behaviourally: with a
    single writer, an unconditional ``HSET`` of ``first_seen`` and a
    first-write-wins update are indistinguishable by output — but the former
    resets the dwell clock every cycle, so the dwell never elapses and R-01
    becomes permanently blind. The multi-cycle tests below pin the behaviour;
    ``calls`` lets a test pin the mechanism.
    """

    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.calls: List[tuple] = []
        self.fail_writes = False

    def hgetall(self, key: str) -> Dict[str, str]:
        self.calls.append(("hgetall", key))
        return dict(self.hashes.get(key, {}))

    def hset(self, key: str, mapping: Optional[Dict[str, str]] = None, **kw):
        self.calls.append(("hset", key, dict(mapping or {})))
        if self.fail_writes:
            raise RuntimeError("redis write failed")
        self.hashes.setdefault(key, {}).update(mapping or {})
        return len(mapping or {})

    def hdel(self, key: str, *fields: str) -> int:
        self.calls.append(("hdel", key, fields))
        bucket = self.hashes.get(key, {})
        return sum(bucket.pop(f, None) is not None for f in fields)

    def delete(self, key: str) -> int:
        self.calls.append(("delete", key))
        return 1 if self.hashes.pop(key, None) is not None else 0

    def expire(self, key: str, seconds: int) -> bool:
        self.calls.append(("expire", key, seconds))
        return key in self.hashes


@pytest.fixture()
def r01(monkeypatch):
    """The R-01 module with its Redis client replaced by a recording double."""
    from canary.invariants import r01_no_zombie_claude as module

    fake = RecordingRedis()
    monkeypatch.setattr(module, "_redis", lambda: fake)
    module._fake = fake  # test handle
    return module


# Container `State.StartedAt` values — the PID-namespace generation. `GEN_A` is
# the default for every agent, so the multi-cycle tests below run the realistic
# "same container throughout" path rather than the unobservable one.
GEN_A = "2026-05-18T11:00:00.000000000Z"
GEN_B = "2026-05-18T13:30:00.000000000Z"


def _snap(
    *,
    at: int,
    pids: Dict[str, Set[int]],
    started_at: Optional[Dict[str, str]] = None,
):
    """A Snapshot carrying only what R-01 reads.

    `started_at=None` means "same container as always" (GEN_A for every agent).
    Pass `{}` to model a cycle where the field was not observable, or an
    explicit map to model a restart.
    """
    from canary.snapshot import Snapshot

    generations = (
        dict(started_at)
        if started_at is not None
        else {name: GEN_A for name in pids}
    )
    return Snapshot(
        snapshot_time=_iso(at),
        zombie_pids=dict(pids),
        zombie_counts={name: len(p) for name, p in pids.items()},
        zombie_container_started_at=generations,
    )


# ---------------------------------------------------------------------------
# The reported bug: a transient must not fire
# ---------------------------------------------------------------------------


def test_single_transient_zombie_does_not_fire(r01):
    """One zombie in cycle N, gone in cycle N+1 → no violation, ever.

    This is the eu2 signature verbatim: all 6 pages were `zombie_count: 1` on
    containers that were never restarted, reaped by the ordinary parent
    `wait()` within one 5-minute cycle.
    """
    assert r01.check(_snap(at=0, pids={"a1": {811}})) == []
    assert r01.check(_snap(at=300, pids={"a1": set()})) == []
    # And the marker is gone, so a later genuine leak starts a fresh dwell.
    assert r01._fake.hashes.get("agent:canary_zombie:a1", {}) == {}


def test_succession_of_distinct_transients_never_fires(r01):
    """The failure a COUNT-based dwell cannot see (ent#337 review finding).

    Three consecutive cycles each observe exactly one zombie, so a count-based
    dwell would read "1, 1, 1 — held for 600s" and page critical. They are
    three DIFFERENT pids: each was reaped and replaced. No zero is ever
    observed, so "clear the marker when the count returns to 0" does not save
    a count-based rule either — which is why the dwell is per-pid.
    """
    assert r01.check(_snap(at=0, pids={"a1": {811}})) == []
    assert r01.check(_snap(at=300, pids={"a1": {902}})) == []
    assert r01.check(_snap(at=600, pids={"a1": {1043}})) == []
    assert r01.check(_snap(at=900, pids={"a1": {1156}})) == []


# ---------------------------------------------------------------------------
# The bug class R-01 must still catch: a PERSISTING zombie
# ---------------------------------------------------------------------------


def test_fires_when_one_pid_persists_past_dwell(r01):
    """#407 regression guard survives the dwell gate."""
    assert r01.check(_snap(at=0, pids={"a1": {811}})) == []
    violations = r01.check(_snap(at=r01.DWELL_SECONDS, pids={"a1": {811}}))

    assert len(violations) == 1
    v = violations[0]
    assert v.invariant_id == "R-01"
    assert v.severity == "critical"
    assert v.observed_state["agent_name"] == "a1"
    assert v.observed_state["persisting_pid"] == 811
    assert v.observed_state["held_for_seconds"] == r01.DWELL_SECONDS
    assert v.observed_state["first_seen_at"] == _iso(0)
    # Load-bearing key: `canary_alerts` renders it. A rename would silently
    # print "0 zombie(s)" rather than failing loudly (`.get(k, 0)`).
    assert v.observed_state["zombie_count"] == 1


def test_dwell_boundary_is_exact(r01):
    """dwell-1 → silent; dwell → fires. Asserted exactly, not with a margin."""
    r01.check(_snap(at=0, pids={"a1": {811}}))
    assert r01.check(_snap(at=r01.DWELL_SECONDS - 1, pids={"a1": {811}})) == []
    assert len(r01.check(_snap(at=r01.DWELL_SECONDS, pids={"a1": {811}}))) == 1


def test_growth_is_reported_as_a_trend(r01):
    """ent#337 AC — an accumulating leak is distinguishable from a stuck one.

    The dwell already covers "grows across cycles" (a growing count has been
    >0 continuously, so the dwell elapses). What the alert reader needs is the
    trend, so first-seen and current counts both ride in observed_state.
    """
    r01.check(_snap(at=0, pids={"a1": {811}}))
    r01.check(_snap(at=300, pids={"a1": {811, 902}}))
    v = r01.check(_snap(at=r01.DWELL_SECONDS, pids={"a1": {811, 902, 1043}}))

    assert len(v) == 1
    assert v[0].observed_state["first_seen_count"] == 1
    assert v[0].observed_state["zombie_count"] == 3


def test_reaped_pid_is_dropped_without_needing_an_observed_zero(r01):
    """A pid that goes away stops counting even while others remain.

    Otherwise a busy agent that always has *some* zombie would accumulate
    dwell against pids that were reaped long ago.
    """
    r01.check(_snap(at=0, pids={"a1": {811}}))
    # 811 reaped, 902 appears — 902's dwell starts now, not at T0.
    r01.check(_snap(at=300, pids={"a1": {902}}))
    assert r01.check(_snap(at=300 + r01.DWELL_SECONDS - 1, pids={"a1": {902}})) == []
    assert len(r01.check(_snap(at=300 + r01.DWELL_SECONDS, pids={"a1": {902}}))) == 1


# ---------------------------------------------------------------------------
# Continuity — the dwell measures observed persistence, not elapsed time
# ---------------------------------------------------------------------------


def test_observation_gap_restarts_the_dwell(r01):
    """A docker-exec gap must not be counted as dwell time.

    Without this, an agent whose container was stopped (or whose exec failed,
    or whose backend was down) for an hour would fire on its very next sample:
    `now - first_seen` exceeds the dwell with zero evidence that the same pid
    was there throughout. That is a false positive of exactly the kind the
    dwell exists to remove.
    """
    r01.check(_snap(at=0, pids={"a1": {811}}))
    # Nothing observed for an hour — the agent is simply absent from the map.
    assert r01.check(_snap(at=3600, pids={"a1": {811}})) == []
    # The dwell restarted at 3600, so the boundary is measured from there.
    assert r01.check(_snap(at=3600 + r01.DWELL_SECONDS - 1, pids={"a1": {811}})) == []
    assert len(r01.check(_snap(at=3600 + r01.DWELL_SECONDS, pids={"a1": {811}}))) == 1


def test_first_seen_is_not_rewritten_on_each_cycle(r01):
    """The dwell-timer trap, pinned at the mechanism.

    An unconditional `first_seen = now` on every positive cycle resets the
    clock each time, so `now - first_seen` never reaches the dwell and R-01 is
    permanently blind — strictly worse than the noisy pages it replaced. With
    one writer that is behaviourally indistinguishable from a correct update,
    so assert the stored value directly.
    """
    key = "agent:canary_zombie:a1"
    r01.check(_snap(at=0, pids={"a1": {811}}))
    first_write = r01._fake.hashes[key]["811"]
    r01.check(_snap(at=300, pids={"a1": {811}}))
    second_write = r01._fake.hashes[key]["811"]

    assert first_write.split(":")[0] == second_write.split(":")[0]
    # ...and last_seen DID advance, which is what powers the continuity check.
    assert float(second_write.split(":")[2]) > float(first_write.split(":")[2])


# ---------------------------------------------------------------------------
# Failure modes — fail toward NOT firing
# ---------------------------------------------------------------------------


def test_agent_absent_from_the_map_is_skipped_not_cleared(r01):
    """An exec failure must neither fire nor reset a dwell in progress.

    A flaky exec resetting the marker would let a genuine leak evade the dwell
    indefinitely on an agent whose exec is intermittently failing.
    """
    key = "agent:canary_zombie:a1"
    r01.check(_snap(at=0, pids={"a1": {811}}))
    before = dict(r01._fake.hashes[key])

    # Cycle where the exec failed: the agent is not in `zombie_pids` at all.
    assert r01.check(_snap(at=300, pids={})) == []
    assert r01._fake.hashes[key] == before


def test_redis_unavailable_fires_nothing(r01, monkeypatch):
    """A canary that pages on its own unreadable state is worse than quiet."""
    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(r01, "_redis", _boom)
    assert r01.check(_snap(at=0, pids={"a1": {811}})) == []


def test_marker_write_failure_does_not_fire_or_raise(r01):
    """A failed write must not raise into the cycle (it is logged instead)."""
    r01._fake.fail_writes = True
    assert r01.check(_snap(at=0, pids={"a1": {811}})) == []


def test_docker_wholesale_unavailable_is_silent(r01):
    """No zombie_counts and no zombie_pids at all → nothing to judge."""
    from canary.snapshot import Snapshot

    assert r01.check(Snapshot(snapshot_time=_iso(0))) == []


def test_unparseable_snapshot_time_skips_cycle(r01):
    """R-01's clock is the snapshot; an unusable one means no judgement."""
    from canary.snapshot import Snapshot

    snap = Snapshot(
        snapshot_time="not-a-timestamp",
        zombie_pids={"a1": {811}},
        zombie_counts={"a1": 1},
    )
    assert r01.check(snap) == []


def test_corrupt_marker_restarts_dwell_rather_than_firing(r01):
    """An unusable stored value is treated as absent, never as 'long ago'."""
    r01._fake.hashes["agent:canary_zombie:a1"] = {"811": "garbage"}
    assert r01.check(_snap(at=0, pids={"a1": {811}})) == []
    # It was rewritten as a fresh marker, so the dwell now runs from here.
    assert len(r01.check(_snap(at=r01.DWELL_SECONDS, pids={"a1": {811}}))) == 1


# ---------------------------------------------------------------------------
# Container restart — a PID is only an identity within ONE pid namespace
# ---------------------------------------------------------------------------


def test_container_restart_restarts_the_dwell(r01):
    """A restart outside the backend must not let a dwell carry over.

    `clear_agent_breakers` covers backend-mediated lifecycle events; a
    `docker restart`, or a restart policy firing after an OOM kill or a crash,
    does not go through it. The new pid namespace hands out low pids
    immediately, so the marker would otherwise be inherited by unrelated
    processes.
    """
    r01.check(_snap(at=0, pids={"a1": {811}}))
    # Restart at ~t+300. Same pid observed, brand-new namespace.
    assert r01.check(_snap(at=300, pids={"a1": {811}}, started_at={"a1": GEN_B})) == []
    # Measured from T0 the dwell has long elapsed — it must not fire.
    assert (
        r01.check(
            _snap(at=r01.DWELL_SECONDS, pids={"a1": {811}}, started_at={"a1": GEN_B})
        )
        == []
    )
    # It fires on the dwell measured from the RESTART, not from before it.
    assert (
        len(
            r01.check(
                _snap(
                    at=300 + r01.DWELL_SECONDS,
                    pids={"a1": {811}},
                    started_at={"a1": GEN_B},
                )
            )
        )
        == 1
    )


def test_restart_then_immediate_transient_on_a_reused_pid_is_silent(r01):
    """The review scenario, end to end (ent#337 review point 1).

    A fresh pid namespace hands out LOW pids immediately, and a zombie `claude`
    in a just-restarted agent is exactly the low-pid case — so a transient
    landing on a pid still in the marker is not a remote possibility. Without
    the generation check it inherits a dwell-old `first_seen` and pages
    critical on its FIRST sample, which is the false positive this whole PR
    removes.
    """
    r01.check(_snap(at=0, pids={"a1": {811}}))
    r01.check(_snap(at=r01.DWELL_SECONDS - 1, pids={"a1": {811}}))
    # Container restarts; a brand-new, unrelated transient happens to get 811.
    assert (
        r01.check(
            _snap(
                at=r01.DWELL_SECONDS,
                pids={"a1": {811}},
                started_at={"a1": GEN_B},
            )
        )
        == []
    )


def test_unobservable_generation_leaves_the_dwell_alone(r01):
    """An unreadable field is not evidence of a restart.

    Restarting the dwell whenever the generation is missing would restart it
    EVERY cycle on any deployment where the field cannot be read — a
    permanently blind critical invariant, the same failure mode as rewriting
    `first_seen` each cycle. Only an observed MISMATCH invalidates.
    """
    assert r01.check(_snap(at=0, pids={"a1": {811}}, started_at={})) == []
    assert (
        len(r01.check(_snap(at=r01.DWELL_SECONDS, pids={"a1": {811}}, started_at={})))
        == 1
    )


def test_generation_is_stored_and_survives_the_departed_pid_reap(r01):
    """`__started_at` is not a pid and must not be HDEL'd as a departed one.

    If it were reaped, the next cycle would find no stored generation, the
    mismatch test could never fire, and the guard would be decorative.
    """
    key = "agent:canary_zombie:a1"
    r01.check(_snap(at=0, pids={"a1": {811}}))
    assert r01._fake.hashes[key][r01._GENERATION_FIELD] == GEN_A

    # 811 reaped, 902 appears — the departed-pid reap runs on this cycle.
    r01.check(_snap(at=300, pids={"a1": {902}}))
    assert "811" not in r01._fake.hashes[key]
    assert r01._fake.hashes[key][r01._GENERATION_FIELD] == GEN_A


def test_generation_field_is_non_numeric_so_it_cannot_collide_with_a_pid(r01):
    """Every other field is `str(pid)`. Collision must be structural, not luck."""
    assert not r01._GENERATION_FIELD.isdigit()


# ---------------------------------------------------------------------------
# The collector side of the generation signal
# ---------------------------------------------------------------------------


class _FakeExecResult:
    def __init__(self, output: bytes) -> None:
        self.output = output
        self.exit_code = 0


class _FakeContainer:
    def __init__(self, name: str, attrs: dict, output: bytes = b"") -> None:
        self.name = name
        self.attrs = attrs
        self._output = output

    def exec_run(self, cmd):
        return _FakeExecResult(self._output)


def _collect_with(containers, monkeypatch):
    """Run `_collect_zombie_counts` against a stubbed docker client."""
    import sys
    import types

    from canary import snapshot as snapshot_module

    fake_client = types.SimpleNamespace(
        containers=types.SimpleNamespace(list=lambda **kw: list(containers))
    )
    module = types.ModuleType("services.docker_service")
    module.docker_client = fake_client
    monkeypatch.setitem(sys.modules, "services.docker_service", module)
    return snapshot_module._collect_zombie_counts()


def test_collector_reports_container_started_at(monkeypatch):
    """The generation rides along at zero cost.

    docker-py's `containers.list()` defaults to `sparse=False`, which already
    issues a full inspect per container — so `State.StartedAt` is in `attrs`
    before we ask, and no extra API call is needed.
    """
    out = _collect_with(
        [
            _FakeContainer(
                "agent-a1",
                {"State": {"Status": "running", "StartedAt": GEN_A}},
                output=b"811\n",
            )
        ],
        monkeypatch,
    )
    assert out["started_at"] == {"a1": GEN_A}
    assert out["pids"] == {"a1": {811}}
    assert out["counts"] == {"a1": 1}


def test_collector_omits_started_at_when_attrs_are_sparse(monkeypatch):
    """On the sparse list path `attrs["State"]` is a status STRING, not a dict.

    The agent must be ABSENT from the map, not carry a falsy placeholder: R-01
    reads absence as "not observed → leave the dwell alone", whereas a constant
    would invalidate every marker every cycle.
    """
    out = _collect_with(
        [_FakeContainer("agent-a1", {"State": "running"}, output=b"")],
        monkeypatch,
    )
    assert out["started_at"] == {}
    assert out["pids"] == {"a1": set()}


def test_collector_records_started_at_even_when_the_exec_fails(monkeypatch):
    """The generation is a property of the CONTAINER, not of the exec.

    An agent whose exec failed is skipped by R-01 this cycle, but a restart it
    went through is still real and must be visible on the cycle the exec
    recovers.
    """

    class _Boom(_FakeContainer):
        def exec_run(self, cmd):
            raise RuntimeError("container is restarting")

    out = _collect_with(
        [_Boom("agent-a1", {"State": {"StartedAt": GEN_B}})],
        monkeypatch,
    )
    assert out["started_at"] == {"a1": GEN_B}
    assert "a1" not in out["pids"]
    assert any("docker.exec[a1]" in u for u in out["unavailable"])


# ---------------------------------------------------------------------------
# #1560 — the marker is name-keyed, so it must be lifecycle-cleared
# ---------------------------------------------------------------------------


def test_marker_key_is_agent_prefixed_so_1560_parity_sees_it():
    """A `canary:`-prefixed key would ship outside the #1560 registry silently.

    `test_1560_agent_redis_key_parity.py` matches on the literal `agent:`
    prefix. E-02's `canary:e02:*` keys are legitimately unregistered because
    they are global; this is the first NAME-KEYED canary key, so it inherits
    the recycled-name hazard the registry exists for.
    """
    from canary.invariants import r01_no_zombie_claude as module

    assert module.REDIS_KEY_PREFIX.startswith("agent:")


def test_marker_keyspace_is_registered_for_lifecycle_clearing():
    """Purge `foo` mid-dwell, a new `foo` appears → it must not inherit."""
    from services.agent_runtime_state import CLEARED_KEYSPACES
    from canary.invariants import r01_no_zombie_claude as module

    assert module.REDIS_KEY_PREFIX in CLEARED_KEYSPACES
