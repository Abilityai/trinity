"""The poller keeps the trinity-enterprise#706 columns (divergence tracking).

Driven through the REAL `SyncHealthService._poll_cycle` against a real DB
(db_harness, #300); only the agent's `/api/git/status` response is faked.
Pinned values are non-ambient (7 ahead, 31 behind, 791 dirty).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from db_harness import db_backend, run as _hrun  # noqa: F401

pytestmark = pytest.mark.unit

_UNSET = object()


def _payload(*, ahead=0, behind=0, changes=0, status="success",
             last_sync_at="2026-09-27T09:00:00+00:00", last_push=_UNSET,
             error=None):
    sync_state = {
        "last_sync_status": status,
        "last_sync_at": last_sync_at,
        "last_error_summary": error,
        "consecutive_failures": 0,
    }
    if last_push is not _UNSET:  # absent on an agent image older than #3011
        sync_state["last_successful_push_at"] = last_push
    return {
        "git_enabled": True,
        "branch": "trinity/alpha/abc123",
        "last_commit": {"sha": "deadbeef"},
        "changes_count": changes,
        "ahead_main": 0,
        "behind_main": 0,
        "ahead_working": ahead,
        "behind_working": behind,
        "sync_state": sync_state,
    }


@pytest.fixture
def db(db_backend):  # noqa: F811
    from database import db as database

    return database


@pytest.fixture
def service(db, monkeypatch):
    import services.sync_health_service as shs

    # Fail-open lease: no Redis, so every test polls (see test_sync_health_service).
    monkeypatch.setattr(shs, "get_breaker_redis", lambda: None)
    # Bind the poller to the SAME db object the test reads — sibling suites
    # evict and re-import `database`, so the module-level name can be stale.
    monkeypatch.setattr(shs, "db", db)
    return shs.SyncHealthService(poll_interval=0)


@pytest.fixture
def seed_agent(db):
    def _seed(name="alpha", *, source_mode=0, auto_sync=1, freeze=1,
              operator_push_at=None):
        _hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES (:n, 1, '2026-01-01T00:00:00Z')",
            n=name,
        )
        _hrun(
            "INSERT INTO agent_git_config "
            "(id, agent_name, github_repo, working_branch, instance_id, created_at, "
            " sync_enabled, source_mode, auto_sync_enabled, "
            " freeze_schedules_if_sync_failing, last_sync_at) "
            "VALUES (:gid, :n, :repo, :wb, 'abc123', '2026-01-01T00:00:00Z', 1, "
            " :sm, :asy, :fz, :ops)",
            gid=name + "-git", n=name, repo=f"org/{name}", wb=f"trinity/{name}/abc123",
            sm=source_mode, asy=auto_sync, fz=freeze, ops=operator_push_at,
        )
        return name
    return _seed


async def _poll(service, payload):
    with patch.object(service, "_fetch_git_status", AsyncMock(return_value=payload)):
        await service._poll_cycle()


# --------------------------------------------------------------------------
# diverged_since
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_diverged_since_is_set_once(service, seed_agent, db):
    seed_agent()
    await _poll(service, _payload(ahead=7))
    first = db.get_sync_state("alpha")["diverged_since"]
    assert first is not None and first.endswith("Z")
    await _poll(service, _payload(ahead=9, behind=31))
    assert db.get_sync_state("alpha")["diverged_since"] == first


@pytest.mark.asyncio
@pytest.mark.parametrize("behind_after", [0, None])
async def test_diverged_since_clears_when_back_in_sync(service, seed_agent, db, behind_after):
    """`behind is None` with a known `ahead == 0` is "no upstream" (#2105):
    nothing on origin to be behind, so it clears too."""
    seed_agent()
    await _poll(service, _payload(ahead=7))
    assert db.get_sync_state("alpha")["diverged_since"] is not None
    await _poll(service, _payload(ahead=0, behind=behind_after))
    assert db.get_sync_state("alpha")["diverged_since"] is None


@pytest.mark.asyncio
async def test_unknown_ahead_neither_starts_nor_clears(service, seed_agent, db):
    seed_agent()
    await _poll(service, _payload(ahead=None, behind=None))
    assert db.get_sync_state("alpha")["diverged_since"] is None  # never starts
    await _poll(service, _payload(ahead=7))
    first = db.get_sync_state("alpha")["diverged_since"]
    await _poll(service, _payload(ahead=None, behind=None))
    assert db.get_sync_state("alpha")["diverged_since"] == first  # never clears


@pytest.mark.asyncio
async def test_known_behind_with_unknown_ahead_starts_the_clock(service, seed_agent, db):
    seed_agent()
    await _poll(service, _payload(ahead=None, behind=31))
    assert db.get_sync_state("alpha")["diverged_since"] is not None


# --------------------------------------------------------------------------
# dirty_files / dirty_since
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dirty_count_and_clock(service, seed_agent, db):
    seed_agent()
    await _poll(service, _payload(changes=791))
    row = db.get_sync_state("alpha")
    assert row["dirty_files"] == 791
    first = row["dirty_since"]
    assert first is not None
    await _poll(service, _payload(changes=12))
    row = db.get_sync_state("alpha")
    assert (row["dirty_files"], row["dirty_since"]) == (12, first)
    await _poll(service, _payload(changes=0))
    row = db.get_sync_state("alpha")
    assert (row["dirty_files"], row["dirty_since"]) == (0, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("garbage", ["791", -1, True, 2**40])
async def test_garbage_dirty_count_keeps_the_prior_values(service, seed_agent, db, garbage):
    seed_agent()
    await _poll(service, _payload(changes=791))
    before = db.get_sync_state("alpha")
    await _poll(service, _payload(changes=garbage))
    after = db.get_sync_state("alpha")
    assert (after["dirty_files"], after["dirty_since"]) == (791, before["dirty_since"])


@pytest.mark.asyncio
async def test_dirt_does_not_start_the_divergence_clock(service, seed_agent, db):
    seed_agent()
    await _poll(service, _payload(changes=791))
    assert db.get_sync_state("alpha")["diverged_since"] is None


# --------------------------------------------------------------------------
# last_successful_push_at
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_last_push_is_the_later_of_heartbeat_and_operator_push(service, seed_agent, db):
    seed_agent(operator_push_at="2026-09-26T08:00:00.000000Z")
    await _poll(service, _payload(last_push="2026-09-26T09:30:00+00:00"))
    assert db.get_sync_state("alpha")["last_successful_push_at"] == "2026-09-26T09:30:00.000000Z"


@pytest.mark.asyncio
async def test_operator_push_wins_when_it_is_later(service, seed_agent, db):
    seed_agent(operator_push_at="2026-09-26T11:15:00.000000Z")
    await _poll(service, _payload(last_push="2026-09-26T09:30:00+00:00"))
    assert db.get_sync_state("alpha")["last_successful_push_at"] == "2026-09-26T11:15:00.000000Z"


@pytest.mark.asyncio
async def test_old_image_falls_back_to_a_successful_cycle(service, seed_agent, db):
    seed_agent()
    await _poll(service, _payload(status="success", last_sync_at="2026-09-26T07:45:00+00:00"))
    assert db.get_sync_state("alpha")["last_successful_push_at"] == "2026-09-26T07:45:00.000000Z"


@pytest.mark.asyncio
async def test_old_image_failed_cycle_is_not_a_push(service, seed_agent, db):
    seed_agent()
    await _poll(service, _payload(status="failed", last_sync_at="2026-09-26T07:45:00+00:00",
                                  error="rejected"))
    assert db.get_sync_state("alpha")["last_successful_push_at"] is None


@pytest.mark.asyncio
async def test_new_image_that_never_pushed_does_not_fall_back(service, seed_agent, db):
    """The key is PRESENT (None) on a #3011 image: no push has ever landed, and
    `last_sync_at` of a success-status cycle is not evidence of one."""
    seed_agent()
    await _poll(service, _payload(status="success", last_push=None))
    assert db.get_sync_state("alpha")["last_successful_push_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "yesterday",
    "2026-09-26T09:30:00",  # naive: our writer always stamps an offset
    "FUTURE",  # resolved below: a collection-time timestamp would give each
               # xdist worker a different test id ("Different tests were collected")
    12345,
])
async def test_agent_push_time_is_validated(service, seed_agent, db, bad):
    if bad == "FUTURE":
        bad = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    seed_agent()
    await _poll(service, _payload(last_push=bad))
    assert db.get_sync_state("alpha")["last_successful_push_at"] is None


@pytest.mark.asyncio
async def test_last_push_is_monotonic(service, seed_agent, db):
    seed_agent()
    await _poll(service, _payload(last_push="2026-09-26T09:30:00+00:00"))
    await _poll(service, _payload(last_push="2026-09-20T09:30:00+00:00"))
    assert db.get_sync_state("alpha")["last_successful_push_at"] == "2026-09-26T09:30:00.000000Z"


# --------------------------------------------------------------------------
# unreachable
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unreachable_agent_changes_nothing(service, seed_agent, db):
    seed_agent()
    await _poll(service, _payload(ahead=7, changes=791))
    before = db.get_sync_state("alpha")
    await _poll(service, None)
    assert db.get_sync_state("alpha") == before
