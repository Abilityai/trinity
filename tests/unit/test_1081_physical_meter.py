"""Physical-occupancy METER for pull-pilot agents — #1081 Phase 3
("capacity becomes physical").

An ADDITIVE, read-only meter term: a pull pilot claims capacity with a pure SQL
``UPDATE`` (no Redis slot ZADD), so the ZSET-derived slot meter reads 0 for it.
``CapacityManager``'s TWO meter methods (``get_all_states`` / ``get_slot_state``)
add the disjoint physical term — the pilot's count of ``running`` leased rows —
so occupancy reflects real rows. This is METERING ONLY: admission
(``acquire`` / ``acquire_slot`` / ``release``) is never touched, so the ZSET
(push) and lease (pull) terms are disjoint-by-construction and summing can't
double-count. Six proofs (see task scope):

  1. ``count_active_leased_by_agent`` counts only ``running`` + non-NULL-lease
     rows; queued / no-lease-push / terminal rows and absent agents ⇒ 0/absent.
  2. Disjointness: a push agent (ZSET) + a pilot (leased rows) → ``active`` is
     ZCARD for push and leased for pilot; #506 clamp respected + floor at 0.
  3. Claim-doesn't-ZADD: ``claim_next_queued`` on a pilot leaves the slot ZSET
     empty while the meter shows leased == 1.
  4. Inertness: a non-pilot's meter output is identical allowlist-set vs unset,
     and the leased reader is never called for it.
  5. Reaper interaction: claim → expire → re-queue / park drops the meter to 0.

Pure DB (real ``db_harness`` schema; SQLite always, PostgreSQL when
``TEST_POSTGRES_URL`` is set) + a ``fakeredis``-backed real ``SlotService`` for
the ZSET term. No live agent / model turn.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import fakeredis
import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable (mirror test_1081_lease_reaper.py).
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _scalar  # noqa: E402

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# sys.modules hygiene (Issue #762): this file evicts shadowing/cached backend
# modules (at import time above, and in the db fixtures below) so production
# code re-resolves against the db_harness engine. Snapshot + restore those
# names around every test so the eviction never leaks to other test files.
# The _STUBBED_MODULE_NAMES + _restore_sys_modules pair is the lint-recognised
# precedent (tests/unit/test_telegram_webhook_backfill.py, tests/lint_sys_modules.py).
# ---------------------------------------------------------------------------
_STUBBED_MODULE_NAMES = [
    "utils",
    "utils.api_client",
    "utils.assertions",
    "utils.cleanup",
    "db.connection",
    "db.schedules",
    "db.operator_queue",
    "db.agent_settings.resources",
    "database",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot the churned backend modules before each test and restore them
    after, so this file's import-time + fixture sys.modules eviction cannot
    pollute unrelated tests in the same session (Issue #762)."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value



def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(db_backend):
    """Fresh full production schema; pop cached db modules so production code
    re-resolves against the harness engine (mirror test_1081_lease_reaper)."""
    def _evict():
        for mod in ("db.connection", "db.schedules", "db.operator_queue",
                    "db.agent_settings.resources", "database"):
            sys.modules.pop(mod, None)

    _evict()
    try:
        yield db_backend
    finally:
        _evict()


@pytest.fixture
def fake_redis(monkeypatch):
    """Point ``redis.from_url`` (used by both CapacityManager and SlotService)
    at one shared fakeredis server so the ZSET term is real but in-memory."""
    import redis as redis_mod

    srv = fakeredis.FakeServer()
    monkeypatch.setattr(
        redis_mod,
        "from_url",
        lambda *a, **k: fakeredis.FakeStrictRedis(server=srv, decode_responses=True),
    )
    return srv


@pytest.fixture
def set_ceiling(monkeypatch):
    """Monkeypatch the #506 fleet ceiling deterministically (no settings DB)."""
    def _set(value: int):
        from services import settings_service as ss
        monkeypatch.setattr(ss, "get_max_parallel_tasks_ceiling", lambda: value)
    return _set


@pytest.fixture
def capacity(tmp_db, fake_redis):
    """A CapacityManager wired to a real SlotService (fakeredis) + mock backlog.

    The meter methods reach the harness DB via their internal
    ``from database import db``.
    """
    from services.slot_service import SlotService
    from services.capacity_manager import CapacityManager

    return CapacityManager(
        redis_url="redis://test",
        slot_service=SlotService("redis://test"),
        backlog_service=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_agent(name: str) -> None:
    _hrun(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
        "VALUES (:n, 1, '2026-01-01T00:00:00Z')",
        n=name,
    )


def _insert_exec(
    eid: str,
    agent: str,
    *,
    status: str = "running",
    lease_offset: int | None = None,
    queued: bool = False,
    redelivery_count: int = 0,
) -> str:
    """Insert one schedule_executions row directly.

    ``lease_offset`` — seconds relative to now for ``lease_expires_at``
    (negative ⇒ already expired, positive ⇒ live, ``None`` ⇒ NULL lease/push).
    ``queued`` ⇒ stamp ``queued_at`` (for the claim path).
    """
    now = datetime.now(timezone.utc)
    lease = _iso(now + timedelta(seconds=lease_offset)) if lease_offset is not None else None
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, queued_at, message, "
        " triggered_by, claim_token, lease_expires_at, claimed_by_worker, "
        " redelivery_count) "
        "VALUES (:id, '__m__', :a, :st, :sa, :qa, 'm', 'manual', :tok, :lease, "
        " :w, :rc)",
        id=eid, a=agent, st=status, sa=_iso(now - timedelta(seconds=60)),
        qa=(_iso(now) if queued else None),
        tok=(f"tok-{eid}" if lease else None), lease=lease,
        w=(f"{agent}#w1" if lease else None), rc=redelivery_count,
    )
    return eid


# ===========================================================================
# Proof 1 — the read-only leased-row counter
# ===========================================================================


class TestCountActiveLeased:
    def test_counts_only_running_with_live_lease(self, tmp_db):
        """1 queued + 2 running-lease + 1 running-no-lease + 1 terminal → 2."""
        _seed_agent("pilot")
        _insert_exec("q1", "pilot", status="queued", queued=True)          # not counted
        _insert_exec("l1", "pilot", status="running", lease_offset=900)    # +1
        _insert_exec("l2", "pilot", status="running", lease_offset=900)    # +1
        _insert_exec("p1", "pilot", status="running", lease_offset=None)   # push, not counted
        _insert_exec("s1", "pilot", status="success", lease_offset=None)   # terminal, not counted
        from database import db

        assert db.count_active_leased_by_agent(["pilot"]) == {"pilot": 2}
        assert db.count_active_leased("pilot") == 2

    def test_expired_lease_still_counted(self, tmp_db):
        """A row whose lease is already past is still `running` → still counted
        (the reaper converges it; excluding would under-report)."""
        _seed_agent("pilot")
        _insert_exec("e1", "pilot", status="running", lease_offset=-30)
        from database import db

        assert db.count_active_leased("pilot") == 1

    def test_push_only_agent_is_zero(self, tmp_db):
        """A non-pilot push agent (running, NULL lease) → absent / 0."""
        _seed_agent("push")
        _insert_exec("pu1", "push", status="running", lease_offset=None)
        from database import db

        assert db.count_active_leased_by_agent(["push"]) == {}
        assert db.count_active_leased("push") == 0

    def test_empty_and_absent(self, tmp_db):
        _seed_agent("pilot")
        from database import db

        assert db.count_active_leased_by_agent([]) == {}
        assert db.count_active_leased_by_agent(["ghost"]) == {}
        assert db.count_active_leased("ghost") == 0

    def test_grouped_query_is_per_agent(self, tmp_db):
        """One grouped query returns a correct per-agent map (not N+1)."""
        _seed_agent("a")
        _seed_agent("b")
        _insert_exec("a1", "a", status="running", lease_offset=900)
        _insert_exec("a2", "a", status="running", lease_offset=900)
        _insert_exec("b1", "b", status="running", lease_offset=900)
        from database import db

        assert db.count_active_leased_by_agent(["a", "b"]) == {"a": 2, "b": 1}


# ===========================================================================
# Proof 2 — disjointness in get_all_states (push ZSET vs pilot leased)
# ===========================================================================


class TestDisjointBulkMeter:
    def test_push_zcard_pilot_leased_no_double_count(
        self, capacity, monkeypatch, set_ceiling
    ):
        set_ceiling(5)
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "pilot")
        _seed_agent("push")
        _seed_agent("pilot")
        # push: one REAL ZSET slot; pilot: two leased rows, ZSET empty.
        assert asyncio.run(
            capacity._slots.acquire_slot("push", "x1", max_parallel_tasks=3)
        ) is True
        _insert_exec("l1", "pilot", status="running", lease_offset=900)
        _insert_exec("l2", "pilot", status="running", lease_offset=900)

        states = asyncio.run(capacity.get_all_states({"push": 3, "pilot": 3}))

        # push = pure ZCARD term (not a pilot → no leased added).
        assert states["push"] == {"max": 3, "active": 1}
        # pilot = pure leased term (ZSET empty, no double count).
        assert states["pilot"] == {"max": 3, "active": 2}
        # And the ZSET really is empty for the pilot (meter didn't ZADD).
        assert capacity._slots.redis.zcard("agent:slots:pilot") == 0

    def test_max_respects_506_clamp(self, capacity, monkeypatch, set_ceiling):
        """`max` is the clamped ceiling, not the stored cap; leased can exceed it
        (a meter, not a gate)."""
        set_ceiling(2)  # clamp 5 → 2
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "pilot")
        _seed_agent("pilot")
        for i in range(3):
            _insert_exec(f"l{i}", "pilot", status="running", lease_offset=900)

        states = asyncio.run(capacity.get_all_states({"pilot": 5}))

        assert states["pilot"]["max"] == 2          # clamped ceiling
        assert states["pilot"]["active"] == 3       # leased term, unclamped meter


# ===========================================================================
# Proof 2b — per-agent get_slot_state merge + available floor
# ===========================================================================


class TestPerAgentMeter:
    def test_leased_added_and_available_floored(
        self, capacity, monkeypatch, set_ceiling
    ):
        set_ceiling(5)
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "pilot")
        _seed_agent("pilot")
        _insert_exec("l1", "pilot", status="running", lease_offset=900)
        _insert_exec("l2", "pilot", status="running", lease_offset=900)

        st = asyncio.run(capacity.get_slot_state("pilot", 3))

        assert st.max_parallel_tasks == 3           # clamped (min(3, 5))
        assert st.active_slots == 2                 # ZSET(0) + leased(2)
        assert st.available_slots == 1              # 3 - 2

    def test_available_floors_at_zero_over_ceiling(
        self, capacity, monkeypatch, set_ceiling
    ):
        set_ceiling(2)
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "pilot")
        _seed_agent("pilot")
        for i in range(4):
            _insert_exec(f"l{i}", "pilot", status="running", lease_offset=900)

        st = asyncio.run(capacity.get_slot_state("pilot", 3))

        assert st.max_parallel_tasks == 2           # clamped
        assert st.active_slots == 4                 # leased > clamped cap
        assert st.available_slots == 0              # floored, not negative


# ===========================================================================
# Proof 3 — claim_next_queued does NOT ZADD the slot ZSET
# ===========================================================================


class TestClaimDoesNotZadd:
    def test_pull_claim_leaves_zset_empty_meter_shows_leased(
        self, capacity, monkeypatch, set_ceiling
    ):
        set_ceiling(5)
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "pilot")
        _seed_agent("pilot")
        _insert_exec("q1", "pilot", status="queued", queued=True)
        from database import db

        claimed = db.claim_next_queued("pilot", worker_id="pilot#w1", lease_seconds=900)
        assert claimed is not None and claimed["id"] == "q1"
        # Pull claim is a pure SQL UPDATE — nothing ZADDed.
        assert capacity._slots.redis.zcard("agent:slots:pilot") == 0
        # But the physical meter sees it.
        assert db.count_active_leased("pilot") == 1
        st = asyncio.run(capacity.get_slot_state("pilot", 3))
        assert st.active_slots == 1


# ===========================================================================
# Proof 4 — inertness for non-pilot agents (allowlist set vs unset)
# ===========================================================================


class TestInertForNonPilots:
    def test_bulk_output_identical_and_reader_not_called(
        self, capacity, monkeypatch, set_ceiling
    ):
        set_ceiling(5)
        _seed_agent("solo")
        assert asyncio.run(
            capacity._slots.acquire_slot("solo", "x1", max_parallel_tasks=3)
        ) is True

        # Spy: the leased reader must not run for a non-pilot fleet.
        from database import db
        calls = {"n": 0}
        real = db.count_active_leased_by_agent
        monkeypatch.setattr(
            db, "count_active_leased_by_agent",
            lambda names: (calls.__setitem__("n", calls["n"] + 1) or real(names)),
        )

        monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)
        unset = asyncio.run(capacity.get_all_states({"solo": 3}))
        # Allowlist naming OTHER agents ⇒ "solo" still not a pilot.
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "other,elsewhere")
        other = asyncio.run(capacity.get_all_states({"solo": 3}))

        assert unset == other == {"solo": {"max": 3, "active": 1}}
        assert calls["n"] == 0   # short-circuit: reader never invoked

    def test_per_agent_output_identical(self, capacity, monkeypatch, set_ceiling):
        set_ceiling(5)
        _seed_agent("solo")
        asyncio.run(capacity._slots.acquire_slot("solo", "x1", max_parallel_tasks=3))

        monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)
        a = asyncio.run(capacity.get_slot_state("solo", 3))
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "other")
        b = asyncio.run(capacity.get_slot_state("solo", 3))

        assert (a.active_slots, a.available_slots, a.max_parallel_tasks) == \
               (b.active_slots, b.available_slots, b.max_parallel_tasks) == (1, 2, 3)


# ===========================================================================
# Proof 5 — reaper interaction: re-queue / park drops the meter to 0
# ===========================================================================


class TestReaperDropsMeter:
    def test_requeue_under_cap_drops_meter(self, tmp_db):
        _seed_agent("pilot")
        _insert_exec("l1", "pilot", status="running", lease_offset=-60,
                     redelivery_count=0)
        from services import lease_reaper_service as lrs
        from database import db

        assert db.count_active_leased("pilot") == 1        # leased before reap
        report = lrs.reap_expired_leases(db, max_redelivery=3)
        assert report.requeued == 1 and report.parked == 0
        # Re-queued row is `queued` now — no longer a running lease.
        assert db.count_active_leased("pilot") == 0
        assert _scalar("SELECT status FROM schedule_executions WHERE id='l1'") == "queued"

    def test_park_at_cap_drops_meter(self, tmp_db):
        _seed_agent("pilot")
        _insert_exec("l1", "pilot", status="running", lease_offset=-90,
                     redelivery_count=3)
        from services import lease_reaper_service as lrs
        from database import db

        assert db.count_active_leased("pilot") == 1
        report = lrs.reap_expired_leases(db, max_redelivery=3)
        assert report.parked == 1 and report.requeued == 0
        # Parked row is terminal (failed) — dropped from the meter.
        assert db.count_active_leased("pilot") == 0
        assert _scalar("SELECT status FROM schedule_executions WHERE id='l1'") == "failed"
