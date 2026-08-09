"""#1813 — H-01 collector blindness: the four properties raised in review.

These live under ``tests/unit/`` deliberately. The main H-01 suite is in
``tests/test_canary_invariants.py``, which **no CI workflow executes** —
``backend-unit-test.yml`` and ``backend-unit-nightly.yml`` both run
``cd tests && python -m pytest unit/``. A guard placed only beside that suite
would never go red (filed as #2037; same finding recorded by
``test_1880_canary_alert_parity.py`` and ``test_ent337_r01_zombie_dwell.py``).

Each class below pins one review finding. They are regression guards for
behaviour that was *wrong*, not restatements of the implementation — every one
of them fails against the pre-review code.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Set

import pytest


T0 = "2026-07-29T12:00:00Z"
T1 = "2026-07-29T12:05:00Z"


class StringRedis:
    """Redis double with real `ex=` / `expire` / `ttl` semantics.

    The TTL behaviour is the point of finding 4, so a fake that ignored `ex=`
    would let the regression through.

    The hash commands are here for #1897's `canary:alert_pending` store, which
    `_run_cycle_inner` reads on every cycle. Without them the fixture below
    (which monkeypatches this over `CanaryService._redis`) would hit
    `AttributeError` inside the service's fail-open handlers — the tests would
    still pass, but for the wrong reason: the retry machinery would be entirely
    inert here and a bug in it undetectable, with a `logger.exception`
    traceback on every cycle for company.
    """

    def __init__(self) -> None:
        self.strings: Dict[str, str] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.ttls: Dict[str, int] = {}
        self.fail_get = False
        self.fail_set = False

    def get(self, key: str) -> Optional[str]:
        if self.fail_get:
            raise RuntimeError("redis down")
        return self.strings.get(key)

    def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        if self.fail_set:
            raise RuntimeError("read-only replica")
        self.strings[key] = str(value)
        if ex is None:
            self.ttls.pop(key, None)
        else:
            self.ttls[key] = int(ex)
        return True

    def expire(self, key: str, seconds: int) -> bool:
        if key not in self.strings:
            return False
        self.ttls[key] = int(seconds)
        return True

    def ttl(self, key: str) -> int:
        if key in self.ttls:
            return self.ttls[key]
        return -1 if key in self.strings else -2

    def delete(self, key: str) -> int:
        self.ttls.pop(key, None)
        return 1 if self.strings.pop(key, None) is not None else 0

    # HASH — #1897's pending-alert store.

    def hset(self, key: str, field: str, value: str) -> int:
        if self.fail_set:
            raise RuntimeError("read-only replica")
        bucket = self.hashes.setdefault(key, {})
        added = 0 if field in bucket else 1
        bucket[field] = str(value)
        return added

    def hgetall(self, key: str) -> Dict[str, str]:
        if self.fail_get:
            raise RuntimeError("redis down")
        return dict(self.hashes.get(key, {}))

    def hdel(self, key: str, *fields: str) -> int:
        bucket = self.hashes.get(key)
        if not bucket:
            return 0
        removed = sum(1 for f in fields if bucket.pop(f, None) is not None)
        # Real Redis deletes a hash that loses its last field; the difference
        # matters to any assertion phrased as key-absence rather than
        # field-absence.
        if not bucket:
            self.hashes.pop(key, None)
        return removed


@pytest.fixture()
def h01(monkeypatch):
    from canary.invariants import h01_collector_blindness as module

    fake = StringRedis()
    monkeypatch.setattr(module, "_redis", lambda: fake)
    module._fake = fake
    return module


def _snap(
    *,
    at: str = T0,
    known_agents: Optional[Set[str]] = None,
    docker: Optional[Set[str]] = None,
    redis_slots: Optional[Dict[str, int]] = None,
    unavailable: Optional[List[str]] = None,
    ran: Optional[Set[str]] = None,
):
    """`ran=None` means both collectors ran — the normal cycle."""
    from canary.snapshot import COLLECTOR_DOCKER, COLLECTOR_REDIS, Snapshot

    return Snapshot(
        snapshot_time=at,
        known_agents=set(known_agents or ()),
        docker_agent_names=set(docker or ()),
        orphan_redis_slots=dict(redis_slots or {}),
        sources_unavailable=list(unavailable or ()),
        collectors_ran=(
            {COLLECTOR_DOCKER, COLLECTOR_REDIS} if ran is None else set(ran)
        ),
    )


def _fire(h01, **kw):
    """Run the two cycles the confirmation gate requires, return the violation."""
    assert h01.check(_snap(at=T0, **kw)) == [], "first sighting must only arm"
    out = h01.check(_snap(at=T1, **kw))
    assert len(out) == 1
    return out[0]


# ---------------------------------------------------------------------------
# Finding 1a — the `roster_read_failed` arm misreported its own evidence
# ---------------------------------------------------------------------------


class TestRosterFailureReportsRealEvidence:
    """`collect_snapshot` returns early when the roster read raises.

    Before the fix, that meant the Docker and Redis collectors never ran on
    this arm — yet the payload reported `docker_available: True`,
    `redis_available: True`, `evidence_agent_count: 0`, rendering as
    "docker=up · redis=up … 0 vs 0 agent(s)". On the one alarm whose stated job
    is legibility, that reads as "everything else is fine and the fleet is
    empty" when neither source had been consulted.
    """

    ROSTER_FAILED = ["sqlite.agent_ownership: connection refused"]

    def test_docker_evidence_is_present_on_the_roster_failed_arm(self, h01):
        from canary.snapshot import COLLECTOR_DOCKER

        v = _fire(
            h01,
            docker={"a1", "a2"},
            unavailable=self.ROSTER_FAILED,
            ran={COLLECTOR_DOCKER},
        )
        assert v.observed_state["reason"] == h01.REASON_ROSTER_READ_FAILED
        assert v.observed_state["evidence_agent_count"] == 2
        assert sorted(v.observed_state["evidence_sample"]) == ["a1", "a2"]

    def test_a_collector_that_never_ran_reports_none_not_available(self, h01):
        from canary.snapshot import COLLECTOR_DOCKER

        v = _fire(
            h01,
            docker={"a1"},
            unavailable=self.ROSTER_FAILED,
            ran={COLLECTOR_DOCKER},
        )
        assert v.observed_state["docker_available"] is True
        assert v.observed_state["redis_available"] is None

    def test_docker_is_collected_before_the_roster_read(self):
        """The ordering is what makes the evidence above exist at all.

        Asserted against the source rather than behaviour: `collect_snapshot`
        needs a live engine to exercise, and the property being protected is
        purely positional — a future edit that moves the Docker block back
        below the roster read reintroduces the bug silently.
        """
        import inspect

        from canary import snapshot as snapshot_module

        src = inspect.getsource(snapshot_module.collect_snapshot)
        assert src.index("_collect_zombie_counts") < src.index(
            "_collect_known_agents"
        ), (
            "Docker must be collected BEFORE the roster read — the roster read "
            "returns early on failure, and that is the arm where H-01 most "
            "needs independent evidence"
        )

    def test_every_collector_records_that_it_ran(self):
        """`collectors_ran` must be written in `finally`, not on success.

        A collector that raised still RAN; recording it only on the happy path
        would report a crashed collector as never-attempted, which H-01 treats
        as "cannot verify" rather than "failed".
        """
        import inspect

        from canary import snapshot as snapshot_module

        src = inspect.getsource(snapshot_module.collect_snapshot)
        assert src.count("collectors_ran.add(") == 2
        assert "finally:" in src


# ---------------------------------------------------------------------------
# Finding 2 — Redis-only evidence must not page critical
# ---------------------------------------------------------------------------


class TestRedisAloneCannotConfirm:
    """`orphan_redis_slots` is BY DEFINITION slot keys whose agent is absent
    from `agent_ownership` — the leaked-slot state L-03 exists to report. On a
    genuinely empty fleet holding one leaked key, a naive union of the two
    evidence sources fires `roster_empty_contradicted` / critical: a correct
    roster, an unrelated Redis leak, and a critical page claiming the harness
    is blind.
    """

    def test_redis_only_is_major_and_unverifiable(self, h01):
        v = _fire(h01, redis_slots={"ghost": 2})
        assert v.observed_state["reason"] == h01.REASON_UNVERIFIABLE
        assert v.severity == h01.SEVERITY_UNVERIFIABLE == "major"

    def test_redis_names_are_still_reported(self, h01):
        """Demoted, not discarded — the operator still needs to see them."""
        v = _fire(h01, redis_slots={"ghost": 2})
        assert v.observed_state["evidence_sample"] == ["ghost"]
        assert v.observed_state["evidence_agent_count"] == 1

    def test_docker_alone_still_confirms_critical(self, h01):
        v = _fire(h01, docker={"a1"})
        assert v.observed_state["reason"] == h01.REASON_CONTRADICTED
        assert v.severity == h01.SEVERITY_CONFIRMED == "critical"

    def test_docker_plus_redis_confirms_critical(self, h01):
        v = _fire(h01, docker={"a1"}, redis_slots={"ghost": 1})
        assert v.observed_state["reason"] == h01.REASON_CONTRADICTED
        assert v.severity == "critical"
        assert v.observed_state["evidence_agent_count"] == 2

    def test_per_agent_exec_failure_still_confirms(self, h01):
        """`docker.exec[name]` shares the `docker` prefix but is not an outage.

        Presence is recorded before the exec is attempted, so the container is
        in `docker_agent_names` and the contradiction branch wins.
        """
        v = _fire(h01, docker={"a1"}, unavailable=["docker.exec[a1]: exec failed"])
        assert v.observed_state["reason"] == h01.REASON_CONTRADICTED
        assert v.severity == "critical"


# ---------------------------------------------------------------------------
# Finding 3 — the confirmation gate applies to EVERY arm
# ---------------------------------------------------------------------------


class TestConfirmationGateAppliesToEveryArm:
    """The docstring table used to call a raised roster read "definitive" and
    exempt from confirmation, while the code sent every arm through the gate.
    The code was right: a raised read is very often a momentary DB blip (a
    connection reset, a PG restart, brief pool exhaustion), and paging critical
    on one of those is how a safety net gets muted. The docs now say so.
    """

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"unavailable": ["sqlite.agent_ownership: connection refused"]},
            {"docker": {"a1"}},
            {"redis_slots": {"ghost": 1}},
            {"unavailable": ["docker: client unavailable"]},
        ],
        ids=["roster_failed", "contradicted", "redis_only", "unverifiable"],
    )
    def test_no_arm_fires_on_its_first_sighting(self, h01, kwargs):
        assert h01.check(_snap(at=T0, **kwargs)) == []

    def test_the_docstring_no_longer_claims_an_exempt_arm(self, h01):
        """The mismatch itself is the finding, so pin the corrected claim."""
        doc = h01.__doc__ or ""
        assert "not needed — definitive" not in doc
        assert "including `roster_read_failed`" in doc

    def test_elapsed_below_the_gate_stays_silent(self, h01):
        h01.check(_snap(at=T0, docker={"a1"}))
        just_under = "2026-07-29T12:00:59Z"
        assert h01.check(_snap(at=just_under, docker={"a1"})) == []


# ---------------------------------------------------------------------------
# Finding 4 — the marker had no TTL and clears best-effort
# ---------------------------------------------------------------------------


class TestMarkerTTL:
    """`_clear_marker` swallows failures, and `POST /api/canary/run-cycle` with
    an `invariant_ids` filter excluding H-01 never reaches the clear path at
    all. Without an expiry, a marker orphaned either way stays armed forever
    and the next genuine episode confirms on its FIRST cycle — skipping the
    delete-race window the gate exists to ride out.
    """

    KEY = "canary:h01:suspect_since"

    def test_arming_sets_a_ttl(self, h01):
        assert h01.check(_snap(at=T0, docker={"a1"})) == []
        assert 0 < h01._fake.ttl(self.KEY) <= h01.MARKER_TTL_SECONDS

    def test_ttl_outlives_many_cycles_so_a_restart_mid_episode_is_safe(self, h01):
        """The marker tracks the CONDITION, not the process — it must survive a
        backend restart. A TTL of one or two cycles would defeat that."""
        from services.canary_service import CANARY_INTERVAL_SECONDS

        assert h01.MARKER_TTL_SECONDS > CANARY_INTERVAL_SECONDS * 10

    def test_a_live_episode_refreshes_the_ttl(self, h01):
        """Idle timeout, not absolute lifetime.

        An episode outliving the TTL would silently re-arm — going green for a
        cycle and re-alerting on a condition that never changed.
        """
        h01.check(_snap(at=T0, docker={"a1"}))
        h01._fake.expire(self.KEY, 5)
        h01.check(_snap(at=T1, docker={"a1"}))
        assert h01._fake.ttl(self.KEY) > 5

    def test_an_orphaned_marker_expires_rather_than_arming_forever(self, h01, monkeypatch):
        """The exact orphaning path: a swallowed DEL on the healthy cycle.

        `_clear_marker` logs and continues on failure by design (failing the
        whole check over a stale marker would be worse). Without a TTL the key
        then stays armed indefinitely and the next genuine episode confirms on
        its first cycle instead of riding out the delete race.
        """
        h01.check(_snap(at=T0, docker={"a1"}))

        def _swallowed(key):
            raise RuntimeError("DEL failed")

        monkeypatch.setattr(h01._fake, "delete", _swallowed)
        # Healthy cycle — tries to clear, fails, keeps going.
        assert h01.check(_snap(at=T1, known_agents={"a1"}, docker={"a1"})) == []

        assert self.KEY in h01._fake.strings, "the orphaned marker survives"
        assert h01._fake.ttl(self.KEY) > 0, "...but bounded by a TTL, not forever"


# ---------------------------------------------------------------------------
# Fail-loud paths must survive the TTL change
# ---------------------------------------------------------------------------


class TestFailLoud:
    def test_unreadable_marker_fires_unconfirmed(self, h01):
        h01._fake.fail_get = True
        v = h01.check(_snap(at=T0, docker={"a1"}))[0]
        assert v.observed_state["confirmation"] == h01.UNCONFIRMED_NO_MARKER

    def test_unwritable_marker_fires_rather_than_rearming_forever(self, h01):
        h01._fake.fail_set = True
        v = h01.check(_snap(at=T0, docker={"a1"}))[0]
        assert v.observed_state["confirmation"] == h01.UNCONFIRMED_NO_MARKER

    def test_genuinely_empty_fleet_is_silent(self, h01):
        """AC #3 — both collectors ran, both agree there is nothing there."""
        assert h01.check(_snap(at=T0)) == []
        assert h01.check(_snap(at=T1)) == []


# ---------------------------------------------------------------------------
# Finding 1b — a DB outage used to abort the cycle before H-01 ever ran
# ---------------------------------------------------------------------------


class _StubDB:
    """`database.db` stand-in whose latest-violation read can be made to raise."""

    def __init__(self) -> None:
        self.fail_latest = False
        self.inserted: List[str] = []

    def get_latest_canary_violation_per_invariant(self):
        if self.fail_latest:
            raise RuntimeError("could not connect to server")
        return {}

    def insert_canary_violation(self, **kw):
        self.inserted.append(kw["invariant_id"])
        return len(self.inserted)


@pytest.fixture()
def service(monkeypatch):
    """A CanaryService with the DB, Redis, snapshot and alert sink stubbed."""
    from canary.snapshot import Snapshot, ViolationReport
    from services import canary_service as module

    stub_db = _StubDB()
    fake_redis = StringRedis()
    emitted: List[str] = []

    monkeypatch.setattr(module, "db", stub_db)
    monkeypatch.setattr(module.CanaryService, "_redis", staticmethod(lambda: fake_redis))
    monkeypatch.setattr(
        module, "collect_snapshot", lambda: Snapshot(snapshot_time=T0)
    )
    monkeypatch.setattr(
        module,
        "run_invariants",
        lambda snap, ids=None: {
            "H-01": [
                ViolationReport(
                    invariant_id="H-01",
                    tier="A",
                    severity="critical",
                    observed_state={"reason": "roster_read_failed"},
                )
            ]
        },
    )

    async def _emit(inv_id, *a, **kw):
        emitted.append(inv_id)

    monkeypatch.setattr(module.CanaryAlerts, "emit_transition", _emit)

    svc = module.CanaryService()
    svc._stub_db = stub_db
    svc._fake_redis = fake_redis
    svc._emitted = emitted
    return svc


class TestCycleSurvivesADatabaseOutage:
    """`_run_cycle_inner` read `get_latest_canary_violation_per_invariant()`
    BEFORE collecting the snapshot, unguarded. With the database down that
    raised, the loop logged "canary cycle raised; will retry next interval",
    and H-01 — whose entire job is to announce that the harness cannot see the
    fleet — never executed. The module promised coverage it did not have, which
    is the same shape of problem it exists to fix.
    """

    @pytest.mark.asyncio
    async def test_cycle_still_runs_and_h01_fires_when_the_db_is_down(self, service):
        service._stub_db.fail_latest = True

        result = await service.run_cycle()

        assert "H-01" in result.violations
        assert result.transition_invariant_ids == ["H-01"]
        assert service._emitted == ["H-01"]

    @pytest.mark.asyncio
    async def test_a_persistent_outage_alerts_once_not_every_cycle(self, service):
        """Fail-open on its own would swap one defect for another.

        An empty `previous_latest` makes every violation look like a fresh
        green→red flip, so a multi-hour outage would alert every 5 minutes —
        breaking the "a persistent condition chirps once" property the module
        docstring relies on. Redis is a separate failure domain from the DB, so
        the previous cycle's red set is still readable and dedupes against it.
        """
        service._stub_db.fail_latest = True

        await service.run_cycle()
        await service.run_cycle()
        await service.run_cycle()

        assert service._emitted == ["H-01"], (
            "a continuously-red invariant must chirp once, not once per cycle"
        )

    @pytest.mark.asyncio
    async def test_the_red_set_is_recorded_on_healthy_cycles_too(self, service):
        """The fallback is only useful if it predates the outage."""
        from services.canary_service import REDIS_KEY_LAST_CYCLE_RED

        await service.run_cycle()
        assert json.loads(service._fake_redis.get(REDIS_KEY_LAST_CYCLE_RED)) == ["H-01"]

    @pytest.mark.asyncio
    async def test_unknown_red_set_fails_toward_notifying(self, service):
        """Redis down as well → we cannot dedupe, so we tell the operator.

        Verbose-on-failure over silent-on-failure: the canary's whole reason to
        exist is catching transitions. This mirrors `_read_prev_cycle_at`'s
        stated policy.
        """
        service._stub_db.fail_latest = True
        service._fake_redis.fail_get = True

        await service.run_cycle()
        await service.run_cycle()

        assert service._emitted == ["H-01", "H-01"]
