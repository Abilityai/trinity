"""`GET /api/internal/agents/{name}/sync-health-status` (trinity-enterprise#706).

The pre-#706 keys keep their names and meaning; `should_freeze` now also
covers the divergence clause, and the response names why
(`freeze_reason`, `divergence_age_s`, `work_agent`). Real DB via db_harness.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from db_harness import db_backend  # noqa: F401

pytestmark = pytest.mark.unit

_OLD_KEYS = {
    "agent_name",
    "freeze_schedules_if_sync_failing",
    "sync_failing",
    "should_freeze",
    "consecutive_failures",
}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def db(db_backend):  # noqa: F811
    from database import db as database

    database.create_git_config(
        agent_name="ent706-i", github_repo="owner/ent706-i",
        working_branch="trinity/ent706-i/x", instance_id="inst-i",
    )
    database.set_freeze_schedules_if_sync_failing("ent706-i", True)
    return database


def _call(name="ent706-i"):
    from routers.internal import internal_agent_sync_health

    return asyncio.run(internal_agent_sync_health(name))


def test_divergence_freeze_is_reported_with_its_reason(db):
    now = datetime.now(timezone.utc)
    db.upsert_sync_state(
        "ent706-i", last_sync_status="success", ahead_working=7,
        diverged_since=_iso(now - timedelta(hours=26)),
        last_check_at=_iso(now - timedelta(seconds=30)),
    )
    body = _call()
    assert _OLD_KEYS <= set(body)
    assert body["freeze_schedules_if_sync_failing"] is True
    assert body["sync_failing"] is False
    assert body["should_freeze"] is True
    assert body["consecutive_failures"] == 0
    assert body["freeze_reason"] == "diverged 0 behind / 7 ahead for 26h"
    assert 26 * 3600 <= body["divergence_age_s"] < 26 * 3600 + 60
    assert body["work_agent"] is True


def test_sync_failing_is_unchanged(db):
    for _ in range(3):
        db.upsert_sync_state("ent706-i", last_sync_status="failed",
                             last_error_summary="rejected")
    body = _call()
    assert (body["sync_failing"], body["should_freeze"], body["consecutive_failures"]) == (
        True, True, 3)
    assert body["freeze_reason"].startswith("last sync failed (seen on 3 polls)")


def test_control_arm_in_sync_does_not_freeze(db):
    db.upsert_sync_state("ent706-i", last_sync_status="success")
    body = _call()
    assert body["should_freeze"] is False
    assert body["freeze_reason"] is None
    assert body["divergence_age_s"] is None


def test_unknown_agent_is_not_frozen(db):
    body = _call("no-such-agent")
    assert _OLD_KEYS <= set(body)
    assert body["should_freeze"] is False
    assert body["work_agent"] is False
