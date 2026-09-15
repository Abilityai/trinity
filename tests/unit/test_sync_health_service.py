"""
SyncHealthService tests (#389 S1).

The service polls each git-enabled agent on an interval, pulls the dual
ahead/behind + sync-state from its `/api/git/status` response, upserts the
`agent_sync_state` row, and emits a `sync_failing` operator-queue entry when
consecutive_failures crosses the threshold.

These are pure unit tests — the AgentClient is replaced with an in-memory
fake so no agent containers are needed.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
# #2080: the shadow-eviction loop that used to sit here is GONE. It popped
# `utils` (and the test-helper submodules) from sys.modules to defeat
# `tests/utils` shadowing `src/backend/utils`. That package is now
# `tests/testkit`, so `utils` IS the backend package — and popping it
# evicted the canonical module mid-session, leaving anything that had
# already imported it holding a stale reference (observed as
# `ImportError: module services.subscription_auto_switch not in sys.modules`
# from an importlib.reload several hundred tests later).
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402


pytestmark = pytest.mark.unit


@pytest.fixture
def tmp_db(db_backend, monkeypatch):
    """Active backend with a fresh FULL schema (db_harness, #300). Runs on
    SQLite and, when TEST_POSTGRES_URL is set, PostgreSQL. Evicts cached db /
    service modules and stubs services.agent_client (the real module needs
    docker/redis; tests patch _fetch_git_status so it's never called)."""
    for modname in list(sys.modules):
        if modname == "database" or modname.startswith("db.") \
                or modname in ("services.sync_health_service", "services.agent_client"):
            if modname in ("db.engine", "db.tables", "db.schema"):
                continue
            sys.modules.pop(modname, None)
    monkeypatch.setitem(
        sys.modules,
        "services.agent_client",
        types.SimpleNamespace(AgentClient=MagicMock()),
    )
    return db_backend


@pytest.fixture
def seed_agent(tmp_db):
    def _seed(name: str, auto_sync: bool = True):
        _hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES (:n, 1, '2026-01-01T00:00:00Z')",
            n=name,
        )
        _hrun(
            "INSERT INTO agent_git_config "
            "(id, agent_name, github_repo, working_branch, instance_id, "
            " created_at, sync_enabled, auto_sync_enabled) "
            "VALUES (:gid, :n, 'org/repo', :wb, 'abc123', "
            " '2026-01-01T00:00:00Z', 1, :asy)",
            gid=name + "-git", n=name, wb=f"trinity/{name}/abc123",
            asy=1 if auto_sync else 0,
        )
    return _seed


def _status_payload(status="success", ahead_working=0, behind_working=0, error=None,
                    lock_recovery=None, index_lock_stuck=None, computed_at=None):
    return {
        "git_enabled": True,
        "branch": "trinity/alpha/abc123",
        "remote_url": "https://github.com/org/repo",
        "last_commit": {"sha": "deadbeef"},
        "changes": [],
        "changes_count": 0,
        "ahead": 0,
        "behind": 0,
        "ahead_main": 0,
        "behind_main": 0,
        "ahead_working": ahead_working,
        "behind_working": behind_working,
        "sync_state": {
            "last_sync_status": status,
            "last_sync_at": "2026-04-18T10:00:00+00:00",
            "last_error_summary": error,
            "consecutive_failures": 0,  # agent-side counter, backend recomputes
            "last_lock_recovery": lock_recovery,  # #2742
        },
        "sync_status": "up_to_date",
        # #2742 fields (None on an agent running an older base image).
        "lock_recovery": lock_recovery,
        "index_lock_stuck": index_lock_stuck,
        "computed_at": computed_at,
    }


@pytest.fixture
def service(tmp_db, monkeypatch):
    """SyncHealthService instance with a stub AgentClient.

    #2742: `get_breaker_redis` is stubbed to None so the leader lease fails OPEN
    and every test keeps polling. Without this, a developer with a local Redis
    would (a) pay a ~1 s connect attempt per cycle at poll_interval=0 and (b)
    leave a real 30 s `synchealth:leader` lease behind, so the NEXT test's fresh
    service loses the election and silently polls nothing — a green suite that
    asserts nothing.
    """
    import services.sync_health_service as shs  # noqa: WPS433
    monkeypatch.setattr(shs, "get_breaker_redis", lambda: None)
    svc = shs.SyncHealthService(poll_interval=0)
    return svc


class TestSyncStatePersistence:
    """Each poll cycle upserts a row per agent."""

    @pytest.mark.asyncio
    async def test_success_recorded(self, service, seed_agent):
        seed_agent("alpha")
        fake_status = _status_payload(status="success")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=fake_status)):
            await service._poll_cycle()
        from database import db
        row = db.get_sync_state("alpha")
        assert row is not None
        assert row["last_sync_status"] == "success"
        assert row["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_failure_increments_counter(self, service, seed_agent):
        seed_agent("alpha")
        payload = _status_payload(status="failed", error="push failed")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=payload)):
            await service._poll_cycle()
            await service._poll_cycle()
        from database import db
        row = db.get_sync_state("alpha")
        assert row["consecutive_failures"] == 2
        assert row["last_sync_status"] == "failed"

    @pytest.mark.asyncio
    async def test_unreachable_agent_is_skipped(self, service, seed_agent):
        seed_agent("alpha")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=None)):
            await service._poll_cycle()
        from database import db
        # Agent unreachable → no row written.
        assert db.get_sync_state("alpha") is None


class TestOperatorQueueEmission:
    """sync_failing entry emitted when consecutive_failures crosses 3."""

    @pytest.mark.asyncio
    async def test_no_entry_on_first_two_failures(self, service, seed_agent):
        seed_agent("alpha")
        payload = _status_payload(status="failed", error="e1")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=payload)):
            await service._poll_cycle()
            await service._poll_cycle()
        from database import db
        items = db.list_operator_queue_items(agent_name="alpha")
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_entry_emitted_on_third_failure(self, service, seed_agent):
        seed_agent("alpha")
        payload = _status_payload(status="failed", error="boom")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=payload)):
            await service._poll_cycle()
            await service._poll_cycle()
            await service._poll_cycle()
        from database import db
        items = db.list_operator_queue_items(agent_name="alpha")
        sync_failing = [i for i in items if i["type"] == "sync_failing"]
        assert len(sync_failing) == 1
        assert "boom" in (sync_failing[0].get("context") or {}).get(
            "last_error_summary", "")

    @pytest.mark.asyncio
    async def test_success_resets_counter_and_allows_future_emissions(
        self, service, seed_agent
    ):
        seed_agent("alpha")
        fail = _status_payload(status="failed", error="e")
        ok = _status_payload(status="success")

        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=fail)):
            await service._poll_cycle()
            await service._poll_cycle()
            await service._poll_cycle()
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=ok)):
            await service._poll_cycle()  # success resets counter
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=fail)):
            await service._poll_cycle()
            await service._poll_cycle()
            await service._poll_cycle()  # third failure since reset

        from database import db
        items = db.list_operator_queue_items(agent_name="alpha")
        sync_failing = [i for i in items if i["type"] == "sync_failing"]
        # Two distinct failure series → two entries (distinct IDs by timestamp).
        assert len(sync_failing) == 2


class TestBehindWorkingRedFlag:
    """Record behind_working so the dashboard can colour a red dot on P6 writes."""

    @pytest.mark.asyncio
    async def test_behind_working_recorded(self, service, seed_agent):
        seed_agent("alpha")
        payload = _status_payload(status="success", behind_working=2)
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=payload)):
            await service._poll_cycle()
        from database import db
        row = db.get_sync_state("alpha")
        assert row["behind_working"] == 2


class TestSoftDeletedExcluded:
    """#1561: a soft-deleted agent must never be polled — otherwise the 60s
    loop hits its removed container forever and each httpx.ConnectError
    poisons the transport circuit breaker (→ DORMANT + a bogus alert)."""

    @staticmethod
    def _soft_delete(name: str):
        _hrun(
            "UPDATE agent_ownership SET deleted_at = '2026-01-01T11:42:52Z' "
            "WHERE agent_name = :n",
            n=name,
        )

    def test_accessor_excludes_soft_deleted(self, tmp_db, seed_agent):
        seed_agent("live")
        seed_agent("dead")
        self._soft_delete("dead")
        from database import db
        names = {c.agent_name for c in db.list_git_enabled_agents()}
        assert "live" in names
        assert "dead" not in names, "soft-deleted agent must not be listed"

    @pytest.mark.asyncio
    async def test_poll_cycle_issues_no_http_to_soft_deleted(
        self, service, seed_agent
    ):
        seed_agent("live")
        seed_agent("dead")
        self._soft_delete("dead")

        fake = _status_payload(status="success")
        spy = AsyncMock(return_value=fake)
        with patch.object(service, "_fetch_git_status", spy):
            await service._poll_cycle()

        # Exactly one fetch — the live agent — never the soft-deleted one.
        polled = {call.args[0] for call in spy.call_args_list}
        assert polled == {"live"}, f"expected only 'live' polled, got {polled}"

        from database import db
        # No sync_state row and no operator-queue entry for the dead agent.
        assert db.get_sync_state("dead") is None
        assert db.list_operator_queue_items(agent_name="dead") == []


class TestLeaderLeaseAlertTiming:
    """#2742 — what the leader lease costs the alerting path, named and pinned.

    The lease's original justification was "a duplicated poll costs only
    duplicate reads and idempotent upserts". That is false: `upsert_sync_state`
    *increments* `consecutive_failures` on every `failed` upsert and
    `ALERT_THRESHOLD` is an edge trigger off that counter. So two unleased
    workers drove a failing agent to `sync_failing` in ~90 s (three failed polls
    arriving in three half-cycles); one leader takes three full cycles, ~180 s.

    Arguably the counter now means what its name says — 3 consecutive failed
    *polls* = 3 minutes — but it is a change to an alerting path, so it is
    asserted here rather than discovered later. The lease mechanics themselves
    live in `test_2742_sync_health_leader_lock.py`.
    """

    @pytest.mark.asyncio
    async def test_upsert_is_not_idempotent_which_is_why_timing_moved(
        self, service, seed_agent
    ):
        """The load-bearing fact, asserted directly: the SAME failing payload
        polled twice increments twice. A test that only counted cycles would
        pass against an idempotent upsert and prove nothing."""
        seed_agent("alpha")
        payload = _status_payload(status="failed", error="push failed")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=payload)):
            await service._poll_cycle()
            await service._poll_cycle()

        from database import db
        assert db.get_sync_state("alpha")["consecutive_failures"] == 2

    @pytest.mark.asyncio
    async def test_one_leader_crosses_the_threshold_on_the_third_cycle(
        self, service, seed_agent
    ):
        """With one poller, `sync_failing` fires on cycle 3 — i.e. ~180 s at the
        60 s cadence, where two unleased workers reached it in ~90 s."""
        seed_agent("alpha")
        payload = _status_payload(status="failed", error="boom")
        from database import db

        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=payload)):
            await service._poll_cycle()
            assert db.list_operator_queue_items(agent_name="alpha") == []
            await service._poll_cycle()
            assert db.list_operator_queue_items(agent_name="alpha") == []
            await service._poll_cycle()

        items = db.list_operator_queue_items(agent_name="alpha")
        assert len(items) == 1
        assert items[0]["type"] == "sync_failing"
        assert db.get_sync_state("alpha")["consecutive_failures"] == 3

    @pytest.mark.asyncio
    async def test_a_non_leader_writes_nothing_at_all(
        self, service, seed_agent, monkeypatch
    ):
        """The non-leader must not advance the counter either — a lease that
        only skipped the HTTP call but still upserted would keep the old timing
        and quietly defeat its own purpose."""
        seed_agent("alpha")
        monkeypatch.setattr(service, "_try_acquire_leadership", lambda: False)
        payload = _status_payload(status="failed", error="boom")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=payload)):
            await service._poll_cycle()

        from database import db
        assert db.get_sync_state("alpha") is None


def _iso_now(offset_seconds: int = 0):
    from datetime import datetime, timedelta, timezone
    return (
        datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    ).isoformat().replace("+00:00", "Z")


class TestLockRecoveryObservability:
    """#2742 — the self-healed wedge reaches the platform, exactly once.

    `sync-state.json` is agent-authored and merged wholesale by the agent
    server, and `git_service.get_git_status` proxies `response.json()`
    UNMODIFIED to the UI and the MCP tool — so the backend never passes the
    agent's dict through, it rebuilds one from values it has checked.
    """

    @pytest.mark.asyncio
    async def test_a_fresh_recovery_logs_once(self, service, seed_agent, caplog):
        seed_agent("alpha")
        payload = _status_payload(
            lock_recovery={"at": _iso_now(-30), "locks": "index.lock"}
        )
        with caplog.at_level("WARNING"):
            with patch.object(service, "_fetch_git_status",
                               AsyncMock(return_value=payload)):
                await service._poll_cycle()
        hits = [r for r in caplog.records if "recovered a stale git lock" in r.message]
        assert len(hits) == 1

    @pytest.mark.asyncio
    async def test_the_same_recovery_is_not_logged_again(
        self, service, seed_agent, caplog
    ):
        """Dedup is against the last OBSERVED value. The rejected alternative —
        `at` newer than the prior row's `last_check_at` — is not a dedup at all:
        that column is re-stamped to `now` on every upsert."""
        seed_agent("alpha")
        payload = _status_payload(
            lock_recovery={"at": _iso_now(-30), "locks": "index.lock"}
        )
        with caplog.at_level("WARNING"):
            with patch.object(service, "_fetch_git_status",
                               AsyncMock(return_value=payload)):
                await service._poll_cycle()
                await service._poll_cycle()
                await service._poll_cycle()
        hits = [r for r in caplog.records if "recovered a stale git lock" in r.message]
        assert len(hits) == 1, f"expected one log line, got {len(hits)}"

    @pytest.mark.asyncio
    async def test_a_far_future_at_is_rejected_and_never_floods(
        self, service, seed_agent, caplog
    ):
        """The flooding case the clamp exists for: with the rejected
        `last_check_at` dedup, `9999-01-01` would be 'newer' on every tick,
        per agent, forever — asserting a platform action that never happened."""
        seed_agent("alpha")
        payload = _status_payload(
            lock_recovery={"at": "9999-01-01T00:00:00Z", "locks": "index.lock"}
        )
        with caplog.at_level("WARNING"):
            with patch.object(service, "_fetch_git_status",
                               AsyncMock(return_value=payload)):
                await service._poll_cycle()
                await service._poll_cycle()
        assert not [r for r in caplog.records
                    if "recovered a stale git lock" in r.message]
        from database import db
        assert db.get_sync_state("alpha") is not None, "the upsert must still land"

    @pytest.mark.asyncio
    async def test_an_ancient_at_is_rejected(self, service, seed_agent, caplog):
        seed_agent("alpha")
        payload = _status_payload(
            lock_recovery={"at": _iso_now(-86400), "locks": "index.lock"}
        )
        with caplog.at_level("WARNING"):
            with patch.object(service, "_fetch_git_status",
                               AsyncMock(return_value=payload)):
                await service._poll_cycle()
        assert not [r for r in caplog.records
                    if "recovered a stale git lock" in r.message]

    @pytest.mark.parametrize("bad", [
        None, "junk", 5, [], {}, {"at": 5}, {"at": None},
        {"at": "not-a-date"}, {"at": True},
        {"at": "2026-09-13T10:00:00"},          # valid ISO, NAIVE — see below
        {"at": "x" * 500},
    ])
    def test_malformed_recoveries_are_dropped_without_raising(self, bad):
        """Includes the naive-ISO case explicitly (R1). It parses cleanly and
        would then raise TypeError on the aware/naive comparison — a raise that
        lands after the upsert and is swallowed by
        `gather(return_exceptions=True)`, i.e. a lost alert with no traceback.
        It is rejected here because our own writer always stamps `Z`, so a naive
        value did not come from us."""
        from services.sync_health_service import _coerce_lock_recovery
        assert _coerce_lock_recovery(bad) is None

    def test_a_well_formed_recovery_is_accepted(self):
        """The other half, so the test above cannot pass by rejecting
        everything."""
        from services.sync_health_service import _coerce_lock_recovery
        out = _coerce_lock_recovery({"at": _iso_now(-30), "locks": "index.lock"})
        assert out is not None
        assert out["locks"] == "index.lock"

    def test_an_offset_form_is_accepted_too(self):
        from datetime import datetime, timedelta, timezone
        from services.sync_health_service import _coerce_lock_recovery
        at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        assert at.endswith("+00:00")
        assert _coerce_lock_recovery({"at": at}) is not None

    def test_agent_supplied_free_text_is_bounded(self):
        from services.sync_health_service import _coerce_lock_recovery
        out = _coerce_lock_recovery({"at": _iso_now(-5), "locks": "y" * 5000})
        assert len(out["locks"]) == 200

    def test_a_non_string_locks_field_becomes_empty(self):
        from services.sync_health_service import _coerce_lock_recovery
        out = _coerce_lock_recovery({"at": _iso_now(-5), "locks": {"evil": 1}})
        assert out["locks"] == ""


class TestStuckLockObservability:
    """#2742 — a currently-wedged workspace is visible, and no operator-queue
    item is created for it (the report is a diagnosis, not a decision)."""

    @pytest.mark.asyncio
    async def test_a_stuck_lock_logs_once_per_episode(
        self, service, seed_agent, caplog
    ):
        seed_agent("alpha")
        payload = _status_payload(index_lock_stuck={
            "path": "index.lock", "age_seconds": 2000,
            "stable_for_seconds": 1800, "sightings": 4, "size_bytes": 0,
        })
        with caplog.at_level("WARNING"):
            with patch.object(service, "_fetch_git_status",
                               AsyncMock(return_value=payload)):
                await service._poll_cycle()
                await service._poll_cycle()
        hits = [r for r in caplog.records if "unchanged across" in r.message]
        assert len(hits) == 1, "edge-triggered, not once per minute"

    @pytest.mark.asyncio
    async def test_it_re_arms_after_the_lock_clears(
        self, service, seed_agent, caplog
    ):
        seed_agent("alpha")
        stuck = _status_payload(index_lock_stuck={
            "age_seconds": 2000, "stable_for_seconds": 1800, "sightings": 4,
        })
        clear = _status_payload()
        with caplog.at_level("WARNING"):
            for payload in (stuck, clear, stuck):
                with patch.object(service, "_fetch_git_status",
                                   AsyncMock(return_value=payload)):
                    await service._poll_cycle()
        hits = [r for r in caplog.records if "unchanged across" in r.message]
        assert len(hits) == 2, "a new episode must be announced"

    @pytest.mark.asyncio
    async def test_no_operator_queue_item_is_created(self, service, seed_agent):
        seed_agent("alpha")
        payload = _status_payload(index_lock_stuck={
            "age_seconds": 2000, "stable_for_seconds": 1800, "sightings": 4,
        })
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=payload)):
            await service._poll_cycle()
        from database import db
        assert db.list_operator_queue_items(agent_name="alpha") == []

    @pytest.mark.parametrize("bad", [
        None, "junk", 5, [], {}, {"age_seconds": "600"},
        {"sightings": True}, {"age_seconds": -1}, {"age_seconds": None},
    ])
    def test_malformed_stuck_reports_are_dropped(self, bad):
        from services.sync_health_service import _coerce_lock_stuck
        assert _coerce_lock_stuck(bad) is None

    def test_a_well_formed_stuck_report_is_accepted_and_path_is_dropped(self):
        """The agent-supplied `path` is composed from a `.git` the agent can
        point anywhere. It adds nothing to a fleet-level WARNING, so it never
        crosses the boundary."""
        from services.sync_health_service import _coerce_lock_stuck
        out = _coerce_lock_stuck({
            "path": "../../etc/passwd", "age_seconds": 2000,
            "stable_for_seconds": 1800, "sightings": 4, "size_bytes": 0,
        })
        assert out == {
            "age_seconds": 2000, "stable_for_seconds": 1800,
            "sightings": 4, "size_bytes": 0,
        }
        assert "path" not in out

    @pytest.mark.asyncio
    async def test_an_older_base_image_without_the_fields_still_upserts(
        self, service, seed_agent
    ):
        """The fleet converges by recreate, so most agents will not carry these
        keys at all for a while."""
        seed_agent("alpha")
        legacy = _status_payload()
        legacy.pop("lock_recovery")
        legacy.pop("index_lock_stuck")
        legacy["sync_state"].pop("last_lock_recovery")
        with patch.object(service, "_fetch_git_status",
                           AsyncMock(return_value=legacy)):
            await service._poll_cycle()
        from database import db
        assert db.get_sync_state("alpha")["last_sync_status"] == "success"
